"""Inspect and encode still images with Pillow."""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import OptimizationCancelled
from app.models import Settings

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 250_000_000

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".avif",
    ".heic",
    ".heif",
    ".gif",
    ".bmp",
}

_FORMAT_KEYS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jpe": "jpeg",
    ".jfif": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".gif": "gif",
    ".bmp": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".avif": "avif",
    ".heic": "heic",
    ".heif": "heif",
}

_LABELS = {
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WebP",
    "gif": "GIF",
    "bmp": "BMP",
    "tiff": "TIFF",
    "avif": "AVIF",
    "heic": "HEIC",
    "heif": "HEIF",
}

_METADATA_TAGS = (270, 271, 272, 305, 306, 315, 33432, 36867, 37510)

Progress = Callable[[float, str], None]

_heif_ready: bool | None = None
_avif_ready: bool | None = None


def register_heif() -> bool:
    """Enable HEIC and AVIF in Pillow when pillow-heif is installed."""

    global _heif_ready
    if _heif_ready is not None:
        return _heif_ready
    try:
        import pillow_heif

        opener = getattr(pillow_heif, "register_heif_opener", None)
        if opener:
            opener()
        avif_opener = getattr(pillow_heif, "register_avif_opener", None)
        if avif_opener:
            avif_opener()
        _heif_ready = True
    except Exception as exc:
        log.info("HEIC/AVIF support is unavailable: %s", exc)
        _heif_ready = False
    return _heif_ready


def heic_supported() -> bool:
    return register_heif()


def avif_supported() -> bool:
    global _avif_ready
    if _avif_ready is not None:
        return _avif_ready
    register_heif()
    _avif_ready = _can_write_avif()
    return _avif_ready


def _can_write_avif() -> bool:
    image = Image.new("RGB", (16, 16), (20, 140, 200))
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="AVIF", quality=50)
        if buffer.tell() > 20:
            return True
    except Exception:
        buffer.seek(0)
        buffer.truncate()
    try:
        import pillow_heif

        pillow_heif.from_pillow(image).save(buffer, quality=50)
        return buffer.tell() > 20
    except Exception:
        return False


def format_key_for(path: Path, pillow_format: str | None = None) -> str:
    key = _FORMAT_KEYS.get(path.suffix.lower())
    if key:
        return key
    if pillow_format:
        return pillow_format.lower()
    return path.suffix.lower().lstrip(".") or "unknown"


def format_label(format_key: str) -> str:
    return _LABELS.get(format_key, format_key.upper() or "Image")


@dataclass
class ImageDetails:
    width: int
    height: int
    format_key: str
    format_label: str
    has_metadata: bool


@dataclass
class ImageResult:
    width: int
    height: int
    warning: str = ""


def fitted_size(
    width: int,
    height: int,
    max_width: int | None,
    max_height: int | None,
) -> tuple[int, int]:
    """Fit inside a box. Never enlarge, and keep the aspect ratio."""

    if not max_width or not max_height:
        return width, height
    if width <= max_width and height <= max_height:
        return width, height
    scale = min(max_width / width, max_height / height)
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def inspect_image(path: Path) -> ImageDetails:
    register_heif()
    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif"} and not heic_supported():
        raise RuntimeError(
            "HEIC support is not available. Install pillow-heif and restart the app, "
            "or export this photo as JPEG first."
        )
    try:
        with Image.open(path) as image:
            _reject_animation(image)
            width, height = _oriented_size(image)
            return ImageDetails(
                width=width,
                height=height,
                format_key=format_key_for(path, image.format),
                format_label=format_label(format_key_for(path, image.format)),
                has_metadata=_has_extra_metadata(image),
            )
    except RuntimeError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise RuntimeError(_read_error(exc)) from exc


def optimize_image(
    source: Path,
    destination: Path,
    settings: Settings,
    progress: Progress,
    cancel: object | None = None,
) -> ImageResult:
    """Write one web image. The destination must not already exist."""

    _check_cancel(cancel)
    if settings.image_format == "avif" and not avif_supported():
        raise RuntimeError("AVIF encoding is not available. Choose WebP or JPEG instead.")

    progress(8, "Reading image")
    register_heif()
    partial = Path(str(destination) + ".partial")
    committed = False
    try:
        with Image.open(source) as opened:
            _check_cancel(cancel)
            _reject_animation(opened)
            warning = ""
            if getattr(opened, "n_frames", 1) > 1:
                warning = "Only the first page of this multi-page image was optimized."
                opened.seek(0)

            icc = opened.info.get("icc_profile")
            exif = opened.getexif()
            progress(25, "Applying orientation")
            image = ImageOps.exif_transpose(opened) or opened
            image.load()

            progress(40, "Preparing pixels")
            image = _normalize(image, settings.image_format)
            if settings.keep_image_dimensions:
                target = image.size
            else:
                target = fitted_size(image.width, image.height, settings.max_width, settings.max_height)
            if target != image.size:
                progress(55, f"Resizing to {target[0]}×{target[1]}")
                image = image.resize(target, Image.Resampling.LANCZOS)
            else:
                progress(55, "Keeping original dimensions")

            _check_cancel(cancel)
            progress(70, f"Encoding {settings.image_format.upper()}")
            exif_bytes = _exif_bytes(exif) if settings.preserve_metadata else None
            save_warning = _save_image(image, partial, settings, exif_bytes, icc if isinstance(icc, bytes) else None)
            if save_warning:
                warning = f"{warning} {save_warning}".strip()

        progress(88, "Checking output")
        width, height = validate_image(partial, target)
        partial.replace(destination)
        committed = True
        progress(100, "Done")
        return ImageResult(width=width, height=height, warning=warning)
    except Exception as exc:
        if not committed:
            partial.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
        if isinstance(exc, (OptimizationCancelled, RuntimeError)):
            raise
        if isinstance(exc, (UnidentifiedImageError, Image.DecompressionBombError, OSError)):
            raise RuntimeError(_read_error(exc)) from exc
        raise


def validate_image(path: Path, expected_size: tuple[int, int]) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("The image encoder did not create an output file.")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            size = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RuntimeError(f"The output image could not be read back. {exc}") from exc
    if size != expected_size:
        raise RuntimeError(
            f"The output image is {size[0]}×{size[1]}, expected {expected_size[0]}×{expected_size[1]}."
        )
    return size


def _save_image(
    image: Image.Image,
    path: Path,
    settings: Settings,
    exif_bytes: bytes | None,
    icc: bytes | None,
) -> str:
    base = _save_kwargs(settings)
    extras: dict = {}
    if icc:
        extras["icc_profile"] = icc
    if exif_bytes:
        extras["exif"] = exif_bytes

    warning = ""
    try:
        image.save(path, **base, **extras)
        return ""
    except Exception as first_error:
        path.unlink(missing_ok=True)
        if extras:
            try:
                image.save(path, **base)
                warning = "Metadata or the color profile could not be stored in this format."
                return warning
            except Exception:
                path.unlink(missing_ok=True)
        if settings.image_format == "avif" and _save_with_heif(image, path, settings.image_quality):
            return warning
        raise RuntimeError(f"Could not write the image. {first_error}") from first_error


def _save_kwargs(settings: Settings) -> dict:
    if settings.image_format == "webp":
        return {"format": "WEBP", "quality": settings.image_quality, "method": 6}
    if settings.image_format == "jpeg":
        return {
            "format": "JPEG",
            "quality": settings.image_quality,
            "optimize": True,
            "progressive": True,
        }
    return {"format": "AVIF", "quality": settings.image_quality}


def _save_with_heif(image: Image.Image, path: Path, quality: int) -> bool:
    try:
        import pillow_heif

        pillow_heif.from_pillow(image).save(str(path), quality=quality)
        return path.is_file() and path.stat().st_size > 0
    except Exception as exc:
        log.info("pillow-heif could not write AVIF: %s", exc)
        path.unlink(missing_ok=True)
        return False


def _normalize(image: Image.Image, output_format: str) -> Image.Image:
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if output_format == "jpeg":
        if image.mode in {"RGBA", "LA"}:
            return _flatten(image)
        if image.mode != "RGB":
            return image.convert("RGB")
        return image
    if image.mode == "CMYK":
        return image.convert("RGB")
    if image.mode == "LA":
        return image.convert("RGBA")
    if image.mode not in {"RGB", "RGBA", "L"}:
        return image.convert("RGB")
    return image


def _flatten(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _oriented_size(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    orientation = _orientation(image)
    if orientation in {5, 6, 7, 8}:
        return height, width
    return width, height


def _orientation(image: Image.Image) -> int:
    try:
        value = image.getexif().get(274, 1)
        return int(value or 1)
    except (TypeError, ValueError):
        return 1


def _has_extra_metadata(image: Image.Image) -> bool:
    try:
        exif = image.getexif()
    except Exception:
        return False
    if not exif:
        return False
    try:
        if exif.get_ifd(34853):
            return True
    except Exception:
        pass
    for tag in _METADATA_TAGS:
        value = exif.get(tag)
        if value not in (None, "", b""):
            return True
    return False


def _exif_bytes(exif: Image.Exif) -> bytes | None:
    if not exif:
        return None
    try:
        copied = exif
        copied[274] = 1
        data = copied.tobytes()
        return data or None
    except Exception:
        return None


def _reject_animation(image: Image.Image) -> None:
    fmt = (image.format or "").upper()
    if fmt in {"GIF", "WEBP"} and getattr(image, "n_frames", 1) > 1:
        raise RuntimeError("Animated images are not supported. Export a video, or use a still image.")


def _read_error(exc: Exception) -> str:
    if isinstance(exc, Image.DecompressionBombError):
        return "This image is too large to open safely."
    return "This image could not be read. It may be corrupted or use an unsupported encoding."


def _check_cancel(cancel: object | None) -> None:
    if cancel is not None and getattr(cancel, "is_set", lambda: False)():
        raise OptimizationCancelled()

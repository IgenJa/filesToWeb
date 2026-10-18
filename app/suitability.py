"""Decide when a file is already close enough to skip re-encoding.

Re-encoding a file that is already small and already in the right format
makes it look worse. A large camera original still needs a real encode.
"""

from __future__ import annotations

from app.models import Settings

# Above this many bytes per pixel, a matching image is still heavy enough to recompress.
IMAGE_COMPACT_BYTES_PER_PIXEL = 0.6

# Rough web bitrates for 1920x1080. Other sizes are scaled by pixel count.
VIDEO_BITRATE_LIMITS = {
    "high": 12_000_000,
    "balanced": 8_000_000,
    "small": 4_000_000,
}
VIDEO_BITRATE_SLACK = 1.4

RESOLUTION_BOX = {
    "2160": (3840, 2160),
    "1440": (2560, 1440),
    "1080": (1920, 1080),
    "720": (1280, 720),
}

SKIP_MESSAGE = (
    "Skipped. This file already matches the selected format and size. "
    'Uncheck "Skip files that are already suitable" to re-encode it.'
)


def image_should_skip(
    *,
    format_key: str,
    width: int | None,
    height: int | None,
    size: int,
    has_metadata: bool,
    settings: Settings,
) -> bool:
    if not settings.skip_suitable:
        return False
    if not width or not height or size <= 0:
        return False
    if format_key != settings.image_format:
        return False
    if not settings.keep_image_dimensions:
        if width > settings.max_width or height > settings.max_height:
            return False
    if not settings.preserve_metadata and has_metadata:
        return False
    bytes_per_pixel = size / (width * height)
    if bytes_per_pixel > IMAGE_COMPACT_BYTES_PER_PIXEL:
        return False
    return True


def video_should_skip(
    *,
    suffix: str,
    format_name: str,
    video_codec: str | None,
    audio_codec: str | None,
    has_audio: bool,
    pix_fmt: str | None,
    color_transfer: str | None,
    width: int | None,
    height: int | None,
    duration: float | None,
    size: int,
    settings: Settings,
) -> bool:
    if not settings.skip_suitable:
        return False
    if not width or not height or size <= 0 or not duration or duration <= 0:
        return False
    if (color_transfer or "").lower() in {"smpte2084", "arib-std-b67"}:
        return False
    if settings.max_resolution != "original":
        max_width, max_height = RESOLUTION_BOX[settings.max_resolution]
        if width > max_width or height > max_height:
            return False
    if not _codecs_match(
        suffix=suffix.lower(),
        format_name=format_name.lower(),
        video_codec=(video_codec or "").lower(),
        audio_codec=(audio_codec or "").lower(),
        has_audio=has_audio,
        pix_fmt=(pix_fmt or "").lower(),
        settings=settings,
    ):
        return False

    pixels = width * height
    scale = max(pixels / (1920 * 1080), 0.25)
    limit = VIDEO_BITRATE_LIMITS[settings.video_quality] * scale
    bitrate = (size * 8) / duration
    if bitrate > limit * VIDEO_BITRATE_SLACK:
        return False
    return True


def _codecs_match(
    *,
    suffix: str,
    format_name: str,
    video_codec: str,
    audio_codec: str,
    has_audio: bool,
    pix_fmt: str,
    settings: Settings,
) -> bool:
    if settings.remove_audio and has_audio:
        return False
    if settings.video_format == "mp4":
        if suffix != ".mp4" or "mp4" not in format_name:
            return False
        if video_codec != "h264":
            return False
        if pix_fmt not in {"yuv420p", "yuvj420p"}:
            return False
        if has_audio and audio_codec != "aac":
            return False
        return True

    if suffix != ".webm" or "webm" not in format_name:
        return False
    if video_codec != "vp9":
        return False
    if pix_fmt not in {"yuv420p", "yuvj420p"}:
        return False
    if has_audio and audio_codec not in {"opus", "vorbis"}:
        return False
    return True

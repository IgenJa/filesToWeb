import random
from pathlib import Path

import pytest
from PIL import Image

from app.images import fitted_size, inspect_image, optimize_image
from app.models import Settings


def _progress(_percent, _message):
    return None


def test_fitted_size_keeps_aspect_ratio_and_never_upscales():
    assert fitted_size(800, 400, 200, 200) == (200, 100)
    assert fitted_size(50, 40, 2560, 2560) == (50, 40)
    assert fitted_size(2560, 1440, 2560, 2560) == (2560, 1440)
    assert fitted_size(4000, 2000, None, None) == (4000, 2000)


def test_lossy_image_is_smaller_and_resized(tmp_path: Path):
    source = tmp_path / "noise.png"
    rng = random.Random(0)
    image = Image.new("RGB", (640, 480))
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(640 * 480)])
    image.save(source)
    original = source.read_bytes()
    destination = tmp_path / "noise.webp"
    result = optimize_image(
        source,
        destination,
        Settings(max_width=320, max_height=320, image_quality=82),
        _progress,
    )
    assert result.width == 320
    assert result.height == 240
    assert destination.is_file()
    assert destination.stat().st_size < source.stat().st_size
    assert source.read_bytes() == original
    with Image.open(destination) as output:
        assert output.size == (320, 240)
        assert output.format == "WEBP"


def test_small_image_is_not_upscaled(tmp_path: Path):
    source = tmp_path / "tiny.png"
    Image.new("RGB", (50, 40), (10, 20, 30)).save(source)
    destination = tmp_path / "tiny.webp"
    result = optimize_image(source, destination, Settings(), _progress)
    assert result.width == 50
    assert result.height == 40


def test_exif_orientation_is_applied(tmp_path: Path):
    source = tmp_path / "turned.jpg"
    image = Image.new("RGB", (30, 10), (200, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "secret-description"
    image.save(source, format="JPEG", exif=exif.tobytes(), quality=95)
    destination = tmp_path / "turned.webp"
    result = optimize_image(source, destination, Settings(keep_image_dimensions=True), _progress)
    assert (result.width, result.height) == (10, 30)


def test_metadata_is_stripped_unless_requested(tmp_path: Path):
    source = tmp_path / "tagged.jpg"
    image = Image.new("RGB", (24, 16), (0, 80, 160))
    exif = Image.Exif()
    exif[270] = "secret-description"
    image.save(source, format="JPEG", exif=exif.tobytes(), quality=95)

    stripped = tmp_path / "stripped.jpg"
    optimize_image(source, stripped, Settings(image_format="jpeg", keep_image_dimensions=True), _progress)
    with Image.open(stripped) as output:
        assert output.getexif().get(270) in (None, "")

    kept = tmp_path / "kept.jpg"
    optimize_image(
        source,
        kept,
        Settings(image_format="jpeg", keep_image_dimensions=True, preserve_metadata=True),
        _progress,
    )
    with Image.open(kept) as output:
        assert output.getexif().get(270) == "secret-description"


def test_transparent_png_becomes_a_white_jpeg(tmp_path: Path):
    source = tmp_path / "empty.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(source)
    destination = tmp_path / "empty.jpg"
    optimize_image(
        source,
        destination,
        Settings(image_format="jpeg", keep_image_dimensions=True),
        _progress,
    )
    with Image.open(destination) as output:
        assert output.mode == "RGB"
        assert output.getpixel((0, 0)) == (255, 255, 255)


def test_animated_gif_is_rejected(tmp_path: Path):
    source = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (20, 20), "red"), Image.new("RGB", (20, 20), "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
    with pytest.raises(RuntimeError, match="Animated"):
        optimize_image(source, tmp_path / "anim.webp", Settings(), _progress)


def test_corrupt_image_does_not_leave_an_output(tmp_path: Path):
    source = tmp_path / "broken.jpg"
    source.write_bytes(b"this is not a jpeg")
    destination = tmp_path / "broken.webp"
    try:
        optimize_image(source, destination, Settings(), _progress)
    except RuntimeError:
        pass
    else:
        raise AssertionError("A corrupt image was reported as optimized.")
    assert not destination.exists()
    assert not Path(str(destination) + ".partial").exists()


def test_failed_validation_is_not_success(tmp_path, monkeypatch):
    source = tmp_path / "photo.png"
    Image.new("RGB", (20, 20), "red").save(source)
    destination = tmp_path / "photo.webp"

    def reject(_path, _expected):
        raise RuntimeError("bad output")

    monkeypatch.setattr("app.images.validate_image", reject)
    try:
        optimize_image(source, destination, Settings(), _progress)
    except RuntimeError as exc:
        assert "bad output" in str(exc)
    else:
        raise AssertionError("Invalid output was treated as success.")
    assert not destination.exists()
    assert not Path(str(destination) + ".partial").exists()


def test_unicode_filename_is_preserved(tmp_path: Path):
    source = tmp_path / "café.png"
    Image.new("RGB", (12, 8), "white").save(source)
    details = inspect_image(source)
    assert details.width == 12
    assert details.height == 8


def test_avif_output_when_the_encoder_is_available(tmp_path: Path):
    from app.images import avif_supported

    if not avif_supported():
        pytest.skip("AVIF encoding is not available")
    source = tmp_path / "still.png"
    Image.new("RGB", (32, 24), (40, 90, 140)).save(source)
    destination = tmp_path / "still.avif"
    result = optimize_image(
        source,
        destination,
        Settings(image_format="avif", keep_image_dimensions=True, image_quality=50),
        _progress,
    )
    assert result.width == 32
    assert result.height == 24
    assert destination.is_file()
    assert destination.stat().st_size > 0

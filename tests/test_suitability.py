from app.models import Settings
from app.suitability import image_should_skip, video_should_skip


def test_large_photo_is_not_treated_as_already_optimized():
    settings = Settings()
    assert image_should_skip(
        format_key="jpeg",
        width=4000,
        height=3000,
        size=25 * 1024 * 1024,
        has_metadata=True,
        settings=settings,
    ) is False
    assert image_should_skip(
        format_key="webp",
        width=4000,
        height=3000,
        size=25 * 1024 * 1024,
        has_metadata=False,
        settings=settings,
    ) is False


def test_compact_webp_can_be_skipped():
    settings = Settings()
    size = int(1920 * 1080 * 0.2)
    assert image_should_skip(
        format_key="webp",
        width=1920,
        height=1080,
        size=size,
        has_metadata=False,
        settings=settings,
    ) is True


def test_metadata_and_dimensions_block_a_skip():
    size = int(800 * 600 * 0.1)
    assert image_should_skip(
        format_key="webp",
        width=800,
        height=600,
        size=size,
        has_metadata=True,
        settings=Settings(preserve_metadata=False),
    ) is False
    assert image_should_skip(
        format_key="webp",
        width=800,
        height=600,
        size=size,
        has_metadata=True,
        settings=Settings(preserve_metadata=True),
    ) is True
    assert image_should_skip(
        format_key="webp",
        width=4000,
        height=3000,
        size=int(4000 * 3000 * 0.1),
        has_metadata=False,
        settings=Settings(),
    ) is False
    assert image_should_skip(
        format_key="webp",
        width=4000,
        height=3000,
        size=int(4000 * 3000 * 0.1),
        has_metadata=False,
        settings=Settings(keep_image_dimensions=True),
    ) is True


def test_huge_compatible_video_is_not_skipped():
    # A 500 MB, 60-second 4K H.264 file is still too heavy for the web preset.
    assert video_should_skip(
        suffix=".mp4",
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        audio_codec="aac",
        has_audio=True,
        pix_fmt="yuv420p",
        color_transfer="bt709",
        width=3840,
        height=2160,
        duration=60,
        size=500 * 1024 * 1024,
        settings=Settings(max_resolution="original"),
    ) is False


def test_small_compatible_video_is_skipped():
    assert video_should_skip(
        suffix=".mp4",
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        audio_codec="aac",
        has_audio=True,
        pix_fmt="yuv420p",
        color_transfer="bt709",
        width=1280,
        height=720,
        duration=30,
        size=8 * 1024 * 1024,
        settings=Settings(),
    ) is True


def test_video_outside_the_resolution_cap_is_not_skipped():
    assert video_should_skip(
        suffix=".mp4",
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        audio_codec="aac",
        has_audio=True,
        pix_fmt="yuv420p",
        color_transfer=None,
        width=3840,
        height=2160,
        duration=30,
        size=8 * 1024 * 1024,
        settings=Settings(max_resolution="1080"),
    ) is False


def test_skip_can_be_turned_off():
    assert image_should_skip(
        format_key="webp",
        width=100,
        height=100,
        size=1000,
        has_metadata=False,
        settings=Settings(skip_suitable=False),
    ) is False

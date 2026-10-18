import threading
import time
from pathlib import Path

import pytest

from app.errors import OptimizationCancelled
from app.models import Settings
from app.videos import (
    build_ffmpeg_command,
    build_video_filters,
    inspect_video,
    mp4_has_faststart,
    optimize_video,
)
from tests.conftest import ffmpeg_exe, make_video

pytestmark = pytest.mark.skipif(ffmpeg_exe() is None, reason="FFmpeg is not installed")


def test_mp4_command_uses_crf_aac_and_faststart(tmp_path: Path):
    command = build_ffmpeg_command(
        tmp_path / "in.mov",
        tmp_path / "out.mp4",
        width=3840,
        height=2160,
        color_transfer="bt709",
        video_index=0,
        audio_index=1,
        has_audio=True,
        settings=Settings(),
        tonemap=False,
    )
    assert "libx264" in command
    assert command[command.index("-crf") + 1] == "24"
    assert command[command.index("-preset") + 1] == "medium"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert "aac" in command
    assert "-an" not in command
    assert "force_original_aspect_ratio=decrease" in command[command.index("-vf") + 1]


def test_filters_never_request_an_upscale_and_can_tonemap():
    kept = build_video_filters(width=320, height=240, color_transfer=None, max_resolution="original", tonemap=False)
    assert kept == "format=yuv420p"
    odd = build_video_filters(width=1919, height=1080, color_transfer=None, max_resolution="original", tonemap=False)
    assert odd.startswith("pad=")
    hdr = build_video_filters(
        width=3840,
        height=2160,
        color_transfer="smpte2084",
        max_resolution="1080",
        tonemap=True,
    )
    assert "tonemap=hable" in hdr
    assert "min(1920\\,iw)" in hdr
    assert "min(1080\\,ih)" in hdr
    muted = build_ffmpeg_command(
        Path("in.mp4"),
        Path("out.webm"),
        width=640,
        height=360,
        color_transfer=None,
        video_index=0,
        audio_index=None,
        has_audio=False,
        settings=Settings(video_format="webm", remove_audio=True),
        tonemap=False,
    )
    assert "libvpx-vp9" in muted
    assert "-an" in muted
    assert "+faststart" not in muted


def test_encode_adds_faststart_and_does_not_upscale(tmp_path: Path):
    source = tmp_path / "small.mp4"
    make_video(source, 320, 240, 1)
    before = source.read_bytes()
    destination = tmp_path / "small-web.mp4"
    seen: list[tuple[float, str]] = []
    result = optimize_video(
        source,
        destination,
        inspect_video(source),
        Settings(skip_suitable=False, max_resolution="1080", encoder_preset="veryfast"),
        lambda percent, message: seen.append((percent, message)),
        threading.Event(),
    )
    assert destination.is_file()
    assert destination.stat().st_size > 0
    assert result.width == 320
    assert result.height == 240
    assert mp4_has_faststart(destination)
    assert source.read_bytes() == before
    checked = inspect_video(destination)
    assert checked.video_codec == "h264"
    assert checked.has_audio is True
    assert seen


def test_downscale_fits_inside_the_selected_box(tmp_path: Path):
    source = tmp_path / "hd.mp4"
    make_video(source, 1600, 900, 1, audio=False)
    destination = tmp_path / "hd-web.mp4"
    result = optimize_video(
        source,
        destination,
        inspect_video(source),
        Settings(skip_suitable=False, max_resolution="720", encoder_preset="veryfast", remove_audio=True),
        lambda _percent, _message: None,
        threading.Event(),
    )
    assert result.width <= 1280
    assert result.height <= 720
    assert result.width < 1600
    assert result.width % 2 == 0
    assert result.height % 2 == 0
    assert inspect_video(destination).has_audio is False


def test_suitable_video_is_skipped_by_the_batch(tmp_path: Path, manager):
    source = tmp_path / "ready.mp4"
    make_video(source, 320, 240, 2, bitrate="150k")
    added = manager.add_paths([str(source)])
    file_id = added["files"][0]["id"]
    assert added["files"][0]["media_type"] == "video"
    job = manager.create_job(
        [file_id],
        str(tmp_path / "out"),
        Settings(skip_suitable=True, max_resolution="1080"),
    )
    snapshot = _wait(manager, job["id"])
    assert snapshot["files"][0]["status"] == "skipped"
    assert list((tmp_path / "out").iterdir()) == []
    assert source.exists()


def test_cancel_stops_ffmpeg_and_removes_the_partial_file(tmp_path: Path):
    source = tmp_path / "long.mp4"
    make_video(source, 1280, 720, 8, audio=False)
    destination = tmp_path / "long-web.mp4"
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        with pytest.raises(OptimizationCancelled):
            optimize_video(
                source,
                destination,
                inspect_video(source),
                Settings(skip_suitable=False, max_resolution="original", encoder_preset="slow", remove_audio=True),
                lambda _percent, _message: None,
                cancel,
            )
    finally:
        timer.cancel()
    assert not destination.exists()
    assert not Path(str(destination) + ".partial").exists()


def test_webm_output_is_a_real_vp9_file(tmp_path: Path):
    source = tmp_path / "clip.mp4"
    make_video(source, 160, 120, 0.5, audio=False)
    destination = tmp_path / "clip.webm"
    result = optimize_video(
        source,
        destination,
        inspect_video(source),
        Settings(
            skip_suitable=False,
            video_format="webm",
            max_resolution="original",
            encoder_preset="ultrafast",
            remove_audio=True,
        ),
        lambda _percent, _message: None,
        threading.Event(),
    )
    assert result.width == 160
    assert result.height == 120
    assert inspect_video(destination).video_codec == "vp9"


def _wait(manager, job_id: str, timeout: float = 60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = manager.job_snapshot(job_id)
        if snapshot["status"] in {"completed", "cancelled", "error"}:
            return snapshot
        time.sleep(0.05)
    raise AssertionError("The batch did not finish.")

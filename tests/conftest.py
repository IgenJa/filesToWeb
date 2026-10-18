"""Shared fixtures. Video helpers shell out to the local FFmpeg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.videos import no_window_flags


@pytest.fixture(autouse=True)
def _stop_ffmpeg():
    yield
    from app.videos import kill_active_ffmpeg

    kill_active_ffmpeg()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("OPTIMIZER_OUTPUT", str(tmp_path / "optimized"))
    monkeypatch.setattr("app.jobs.open_in_file_manager", lambda _path: None)
    from app.jobs import reset_manager

    instance = reset_manager()
    yield instance
    reset_manager()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("OPTIMIZER_OUTPUT", str(tmp_path / "optimized"))
    monkeypatch.setattr("app.jobs.open_in_file_manager", lambda _path: None)
    from app.jobs import reset_manager
    from app.main import app
    from fastapi.testclient import TestClient

    reset_manager()
    with TestClient(app) as client:
        yield client
    reset_manager()


def ffmpeg_exe() -> str | None:
    return shutil.which("ffmpeg")


def make_video(
    path: Path,
    width: int,
    height: int,
    seconds: float,
    *,
    audio: bool = True,
    bitrate: str | None = None,
) -> None:
    exe = ffmpeg_exe()
    if not exe:
        pytest.skip("FFmpeg is not installed")
    command = [
        exe,
        "-y",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={width}x{height}:rate=12:duration={seconds}",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    if bitrate:
        command += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
    if audio:
        command += ["-c:a", "aac", "-b:a", "64k", "-shortest"]
    else:
        command += ["-an"]
    command.append(str(path))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_flags(),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-800:])

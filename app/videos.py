"""Inspect and encode videos with FFmpeg. Frames stay on disk."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.errors import OptimizationCancelled
from app.models import Settings
from app.suitability import RESOLUTION_BOX

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]

H264_CRF = {"high": 20, "balanced": 24, "small": 28}
VP9_CRF = {"high": 28, "balanced": 32, "small": 36}
VP9_CPU = {
    "ultrafast": "5",
    "superfast": "5",
    "veryfast": "4",
    "faster": "3",
    "fast": "2",
    "medium": "2",
    "slow": "1",
    "slower": "1",
    "veryslow": "0",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".mpeg",
    ".mpg",
    ".wmv",
    ".flv",
    ".ts",
    ".mts",
    ".m2ts",
    ".3gp",
    ".ogv",
    ".vob",
    ".asf",
    ".f4v",
}

FFMPEG_MISSING = (
    "FFmpeg is not installed or not on PATH. "
    "Install it with: winget install Gyan.FFmpeg. "
    "Then close this app and start it again."
)

_TIME_RE = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)")
_active_lock = threading.Lock()
_active: set[subprocess.Popen] = set()
_probe_cache: dict | None = None


@dataclass
class VideoDetails:
    width: int
    height: int
    duration: float | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool
    pix_fmt: str | None
    color_transfer: str | None
    format_name: str
    video_index: int
    audio_index: int | None


@dataclass
class VideoResult:
    width: int
    height: int
    warning: str = ""


def no_window_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def capabilities() -> dict:
    info = _tool_info()
    return {
        "ffmpeg": bool(info["ffmpeg"]),
        "ffprobe": bool(info["ffprobe"]),
        "ffmpeg_version": info["version"],
        "encoders": {
            "libx264": "libx264" in info["encoders"],
            "libvpx-vp9": "libvpx-vp9" in info["encoders"],
            "aac": "aac" in info["encoders"],
            "libopus": "libopus" in info["encoders"],
        },
        "hdr_tonemap": "zscale" in info["filters"] and "tonemap" in info["filters"],
    }


def kill_active_ffmpeg() -> None:
    with _active_lock:
        processes = list(_active)
    for process in processes:
        _kill(process)


def inspect_video(path: Path) -> VideoDetails:
    if not ffprobe_path():
        raise RuntimeError(FFMPEG_MISSING)
    data = _probe(path)
    streams = data.get("streams") or []
    videos = []
    for index, stream in enumerate(streams):
        if stream.get("codec_type") != "video":
            continue
        if stream.get("disposition", {}).get("attached_pic") == 1:
            continue
        if stream.get("codec_name") in {"mjpeg", "png", "jpeg"} and len(streams) > 1:
            continue
        videos.append((index, stream))
    if not videos:
        raise RuntimeError("No video stream was found in this file.")
    index, stream = max(videos, key=lambda item: int(item[1].get("width") or 0) * int(item[1].get("height") or 0))
    audio_index = None
    audio_codec = None
    for stream_index, candidate in enumerate(streams):
        if candidate.get("codec_type") == "audio":
            audio_index = stream_index
            audio_codec = (candidate.get("codec_name") or "").lower() or None
            break

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("The video picture size could not be read.")
    if _rotation_swaps_axes(stream):
        width, height = height, width

    raw_duration = (data.get("format") or {}).get("duration") or stream.get("duration")
    duration = None
    if raw_duration not in (None, "N/A"):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    return VideoDetails(
        width=width,
        height=height,
        duration=duration,
        video_codec=(stream.get("codec_name") or "").lower() or None,
        audio_codec=audio_codec,
        has_audio=audio_index is not None,
        pix_fmt=(stream.get("pix_fmt") or "").lower() or None,
        color_transfer=(stream.get("color_transfer") or "").lower() or None,
        format_name=(data.get("format") or {}).get("format_name") or "",
        video_index=index,
        audio_index=audio_index,
    )


def optimize_video(
    source: Path,
    destination: Path,
    details: VideoDetails,
    settings: Settings,
    progress: Progress,
    cancel: threading.Event,
) -> VideoResult:
    if not ffmpeg_path():
        raise RuntimeError(FFMPEG_MISSING)
    _require_encoder(settings)
    if cancel.is_set():
        raise OptimizationCancelled()

    partial = Path(str(destination) + ".partial")
    warning = ""
    committed = False
    use_tonemap = _should_tonemap(details)
    try:
        progress(1, "Starting FFmpeg")
        try:
            _encode(source, partial, details, settings, progress, cancel, tonemap=use_tonemap)
        except RuntimeError:
            if not use_tonemap or cancel.is_set():
                raise
            partial.unlink(missing_ok=True)
            warning = "HDR tone mapping failed, so a simpler conversion was used. Colors may look different."
            log.warning("HDR tonemap failed for %s; retrying without it", source.name)
            _encode(source, partial, details, settings, progress, cancel, tonemap=False)
        if cancel.is_set():
            raise OptimizationCancelled()
        progress(99, "Checking output")
        checked = validate_video(partial)
        partial.replace(destination)
        committed = True
        progress(100, "Done")
        return VideoResult(width=checked.width, height=checked.height, warning=warning)
    except Exception:
        if not committed:
            partial.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
        raise


def validate_video(path: Path) -> VideoDetails:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("FFmpeg did not create an output file.")
    try:
        details = inspect_video(path)
    except RuntimeError as exc:
        raise RuntimeError(f"The output video could not be read back. {exc}") from exc
    if details.width <= 0 or details.height <= 0:
        raise RuntimeError("The output video has no readable picture.")
    return details


def build_ffmpeg_command(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    color_transfer: str | None,
    video_index: int,
    audio_index: int | None,
    has_audio: bool,
    settings: Settings,
    tonemap: bool,
) -> list[str]:
    ffmpeg = ffmpeg_path() or "ffmpeg"
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(source)]
    filters = build_video_filters(
        width=width,
        height=height,
        color_transfer=color_transfer,
        max_resolution=settings.max_resolution,
        tonemap=tonemap and _hdr(color_transfer) and _tonemap_available(),
    )
    if filters:
        command += ["-vf", filters]
    command += ["-map", f"0:{video_index}", "-sn", "-dn"]
    if not settings.preserve_metadata:
        command += ["-map_metadata", "-1", "-map_chapters", "-1"]

    if settings.video_format == "mp4":
        command += [
            "-c:v",
            "libx264",
            "-crf",
            str(H264_CRF[settings.video_quality]),
            "-preset",
            settings.encoder_preset,
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
        ]
    else:
        command += [
            "-c:v",
            "libvpx-vp9",
            "-crf",
            str(VP9_CRF[settings.video_quality]),
            "-b:v",
            "0",
            "-row-mt",
            "1",
            "-cpu-used",
            VP9_CPU.get(settings.encoder_preset, "2"),
            "-deadline",
            "good",
            "-pix_fmt",
            "yuv420p",
        ]

    if has_audio and not settings.remove_audio and audio_index is not None:
        audio_codec = "aac" if settings.video_format == "mp4" else "libopus"
        command += ["-map", f"0:{audio_index}", "-c:a", audio_codec, "-b:a", "128k", "-ac", "2"]
    else:
        command += ["-an"]

    if settings.video_format == "mp4":
        command += ["-movflags", "+faststart"]
    # The temp file ends in .partial, so the container has to be named explicitly.
    container = "mp4" if settings.video_format == "mp4" else "webm"
    command += ["-max_muxing_queue_size", "1024", "-progress", "pipe:1", "-nostats", "-f", container, str(destination)]
    return command


def build_video_filters(
    *,
    width: int,
    height: int,
    color_transfer: str | None,
    max_resolution: str,
    tonemap: bool,
) -> str:
    filters: list[str] = []
    if tonemap and _hdr(color_transfer):
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    if max_resolution != "original":
        max_width, max_height = RESOLUTION_BOX[max_resolution]
        # min() stops an upscale. decrease keeps the aspect ratio inside that box.
        filters.append(
            f"scale=w='min({max_width}\\,iw)':h='min({max_height}\\,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
        )
    elif width % 2 or height % 2:
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    filters.append("format=yuv420p")
    return ",".join(filters)


def mp4_has_faststart(path: Path) -> bool:
    """True when the moov atom is before the media data."""

    with path.open("rb") as handle:
        head = handle.read(4_000_000)
    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov < 0:
        return False
    if mdat < 0:
        return True
    return moov < mdat


def _encode(
    source: Path,
    destination: Path,
    details: VideoDetails,
    settings: Settings,
    progress: Progress,
    cancel: threading.Event,
    *,
    tonemap: bool,
) -> None:
    command = build_ffmpeg_command(
        source,
        destination,
        width=details.width,
        height=details.height,
        color_transfer=details.color_transfer,
        video_index=details.video_index,
        audio_index=details.audio_index,
        has_audio=details.has_audio,
        settings=settings,
        tonemap=tonemap,
    )
    log.info("FFmpeg %s", " ".join(command[1:]))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_flags(),
    )
    with _active_lock:
        _active.add(process)
    errors: list[str] = []

    def read_errors() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            errors.append(line.rstrip())
            if len(errors) > 120:
                del errors[:60]

    reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    duration = details.duration or 0
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel.is_set():
                _kill(process)
                raise OptimizationCancelled()
            _report_progress(raw_line.strip(), duration, progress)
        return_code = process.wait(timeout=60)
    finally:
        if process.poll() is None:
            _kill(process)
        reader.join(timeout=2)
        with _active_lock:
            _active.discard(process)

    if cancel.is_set():
        raise OptimizationCancelled()
    if return_code != 0:
        tail = "\n".join(errors[-12:]).strip()
        raise RuntimeError(tail or f"FFmpeg exited with code {return_code}.")


def _report_progress(line: str, duration: float, progress: Progress) -> None:
    if "=" not in line:
        return
    key, value = line.split("=", 1)
    if key == "progress" and value == "end":
        progress(99, "Finishing file")
        return
    seconds = _progress_seconds(key, value, duration)
    if seconds is None:
        return
    if duration > 0:
        percent = min(99.0, max(0.0, seconds / duration * 100))
        progress(percent, f"Encoding {percent:.0f}% ({_clock(seconds)} of {_clock(duration)})")
    else:
        progress(-1, f"Encoding {_clock(seconds)}")


def _progress_seconds(key: str, value: str, duration: float) -> float | None:
    try:
        if key == "out_time_us":
            return int(value) / 1_000_000
        if key == "out_time_ms":
            # FFmpeg's out_time_ms field is actually microseconds.
            raw = int(value)
            seconds = raw / 1_000_000
            if duration and seconds > duration * 5:
                seconds = raw / 1000
            return seconds
    except ValueError:
        return None
    if key != "out_time":
        return None
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _probe(path: Path) -> dict:
    ffprobe = ffprobe_path()
    if not ffprobe:
        raise RuntimeError(FFMPEG_MISSING)
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_flags(),
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        message = result.stderr.strip() or "ffprobe could not read this file."
        raise RuntimeError(message)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned data that could not be read.") from exc


def _rotation_swaps_axes(stream: dict) -> bool:
    rotation = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(str(tags["rotate"]))
        except ValueError:
            rotation = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" not in side:
            continue
        try:
            rotation = int(side["rotation"])
        except (TypeError, ValueError):
            continue
    return abs(rotation) % 180 == 90


def _hdr(color_transfer: str | None) -> bool:
    return (color_transfer or "").lower() in {"smpte2084", "arib-std-b67"}


def _should_tonemap(details: VideoDetails) -> bool:
    return _hdr(details.color_transfer) and _tonemap_available()


def _tonemap_available() -> bool:
    info = _tool_info()
    return "zscale" in info["filters"] and "tonemap" in info["filters"]


def _require_encoder(settings: Settings) -> None:
    info = _tool_info()
    if settings.video_format == "mp4":
        missing = [name for name in ("libx264", "aac") if name not in info["encoders"]]
        if missing:
            raise RuntimeError("This FFmpeg build cannot encode H.264/AAC (" + ", ".join(missing) + ").")
        return
    missing = [name for name in ("libvpx-vp9", "libopus") if name not in info["encoders"]]
    if missing:
        raise RuntimeError("This FFmpeg build cannot encode WebM (" + ", ".join(missing) + "). Choose MP4 instead.")


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            creationflags=no_window_flags(),
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def _tool_info() -> dict:
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    ffmpeg = ffmpeg_path()
    ffprobe = ffprobe_path()
    version = ""
    encoders: set[str] = set()
    filters: set[str] = set()
    if ffmpeg:
        version_run = _capture([ffmpeg, "-version"])
        match = re.search(r"ffmpeg version (\d+\.\d+(?:\.\d+)?)", version_run.stdout)
        version = match.group(1) if match else ""
        encoder_run = _capture([ffmpeg, "-hide_banner", "-encoders"])
        encoders = set(re.findall(r"\b(libx264|libvpx-vp9|aac|libopus)\b", encoder_run.stdout))
        filter_run = _capture([ffmpeg, "-hide_banner", "-filters"])
        filters = set(re.findall(r"\b(zscale|tonemap)\b", filter_run.stdout))
    _probe_cache = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "version": version,
        "encoders": encoders,
        "filters": filters,
    }
    return _probe_cache


def _capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_flags(),
        check=False,
    )

"""In-memory queue, batch runner, and file library for one local session."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import load_user_config
from app.display import format_bytes, savings
from app.errors import ConflictError, NotFoundError, OptimizationCancelled
from app.images import IMAGE_EXTENSIONS, inspect_image, optimize_image
from app.models import Settings
from app.paths import cleanup_partials, is_relative_to, open_in_file_manager, safe_stem, safe_upload_name, unique_output_path
from app.suitability import SKIP_MESSAGE, image_should_skip, video_should_skip
from app.videos import VIDEO_EXTENSIONS, VideoDetails, inspect_video, optimize_video

log = logging.getLogger(__name__)

SCAN_LIMIT = 2000
MIN_FREE_BYTES = 64 * 1024 * 1024
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
_SKIP_NAMES = {"thumbs.db", "desktop.ini"}

_manager: "Manager | None" = None
_manager_lock = threading.Lock()


@dataclass
class MediaFile:
    id: str
    path: str
    name: str
    media_type: str
    size: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    format_label: str = ""
    format_key: str = ""
    container: str = ""
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool = False
    pix_fmt: str | None = None
    color_transfer: str | None = None
    has_metadata: bool = False
    video_index: int | None = None
    audio_index: int | None = None
    staged: bool = False
    error: str | None = None


@dataclass
class Task:
    file: MediaFile
    status: str = "pending"
    progress: float = 0
    message: str = ""
    error: str | None = None
    output_path: str | None = None
    output_size: int | None = None
    output_width: int | None = None
    output_height: int | None = None
    warning: str | None = None
    larger: bool = False
    decision: str | None = None


@dataclass
class Job:
    id: str
    tasks: list[Task]
    settings: Settings
    output_dir: Path
    status: str = "pending"
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float | None = None
    finished: float | None = None


class Manager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.library: dict[str, MediaFile] = {}
        self.jobs: dict[str, Job] = {}
        self.threads: list[threading.Thread] = []
        self.allowed_dirs: set[Path] = set()
        self.staging = _staging_root()
        self.default_output = _default_output_dir()
        self.allow_directory(self.default_output)
        self.staging.mkdir(parents=True, exist_ok=True)
        cleanup_partials(self.staging)
        if self.default_output.exists():
            cleanup_partials(self.default_output)

    def allow_directory(self, path: Path) -> None:
        try:
            self.allowed_dirs.add(path.resolve())
        except OSError:
            self.allowed_dirs.add(path.absolute())

    def shutdown(self) -> None:
        with self.lock:
            jobs = list(self.jobs.values())
            threads = list(self.threads)
        for job in jobs:
            job.cancel.set()
        from app.videos import kill_active_ffmpeg

        kill_active_ffmpeg()
        for thread in threads:
            thread.join(timeout=5)

    def add_paths(self, raw_paths: list[str]) -> dict:
        files: list[dict] = []
        notices: list[str] = []
        for raw in raw_paths:
            text = str(raw).strip().strip('"')
            if not text:
                continue
            path = Path(text).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.is_dir():
                found, truncated = scan_folder(path)
                if truncated:
                    notices.append(f"Stopped after {SCAN_LIMIT} media files in {path.name}.")
                if not found and not truncated:
                    notices.append(f"No supported images or videos were found in {path.name}.")
                for item in found:
                    files.append(self._add_file(item, staged=False))
                continue
            files.append(self._add_file(path, staged=False))
        return {"files": files, "notice": " ".join(notices)}

    def save_upload(self, filename: str | None, stream) -> dict:
        file_id = uuid.uuid4().hex
        folder = self.staging / file_id
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / safe_upload_name(filename)
        try:
            with destination.open("wb") as handle:
                shutil.copyfileobj(stream, handle, length=1024 * 1024)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self._add_file(destination, staged=True)

    def list_files(self) -> dict:
        with self.lock:
            return {"files": [_file_dict(item) for item in self.library.values()], "notice": ""}

    def remove_file(self, file_id: str) -> None:
        with self.lock:
            if self._busy():
                raise ConflictError("Wait for the current batch to finish before removing files.")
            media = self.library.pop(file_id, None)
        if media is None:
            raise NotFoundError("That file is not in the list.")
        if media.staged:
            _delete_staged(media, self.staging)

    def reset(self) -> None:
        with self.lock:
            if self._busy():
                raise ConflictError("Wait for the current batch to finish.")
            files = list(self.library.values())
            self.library.clear()
            self.jobs.clear()
        for media in files:
            if media.staged:
                _delete_staged(media, self.staging)

    def create_job(self, file_ids: list[str], output_dir: str | None, settings: Settings) -> dict:
        folder = resolve_output_dir(output_dir, self.default_output)
        if folder.exists() and not folder.is_dir():
            raise RuntimeError("The output path is a file, not a folder.")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Could not create the output folder: {exc}") from exc
        self.allow_directory(folder)

        with self.lock:
            if self._busy():
                raise ConflictError("A batch is already running.")
            missing = [file_id for file_id in file_ids if file_id not in self.library]
            if missing:
                raise NotFoundError("One or more files are no longer in the list. Add them again.")
            tasks = [Task(file=self.library[file_id]) for file_id in file_ids]
            job = Job(id=uuid.uuid4().hex, tasks=tasks, settings=settings, output_dir=folder)
            self.jobs[job.id] = job
            thread = threading.Thread(target=self._run, args=(job,), name=f"optimize-{job.id[:8]}", daemon=False)
            self.threads.append(thread)
            thread.start()
            return self._job_dict(job)

    def job_snapshot(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFoundError("That batch is no longer available. If the app was restarted, finished files are still in the output folder.")
            return self._job_dict(job)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFoundError("That batch is no longer available.")
            job.cancel.set()
            if job.status in {"completed", "cancelled", "error"}:
                return self._job_dict(job)
        from app.videos import kill_active_ffmpeg

        kill_active_ffmpeg()
        return self.job_snapshot(job_id)

    def decide(self, job_id: str, file_id: str, decision: str) -> dict:
        if decision not in {"keep", "discard"}:
            raise RuntimeError("Choose keep or discard.")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFoundError("That batch is no longer available.")
            task = next((item for item in job.tasks if item.file.id == file_id), None)
            if task is None:
                raise NotFoundError("That file is not part of this batch.")
            if task.decision != "pending":
                raise RuntimeError("This file is not waiting for a decision.")
            output = Path(task.output_path) if task.output_path else None
            folder = job.output_dir
        if decision == "keep":
            with self.lock:
                task.decision = "keep"
                task.message = "Kept, even though it is larger than the original."
            return self.job_snapshot(job_id)
        if output is None or not is_relative_to(output, folder):
            raise RuntimeError("Refusing to delete a file outside the output folder.")
        output.unlink(missing_ok=True)
        with self.lock:
            task.status = "discarded"
            task.decision = "discard"
            task.larger = False
            task.output_size = None
            task.output_path = None
            task.warning = None
            task.message = "Discarded because it was larger than the original."
        return self.job_snapshot(job_id)

    def open_output(self, job_id: str) -> Path:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFoundError("That batch is no longer available.")
            folder = job.output_dir
        self._open_allowed(folder)
        return folder

    def open_default_output(self) -> Path:
        self.default_output.mkdir(parents=True, exist_ok=True)
        self._open_allowed(self.default_output)
        return self.default_output

    def _open_allowed(self, folder: Path) -> None:
        try:
            resolved = folder.resolve()
        except OSError:
            resolved = folder.absolute()
        if resolved not in self.allowed_dirs:
            raise RuntimeError("That folder cannot be opened from this app.")
        open_in_file_manager(resolved)

    def _add_file(self, path: Path, *, staged: bool) -> dict:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        with self.lock:
            for existing in self.library.values():
                if existing.path == resolved:
                    return _file_dict(existing)
        media = _inspect(path, staged=staged, resolved=resolved)
        with self.lock:
            for existing in self.library.values():
                if existing.path == media.path:
                    return _file_dict(existing)
            self.library[media.id] = media
            return _file_dict(media)

    def _busy(self) -> bool:
        return any(job.status in {"pending", "running"} for job in self.jobs.values())

    def _run(self, job: Job) -> None:
        import time

        with self.lock:
            job.status = "running"
            job.started = time.time()
        log.info("Optimizing %s file(s) into %s", len(job.tasks), job.output_dir)
        try:
            for task in job.tasks:
                if job.cancel.is_set():
                    with self.lock:
                        task.status = "cancelled"
                        task.message = "Cancelled."
                    continue
                with self.lock:
                    task.status = "running"
                    task.message = "Starting"
                try:
                    self._process(job, task)
                except OptimizationCancelled:
                    with self.lock:
                        task.status = "cancelled"
                        task.message = "Cancelled."
                        task.progress = task.progress if task.progress > 0 else 0
                    job.cancel.set()
                except Exception as exc:
                    log.exception("Could not optimize %s", task.file.name)
                    message = _short(str(exc))
                    with self.lock:
                        task.status = "error"
                        task.error = message
                        task.message = message
                        task.progress = 0
                else:
                    if task.file.staged and task.status in {"completed", "skipped"}:
                        _delete_staged(task.file, self.staging)
        finally:
            with self.lock:
                job.finished = time.time()
                if job.status != "error":
                    job.status = "cancelled" if job.cancel.is_set() else "completed"
            log.info("Batch %s finished with status %s", job.id, job.status)

    def _process(self, job: Job, task: Task) -> None:
        media = task.file
        if media.media_type not in {"image", "video"} or media.error:
            raise RuntimeError(media.error or "This file type is not supported.")
        source = Path(media.path)
        if not source.is_file():
            raise RuntimeError("The file is no longer available.")

        if media.media_type == "image" and image_should_skip(
            format_key=media.format_key,
            width=media.width,
            height=media.height,
            size=media.size,
            has_metadata=media.has_metadata,
            settings=job.settings,
        ):
            self._mark_skipped(task)
            return
        if media.media_type == "video" and video_should_skip(
            suffix=source.suffix,
            format_name=media.container,
            video_codec=media.video_codec,
            audio_codec=media.audio_codec,
            has_audio=media.has_audio,
            pix_fmt=media.pix_fmt,
            color_transfer=media.color_transfer,
            width=media.width,
            height=media.height,
            duration=media.duration,
            size=media.size,
            settings=job.settings,
        ):
            self._mark_skipped(task)
            return

        extension = job.settings.image_extension() if media.media_type == "image" else job.settings.video_extension()
        destination = unique_output_path(job.output_dir, safe_stem(media.name), extension, source)
        space_warning = _disk_warning(job.output_dir, media.size)

        def progress(percent: float, message: str) -> None:
            with self.lock:
                task.progress = percent
                task.message = message

        if media.media_type == "image":
            result = optimize_image(source, destination, job.settings, progress, job.cancel)
        else:
            details = VideoDetails(
                width=media.width or 0,
                height=media.height or 0,
                duration=media.duration,
                video_codec=media.video_codec,
                audio_codec=media.audio_codec,
                has_audio=media.has_audio,
                pix_fmt=media.pix_fmt,
                color_transfer=media.color_transfer,
                format_name=media.container,
                video_index=media.video_index if media.video_index is not None else 0,
                audio_index=media.audio_index,
            )
            result = optimize_video(source, destination, details, job.settings, progress, job.cancel)

        if not destination.is_file() or destination.stat().st_size <= 0:
            raise RuntimeError("The output file was not created.")
        output_size = destination.stat().st_size
        saved, percent, larger = savings(media.size, output_size)
        warnings = [part for part in (space_warning, result.warning) if part]
        with self.lock:
            task.output_path = str(destination)
            task.output_size = output_size
            task.output_width = result.width
            task.output_height = result.height
            task.larger = larger
            task.status = "completed"
            task.progress = 100
            if larger:
                task.decision = "pending"
                warnings.append(
                    f"This file is larger than the original ({format_bytes(media.size)} → {format_bytes(output_size)}). "
                    "Keep it, or discard it."
                )
                task.message = warnings[-1]
            else:
                task.message = f"Saved {format_bytes(saved)} ({percent:.0f}%)."
            task.warning = " ".join(warnings) if warnings else None

    def _mark_skipped(self, task: Task) -> None:
        with self.lock:
            task.status = "skipped"
            task.progress = 100
            task.message = SKIP_MESSAGE

    def _job_dict(self, job: Job) -> dict:
        return {
            "id": job.id,
            "status": job.status,
            "output_dir": str(job.output_dir),
            "error": job.error,
            "summary": _summary(job),
            "files": [_task_dict(task) for task in job.tasks],
        }


def get_manager() -> Manager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = Manager()
        return _manager


def reset_manager() -> Manager:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = Manager()
        return _manager


def scan_folder(folder: Path) -> tuple[list[Path], bool]:
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(folder):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.lower() in _SKIP_NAMES:
                continue
            if Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            found.append(Path(directory) / name)
            if len(found) >= SCAN_LIMIT:
                found.sort(key=lambda item: str(item).lower())
                return found, True
    found.sort(key=lambda item: str(item).lower())
    return found, False


def resolve_output_dir(value: str | None, default: Path) -> Path:
    if value is None or not str(value).strip():
        return default
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _inspect(path: Path, *, staged: bool, resolved: str) -> MediaFile:
    file_id = uuid.uuid4().hex
    name = path.name
    if not path.exists() or not path.is_file():
        return MediaFile(
            id=file_id,
            path=resolved,
            name=name,
            media_type="unsupported",
            size=0,
            error="File not found.",
            staged=staged,
        )
    size = path.stat().st_size
    suffix = path.suffix.lower()
    try:
        if suffix in VIDEO_EXTENSIONS:
            return _video_file(path, file_id, resolved, size, staged)
        if suffix in IMAGE_EXTENSIONS:
            return _image_file(path, file_id, resolved, size, staged)
        try:
            return _image_file(path, file_id, resolved, size, staged)
        except Exception:
            return _video_file(path, file_id, resolved, size, staged)
    except Exception as exc:
        return MediaFile(
            id=file_id,
            path=resolved,
            name=name,
            media_type="unsupported",
            size=size,
            format_label=suffix.lstrip(".").upper() or "File",
            error=_short(str(exc)) or "This file type is not supported.",
            staged=staged,
        )


def _image_file(path: Path, file_id: str, resolved: str, size: int, staged: bool) -> MediaFile:
    details = inspect_image(path)
    return MediaFile(
        id=file_id,
        path=resolved,
        name=path.name,
        media_type="image",
        size=size,
        width=details.width,
        height=details.height,
        format_label=details.format_label,
        format_key=details.format_key,
        has_metadata=details.has_metadata,
        staged=staged,
    )


def _video_file(path: Path, file_id: str, resolved: str, size: int, staged: bool) -> MediaFile:
    details = inspect_video(path)
    codec = (details.video_codec or "").upper()
    label = path.suffix.lstrip(".").upper() or "VIDEO"
    if codec:
        label = f"{label} · {codec}"
    return MediaFile(
        id=file_id,
        path=resolved,
        name=path.name,
        media_type="video",
        size=size,
        width=details.width,
        height=details.height,
        duration=details.duration,
        format_label=label,
        format_key=path.suffix.lower().lstrip("."),
        container=details.format_name,
        video_codec=details.video_codec,
        audio_codec=details.audio_codec,
        has_audio=details.has_audio,
        pix_fmt=details.pix_fmt,
        color_transfer=details.color_transfer,
        video_index=details.video_index,
        audio_index=details.audio_index,
        staged=staged,
    )


def _file_dict(media: MediaFile) -> dict:
    return {
        "id": media.id,
        "name": media.name,
        "path": media.path,
        "media_type": media.media_type,
        "size": media.size,
        "width": media.width,
        "height": media.height,
        "duration": media.duration,
        "format_label": media.format_label,
        "error": media.error,
        "status": "error" if media.error else "ready",
        "progress": 0,
        "message": media.error or "",
        "output_size": None,
        "output_name": None,
        "saved_bytes": None,
        "saved_percent": None,
        "larger": False,
        "warning": None,
        "decision": None,
    }


def _task_dict(task: Task) -> dict:
    media = task.file
    saved = None
    percent = None
    if task.output_size is not None and task.status == "completed":
        saved_bytes, saved_percent, _larger = savings(media.size, task.output_size)
        saved = saved_bytes
        percent = round(saved_percent, 1)
    return {
        "id": media.id,
        "name": media.name,
        "path": media.path,
        "media_type": media.media_type,
        "size": media.size,
        "width": media.width,
        "height": media.height,
        "duration": media.duration,
        "format_label": media.format_label,
        "error": task.error or media.error,
        "status": task.status,
        "progress": task.progress,
        "message": task.message,
        "output_size": task.output_size,
        "output_name": Path(task.output_path).name if task.output_path else None,
        "output_width": task.output_width,
        "output_height": task.output_height,
        "saved_bytes": saved,
        "saved_percent": percent,
        "larger": task.larger,
        "warning": task.warning,
        "decision": task.decision,
    }


def _summary(job: Job) -> dict:
    completed = [task for task in job.tasks if task.status == "completed"]
    smaller = [task for task in completed if task.output_size is not None and not task.larger]
    saved = sum(task.file.size - (task.output_size or 0) for task in smaller)
    original = sum(task.file.size for task in smaller)
    percent = (saved / original * 100) if original else 0.0
    return {
        "total": len(job.tasks),
        "completed": len(completed),
        "skipped": sum(1 for task in job.tasks if task.status == "skipped"),
        "failed": sum(1 for task in job.tasks if task.status == "error"),
        "cancelled": sum(1 for task in job.tasks if task.status == "cancelled"),
        "discarded": sum(1 for task in job.tasks if task.status == "discarded"),
        "pending_decision": sum(1 for task in job.tasks if task.decision == "pending"),
        "saved_bytes": saved,
        "saved_percent": round(percent, 1),
        "larger_count": sum(1 for task in completed if task.larger),
    }


def _disk_warning(directory: Path, input_size: int) -> str:
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return ""
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"Not enough free disk space ({format_bytes(free)} left).")
    if input_size > 0 and free < input_size:
        return (
            f"Free space ({format_bytes(free)}) is less than the original file. "
            "Encoding will continue because the output is usually smaller."
        )
    return ""


def _delete_staged(media: MediaFile, staging: Path) -> None:
    path = Path(media.path)
    try:
        staging_root = staging.resolve()
    except OSError:
        return
    if not is_relative_to(path, staging_root):
        return
    try:
        path.unlink(missing_ok=True)
        parent = path.parent.resolve()
        if parent != staging_root and is_relative_to(parent, staging_root):
            parent.rmdir()
    except OSError:
        pass


def _short(message: str) -> str:
    compact = " ".join(message.split())
    if len(compact) > 500:
        return compact[:500].rstrip() + "..."
    return compact


def _staging_root() -> Path:
    env = os.environ.get("OPTIMIZER_STAGING")
    return Path(env) if env else Path.cwd() / ".staging"


def _default_output_dir() -> Path:
    env = os.environ.get("OPTIMIZER_OUTPUT")
    if env:
        return Path(env)
    _settings, configured = load_user_config()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    return Path.cwd() / "optimized"

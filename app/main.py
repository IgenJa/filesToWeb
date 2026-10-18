"""Local web app. It binds to 127.0.0.1 and does not call any network service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import ROOT, load_user_config
from app.errors import ConflictError, NotFoundError
from app.images import avif_supported, heic_supported
from app.jobs import get_manager, resolve_output_dir
from app.models import DialogRequest, JobRequest, OpenRequest, PathsRequest
from app.paths import cleanup_partials
from app.videos import capabilities

log = logging.getLogger(__name__)
STATIC = ROOT / "static"
_dialog_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manager = get_manager()
    cleanup_partials(manager.staging)
    if manager.default_output.exists():
        cleanup_partials(manager.default_output)
    yield
    get_manager().shutdown()


app = FastAPI(title="Optimize for Web", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def disable_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
def health() -> dict:
    manager = get_manager()
    return {
        "version": __version__,
        "avif": avif_supported(),
        "heic": heic_supported(),
        "output_dir": str(manager.default_output),
        **capabilities(),
    }


@app.get("/api/defaults")
def defaults() -> dict:
    settings, configured = load_user_config()
    output = configured or str(get_manager().default_output)
    return {"settings": settings.model_dump(), "output_dir": output}


@app.get("/api/files")
def list_files() -> dict:
    return get_manager().list_files()


@app.post("/api/paths")
def add_paths(body: PathsRequest) -> dict:
    return get_manager().add_paths(body.paths)


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    manager = get_manager()
    saved = []
    for item in files:
        saved.append(await asyncio.to_thread(manager.save_upload, item.filename, item.file))
    return {"files": saved, "notice": ""}


@app.post("/api/dialog")
async def dialog(body: DialogRequest) -> dict:
    if _dialog_lock.locked():
        raise HTTPException(status_code=409, detail="A file dialog is already open.")
    async with _dialog_lock:
        paths = await _run_dialog(body.mode)
    if body.mode == "output":
        if not paths:
            return {"path": ""}
        folder = resolve_output_dir(paths[0], get_manager().default_output)
        get_manager().allow_directory(folder)
        return {"path": str(folder)}
    if not paths:
        return {"files": [], "notice": ""}
    return await asyncio.to_thread(get_manager().add_paths, paths)


@app.delete("/api/files/{file_id}")
def remove_file(file_id: str) -> dict:
    get_manager().remove_file(file_id)
    return {"ok": True}


@app.post("/api/reset")
def reset() -> dict:
    get_manager().reset()
    return {"ok": True}


@app.post("/api/jobs")
def create_job(body: JobRequest) -> dict:
    return get_manager().create_job(body.file_ids, body.output_dir, body.settings)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return get_manager().job_snapshot(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return get_manager().cancel(job_id)


@app.post("/api/jobs/{job_id}/files/{file_id}/keep")
def keep_output(job_id: str, file_id: str) -> dict:
    return get_manager().decide(job_id, file_id, "keep")


@app.post("/api/jobs/{job_id}/files/{file_id}/discard")
def discard_output(job_id: str, file_id: str) -> dict:
    return get_manager().decide(job_id, file_id, "discard")


@app.post("/api/jobs/{job_id}/open")
def open_job_folder(job_id: str) -> dict:
    path = get_manager().open_output(job_id)
    return {"path": str(path)}


@app.post("/api/open-output")
def open_output(body: OpenRequest) -> dict:
    manager = get_manager()
    if body.job_id:
        path = manager.open_output(body.job_id)
        return {"path": str(path)}
    folder = resolve_output_dir(body.path, manager.default_output)
    manager.allow_directory(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manager._open_allowed(folder)
    return {"path": str(folder)}


@app.exception_handler(ConflictError)
async def conflict_handler(_request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(NotFoundError)
async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(RuntimeError)
async def runtime_handler(_request: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    parts = []
    for error in exc.errors():
        location = " ".join(str(item) for item in error.get("loc", []) if item != "body")
        message = error.get("msg", "Invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "Check the settings and try again."})


async def _run_dialog(mode: str) -> list[str]:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = str(pythonw if pythonw.exists() else sys.executable)
    helper = ROOT / "app" / "dialog_helper.py"
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            str(helper),
            mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT),
            env=env,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"The file dialog could not be opened. Drop the files instead. {exc}",
        ) from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip() or "The file dialog could not be opened. Drop the files instead."
        raise HTTPException(status_code=500, detail=message)
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="The file dialog returned an unexpected response.") from exc
    if isinstance(payload, dict):
        raise HTTPException(status_code=500, detail=str(payload.get("error") or "The file dialog failed."))
    return [str(item) for item in payload]


# Imported so a misconfigured static mount fails at startup instead of on first click.
if not (STATIC / "index.html").is_file():
    log.warning("The interface file is missing: %s", STATIC / "index.html")

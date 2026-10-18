"""Filenames and folders that are safe on Windows."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def safe_stem(filename: str) -> str:
    """Return a readable filename stem that Windows can store."""

    stem = Path(str(filename)).stem
    stem = _INVALID.sub("_", stem).strip(" .")
    if not stem:
        stem = "file"
    if stem.upper() in _RESERVED:
        stem = f"file-{stem}"
    if stem.startswith("-"):
        stem = f"file{stem}"
    if len(stem) > 100:
        stem = stem[:100].rstrip(" .")
    return stem or "file"


def safe_upload_name(filename: str | None) -> str:
    original = Path(filename or "file").name
    stem = safe_stem(original)
    extension = Path(original).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ""
    return f"{stem}{extension}"


def unique_output_path(directory: Path, stem: str, extension: str, source: Path) -> Path:
    """Pick an output path that does not replace the original or an existing file."""

    if not extension.startswith("."):
        extension = f".{extension}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        source_resolved = source.resolve()
    except OSError:
        source_resolved = source.absolute()

    for index in range(1, 10000):
        suffix = "" if index == 1 else f"-{index}"
        candidate = directory / f"{stem}{suffix}{extension}"
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if resolved == source_resolved or candidate.exists():
            continue
        return candidate
    raise RuntimeError("Could not choose a unique output filename.")


def cleanup_partials(directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in directory.glob("*.partial"):
        try:
            path.unlink()
        except OSError:
            pass


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def open_in_file_manager(path: Path) -> None:
    """Open a folder in the system file manager."""

    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)

"""Human-readable sizes and durations."""

from __future__ import annotations


def format_bytes(size: int) -> str:
    if size < 0:
        size = 0
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            if value >= 10:
                return f"{value:.1f} {unit}"
            return f"{value:.2f} {unit}"
    return f"{value:.1f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def savings(original: int, optimized: int) -> tuple[int, float, bool]:
    """Return bytes saved, percent saved, and whether the output grew."""

    saved = original - optimized
    percent = (saved / original * 100) if original else 0.0
    return saved, percent, optimized > original

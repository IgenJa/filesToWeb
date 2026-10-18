"""Built-in defaults and an optional config.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.models import Settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


def load_user_config(path: Path | None = None) -> tuple[Settings, str | None]:
    """Return settings and an optional default output directory.

    A missing or broken config file falls back to the built-in defaults.
    """

    config_path = CONFIG_PATH if path is None else path
    if not config_path.is_file():
        return Settings(), None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring %s: %s", config_path, exc)
        return Settings(), None
    if not isinstance(raw, dict):
        log.warning("Ignoring %s because it is not a JSON object.", config_path)
        return Settings(), None

    output_dir = raw.pop("output_dir", None)
    raw.pop("_comment", None)
    if isinstance(output_dir, str):
        output_dir = output_dir.strip() or None
    elif output_dir is not None:
        output_dir = None

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        log.warning("Ignoring invalid values in %s: %s", config_path, exc)
        return Settings(), output_dir
    return settings, output_dir

"""Shared utility helpers for plugin core modules."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,50}$")


def validate_plugin_name(name: str) -> str:
    """Validate plugin name with strict character set and length constraints."""
    cleaned = name.strip()
    if not PLUGIN_NAME_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "Invalid plugin name. Use 1-50 chars from: a-z, A-Z, 0-9, underscore."
        )
    return cleaned


def get_runtime_root(plugin_root: Path) -> Path:
    """Resolve plugin runtime root under AstrBot data directory."""
    plugin_name = plugin_root.name
    data_dir = _resolve_astrbot_data_dir(plugin_root)
    return data_dir / "plugin_data" / plugin_name / "runtime"


def _resolve_astrbot_data_dir(plugin_root: Path) -> Path:
    """Resolve AstrBot data directory from official helper when available."""
    try:
        from astrbot.core.utils import path_utils  # type: ignore
    except Exception:
        path_utils = None

    if path_utils is not None:
        for getter_name in ("get_astrbot_data_dir", "get_data_dir"):
            getter = getattr(path_utils, getter_name, None)
            if callable(getter):
                try:
                    return Path(str(getter())).resolve()
                except Exception:
                    continue

    # Fallback: infer `.../AstrBot/data` from plugin path `.../AstrBot/data/plugins/<name>`.
    if plugin_root.parent.name == "plugins":
        return plugin_root.parent.parent.resolve()
    return (plugin_root / "data").resolve()

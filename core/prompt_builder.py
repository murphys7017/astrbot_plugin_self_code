"""Prompt construction utilities for Codex development tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_prompt(
    goal: str,
    history: str | list[dict[str, Any]],
    user_message: str,
    project_tree: str,
) -> str:
    """Build the full prompt string with the required AstrBot plugin template."""
    history_text = _normalize_history(history)
    docs_reference = _load_default_docs_reference()
    return (
        "You are an AstrBot plugin developer.\n\n"
        "Goal:\n"
        f"{goal}\n\n"
        "Project structure:\n"
        f"{project_tree}\n\n"
        "Conversation history:\n"
        f"{history_text}\n\n"
        "User request:\n"
        f"{user_message}\n\n"
        "Instructions:\n\n"
        "Modify the plugin project files.\n"
        "Keep code valid Python.\n"
        "Use AstrBot plugin API.\n"
        "Use relative imports inside this plugin package; do not modify sys.path.\n"
        "Always follow AstrBot plugin rules from astrbot_dev_docs.md.\n\n"
        "Reference document (astrbot_dev_docs.md):\n"
        f"{docs_reference}\n"
    )


def _normalize_history(history: str | list[dict[str, Any]]) -> str:
    """Convert history to readable plain text for prompt context."""
    if isinstance(history, str):
        return history.strip() or "(empty)"
    if not history:
        return "(empty)"

    lines: list[str] = []
    for item in history:
        role = str(item.get("role", "unknown"))
        content = str(item.get("content", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines).strip() or "(empty)"


def _load_default_docs_reference() -> str:
    """Load default AstrBot plugin reference text for prompt grounding."""
    docs_path = Path(__file__).resolve().parents[1] / "astrbot_dev_docs.md"
    try:
        content = docs_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "Unavailable: astrbot_dev_docs.md (read failed). Still follow that file as source of truth."
    return content or "Empty document."

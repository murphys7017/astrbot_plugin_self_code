"""Prompt construction utilities for Codex development tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .skills_cache import SkillsCacheManager


def build_prompt(
    goal: str,
    history: str | list[dict[str, Any]],
    user_message: str,
    project_tree: str,
    plugin_root: Path | None = None,
) -> str:
    """Build the full prompt string with the required AstrBot plugin template."""
    history_text = _normalize_history(history)
    docs_reference = _load_default_docs_reference()
    local_skills_reference = _load_local_skills_reference(plugin_root)
    skills_reference = _load_skills_cache_reference(plugin_root)
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
        "Skill-first rule:\n"
        "If the user request describes a reusable interaction pattern or simple conversational behavior,\n"
        "you MUST create a local skill instead of modifying plugin source code.\n"
        "Only modify plugin code when the requested functionality cannot be implemented as a skill.\n"
        "Examples of skill cases:\n"
        '- user says "hello" -> reply "are you ok"\n'
        "- greeting responses\n"
        "- trigger-based replies\n"
        "- reusable interaction patterns\n"
        "Safety restriction for source edits:\n"
        "Do NOT modify `main.py` unless either (a) adding a new command entrypoint is required, or\n"
        "(b) the skill system cannot implement the requested functionality.\n"
        "For log-driven autofix: diagnose plugin owner first, reject cross-plugin writes, "
        "edit only current session workspace, run tests, then apply.\n"
        "Always follow AstrBot plugin rules from astrbot_dev_docs.md.\n\n"
        "Reference document (astrbot_dev_docs.md):\n"
        f"{docs_reference}\n\n"
        "Local registered skills:\n"
        f"{local_skills_reference}\n\n"
        "Additional reference (AstrBot-Skill cache):\n"
        f"{skills_reference}\n"
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


def _load_skills_cache_reference(plugin_root: Path | None) -> str:
    """Load summarized skill cache content for prompt grounding."""
    if plugin_root is None:
        return "Unavailable: plugin_root missing, skip skill cache."
    try:
        manager = SkillsCacheManager(plugin_root)
        return manager.build_prompt_summary(max_skills=20, max_chars=2800)
    except Exception as exc:  # pragma: no cover - defensive runtime path.
        return f"Unavailable: failed to load skill cache summary ({exc})."


def _load_local_skills_reference(plugin_root: Path | None) -> str:
    """Load local registered skill snippets from repository files."""
    if plugin_root is None:
        return "Unavailable: plugin_root missing, skip local skills."

    skills_root = plugin_root / "data" / "local_skills"
    if not skills_root.exists():
        return "No local skills registered."

    skill_files = sorted(skills_root.rglob("SKILL.md"))
    if not skill_files:
        return "No local skills registered."

    lines: list[str] = []
    char_budget = 2400
    used = 0
    for skill_file in skill_files[:12]:
        rel_path = skill_file.relative_to(plugin_root).as_posix()
        try:
            content = skill_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        compact = _compact_text(content, limit=360)
        line = f"- {rel_path}: {compact}"
        if used + len(line) > char_budget:
            break
        lines.append(line)
        used += len(line)

    if not lines:
        return "Local skills exist but failed to load."
    return "\n".join(lines)


def _compact_text(text: str, limit: int) -> str:
    """Compact multiline text to one line with max char limit."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."

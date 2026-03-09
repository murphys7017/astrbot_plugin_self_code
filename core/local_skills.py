"""Local skill suggestion and lifecycle management utilities."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from astrbot.api import logger


class SkillSuggestion(TypedDict):
    """Structured suggestion metadata for creating/updating a local skill."""

    skill_name: str
    description: str
    trigger_examples: list[str]
    scope: str
    benefit: str
    draft_summary: str


class SkillCreateResult(TypedDict):
    """Structured write result for a local skill file."""

    success: bool
    action: str
    skill_name: str
    skill_path: str
    backup_path: str
    message: str


class SkillPlan(TypedDict):
    """Structured plan shown before skill creation confirmation."""

    skill_name: str
    purpose: str
    trigger_examples: list[str]
    workflow: list[str]
    constraints: list[str]
    output_requirements: list[str]


class LocalSkillsManager:
    """Manage local skills under `data/local_skills`."""

    _SAFE_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")

    def __init__(self, plugin_root: Path) -> None:
        self.plugin_root = plugin_root
        self.local_skills_root = self.plugin_root / "data" / "local_skills"
        self.backup_root = self.local_skills_root / "_backup"

    def ensure_structure(self) -> None:
        """Ensure skill directories exist."""
        self.local_skills_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[str]:
        """List skill names that contain a `SKILL.md` file."""
        self.ensure_structure()
        skills: list[str] = []
        for skill_file in sorted(self.local_skills_root.glob("*/SKILL.md")):
            parent_name = skill_file.parent.name
            if parent_name == "_backup":
                continue
            skills.append(parent_name)
        return skills

    def show_skill(self, skill_name: str) -> str:
        """Return skill content for one local skill."""
        safe_name = self.normalize_skill_name(skill_name)
        skill_path = self._skill_file(safe_name)
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill not found: {safe_name}")
        return skill_path.read_text(encoding="utf-8")

    def propose_from_text(self, requirement: str) -> SkillSuggestion:
        """Build one deterministic skill suggestion from user requirement text."""
        text = requirement.strip()
        if not text:
            text = "reusable troubleshooting workflow"
        name = self._infer_skill_name(text)
        return {
            "skill_name": name,
            "description": f"Handle: {text[:120]}",
            "trigger_examples": [
                f"请用 {name} 处理这个问题",
                text[:80],
                f"把这个流程沉淀成 skill：{name}",
            ],
            "scope": "current_session_only",
            "benefit": "减少重复提示词输入，稳定诊断/修复流程",
            "draft_summary": (
                "Purpose + Trigger examples + Workflow + Constraints + Output requirements"
            ),
        }

    def render_suggestion_card(self, suggestion: SkillSuggestion) -> str:
        """Render suggestion as human-readable card."""
        triggers = "\n".join(f"- {item}" for item in suggestion["trigger_examples"])
        return (
            "Skill 创建建议\n"
            f"名称: {suggestion['skill_name']}\n"
            f"描述: {suggestion['description']}\n"
            f"范围: {suggestion['scope']}\n"
            f"收益: {suggestion['benefit']}\n"
            "触发示例:\n"
            f"{triggers}\n"
            f"草稿结构: {suggestion['draft_summary']}\n\n"
            f"确认创建请发送：确认创建 skill {suggestion['skill_name']}"
        )

    def build_plan_from_requirement(
        self,
        skill_name: str,
        requirement: str,
    ) -> SkillPlan:
        """Build a deterministic pre-creation plan card for user confirmation."""
        safe_name = self.normalize_skill_name(skill_name)
        text = requirement.strip() or "reusable workflow"
        return {
            "skill_name": safe_name,
            "purpose": text[:180],
            "trigger_examples": [
                f"请用 {safe_name} 处理这个问题",
                f"按 {safe_name} 流程执行",
                text[:80],
            ],
            "workflow": [
                "收集最小必要上下文",
                "执行固定检查并输出证据",
                "按约束进行最小化操作",
                "给出结果与下一步建议",
            ],
            "constraints": [
                "默认仅在当前会话工作区内操作",
                "避免破坏性命令",
                "出现不确定性时先汇报再执行",
            ],
            "output_requirements": [
                "结构化结论",
                "关键证据",
                "风险与后续建议",
            ],
        }

    def render_plan_card(self, plan: SkillPlan) -> str:
        """Render skill plan for confirmation step."""
        triggers = "\n".join(f"- {item}" for item in plan["trigger_examples"])
        workflow = "\n".join(f"- {item}" for item in plan["workflow"])
        constraints = "\n".join(f"- {item}" for item in plan["constraints"])
        outputs = "\n".join(f"- {item}" for item in plan["output_requirements"])
        skill_name = plan["skill_name"]
        return (
            "Skill 计划（待确认）\n"
            f"名称: {skill_name}\n"
            f"目的: {plan['purpose']}\n"
            "触发示例:\n"
            f"{triggers}\n"
            "工作流:\n"
            f"{workflow}\n"
            "约束:\n"
            f"{constraints}\n"
            "输出要求:\n"
            f"{outputs}\n\n"
            f"确认创建请发送：确认创建 skill {skill_name}"
        )

    def create_or_update_skill(
        self,
        skill_name: str,
        requirement: str,
        draft_content: str = "",
    ) -> SkillCreateResult:
        """Create or update one local skill with backup-on-duplicate behavior."""
        self.ensure_structure()
        safe_name = self.normalize_skill_name(skill_name)
        skill_path = self._skill_file(safe_name)
        backup_path = ""
        action = "created"

        if skill_path.exists():
            backup_path = self._backup_existing_skill(safe_name)
            action = "updated"

        content = self._normalize_or_build_skill_content(
            safe_name=safe_name,
            requirement=requirement,
            draft_content=draft_content,
        )
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")
        logger.info(
            "Local skill %s: name=%s path=%s backup=%s",
            action,
            safe_name,
            skill_path,
            backup_path,
        )
        return {
            "success": True,
            "action": action,
            "skill_name": safe_name,
            "skill_path": str(skill_path),
            "backup_path": backup_path,
            "message": (
                f"Skill {action}: {safe_name}"
                + (f" (backup: {backup_path})" if backup_path else "")
            ),
        }

    def normalize_skill_name(self, raw_name: str) -> str:
        """Normalize user-provided name to lowercase hyphen-case under 64 chars."""
        lowered = raw_name.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
        normalized = re.sub(r"-{2,}", "-", normalized)
        normalized = normalized[:64].strip("-")
        if not normalized:
            normalized = "generated-skill"
        if not self._SAFE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid skill name after normalization.")
        return normalized

    def _skill_file(self, safe_name: str) -> Path:
        return self.local_skills_root / safe_name / "SKILL.md"

    def _backup_existing_skill(self, safe_name: str) -> str:
        """Backup existing skill file into `_backup/<name>/<timestamp>/SKILL.md`."""
        source = self._skill_file(safe_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.backup_root / safe_name / timestamp / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return str(target)

    def _infer_skill_name(self, text: str) -> str:
        lowered = text.lower()
        if "日志" in text or "log" in lowered:
            return "log-diagnosis"
        if "修复" in text or "fix" in lowered:
            return "plugin-autofix"
        if "部署" in text or "apply" in lowered:
            return "deploy-workflow"
        words = [item for item in re.findall(r"[a-z0-9]+", lowered) if len(item) > 2]
        joined = "-".join(words[:4]) or "generated-skill"
        return self.normalize_skill_name(joined)

    def _normalize_or_build_skill_content(
        self,
        safe_name: str,
        requirement: str,
        draft_content: str,
    ) -> str:
        """Normalize externally drafted content, or build fallback template."""
        cleaned_draft = draft_content.strip()
        if cleaned_draft:
            if cleaned_draft.startswith("---"):
                return cleaned_draft + ("\n" if not cleaned_draft.endswith("\n") else "")
            return self._wrap_with_frontmatter(
                safe_name=safe_name,
                description=f"Local skill for: {requirement[:120]}",
                body=cleaned_draft,
            )
        return self._build_template(safe_name=safe_name, requirement=requirement)

    def _build_template(self, safe_name: str, requirement: str) -> str:
        body = (
            f"# {safe_name}\n\n"
            "## Purpose\n"
            f"Handle reusable workflow: {requirement.strip() or 'general repeated task'}.\n\n"
            "## Trigger examples\n"
            f"- 使用 {safe_name} 处理这个问题\n"
            f"- 帮我按 {safe_name} 流程执行\n"
            f"- 把这个需求按 {safe_name} 做完\n\n"
            "## Workflow\n"
            "1. Clarify goal and gather the minimum context.\n"
            "2. Execute deterministic checks first, then targeted changes.\n"
            "3. Report results with concise evidence and next step.\n\n"
            "## Constraints\n"
            "- Operate within current session workspace unless explicitly allowed.\n"
            "- Avoid destructive operations.\n"
            "- Keep changes minimal and testable.\n\n"
            "## Output requirements\n"
            "- Summarize diagnosis and actions.\n"
            "- Include key evidence lines or files touched.\n"
            "- State residual risks if any.\n"
        )
        return self._wrap_with_frontmatter(
            safe_name=safe_name,
            description=f"Local skill for: {requirement[:120]}",
            body=body,
        )

    def _wrap_with_frontmatter(self, safe_name: str, description: str, body: str) -> str:
        normalized_desc = description.strip() or f"Skill: {safe_name}"
        payload = (
            "---\n"
            f"name: {safe_name}\n"
            f'description: "{normalized_desc.replace(chr(34), chr(39))}"\n'
            "---\n\n"
            f"{body.strip()}\n"
        )
        # Validate minimal YAML-like frontmatter format to avoid empty outputs.
        if not payload.startswith("---\nname:"):
            raise ValueError("Failed to build valid skill content.")
        return payload

    def try_parse_json_suggestion(self, raw: str) -> SkillSuggestion | None:
        """Parse suggestion JSON from codex output when available."""
        text = raw.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            name = self.normalize_skill_name(str(data.get("skill_name", "")).strip())
        except Exception:
            return None
        triggers_raw = data.get("trigger_examples", [])
        if not isinstance(triggers_raw, list):
            triggers_raw = []
        triggers = [str(item).strip() for item in triggers_raw if str(item).strip()][:3]
        if not triggers:
            triggers = [f"请用 {name} 处理这个问题"]
        return {
            "skill_name": name,
            "description": str(data.get("description", "")).strip() or f"Handle {name}",
            "trigger_examples": triggers,
            "scope": str(data.get("scope", "current_session_only")).strip()
            or "current_session_only",
            "benefit": str(data.get("benefit", "减少重复操作")).strip() or "减少重复操作",
            "draft_summary": str(
                data.get(
                    "draft_summary",
                    "Purpose + Trigger examples + Workflow + Constraints + Output requirements",
                )
            ).strip(),
        }

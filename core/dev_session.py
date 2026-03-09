"""Session model and manager for `/dev` development workflows."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
import re
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.star import Context

from .codex_runner import run_codex
from .local_skills import LocalSkillsManager, SkillPlan, SkillSuggestion
from .prompt_builder import build_prompt
from .tester import Tester
from .utils import get_runtime_root
from .workspace import WorkspaceManager


@dataclass(slots=True)
class DevSessionRecord:
    """Represent one user-bound development session."""

    session_id: str
    user_id: str
    goal: str
    workspace: str
    history: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def create(cls, user_id: str, goal: str, workspace: str) -> DevSessionRecord:
        """Build a new session object using a generated session id."""
        return cls(
            session_id=str(uuid4()),
            user_id=user_id,
            goal=goal,
            workspace=workspace,
            history=[],
        )

    def to_dict(self) -> dict[str, object]:
        """Convert the session into a JSON-serializable dictionary."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "goal": self.goal,
            "workspace": self.workspace,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DevSessionRecord:
        """Build a `DevSession` from JSON data and normalize field types."""
        raw_history = data.get("history", [])
        normalized_history: list[dict[str, str]] = []
        if isinstance(raw_history, list):
            for item in raw_history:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", ""))
                content = str(item.get("content", ""))
                normalized_history.append({"role": role, "content": content})

        return cls(
            session_id=str(data.get("session_id", "")),
            user_id=str(data.get("user_id", "")),
            goal=str(data.get("goal", "")),
            workspace=str(data.get("workspace", "")),
            history=normalized_history,
        )


class DevSessionManager:
    """Manage user sessions persisted in plugin-data runtime sessions file safely."""

    # Global lock table so managers pointing to the same file share one lock.
    _LOCK_REGISTRY: dict[Path, threading.RLock] = {}
    _LOCK_REGISTRY_GUARD = threading.Lock()

    def __init__(self, plugin_root: Path) -> None:
        """Initialize runtime paths and load existing sessions from disk."""
        self.plugin_root = plugin_root
        self.runtime_dir = get_runtime_root(self.plugin_root)
        self.sessions_file = self.runtime_dir / "sessions.json"
        self.workspaces_dir = self.runtime_dir / "workspaces"
        self._sessions: dict[str, DevSessionRecord] = {}
        self._lock = self._get_file_lock(self.sessions_file)
        self._ensure_runtime_files()
        self.load()

    def create_session(
        self,
        user_id: str,
        goal: str,
        workspace: str | None = None,
    ) -> DevSessionRecord:
        """Create one session for a user, or return the existing one."""
        with self._lock:
            # Reload before mutation so concurrent writes are reflected in memory.
            self.load()
            existing = self._sessions.get(user_id)
            if existing is not None:
                return existing

            workspace_path = self._resolve_workspace_path(workspace, user_id)
            session = DevSessionRecord.create(
                user_id=user_id,
                goal=goal,
                workspace=str(workspace_path),
            )
            self._sessions[user_id] = session
            self.save()
            return session

    def get_session(self, user_id: str) -> DevSessionRecord | None:
        """Get one session by user id, returning `None` if not found."""
        with self._lock:
            self.load()
            return self._sessions.get(user_id)

    def get_workspace_path(self, user_id: str) -> Path | None:
        """Return validated workspace path for a user session, or `None` if invalid."""
        with self._lock:
            self.load()
            session = self._sessions.get(user_id)
            if session is None:
                return None
            workspace_path = Path(session.workspace).resolve()
            if not self._is_workspace_path_safe(workspace_path):
                return None
            return workspace_path

    def add_message(self, user_id: str, role: str, content: str) -> DevSessionRecord:
        """Append one history message to a user's existing session."""
        with self._lock:
            self.load()
            session = self._sessions.get(user_id)
            if session is None:
                raise KeyError(f"Session not found for user_id={user_id}")
            session.history.append({"role": role, "content": content})
            self.save()
            return session

    def delete_session(self, user_id: str) -> bool:
        """Delete a user session and persist result, returning deletion status."""
        with self._lock:
            self.load()
            deleted = self._sessions.pop(user_id, None) is not None
            if deleted:
                self.save()
            return deleted

    def save(self) -> None:
        """Persist all sessions to runtime sessions file with atomic replace."""
        with self._lock:
            self._ensure_runtime_files()
            data = [session.to_dict() for session in self._sessions.values()]
            temp_file = self.sessions_file.with_suffix(".json.tmp")
            try:
                temp_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                # Atomic replacement prevents truncated files on interrupted writes.
                temp_file.replace(self.sessions_file)
            except OSError as exc:
                logger.exception("Failed to save sessions file: %s", exc)

    def load(self) -> dict[str, DevSessionRecord]:
        """Load sessions from disk into memory and return the cache."""
        with self._lock:
            self._ensure_runtime_files()
            try:
                raw = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = []

            loaded: dict[str, DevSessionRecord] = {}
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    session = DevSessionRecord.from_dict(item)
                    workspace_path = Path(session.workspace).resolve()
                    if session.user_id and self._is_workspace_path_safe(workspace_path):
                        loaded[session.user_id] = session

            self._sessions = loaded
            return self._sessions

    def _ensure_runtime_files(self) -> None:
        """Ensure runtime folders and `sessions.json` exist."""
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.workspaces_dir.mkdir(parents=True, exist_ok=True)
            if not self.sessions_file.exists():
                self.sessions_file.write_text("[]\n", encoding="utf-8")
        except OSError as exc:
            logger.exception("Failed to initialize runtime files: %s", exc)

    def _create_workspace(self, user_id: str) -> Path:
        """Create and return one unique workspace directory for a user session."""
        safe_user_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in user_id
        )
        workspace_path = self.workspaces_dir / f"ws_{safe_user_id}_{uuid4().hex[:8]}"
        workspace_path.mkdir(parents=True, exist_ok=False)
        return workspace_path

    def _resolve_workspace_path(self, workspace: str | None, user_id: str) -> Path:
        """Resolve workspace path and enforce it stays under runtime workspaces."""
        workspace_path = (
            Path(workspace).resolve()
            if workspace is not None
            else self._create_workspace(user_id)
        )
        if not self._is_workspace_path_safe(workspace_path):
            raise ValueError("workspace path must be inside runtime workspaces")
        workspace_path.mkdir(parents=True, exist_ok=True)
        return workspace_path

    def _is_workspace_path_safe(self, workspace_path: Path) -> bool:
        """Check whether workspace path is located inside runtime workspaces."""
        try:
            workspace_path.resolve().relative_to(self.workspaces_dir.resolve())
            return True
        except ValueError:
            return False

    @classmethod
    def _get_file_lock(cls, sessions_file: Path) -> threading.RLock:
        """Get one shared re-entrant lock for each sessions file path."""
        normalized = sessions_file.resolve()
        with cls._LOCK_REGISTRY_GUARD:
            lock = cls._LOCK_REGISTRY.get(normalized)
            if lock is None:
                lock = threading.RLock()
                cls._LOCK_REGISTRY[normalized] = lock
            return lock


@dataclass(slots=True)
class DevSession:
    """Single active development session used by V2 session mode."""

    plugin_name: str
    workspace_path: str
    active: bool = True
    history: list[dict[str, str]] = field(default_factory=list)
    pending_skill_suggestion: SkillSuggestion | None = None
    pending_skill_intake: bool = False
    pending_skill_plan: SkillPlan | None = None

    def __post_init__(self) -> None:
        """Normalize workspace path after dataclass initialization."""
        self.workspace_path = str(Path(self.workspace_path).resolve())

    async def handle_message(
        self,
        message: str,
        workspace_manager: WorkspaceManager,
        tester: Tester,
        local_skills_manager: LocalSkillsManager,
        context: Context,
        codex_timeout: int,
        auto_test_before_apply: bool,
        codex_bin: str = "codex",
    ) -> str:
        """Handle natural-language dev-mode message and execute mapped action."""
        logger.info(
            "DevSession message received: plugin=%s message=%s",
            self.plugin_name,
            message,
        )
        safe_workspace, error = self._get_safe_workspace_path(workspace_manager)
        if safe_workspace is None:
            logger.warning(
                "Rejected message due to invalid workspace: plugin=%s error=%s",
                self.plugin_name,
                error,
            )
            return f"Invalid workspace: {error}"

        text = message.strip()
        if not text:
            return "Please provide a message."

        if self._is_files_intent(text):
            logger.debug("Detected files intent: plugin=%s", self.plugin_name)
            files = workspace_manager.list_files(self.plugin_name)
            return (
                "workspace is empty"
                if not files
                else "workspace files:\n" + "\n".join(f"- {f}" for f in files)
            )

        file_path = self._extract_file_path(text)
        if file_path:
            logger.debug(
                "Detected read-file intent: plugin=%s path=%s",
                self.plugin_name,
                file_path,
            )
            try:
                content = workspace_manager.read_file(self.plugin_name, file_path)
            except Exception as exc:
                return f"Read file failed: {exc}"
            return f"`{file_path}`:\n{content}"

        if self._is_test_intent(text):
            logger.info("Detected test intent: plugin=%s", self.plugin_name)
            result = tester.run(self.plugin_name)
            if result["success"]:
                return "Basic test passed."
            return f"Basic test failed: {result['error_message']}"

        if self._is_apply_intent(text):
            logger.info("Detected apply intent: plugin=%s", self.plugin_name)
            return await self._apply(
                context=context,
                tester=tester,
                workspace_manager=workspace_manager,
                auto_test=auto_test_before_apply,
            )
        if self._is_log_inspection_intent(text):
            logger.info("Detected log inspection intent: plugin=%s", self.plugin_name)
            return await self._run_log_inspection_pipeline(
                message=text,
                workspace_manager=workspace_manager,
                codex_timeout=codex_timeout,
                codex_bin=codex_bin,
            )
        if self._is_autofix_intent(text):
            logger.info("Detected autofix intent: plugin=%s", self.plugin_name)
            return await self._run_autofix_pipeline(
                message=text,
                workspace_manager=workspace_manager,
                tester=tester,
                context=context,
                codex_timeout=codex_timeout,
                codex_bin=codex_bin,
            )
        if self._is_skill_create_intent(text):
            return self._start_skill_intake()
        if self.pending_skill_intake:
            return self._build_skill_plan_from_intake(
                text=text,
                local_skills_manager=local_skills_manager,
            )
        if self._is_skill_confirm_intent(text):
            return await self._handle_skill_confirm(
                text=text,
                workspace_manager=workspace_manager,
                local_skills_manager=local_skills_manager,
                context=context,
                codex_timeout=codex_timeout,
                codex_bin=codex_bin,
            )
        skill_first_reply = self._maybe_start_skill_first_flow(
            text=text,
            local_skills_manager=local_skills_manager,
        )
        if skill_first_reply is not None:
            return skill_first_reply

        project_tree = (
            "\n".join(workspace_manager.list_files(self.plugin_name)) or "(empty)"
        )
        prompt = build_prompt(
            goal=f"Develop plugin `{self.plugin_name}`",
            history=self.history,
            user_message=text,
            project_tree=project_tree,
            plugin_root=workspace_manager.plugin_root,
        )
        self.history.append({"role": "user", "content": text})
        logger.info(
            "Running codex in workspace: plugin=%s timeout=%s",
            self.plugin_name,
            codex_timeout,
        )
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=safe_workspace,
            timeout=codex_timeout,
            codex_bin=codex_bin,
        )
        if result["exit_code"] == 0:
            reply = result["stdout"].strip() or "Codex execution completed."
            self.history.append({"role": "assistant", "content": reply})
            logger.info("Codex execution succeeded: plugin=%s", self.plugin_name)
            suggestion = self._detect_skill_opportunity(text)
            if suggestion is not None:
                self.pending_skill_suggestion = suggestion
                return (
                    f"{reply}\n\n"
                    f"{local_skills_manager.render_suggestion_card(suggestion)}"
                )
            return reply

        error = (
            result["stderr"].strip()
            or f"Codex failed with exit code {result['exit_code']}"
        )
        self.history.append({"role": "assistant", "content": f"ERROR: {error}"})
        logger.warning(
            "Codex execution failed: plugin=%s exit_code=%s error=%s",
            self.plugin_name,
            result["exit_code"],
            error,
        )
        return error

    async def _apply(
        self,
        context: Context,
        tester: Tester,
        workspace_manager: WorkspaceManager,
        auto_test: bool,
    ) -> str:
        """Copy workspace plugin to AstrBot plugin directory and reload."""
        safe_workspace, error = self._get_safe_workspace_path(workspace_manager)
        if safe_workspace is None:
            logger.warning(
                "Apply blocked due to invalid workspace: plugin=%s error=%s",
                self.plugin_name,
                error,
            )
            return f"Apply failed: invalid workspace ({error})"

        if auto_test:
            logger.info("Running auto test before apply: plugin=%s", self.plugin_name)
            test_result = tester.run(self.plugin_name)
            if not test_result["success"]:
                logger.warning(
                    "Auto test failed before apply: plugin=%s error=%s",
                    self.plugin_name,
                    test_result["error_message"],
                )
                return f"Basic test failed: {test_result['error_message']}"

        # Use current plugin directory parent as canonical AstrBot plugins path.
        # This is robust even when process cwd is not AstrBot project root.
        plugins_dir = workspace_manager.plugin_root.parent.resolve()
        target_path = plugins_dir / self.plugin_name
        logger.info("Applying plugin to directory: %s", target_path)
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                import shutil

                shutil.rmtree(target_path)
            import shutil

            shutil.copytree(safe_workspace, target_path)
            logger.info(
                "Applied workspace to plugins: plugin=%s source=%s target=%s",
                self.plugin_name,
                safe_workspace,
                target_path,
            )
        except OSError as exc:
            logger.exception("Failed to apply workspace: %s", exc)
            return f"Apply failed: {exc}"

        manager = getattr(context, "_star_manager", None)
        if manager is None or not hasattr(manager, "reload"):
            return f"Applied to plugins: {self.plugin_name}. Reload skipped."
        try:
            reload_result = manager.reload(self.plugin_name)
            if hasattr(reload_result, "__await__"):
                await reload_result
            return f"Applied and reloaded plugin: {self.plugin_name}"
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Reload failed for %s: %s", self.plugin_name, exc)
            return f"Applied to plugins, but reload failed: {exc}"

    def _is_files_intent(self, text: str) -> bool:
        """Return whether user asks for file listing."""
        lowered = text.lower()
        return any(
            keyword in lowered
            for keyword in ("files", "list files", "有哪些文件", "文件列表")
        )

    def _is_test_intent(self, text: str) -> bool:
        """Return whether user asks to run tests."""
        lowered = text.lower().strip()
        exact_tokens = {
            "test",
            "run test",
            "测试",
            "运行测试",
            "执行测试",
            "跑测试",
            "测一下",
        }
        if lowered in exact_tokens:
            return True
        return lowered.startswith("test ") or lowered.startswith("run test ")

    def _is_apply_intent(self, text: str) -> bool:
        """Return whether user asks to deploy/apply plugin."""
        lowered = text.lower()
        return any(
            keyword in lowered for keyword in ("apply", "deploy", "安装插件", "部署")
        )

    def _is_autofix_intent(self, text: str) -> bool:
        """Return whether user asks for log-based plugin diagnosis and autofix."""
        lowered = text.lower()
        keywords = (
            "自动修复",
            "自己修复",
            "根据日志修复",
            "看哪个插件",
            "检查哪个插件",
            "autofix",
            "fix from logs",
        )
        return any(keyword in lowered for keyword in keywords)

    def _is_log_inspection_intent(self, text: str) -> bool:
        """Return whether user requests log inspection without direct fixing."""
        lowered = text.lower()
        keywords = (
            "检查日志",
            "检查一下日志",
            "看日志",
            "看看日志",
            "查看日志",
            "分析日志",
            "看报错",
            "有没有报错",
            "报错日志",
            "错误日志",
            "log check",
            "check logs",
        )
        if any(keyword in lowered for keyword in keywords):
            # Explicit repair wording should go to autofix path instead.
            if self._is_autofix_intent(text):
                return False
            if "修复" in text or "fix" in lowered:
                return False
            return True
        return False

    def _extract_file_path(self, text: str) -> str | None:
        """Extract probable file path from natural-language request."""
        stripped = text.strip()
        if stripped.lower().startswith("cat "):
            return stripped[4:].strip()
        prefixes = ("看看 ", "查看 ", "读取 ", "cat ")
        for prefix in prefixes:
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return None

    def _is_skill_confirm_intent(self, text: str) -> bool:
        """Return whether user confirms creating a suggested skill."""
        normalized = text.strip().lower()
        return normalized.startswith("确认创建 skill ") or normalized.startswith(
            "confirm create skill "
        )

    def _is_skill_create_intent(self, text: str) -> bool:
        """Return whether user asks to create a new skill without details."""
        normalized = text.strip().lower()
        intents = (
            "添加一个新的skill",
            "新增skill",
            "创建skill",
            "create a new skill",
            "add a new skill",
        )
        return any(item in normalized for item in intents)

    def _start_skill_intake(self) -> str:
        """Enter skill-intake state and ask for missing functional details."""
        self.pending_skill_intake = True
        self.pending_skill_plan = None
        self.pending_skill_suggestion = None
        return (
            "你要新增 skill，但我还缺少功能描述。\n"
            "请告诉我这个 skill 具体做什么（输入、步骤、期望输出）。\n"
            "我会先给出计划，等你确认后再创建并自动重载。\n"
            "你也可以用命令：`/codexdev skills suggest <需求>` 或 "
            "`/codexdev skills create <skill_name> <需求>`。"
        )

    def _build_skill_plan_from_intake(
        self,
        text: str,
        local_skills_manager: LocalSkillsManager,
    ) -> str:
        """Create pending skill plan from user-provided feature description."""
        detail = text.strip()
        if len(detail) < 4:
            return "功能描述太短，请再具体一些（例如：处理对象、关键步骤、输出格式）。"
        suggested_name = self._suggest_skill_name_from_text(detail)
        plan = local_skills_manager.build_plan_from_requirement(
            skill_name=suggested_name,
            requirement=detail,
        )
        self.pending_skill_intake = False
        self.pending_skill_plan = plan
        return (
            local_skills_manager.render_plan_card(plan)
            + "\n\n提示：日志检查默认只分析不修复。"
        )

    async def _handle_skill_confirm(
        self,
        text: str,
        workspace_manager: WorkspaceManager,
        local_skills_manager: LocalSkillsManager,
        context: Context,
        codex_timeout: int,
        codex_bin: str,
    ) -> str:
        """Create/update local skill after explicit user confirmation."""
        plan_name = self.pending_skill_plan["skill_name"] if self.pending_skill_plan else ""
        suggestion_name = (
            self.pending_skill_suggestion["skill_name"]
            if self.pending_skill_suggestion
            else ""
        )
        active_name = plan_name or suggestion_name
        if not active_name:
            return "当前没有待确认的 skill 建议。"
        requested_name = self._extract_confirm_skill_name(text)
        if requested_name and requested_name != active_name:
            return (
                "待确认 skill 名称不匹配。\n"
                f"建议名称: {active_name}\n"
                f"确认示例: 确认创建 skill {active_name}"
            )

        if self.pending_skill_plan is not None:
            skill_name = self.pending_skill_plan["skill_name"]
            requirement = self.pending_skill_plan["purpose"]
        elif self.pending_skill_suggestion is not None:
            skill_name = self.pending_skill_suggestion["skill_name"]
            requirement = self.pending_skill_suggestion["description"]
        else:
            return "当前没有待确认的 skill 建议。"
        draft = await self._draft_skill_content_with_codex(
            skill_name=skill_name,
            requirement=requirement,
            workspace_manager=workspace_manager,
            codex_timeout=codex_timeout,
            codex_bin=codex_bin,
        )
        result = local_skills_manager.create_or_update_skill(
            skill_name=skill_name,
            requirement=requirement,
            draft_content=draft,
        )
        self.pending_skill_suggestion = None
        self.pending_skill_plan = None
        self.pending_skill_intake = False
        if not result["success"]:
            return f"Skill 创建失败：{result['message']}"
        reload_text = await self._reload_self_plugin(context)
        return (
            f"{result['message']}\n"
            f"路径: {result['skill_path']}\n"
            f"{reload_text}"
        )

    def _extract_confirm_skill_name(self, text: str) -> str:
        normalized = text.strip().lower()
        if normalized.startswith("确认创建 skill "):
            return normalized[len("确认创建 skill ") :].strip()
        if normalized.startswith("confirm create skill "):
            return normalized[len("confirm create skill ") :].strip()
        return ""

    def _detect_skill_opportunity(self, text: str) -> SkillSuggestion | None:
        """Heuristic detector for repeatable workflow that should become a skill."""
        normalized = text.lower()
        keywords = (
            "每次",
            "经常",
            "重复",
            "固定流程",
            "日志排查",
            "自动修复",
            "模板化",
            "workflow",
            "repeat",
            "playbook",
        )
        if not any(keyword in normalized for keyword in keywords):
            return None
        # Suggest one concrete skill derived from current message.
        skill_name = self._suggest_skill_name_from_text(text)
        return {
            "skill_name": skill_name,
            "description": text.strip()[:140] or "reusable workflow",
            "trigger_examples": [
                text.strip()[:80] or f"使用 {skill_name} 处理该任务",
                f"请用 {skill_name} 流程执行",
                f"把这类问题沉淀成 {skill_name}",
            ],
            "scope": "current_session_only",
            "benefit": "减少重复提示词，固化高频任务步骤",
            "draft_summary": "Purpose + Trigger examples + Workflow + Constraints + Output requirements",
        }

    def _maybe_start_skill_first_flow(
        self,
        text: str,
        local_skills_manager: LocalSkillsManager,
    ) -> str | None:
        """Start skill creation plan automatically for obvious skill-like requests."""
        if self.pending_skill_intake or self.pending_skill_plan is not None:
            return None
        if not self._is_obvious_skill_case(text):
            return None
        if self._requires_plugin_source_change(text):
            return None

        requirement = text.strip()
        if len(requirement) < 4:
            return None
        skill_name = self._suggest_skill_name_from_text(requirement)
        plan = local_skills_manager.build_plan_from_requirement(
            skill_name=skill_name,
            requirement=requirement,
        )
        self.pending_skill_intake = False
        self.pending_skill_plan = plan
        self.pending_skill_suggestion = None
        return (
            "检测到该需求属于可复用对话规则，按 skill-first 策略优先创建 local skill。\n\n"
            f"{local_skills_manager.render_plan_card(plan)}\n\n"
            "提示：优先使用 `/codexdev skills suggest <需求>` 或 "
            "`/codexdev skills create <skill_name> <需求>`。"
        )

    def _is_obvious_skill_case(self, text: str) -> bool:
        """Heuristic detector for requests that should be implemented as skills."""
        lowered = text.lower()
        patterns = (
            r"(用户|user).{0,16}(说|says?|输入|send).{0,24}(回复|reply|返回|respond)",
            r"(触发词|关键词|trigger).{0,20}(回复|reply|返回|respond)",
            r"(固定回复|固定回答|simple conversation|简单对话|greeting|问候)",
            r"(可复用|reusable).{0,20}(行为|流程|pattern|interaction)",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _requires_plugin_source_change(self, text: str) -> bool:
        """Return whether request likely requires direct plugin code changes."""
        lowered = text.lower()
        keywords = (
            "新增命令",
            "新命令",
            "slash command",
            "api",
            "http",
            "webhook",
            "数据库",
            "database",
            "定时任务",
            "scheduler",
            "中间件",
            "事件钩子",
            "main.py",
            "插件源码",
        )
        return any(keyword in lowered for keyword in keywords)

    def _suggest_skill_name_from_text(self, text: str) -> str:
        lowered = text.lower()
        if "日志" in text or "log" in lowered:
            return "log-diagnosis"
        if "修复" in text or "fix" in lowered:
            return "plugin-autofix"
        tokens = [item for item in re.findall(r"[a-z0-9]+", lowered) if len(item) > 2]
        name = "-".join(tokens[:4]) or "generated-skill"
        name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
        name = re.sub(r"-{2,}", "-", name)[:64].strip("-")
        return name or "generated-skill"

    async def _draft_skill_content_with_codex(
        self,
        skill_name: str,
        requirement: str,
        workspace_manager: WorkspaceManager,
        codex_timeout: int,
        codex_bin: str,
    ) -> str:
        """Best-effort draft generation for SKILL.md body via Codex CLI."""
        safe_workspace, error = self._get_safe_workspace_path(workspace_manager)
        if safe_workspace is None:
            logger.warning("Skip codex draft due to invalid workspace: %s", error)
            return ""
        prompt = (
            "Write a concise SKILL.md content with YAML frontmatter.\n"
            f"name: {skill_name}\n"
            f"description: Local skill for {requirement}\n"
            "Must include sections: Purpose, Trigger examples, Workflow, Constraints, Output requirements.\n"
            "Output markdown only."
        )
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=safe_workspace,
            timeout=min(180, codex_timeout),
            codex_bin=codex_bin,
        )
        if result["exit_code"] != 0:
            return ""
        return result["stdout"].strip()

    async def _run_autofix_pipeline(
        self,
        message: str,
        workspace_manager: WorkspaceManager,
        tester: Tester,
        context: Context,
        codex_timeout: int,
        codex_bin: str,
    ) -> str:
        """Run codex-driven autofix, then test and apply on success."""
        safe_workspace, error = self._get_safe_workspace_path(workspace_manager)
        if safe_workspace is None:
            return f"Autofix failed: invalid workspace ({error})"

        project_tree = "\n".join(workspace_manager.list_files(self.plugin_name)) or "(empty)"
        decision_protocol = (
            "Autofix protocol:\n"
            "1) Read logs from `data/logs/astrbot.log` and `data/logs/astrbot.trace.log` first.\n"
            "2) Identify likely plugin/module owner of the error.\n"
            f"3) Current session plugin is `{self.plugin_name}`.\n"
            "4) If owner is not current session plugin, DO NOT modify files. "
            "Return first line exactly: `AUTOFIX_DECISION: mismatch`.\n"
            "5) If logs are insufficient, DO NOT modify files. "
            "Return first line exactly: `AUTOFIX_DECISION: insufficient`.\n"
            "6) If owner matches current session plugin, perform minimal fix in workspace and "
            "return first line exactly: `AUTOFIX_DECISION: proceed`.\n"
            "7) Always include a short evidence summary.\n"
        )
        prompt = build_prompt(
            goal=f"Diagnose and autofix plugin `{self.plugin_name}` from logs",
            history=self.history,
            user_message=f"{message}\n\n{decision_protocol}",
            project_tree=project_tree,
            plugin_root=workspace_manager.plugin_root,
        )
        self.history.append({"role": "user", "content": f"[autofix] {message}"})
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=safe_workspace,
            timeout=codex_timeout,
            codex_bin=codex_bin,
        )
        if result["exit_code"] != 0:
            error_text = (
                result["stderr"].strip()
                or f"Codex failed with exit code {result['exit_code']}"
            )
            self.history.append({"role": "assistant", "content": f"ERROR: {error_text}"})
            return f"Autofix stage failed: {error_text}"

        reply = result["stdout"].strip() or "Codex execution completed."
        self.history.append({"role": "assistant", "content": reply})
        decision = self._extract_autofix_decision(reply)
        if decision == "mismatch":
            return (
                "日志归因结果：问题不属于当前会话插件，已拒绝跨插件写入。\n\n"
                f"{reply}"
            )
        if decision == "insufficient":
            return f"日志线索不足，未执行修改。\n\n{reply}"

        test_result = tester.run(self.plugin_name)
        if not test_result["success"]:
            return (
                "Autofix completed, but test failed. Apply aborted.\n"
                f"Test error: {test_result['error_message']}\n\n"
                f"{reply}"
            )
        apply_result = await self._apply(
            context=context,
            tester=tester,
            workspace_manager=workspace_manager,
            auto_test=False,
        )
        if "Applied" not in apply_result and "reloaded" not in apply_result:
            return f"Autofix test passed, but apply failed: {apply_result}\n\n{reply}"
        return f"Autofix completed, test passed, and apply succeeded.\n\n{apply_result}\n\n{reply}"

    async def _run_log_inspection_pipeline(
        self,
        message: str,
        workspace_manager: WorkspaceManager,
        codex_timeout: int,
        codex_bin: str,
    ) -> str:
        """Run log-only diagnosis workflow without code modification."""
        safe_workspace, error = self._get_safe_workspace_path(workspace_manager)
        if safe_workspace is None:
            return f"Log inspection failed: invalid workspace ({error})"
        project_tree = "\n".join(workspace_manager.list_files(self.plugin_name)) or "(empty)"
        protocol = (
            "Log inspection protocol (read-only):\n"
            "1) Inspect `data/logs/astrbot.log` and `data/logs/astrbot.trace.log`.\n"
            "2) Report findings: diagnosis, evidence, risks, and suggestions.\n"
            "3) Do NOT modify code or files.\n"
            "4) End with: `NEXT_ACTION: ask_user_decision`.\n"
        )
        prompt = build_prompt(
            goal=f"Inspect logs for plugin `{self.plugin_name}` without code changes",
            history=self.history,
            user_message=f"{message}\n\n{protocol}",
            project_tree=project_tree,
            plugin_root=workspace_manager.plugin_root,
        )
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=safe_workspace,
            timeout=min(180, codex_timeout),
            codex_bin=codex_bin,
        )
        if result["exit_code"] != 0:
            error_text = (
                result["stderr"].strip()
                or f"Codex failed with exit code {result['exit_code']}"
            )
            return f"日志检查失败：{error_text}"
        reply = result["stdout"].strip() or "日志检查完成，但没有返回内容。"
        self.history.append({"role": "assistant", "content": reply})
        return (
            f"{reply}\n\n"
            "请你决定下一步：修复 / 增强 / 继续观察。"
        )

    async def _reload_self_plugin(self, context: Context) -> str:
        """Reload this plugin after local skill creation."""
        manager = getattr(context, "_star_manager", None)
        if manager is None or not hasattr(manager, "reload"):
            return "Skill 已写入并生效；插件重载跳过（无可用 reload 接口）。"
        try:
            reload_result = manager.reload("astrbot_plugin_self_code")
            if hasattr(reload_result, "__await__"):
                await reload_result
            return "Skill 已写入并生效，已自动重载 `astrbot_plugin_self_code`。"
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Self plugin reload failed: %s", exc)
            return f"Skill 已写入并生效，但自动重载失败：{exc}"

    def _extract_autofix_decision(self, reply: str) -> str:
        """Parse autofix decision marker from codex output."""
        for line in reply.splitlines():
            normalized = line.strip().lower()
            if normalized == "autofix_decision: mismatch":
                return "mismatch"
            if normalized == "autofix_decision: insufficient":
                return "insufficient"
            if normalized == "autofix_decision: proceed":
                return "proceed"
        # If marker missing, use safe fallback.
        return "insufficient"

    def _get_safe_workspace_path(
        self,
        workspace_manager: WorkspaceManager,
    ) -> tuple[Path | None, str]:
        """Validate workspace path is exactly the managed plugin workspace path."""
        try:
            expected_workspace = workspace_manager.get_workspace(
                self.plugin_name
            ).resolve()
            actual_workspace = Path(self.workspace_path).resolve()
        except Exception as exc:
            return None, str(exc)
        if actual_workspace != expected_workspace:
            return None, "workspace path mismatch"
        if not actual_workspace.exists() or not actual_workspace.is_dir():
            return None, "workspace path does not exist"
        return actual_workspace, ""

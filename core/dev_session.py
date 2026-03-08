"""Session model and manager for `/dev` development workflows."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.star import Context

from .codex_runner import run_codex
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

    def __post_init__(self) -> None:
        """Normalize workspace path after dataclass initialization."""
        self.workspace_path = str(Path(self.workspace_path).resolve())

    async def handle_message(
        self,
        message: str,
        workspace_manager: WorkspaceManager,
        tester: Tester,
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

        project_tree = (
            "\n".join(workspace_manager.list_files(self.plugin_name)) or "(empty)"
        )
        prompt = build_prompt(
            goal=f"Develop plugin `{self.plugin_name}`",
            history=self.history,
            user_message=text,
            project_tree=project_tree,
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
        lowered = text.lower()
        return any(
            keyword in lowered for keyword in ("test", "run test", "运行测试", "测试")
        )

    def _is_apply_intent(self, text: str) -> bool:
        """Return whether user asks to deploy/apply plugin."""
        lowered = text.lower()
        return any(
            keyword in lowered for keyword in ("apply", "deploy", "安装插件", "部署")
        )

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

"""Main entrypoint for the astrbot_plugin_self_code plugin."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.util import SessionController, session_waiter

from .core.codex_runner import run_codex
from .core.dev_session import DevSession
from .core.llm_tools import build_selfcode_tools
from .core.local_skills import LocalSkillsManager
from .core.skills_cache import SkillsCacheManager
from .core.tester import Tester
from .core.utils import validate_plugin_name
from .core.workspace import WorkspaceManager

COMMAND_NAME = "codexdev"
COMMAND_PREFIX = f"/{COMMAND_NAME}"


@register(
    "astrbot_plugin_self_code",
    "YakumoAki",
    "AI coding assistant plugin for AstrBot.",
    "0.3.0",
)
class SelfCodePlugin(Star):
    """Provide V1 commands and V2 Dev Session Mode for coding workflows."""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        """Initialize plugin managers and runtime state."""
        super().__init__(context)
        self.config = config or {}
        self.plugin_root = Path(__file__).resolve().parent
        self.workspace_manager = WorkspaceManager(self.plugin_root)
        self.tester = Tester(self.plugin_root, context=self.context)
        self.skills_cache = SkillsCacheManager(self.plugin_root)
        self.local_skills = LocalSkillsManager(self.plugin_root)
        self.dev_session: DevSession | None = None
        self.last_plugin_name: str | None = None
        self.pending_stop_confirm_until: float | None = None
        self._register_llm_tools_dataclass()

    def _register_llm_tools_dataclass(self) -> None:
        """Register FunctionTool-based LLM tools via AstrBot unified API."""
        tools = build_selfcode_tools(self)
        self.context.add_llm_tools(*tools)
        logger.info("Registered %s selfcode FunctionTools.", len(tools))

    async def initialize(self) -> None:
        """Prepare runtime directories during plugin startup."""
        self.workspace_manager.ensure_runtime_structure()
        self.skills_cache.ensure_structure()
        self.local_skills.ensure_structure()
        logger.info("astrbot_plugin_self_code runtime directories are ready.")

    async def terminate(self) -> None:
        """Best-effort cleanup when plugin unloads."""
        if self.dev_session:
            self.dev_session.active = False
        self.dev_session = None
        self.pending_stop_confirm_until = None
        logger.info("astrbot_plugin_self_code terminated and in-memory state cleared.")

    @filter.command(COMMAND_NAME)
    async def dev(self, event: AstrMessageEvent):
        """Handle `/codexdev` commands and dispatch into V2 session mode."""
        user_id = event.get_sender_id()
        allowed_user_id = self._cfg_str("single_user_id", "")
        logger.debug(
            "Received %s message: user_id=%s text=%s",
            COMMAND_PREFIX,
            user_id,
            event.message_str,
        )
        if allowed_user_id and user_id != allowed_user_id:
            logger.warning(
                "Rejected %s command: user_id=%s is not allowed (allowed_user_id=%s)",
                COMMAND_PREFIX,
                user_id,
                allowed_user_id,
            )
            yield event.plain_result(
                self._append_command_hint("你没有权限使用该开发插件。")
            )
            return

        command, argument = self._parse_dev_command(event.message_str.strip())
        logger.info(
            "Parsed %s command: user_id=%s command=%s argument=%s",
            COMMAND_PREFIX,
            user_id,
            command,
            argument,
        )
        if not command:
            yield event.plain_result(
                self._append_command_hint(
                    f"用法：{COMMAND_PREFIX} start <plugin_name> | {COMMAND_PREFIX} resume <plugin_name> | "
                    f"{COMMAND_PREFIX} ask <需求> | {COMMAND_PREFIX} files | {COMMAND_PREFIX} cat <文件> | "
                    f"{COMMAND_PREFIX} test | {COMMAND_PREFIX} apply | {COMMAND_PREFIX} stop | {COMMAND_PREFIX} status | "
                    f"{COMMAND_PREFIX} skills <update|status|suggest|create|list|show>"
                )
            )
            return

        if command == "start":
            if not argument:
                yield event.plain_result(
                    self._append_command_hint(
                        f"用法：{COMMAND_PREFIX} start <plugin_name>"
                    )
                )
                return
            try:
                start_message = self._start_session(argument)
                logger.info("Dev mode started: user_id=%s plugin=%s", user_id, argument)
                yield event.plain_result(
                    self._append_command_hint(start_message, in_dev_mode=True)
                )
                exit_message = await self._run_dev_mode(event)
                if exit_message:
                    logger.info(
                        "Dev mode exited: user_id=%s reason=%s", user_id, exit_message
                    )
                    yield event.plain_result(self._append_command_hint(exit_message))
            except Exception as exc:  # pragma: no cover - runtime defensive path.
                logger.exception("Failed to start dev mode: %s", exc)
                yield event.plain_result(
                    self._append_command_hint(f"Failed to start dev mode: {exc}")
                )
            return
        if command == "resume":
            if not argument:
                if self.last_plugin_name:
                    argument = self.last_plugin_name
                else:
                    yield event.plain_result(
                        self._append_command_hint(
                            f"用法：{COMMAND_PREFIX} resume <plugin_name>"
                        )
                    )
                    return
            try:
                start_message = self._start_session(argument, resume=True)
                yield event.plain_result(
                    self._append_command_hint(start_message, in_dev_mode=True)
                )
                exit_message = await self._run_dev_mode(event)
                if exit_message:
                    yield event.plain_result(self._append_command_hint(exit_message))
            except Exception as exc:  # pragma: no cover - runtime defensive path.
                logger.exception("Failed to resume dev mode: %s", exc)
                yield event.plain_result(
                    self._append_command_hint(f"恢复开发模式失败：{exc}")
                )
            return

        try:
            response = await self._execute_v1_command(
                command=command, argument=argument
            )
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Failed to execute %s command: %s", COMMAND_PREFIX, exc)
            response = f"命令执行失败：{exc}"
        yield event.plain_result(
            self._append_command_hint(
                response, in_dev_mode=bool(self.dev_session and self.dev_session.active)
            )
        )

    async def _run_dev_mode(self, event: AstrMessageEvent) -> str | None:
        """Enter session-control waiting mode and handle natural language messages."""
        timeout_seconds = self._cfg_int(
            "dev_mode_timeout", 1800, minimum=30, maximum=86400
        )
        logger.info("Entering session_waiter dev mode: timeout=%s", timeout_seconds)

        @session_waiter(timeout_seconds)
        async def dev_mode_waiter(
            controller: SessionController, incoming: AstrMessageEvent
        ) -> None:
            if not self.dev_session or not self.dev_session.active:
                logger.info("Dev session inactive, stopping session_waiter controller")
                controller.stop()
                return
            response = await self._handle_session_message(
                incoming.message_str.strip(),
                controller=controller,
            )
            if response:
                incoming.set_result(
                    incoming.plain_result(
                        self._append_command_hint(response, in_dev_mode=True)
                    )
                )
            # Keep session mode alive across multiple natural-language turns.
            if self.dev_session and self.dev_session.active:
                controller.keep(timeout=timeout_seconds)

        try:
            await dev_mode_waiter(event)
        except TimeoutError:
            if self.dev_session:
                self.dev_session.active = False
            logger.info("Dev mode timeout reached")
            self._terminate_dev_session()
            timeout_minutes = max(1, round(timeout_seconds / 60))
            return (
                f"开发模式等待超时（约{timeout_minutes}分钟），会话已退出。"
                f"可使用 {COMMAND_PREFIX} resume <plugin_name> 继续。"
            )
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Dev mode waiter failed: %s", exc)
            # Keep session active on runtime errors; only timeout/stop-confirm can exit.
            if self.dev_session and self.dev_session.active:
                return (
                    f"开发模式处理异常：{exc}\n"
                    f"会话仍保持激活。可继续发送消息，或执行 "
                    f"`{COMMAND_PREFIX} stop confirm` 退出。"
                )
            return f"开发模式处理异常：{exc}"
        return None

    async def _handle_session_message(
        self,
        raw_message: str,
        controller: SessionController,
    ) -> str:
        """Handle messages while session control is active."""
        if not self.dev_session or not self.dev_session.active:
            controller.stop()
            return "当前没有进行中的开发会话。"

        explicit_command, command, argument = self._extract_explicit_command(
            raw_message
        )
        if explicit_command:
            logger.info(
                "Session mode %s command: command=%s argument=%s",
                COMMAND_PREFIX,
                command,
                argument,
            )
            if command == "stop":
                if self._should_confirm_stop(argument):
                    self._terminate_dev_session()
                    controller.stop()
                    logger.info("Dev mode stopped by command")
                    return "已退出开发模式。"
                return (
                    "请确认退出开发模式：\n"
                    f"发送 `{COMMAND_PREFIX} stop confirm` 确认退出。"
                )
            if command == "abort":
                return f"请使用 `{COMMAND_PREFIX} stop confirm` 退出开发模式。"
            if command == "status":
                return self._status_text()
            try:
                reply = await self._execute_v1_command(
                    command=command, argument=argument
                )
                self._keep_dev_session_active()
                return reply
            except Exception as exc:  # pragma: no cover - runtime defensive path.
                logger.exception("Session command failed: %s", exc)
                self._keep_dev_session_active()
                return f"会话命令执行失败：{exc}"

        if self._is_natural_stop_intent(raw_message):
            self.pending_stop_confirm_until = time.time() + 60
            return (
                "检测到你想退出开发模式。\n"
                f"请发送 `{COMMAND_PREFIX} stop confirm` 确认退出。"
            )

        try:
            reply = await self.dev_session.handle_message(
                message=raw_message,
                workspace_manager=self.workspace_manager,
                tester=self.tester,
                local_skills_manager=self.local_skills,
                context=self.context,
                codex_timeout=self._cfg_int(
                    "codex_timeout", 300, minimum=10, maximum=3600
                ),
                auto_test_before_apply=self._cfg_bool("auto_test_before_apply", True),
            )
            self._keep_dev_session_active()
            return reply
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Dev session message handling failed: %s", exc)
            self._keep_dev_session_active()
            return f"开发会话处理失败：{exc}"

    async def _execute_v1_command(self, command: str, argument: str) -> str:
        """Execute V1 command set and compatibility aliases."""
        # Compatibility aliases for old command names.
        if command == "inspect":
            command = "files"
        if command == "abort":
            return f"请使用 `{COMMAND_PREFIX} stop confirm` 退出开发模式。"

        if command == "status":
            return self._status_text()
        if command == "skills" or command == "skill":
            sub_tokens = argument.split() if argument else []
            subcommand = sub_tokens[0].strip().lower() if sub_tokens else ""
            if subcommand == "update":
                result = await asyncio.to_thread(self.skills_cache.update_from_remote)
                status_text = self.skills_cache.render_status_text()
                if result.get("success"):
                    return f"{result.get('message', 'Skills cache updated.')}\n\n{status_text}"
                return f"{result.get('message', 'Skills cache update failed.')}\n\n{status_text}"
            if subcommand == "status":
                return self.skills_cache.render_status_text()
            if subcommand == "list":
                names = self.local_skills.list_skills()
                if not names:
                    return "暂无本地 skills。"
                return "本地 skills:\n" + "\n".join(f"- {item}" for item in names)
            if subcommand == "show":
                if len(sub_tokens) < 2:
                    return f"用法：{COMMAND_PREFIX} skills show <skill_name>"
                skill_name = sub_tokens[1].strip()
                try:
                    content = self.local_skills.show_skill(skill_name)
                except Exception as exc:
                    return f"读取 skill 失败：{exc}"
                preview = "\n".join(content.splitlines()[:80])
                return f"`{skill_name}`:\n{preview}"
            if subcommand == "suggest":
                requirement = " ".join(sub_tokens[1:]).strip()
                if not requirement:
                    return f"用法：{COMMAND_PREFIX} skills suggest <需求>"
                suggestion = await self._suggest_skill(requirement)
                if self.dev_session and self.dev_session.active:
                    self.dev_session.pending_skill_suggestion = suggestion
                return self.local_skills.render_suggestion_card(suggestion)
            if subcommand == "create":
                if len(sub_tokens) < 3:
                    return (
                        f"用法：{COMMAND_PREFIX} skills create <skill_name> <需求>\n"
                        f"或在会话中发送：确认创建 skill <skill_name>"
                    )
                skill_name = sub_tokens[1].strip()
                requirement = " ".join(sub_tokens[2:]).strip()
                return await self._create_skill(skill_name=skill_name, requirement=requirement)
            return f"用法：{COMMAND_PREFIX} skills <update|status|suggest|create|list|show>"
        if command == "stop":
            if self.dev_session and self.dev_session.active:
                if self._should_confirm_stop(argument):
                    self._terminate_dev_session()
                    logger.info(
                        "Stopped active dev session via %s stop", COMMAND_PREFIX
                    )
                    return "已退出开发模式。"
                return (
                    "请确认退出开发模式：\n"
                    f"发送 `{COMMAND_PREFIX} stop confirm` 确认退出。"
                )
            return "当前没有进行中的开发会话。"
        if command == "ask":
            if not argument:
                return f"用法：{COMMAND_PREFIX} ask <需求>"
            if not self.dev_session or not self.dev_session.active:
                return (
                    "当前没有进行中的开发会话。"
                    f"请先使用 {COMMAND_PREFIX} start <plugin_name>。"
                )
            return await self.dev_session.handle_message(
                message=argument,
                workspace_manager=self.workspace_manager,
                tester=self.tester,
                local_skills_manager=self.local_skills,
                context=self.context,
                codex_timeout=self._cfg_int(
                    "codex_timeout", 300, minimum=10, maximum=3600
                ),
                auto_test_before_apply=self._cfg_bool("auto_test_before_apply", True),
            )
        if command == "files":
            if not self.dev_session or not self.dev_session.active:
                return (
                    "当前没有进行中的开发会话。"
                    f"请先使用 {COMMAND_PREFIX} start <plugin_name>。"
                )
            files = self.workspace_manager.list_files(self.dev_session.plugin_name)
            return (
                "workspace 为空"
                if not files
                else "workspace 文件列表：\n" + "\n".join(f"- {item}" for item in files)
            )
        if command == "cat":
            if not argument:
                return f"用法：{COMMAND_PREFIX} cat <文件>"
            if not self.dev_session or not self.dev_session.active:
                return (
                    "当前没有进行中的开发会话。"
                    f"请先使用 {COMMAND_PREFIX} start <plugin_name>。"
                )
            try:
                content = self.workspace_manager.read_file(
                    self.dev_session.plugin_name, argument
                )
            except Exception as exc:
                return f"读取文件失败：{exc}"
            return f"`{argument}`:\n{content}"
        if command == "test":
            if not self.dev_session or not self.dev_session.active:
                return (
                    "当前没有进行中的开发会话。"
                    f"请先使用 {COMMAND_PREFIX} start <plugin_name>。"
                )
            result = self.tester.run(self.dev_session.plugin_name)
            if result["success"]:
                return "基础测试通过。"
            return f"基础测试失败：{result['error_message']}"
        if command == "apply":
            if not self.dev_session or not self.dev_session.active:
                return (
                    "当前没有进行中的开发会话。"
                    f"请先使用 {COMMAND_PREFIX} start <plugin_name>。"
                )
            logger.info(
                "Running %s apply for plugin=%s",
                COMMAND_PREFIX,
                self.dev_session.plugin_name,
            )
            return await self.dev_session.handle_message(
                message="部署",
                workspace_manager=self.workspace_manager,
                tester=self.tester,
                local_skills_manager=self.local_skills,
                context=self.context,
                codex_timeout=self._cfg_int(
                    "codex_timeout", 300, minimum=10, maximum=3600
                ),
                auto_test_before_apply=self._cfg_bool("auto_test_before_apply", True),
            )

        # Keep V1 fallback: `<prefix> <message>` behaves like `<prefix> ask <message>`.
        if self.dev_session and self.dev_session.active and command:
            merged = " ".join([command, argument]).strip()
            logger.info("Fallback to ask-like flow with merged message=%s", merged)
            return await self.dev_session.handle_message(
                message=merged,
                workspace_manager=self.workspace_manager,
                tester=self.tester,
                local_skills_manager=self.local_skills,
                context=self.context,
                codex_timeout=self._cfg_int(
                    "codex_timeout", 300, minimum=10, maximum=3600
                ),
                auto_test_before_apply=self._cfg_bool("auto_test_before_apply", True),
            )
        return f"未知命令。可用 `{COMMAND_PREFIX} status` 查看当前会话状态。"

    def _start_session(self, plugin_name: str, resume: bool = False) -> str:
        """Create workspace and activate single dev session."""
        safe_name = validate_plugin_name(plugin_name)
        workspace_path = self.workspace_manager.create_workspace(safe_name)
        existed_files = self.workspace_manager.list_files(safe_name)
        self.dev_session = DevSession(
            plugin_name=safe_name,
            workspace_path=str(workspace_path),
            active=True,
        )
        self.last_plugin_name = safe_name
        self.pending_stop_confirm_until = None
        logger.info(
            "Created dev session: plugin=%s workspace=%s", safe_name, workspace_path
        )
        action_text = "已恢复" if resume or existed_files else "已开始"
        return (
            f"开发会话{action_text}：`{safe_name}`。\n"
            "你现在处于开发模式，可以直接用自然语言交流。\n"
            f"使用 {COMMAND_PREFIX} stop 退出，{COMMAND_PREFIX} status 查看状态。"
        )

    def _terminate_dev_session(self) -> None:
        """Terminate current dev session and clear in-memory state."""
        if self.dev_session:
            self.last_plugin_name = self.dev_session.plugin_name
            self.dev_session.active = False
        self.dev_session = None
        self.pending_stop_confirm_until = None

    def _keep_dev_session_active(self) -> None:
        """Force session to remain active after non-exit task handling."""
        if self.dev_session is not None:
            self.dev_session.active = True

    def _status_text(self) -> str:
        """Return current dev session status text."""
        if not self.dev_session or not self.dev_session.active:
            if self.last_plugin_name:
                return (
                    "当前没有进行中的开发会话。\n"
                    f"可用 `{COMMAND_PREFIX} resume {self.last_plugin_name}` 继续开发。"
                )
            return "当前没有进行中的开发会话。"
        return (
            "开发会话进行中\n"
            f"插件名: {self.dev_session.plugin_name}\n"
            f"工作目录: {self.dev_session.workspace_path}"
        )

    def _extract_dev_content(self, message_text: str) -> str:
        """Extract user payload after command prefix."""
        if not message_text.startswith(COMMAND_PREFIX):
            return message_text
        return message_text[len(COMMAND_PREFIX) :].strip()

    def _parse_dev_command(self, message_text: str) -> tuple[str, str]:
        """Parse command robustly from raw message text."""
        raw = message_text.strip()
        if not raw:
            return "", ""

        tokens = raw.split()
        dev_index = -1
        for index, token in enumerate(tokens):
            lowered = token.lower()
            if (
                lowered == COMMAND_PREFIX
                or lowered == COMMAND_NAME
                or lowered.startswith(f"{COMMAND_PREFIX}@")
            ):
                dev_index = index
                break

        # If command token is found in the message, use tokens after it.
        if dev_index >= 0:
            sub_tokens = tokens[dev_index + 1 :]
        else:
            # Fallback for direct command body in some adapter command parsers.
            content = self._extract_dev_content(raw)
            sub_tokens = content.split()

        if not sub_tokens:
            return "", ""

        command = sub_tokens[0].strip().lower()
        argument = " ".join(sub_tokens[1:]).strip()
        return command, argument

    def _should_confirm_stop(self, argument: str) -> bool:
        """Handle two-step stop confirmation with a short validity window."""
        if argument.strip().lower() == "confirm":
            return True
        self.pending_stop_confirm_until = time.time() + 60
        return False

    def _extract_explicit_command(self, message_text: str) -> tuple[bool, str, str]:
        """Extract explicit command only when message clearly addresses this plugin."""
        tokens = message_text.strip().split()
        if not tokens:
            return False, "", ""

        command_token = COMMAND_PREFIX.lower()
        command_name = COMMAND_NAME.lower()

        # Match first explicit token form: /codexdev, codexdev, /codexdev@bot
        first = tokens[0].lower()
        if first in {command_token, command_name} or first.startswith(
            f"{command_token}@"
        ):
            if len(tokens) < 2:
                return True, "", ""
            return True, tokens[1].strip().lower(), " ".join(tokens[2:]).strip()

        # Match mention-first form: @bot /codexdev stop
        if len(tokens) >= 2:
            second = tokens[1].lower()
            if second in {command_token, command_name} or second.startswith(
                f"{command_token}@"
            ):
                if len(tokens) < 3:
                    return True, "", ""
                return True, tokens[2].strip().lower(), " ".join(tokens[3:]).strip()

        return False, "", ""

    def _is_natural_stop_intent(self, message_text: str) -> bool:
        """Check whether user intends to exit dev mode in natural language."""
        normalized = message_text.strip().lower()
        intents = {
            "退出",
            "退出开发模式",
            "结束",
            "结束开发",
            "停止",
            "停止开发",
            "结束会话",
        }
        return normalized in intents

    async def _suggest_skill(self, requirement: str) -> dict[str, object]:
        """Generate one skill suggestion with Codex best-effort fallback."""
        heuristic = self.local_skills.propose_from_text(requirement)
        workspace = self.plugin_root
        if self.dev_session and self.dev_session.active:
            workspace = self.workspace_manager.get_workspace(self.dev_session.plugin_name)

        prompt = (
            "Return JSON only with keys: skill_name, description, trigger_examples, scope, benefit, draft_summary.\n"
            "Constraints: scope must be current_session_only; concise and reusable.\n"
            f"Requirement: {requirement}"
        )
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=workspace,
            timeout=min(120, self._cfg_int("codex_timeout", 300, minimum=10, maximum=3600)),
        )
        if result["exit_code"] == 0:
            parsed = self.local_skills.try_parse_json_suggestion(result["stdout"])
            if parsed is not None:
                return parsed
        return heuristic

    async def _create_skill(self, skill_name: str, requirement: str) -> str:
        """Create or update local skill, backing up old content on duplicate."""
        workspace = self.plugin_root
        if self.dev_session and self.dev_session.active:
            workspace = self.workspace_manager.get_workspace(self.dev_session.plugin_name)
        prompt = (
            "Write SKILL.md markdown with YAML frontmatter (name, description). "
            "Sections required: Purpose, Trigger examples, Workflow, Constraints, Output requirements.\n"
            f"skill_name={skill_name}\n"
            f"requirement={requirement}\n"
            "Output markdown only."
        )
        result = await asyncio.to_thread(
            run_codex,
            prompt=prompt,
            workspace_path=workspace,
            timeout=min(180, self._cfg_int("codex_timeout", 300, minimum=10, maximum=3600)),
        )
        draft = result["stdout"].strip() if result["exit_code"] == 0 else ""
        write_result = self.local_skills.create_or_update_skill(
            skill_name=skill_name,
            requirement=requirement,
            draft_content=draft,
        )
        if not write_result["success"]:
            return f"Skill 创建失败：{write_result['message']}"
        reload_text = await self._reload_self_plugin()
        return (
            f"{write_result['message']}\n"
            f"路径: {write_result['skill_path']}\n"
            f"{reload_text}"
        )

    async def _reload_self_plugin(self) -> str:
        """Reload this plugin after local skill creation/update."""
        manager = getattr(self.context, "_star_manager", None)
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

    def _cfg_str(self, key: str, default: str) -> str:
        """Read string config with safe fallback."""
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """Read bool config with permissive normalization."""
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        """Read bounded integer config and clamp to a safe range."""
        value = self.config.get(key, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _append_command_hint(self, message: str, in_dev_mode: bool = False) -> str:
        """Append concise command hints to response text."""
        hint = (
            f"命令提示：{COMMAND_PREFIX} status | {COMMAND_PREFIX} files | "
            f"{COMMAND_PREFIX} test | {COMMAND_PREFIX} apply | {COMMAND_PREFIX} stop | "
            f"{COMMAND_PREFIX} skills status | {COMMAND_PREFIX} skills suggest <需求>\n"
            "引导：日志检查默认只分析不修复；skill 创建需先确认计划。"
            if in_dev_mode
            else (
                f"命令提示：{COMMAND_PREFIX} start <plugin_name> | "
                f"{COMMAND_PREFIX} resume <plugin_name> | {COMMAND_PREFIX} status | "
                f"{COMMAND_PREFIX} skills status | {COMMAND_PREFIX} skills list\n"
                "引导：日志检查默认只分析不修复；skill 创建需先确认计划。"
            )
        )
        if hint in message:
            return message
        return f"{message}\n\n{hint}"

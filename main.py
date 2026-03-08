"""Main entrypoint for the astrbot_plugin_self_code plugin."""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.util import SessionController, session_waiter

from .core.dev_session import DevSession
from .core.tester import Tester
from .core.utils import validate_plugin_name
from .core.workspace import WorkspaceManager

COMMAND_NAME = "codexdev"
COMMAND_PREFIX = f"/{COMMAND_NAME}"


@register(
    "astrbot_plugin_self_code",
    "YakumoAki",
    "AI coding assistant plugin for AstrBot.",
    "0.2.0",
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
        self.dev_session: DevSession | None = None
        self.last_plugin_name: str | None = None
        self.pending_stop_confirm_until: float | None = None

    async def initialize(self) -> None:
        """Prepare runtime directories during plugin startup."""
        self.workspace_manager.ensure_runtime_structure()
        logger.info("astrbot_plugin_self_code runtime directories are ready.")

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
                    f"{COMMAND_PREFIX} test | {COMMAND_PREFIX} apply | {COMMAND_PREFIX} stop | {COMMAND_PREFIX} status"
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
            return "开发模式等待超时（10分钟），会话已退出。可使用 /codexdev resume <plugin_name> 继续。"
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Dev mode waiter failed: %s", exc)
            if self.dev_session:
                self.dev_session.active = False
            self._terminate_dev_session()
            return f"开发模式因错误退出：{exc}"
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
            if command == "stop" or command == "abort":
                if self._should_confirm_stop(argument):
                    self._terminate_dev_session()
                    controller.stop()
                    logger.info("Dev mode stopped by command")
                    return "已退出开发模式。"
                return (
                    "请确认退出开发模式：\n"
                    f"发送 `{COMMAND_PREFIX} stop confirm` 确认退出。"
                )
            if command == "status":
                return self._status_text()
            try:
                return await self._execute_v1_command(
                    command=command, argument=argument
                )
            except Exception as exc:  # pragma: no cover - runtime defensive path.
                logger.exception("Session command failed: %s", exc)
                return f"会话命令执行失败：{exc}"

        if self._is_natural_stop_intent(raw_message):
            if self._should_confirm_stop(""):
                self._terminate_dev_session()
                controller.stop()
                logger.info("Dev mode stopped by natural-language intent")
                return "已退出开发模式。"
            return (
                "检测到你想退出开发模式。\n"
                f"请发送 `{COMMAND_PREFIX} stop confirm` 确认退出。"
            )

        try:
            return await self.dev_session.handle_message(
                message=raw_message,
                workspace_manager=self.workspace_manager,
                tester=self.tester,
                context=self.context,
                codex_timeout=self._cfg_int(
                    "codex_timeout", 300, minimum=10, maximum=3600
                ),
                auto_test_before_apply=self._cfg_bool("auto_test_before_apply", True),
            )
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Dev session message handling failed: %s", exc)
            return f"开发会话处理失败：{exc}"

    async def _execute_v1_command(self, command: str, argument: str) -> str:
        """Execute V1 command set and compatibility aliases."""
        # Compatibility aliases for old command names.
        if command == "inspect":
            command = "files"
        if command == "abort":
            command = "stop"

        if command == "status":
            return self._status_text()
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
        now = time.time()
        confirm_tokens = {"confirm", "确认", "yes", "y"}
        if argument.strip().lower() in confirm_tokens:
            # Explicit confirmation should always succeed to avoid confirmation loops.
            return True
        self.pending_stop_confirm_until = now + 60
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
            f"{COMMAND_PREFIX} test | {COMMAND_PREFIX} apply | {COMMAND_PREFIX} stop"
            if in_dev_mode
            else (
                f"命令提示：{COMMAND_PREFIX} start <plugin_name> | "
                f"{COMMAND_PREFIX} resume <plugin_name> | {COMMAND_PREFIX} status"
            )
        )
        if hint in message:
            return message
        return f"{message}\n\n{hint}"

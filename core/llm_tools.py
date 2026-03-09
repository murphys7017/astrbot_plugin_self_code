"""FunctionTool definitions for Self Code plugin LLM integration."""

from __future__ import annotations

from typing import Any, Protocol

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

_PERMISSION_DENIED = "你没有权限使用该开发插件。"


class _SelfCodePluginLike(Protocol):
    """Minimal plugin interface needed by FunctionTools."""

    last_plugin_name: str | None

    def _cfg_str(self, key: str, default: str) -> str:
        ...

    def _start_session(self, plugin_name: str, resume: bool = False) -> str:
        ...

    def _status_text(self) -> str:
        ...

    async def _execute_v1_command(self, command: str, argument: str) -> str:
        ...


@dataclass
class _SelfCodeToolBase(FunctionTool[AstrAgentContext]):
    """Shared runtime behavior for all selfcode FunctionTools."""

    plugin: Any = Field(default=None, repr=False)

    def _resolve_plugin(self) -> _SelfCodePluginLike:
        plugin = self.plugin
        if plugin is None:
            raise RuntimeError("SelfCode plugin instance is not bound to tool.")
        return plugin

    def _resolve_event(self, context: ContextWrapper[AstrAgentContext]) -> Any:
        wrapped = getattr(context, "context", None)
        return getattr(wrapped, "event", None)

    def _check_permission(self, context: ContextWrapper[AstrAgentContext]) -> str:
        plugin = self._resolve_plugin()
        allowed_user_id = plugin._cfg_str("single_user_id", "")
        if not allowed_user_id:
            return ""

        event = self._resolve_event(context)
        if event is None or not hasattr(event, "get_sender_id"):
            return _PERMISSION_DENIED

        user_id = event.get_sender_id()
        if not user_id or user_id != allowed_user_id:
            return _PERMISSION_DENIED
        return ""

    async def _run_command(
        self,
        context: ContextWrapper[AstrAgentContext],
        command: str,
        argument: str,
    ) -> ToolExecResult:
        denied = self._check_permission(context)
        if denied:
            return denied

        plugin = self._resolve_plugin()
        try:
            return await plugin._execute_v1_command(command=command, argument=argument)
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("FunctionTool `%s` failed: %s", self.name, exc)
            return f"工具执行失败：{exc}"


@dataclass
class SelfCodeSkillCacheStatusTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_cache_status"
    description: str = (
        "读取 AstrBot-Skill 缓存状态（更新时间、Revision、数量、最近错误）。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "无业务参数。该工具只读，不会写文件。"
                "适合在调用 update 前确认缓存健康度。"
            ),
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return await self._run_command(context, "skills", "status")


@dataclass
class SelfCodeSkillCacheUpdateTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_cache_update"
    description: str = (
        "手动更新 AstrBot-Skill 缓存快照并返回最新状态，包含成功或失败原因。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "无业务参数。该工具会访问网络并刷新 runtime/skills_cache。"
                "建议在调用后再用 selfcode_skill_cache_status 复核。"
            ),
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return await self._run_command(context, "skills", "update")


@dataclass
class SelfCodeSkillListTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_list"
    description: str = "列出本地已注册的 skill 名称（来源：data/local_skills）。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": "无业务参数。只返回 skill 名称列表，不返回全文内容。",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return await self._run_command(context, "skills", "list")


@dataclass
class SelfCodeSkillShowTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_show"
    description: str = "预览指定本地 skill 内容（默认前 80 行）。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": "查看具体 skill 文本内容。skill_name 不能为空。",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "本地 skill 名称。例如 log_inspection。"
                        "仅用于读取，不触发修改。"
                    ),
                    "minLength": 1,
                    "examples": ["log_inspection", "plugin_autofix"],
                }
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        skill_name = str(kwargs.get("skill_name", "")).strip()
        if not skill_name:
            return "参数不完整：需要 skill_name。"
        return await self._run_command(context, "skills", f"show {skill_name}")


@dataclass
class SelfCodeSkillSuggestTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_suggest"
    description: str = "根据需求生成 skill 建议卡，包含名称、触发语句和收益。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "输入一个可复用流程需求，输出建议卡。"
                "仅建议，不会写入文件。"
            ),
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": (
                        "技能需求描述，应包含输入、关键步骤、期望输出。"
                        "内容越具体，建议越稳定。"
                    ),
                    "minLength": 4,
                    "examples": [
                        "分析支付失败日志并输出分级建议",
                        "自动整理部署前检查清单并给出结果模板",
                    ],
                }
            },
            "required": ["requirement"],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        requirement = str(kwargs.get("requirement", "")).strip()
        if not requirement:
            return "参数不完整：需要 requirement。"
        return await self._run_command(context, "skills", f"suggest {requirement}")


@dataclass
class SelfCodeSkillCreateTool(_SelfCodeToolBase):
    name: str = "selfcode_skill_create"
    description: str = (
        "创建或更新本地 skill（同名自动备份），并尝试重载 self_code 插件使其生效。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "创建/更新 skill 的写操作工具。会触发 codex 生成草稿并落盘。"
                "建议先调用 suggest 再 create。"
            ),
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "目标 skill 名称。建议使用小写短横线命名；"
                        "系统会在写入时进一步规范化。"
                    ),
                    "minLength": 1,
                    "examples": ["payment-log-triage", "log-diagnosis"],
                },
                "requirement": {
                    "type": "string",
                    "description": "技能需求描述（目的、流程、输出约束）。",
                    "minLength": 4,
                    "examples": [
                        "用户说 hello 时回复 are you ok?",
                        "日志检查只分析不修复并输出下一步建议",
                    ],
                },
            },
            "required": ["skill_name", "requirement"],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        skill_name = str(kwargs.get("skill_name", "")).strip()
        requirement = str(kwargs.get("requirement", "")).strip().replace("\n", " ")
        if not skill_name or not requirement:
            return "参数不完整：需要 skill_name 和 requirement。"
        return await self._run_command(
            context,
            "skills",
            f"create {skill_name} {requirement}",
        )


@dataclass
class SelfCodeDevStartTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_start"
    description: str = "启动开发会话并绑定到指定插件工作区。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "开始一个新的开发会话。"
                "仅允许 [a-zA-Z0-9_]，长度 3-50。"
            ),
            "properties": {
                "plugin_name": {
                    "type": "string",
                    "description": "插件名，作为 workspace 目录名与部署目标名。",
                    "pattern": "^[a-zA-Z0-9_]{3,50}$",
                    "examples": ["weather_plugin", "hello_agent"],
                }
            },
            "required": ["plugin_name"],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        denied = self._check_permission(context)
        if denied:
            return denied
        plugin_name = str(kwargs.get("plugin_name", "")).strip()
        if not plugin_name:
            return "参数不完整：需要 plugin_name。"
        plugin = self._resolve_plugin()
        try:
            return plugin._start_session(plugin_name)
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("FunctionTool `%s` failed: %s", self.name, exc)
            return f"启动开发会话失败：{exc}"


@dataclass
class SelfCodeDevResumeTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_resume"
    description: str = "恢复指定插件会话；不传名称时自动恢复最近一次会话。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "恢复已存在开发会话。plugin_name 可选；为空时回退到 last_plugin_name。"
            ),
            "properties": {
                "plugin_name": {
                    "type": "string",
                    "description": "要恢复的插件名，可为空字符串。",
                    "pattern": "^[a-zA-Z0-9_]{0,50}$",
                    "examples": ["weather_plugin", ""],
                }
            },
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        denied = self._check_permission(context)
        if denied:
            return denied
        plugin = self._resolve_plugin()
        plugin_name = str(kwargs.get("plugin_name", "")).strip()
        target_name = plugin_name or (plugin.last_plugin_name or "")
        if not target_name:
            return "没有可恢复会话，请先使用 /codexdev start <plugin_name>。"
        try:
            return plugin._start_session(target_name, resume=True)
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("FunctionTool `%s` failed: %s", self.name, exc)
            return f"恢复开发会话失败：{exc}"


@dataclass
class SelfCodeDevStatusTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_status"
    description: str = "查询当前开发会话状态（是否活跃、插件名、workspace 路径）。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": "无业务参数。只读状态，不触发编程或部署。",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        denied = self._check_permission(context)
        if denied:
            return denied
        plugin = self._resolve_plugin()
        try:
            return plugin._status_text()
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("FunctionTool `%s` failed: %s", self.name, exc)
            return f"读取会话状态失败：{exc}"


@dataclass
class SelfCodeDevChatTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_chat"
    description: str = "向开发会话发送自然语言需求，触发 codex CLI 编程流程。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "会话编程主入口。要求先 start/resume 再调用。"
                "支持自然语言开发、日志检查、自动修复等分支。"
            ),
            "properties": {
                "message": {
                    "type": "string",
                    "description": "开发需求文本，例如“实现 hello 回复 are you ok?”。",
                    "minLength": 1,
                    "examples": [
                        "实现 hello 命令，返回 are you ok?",
                        "检查一下日志并给出诊断",
                        "根据日志自动修复并部署",
                    ],
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        message = str(kwargs.get("message", "")).strip()
        if not message:
            return "参数不完整：需要 message。"
        return await self._run_command(context, "ask", message)


@dataclass
class SelfCodeDevTestTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_test"
    description: str = "运行当前开发会话插件的基础测试。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "无业务参数。要求当前存在活跃会话；否则返回提示先 start。"
            ),
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return await self._run_command(context, "test", "")


@dataclass
class SelfCodeDevApplyTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_apply"
    description: str = "部署当前会话插件到 AstrBot 插件目录并尝试热更新。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "无业务参数。会执行部署动作；是否先自动测试由配置项决定。"
            ),
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return await self._run_command(context, "apply", "")


@dataclass
class SelfCodeDevStopTool(_SelfCodeToolBase):
    name: str = "selfcode_dev_stop"
    description: str = "结束开发会话。支持两段式确认，防止误退出。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "description": (
                "confirm 可选。传 confirm=confirm 可直接退出；"
                "不传则只发起确认提示。"
            ),
            "properties": {
                "confirm": {
                    "type": "string",
                    "description": "退出确认令牌，推荐值：confirm。",
                    "examples": ["confirm", ""],
                    "default": "",
                }
            },
            "required": [],
            "additionalProperties": False,
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        confirm = str(kwargs.get("confirm", "")).strip()
        return await self._run_command(context, "stop", confirm)


def build_selfcode_tools(
    plugin: _SelfCodePluginLike,
) -> list[FunctionTool[AstrAgentContext]]:
    """Create all FunctionTool instances bound to one plugin instance."""
    return [
        SelfCodeSkillCacheStatusTool(plugin=plugin),
        SelfCodeSkillCacheUpdateTool(plugin=plugin),
        SelfCodeSkillListTool(plugin=plugin),
        SelfCodeSkillShowTool(plugin=plugin),
        SelfCodeSkillSuggestTool(plugin=plugin),
        SelfCodeSkillCreateTool(plugin=plugin),
        SelfCodeDevStartTool(plugin=plugin),
        SelfCodeDevResumeTool(plugin=plugin),
        SelfCodeDevStatusTool(plugin=plugin),
        SelfCodeDevChatTool(plugin=plugin),
        SelfCodeDevTestTool(plugin=plugin),
        SelfCodeDevApplyTool(plugin=plugin),
        SelfCodeDevStopTool(plugin=plugin),
    ]

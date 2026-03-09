---
name: session-controlled-codex-dev
description: "触发 codex CLI 编程时必须进入会话控制，持续交互直到超时或显式退出"
---

# Session Controlled Codex Dev Skill

## Purpose
一旦进入“用 codex CLI 写代码”的流程，必须通过 AstrBot Session Controller 保持连续会话，支持多轮迭代修改，直到用户长时间无响应或主动退出。

## Trigger examples
- 添加一个新的 skill
- 帮我生成插件并自动部署
- 继续修改刚才那个功能

## Command and tool mapping (current implementation)
- 命令入口：`/codexdev start|resume|ask|test|apply|stop`
- 会话控制：通过 `session_waiter` 保持会话，消息连续进入同一开发流程
- 退出控制：`/codexdev stop confirm` 或自然语言退出意图
- skills 工具：`selfcode_skill_cache_status/update + selfcode_skill_list/show/suggest/create`
- dev 工具：`selfcode_dev_start/resume/status/chat/test/apply/stop`

## Workflow
1. 进入会话控制器（`@session_waiter(timeout=...)`）。
2. 首轮收集目标与边界（改 skill 还是 plugin、是否自动部署）。
3. 生成并展示执行计划；确认后调用 codex CLI 开始编程。
4. 每轮执行后返回结果，并 `controller.keep(timeout=..., reset_timeout=True)` 维持会话。
5. 用户可继续追加需求（例如“再加一个命令”），继续 codex CLI 增量开发。
6. 若用户发送“退出/结束/stop confirm”等结束词，执行 `controller.stop()` 退出会话。
7. 若超时未响应，抛出 TimeoutError 并返回“会话已超时结束”。

## Session protocol (strict)
1. 会话内每次收到用户输入后，先判断是否退出。
2. 非退出消息进入 codex CLI 执行或命令分发。
3. 完成一轮后调用 `controller.keep(timeout=..., reset_timeout=True)`。
4. 捕获 `TimeoutError` 后统一清理会话并返回超时提示。
5. 异常场景必须记录并返回可诊断错误，不得静默失败。

## Constraints
- 会话控制回调内发送消息应使用 `await event.send(...)`，不要用 `yield`。
- 会话中所有 codex CLI 操作都必须限制在当前 workspace。
- 显式退出优先于自动超时；退出后必须清理会话状态。
- 会话内执行部署前必须先测试。
- 禁止跨插件目录写入和热更新。
- 当用户需求同时出现“编程 + 部署”时，默认顺序应为：编程 -> 测试 -> 部署。

## Output requirements
- 当前会话状态（active/timeout/stopped）
- 本轮 codex 执行结果摘要
- 测试与部署状态
- 下一步建议（继续迭代或结束会话）

---
name: auto-skill-authoring
description: "通过自然语言需求自动调用 codex CLI 生成 skill/plugin，并自动测试、部署、热更新"
---

# Auto Skill Authoring Skill

## Purpose
当用户说“添加一个新的 skill”时，把需求转成可执行开发任务，调用 codex CLI 自动编写 skill 或 plugin，随后自动测试、部署并触发热更新。

## Trigger examples
- 添加一个新的 skill
- 帮我做一个插件：我说 hello 你回复 are you ok?
- 给我自动生成一个可部署的 skill 或 plugin

## Capability mapping (current implementation)
- 支持：会话编程（`/codexdev start|resume` + 持续对话）
- 支持：自然语言触发 codex CLI 改代码（会话内普通消息）
- 支持：测试（`/codexdev test` 或会话内“测试”）
- 支持：部署和热更新（`/codexdev apply` 或会话内“部署”）
- 支持：本地 skill 创建（`/codexdev skills create` 或“添加一个新的skill”->确认）
- 支持：Agent tools 触发开发链路（`selfcode_dev_start/resume/status/chat/test/apply/stop`）
- 不支持：单个 llm_tool 一步完成“编写+测试+部署+热更新”
- 说明：可通过 `selfcode_dev_start` 主动创建开发会话，再连续调用 `selfcode_dev_chat`

## Recommended end-to-end flow
1. 启动会话：`/codexdev start <plugin_name>`
2. 发送目标需求：
   - 示例：`添加一个新的skill，作用是我说hello的时候你说 are you ok?`
3. 如果是创建 skill：
   - 补充功能描述
   - 收到计划卡后发送：`确认创建 skill <name>`
4. 如果是改 plugin：
   - 直接发送实现要求，触发 codex CLI 编程
5. 运行测试：`测试` 或 `/codexdev test`
6. 部署热更新：`部署` 或 `/codexdev apply`
7. 持续迭代直到完成，最后 ` /codexdev stop` -> `/codexdev stop confirm`

## Workflow
1. 解析用户目标，提取最小规格：
   - 输入触发词（如 `hello`）
   - 期望输出（如 `are you ok?`）
   - 产物类型（skill 或 plugin）
2. 先输出执行计划卡（文件、步骤、风险、验证方式）。
3. 用户确认后，调用 codex CLI 生成/修改代码与配置文件。
4. 自动执行测试（至少基础可导入/可加载检查）。
5. 测试通过后自动部署到目标插件目录。
6. 自动调用 AstrBot reload 热更新。
7. 返回完整结果：创建内容、测试结果、部署状态、热更新状态。

## Example requirement
- 用户输入：添加一个新的 skill，作用是我说 hello 的时候你说 are you ok?
- 期望动作：自动生成对应 skill/plugin，实现消息匹配与回复逻辑，并完成测试和热更新。

## Constraints
- 未确认前不得执行写入、部署、重载。
- codex CLI 编程阶段必须在受控 workspace 中进行，禁止越界写入。
- 变更优先最小化，避免无关重构。
- 如果测试失败，不得自动部署；需要返回失败原因和修复建议。
- 发生异常时要中止流程并保留诊断信息。
- 当需求包含“并部署”这类词时，先确认是否应先编程再部署，避免直接部署旧代码。

## Output requirements
- 计划卡（可确认）
- 执行日志摘要（codex 生成、测试、部署、热更新）
- 结果状态：`success` / `failed`
- 失败时给出下一步动作（可重试命令或需要补充的信息）

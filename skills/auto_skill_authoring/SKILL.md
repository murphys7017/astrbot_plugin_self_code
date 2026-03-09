---
name: auto-skill-authoring
description: "将高频对话流程自动沉淀为可复用 SKILL.md（先计划后确认）"
---

# Auto Skill Authoring Skill

## Purpose
当用户提出“这个流程经常重复”或“帮我做成 skill”时，自动生成 skill 建议与计划，并在确认后落盘。

## Trigger examples
- 这个流程我经常重复，帮我沉淀成 skill
- 帮我把这套排查流程做成一个 skill
- 自动生成一个可复用 skill 模板

## Workflow
1. 识别需求是否为可复用流程（有稳定输入、步骤、输出）。
2. 提取核心信息：目标、触发条件、关键步骤、约束、输出格式。
3. 生成 skill 建议卡：
   - `skill_name`
   - `description`
   - `trigger_examples`
   - `benefit`
4. 输出“待确认计划卡”，不立即写文件。
5. 收到明确确认（例如：`确认创建 skill <name>`）后创建/更新文件。
6. 若同名 skill 已存在，先备份再更新。
7. 返回写入路径、备份路径（如有）和生效状态。

## Constraints
- 未确认前不得写入文件。
- 名称规范：小写 + 短横线，避免空名和超长名。
- 内容必须包含固定章节：
  - Purpose
  - Trigger examples
  - Workflow
  - Constraints
  - Output requirements
- 尽量保持技能描述可执行、可验证，不写空泛表述。

## Output requirements
- 建议卡（可读、可确认）
- 计划卡（结构化）
- 创建结果（created/updated + path）
- 失败时给出明确原因与下一步动作


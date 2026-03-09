# astrbot_plugin_self_code

一个用于 AstrBot 的 AI 编程助手插件。  
它把聊天指令转为 `codex CLI` 操作，在 workspace 中生成/修改插件代码，并支持测试和部署。

## 功能概览

- 单用户开发会话（可配置限制用户 ID）
- Dev Mode 连续对话开发（无需每次都写 `ask`）
- Workspace 文件管理（读/写/列表）
- Sandbox 基础测试（`main.py`、`metadata.yaml`、导入/重载）
- 一键部署到 AstrBot 插件目录
- AstrBot-Skill 手动缓存更新（用于增强提示词参考）
- 本地可注册技能（`data/local_skills/**/SKILL.md`），可定义日志检查等固定流程
- 日志归因自动修复（仅当前会话插件；测试通过后自动 apply）
- 支持 AI 自动建议并创建本地 skill（确认后即时生效）
- 提示词自动拼接：本地 skills 摘要 + AstrBot-Skill 缓存摘要

## 项目结构

```text
astrbot_plugin_self_code/
├── main.py
├── metadata.yaml
├── requirements.txt
├── _conf_schema.json
├── core/
│   ├── codex_runner.py
│   ├── dev_session.py
│   ├── local_skills.py
│   ├── prompt_builder.py
│   ├── skills_cache.py
│   ├── tester.py
│   ├── utils.py
│   └── workspace.py
├── data/
│   └── local_skills/
│       └── log_inspection/SKILL.md
│       └── plugin_autofix/SKILL.md
│       └── <generated-skill>/SKILL.md
└── astrbot_dev_docs.md
```

运行时持久化目录（重要）：

```text
AstrBot/data/plugin_data/astrbot_plugin_self_code/runtime/
├── sessions.json
├── skills_cache/
├── sandbox_plugins/
└── workspaces/
```

## 命令说明

插件命令前缀是：`/codexdev`

- `/codexdev start <plugin_name>`
  - 创建/加载 `data/plugin_data/astrbot_plugin_self_code/runtime/workspaces/<plugin_name>`
  - 进入 Dev Mode（会话模式）
- `/codexdev resume <plugin_name>`
  - 恢复指定插件的开发会话
- `/codexdev status`
  - 查看当前会话状态
- `/codexdev stop`
  - 发起退出开发模式（需要确认）
- `/codexdev stop confirm`
  - 确认退出开发模式
- `/codexdev skills update`
  - 手动刷新 AstrBot-Skill 本地缓存快照
- `/codexdev skills status`
  - 查看缓存状态（更新时间、文件数、来源分支、最近错误）
- `/codexdev skills suggest <需求>`
  - 生成本地 skill 建议卡（名称、触发语句、收益）
- `/codexdev skills create <skill_name> <需求>`
  - 创建或更新本地 skill（同名先备份再更新）
- `/codexdev skills list`
  - 列出本地 skill 名称
- `/codexdev skills show <skill_name>`
  - 查看本地 skill 内容预览

LLM Tools（可供 Agent 调用）：
- `codexdev_skills_status`
- `codexdev_skills_update`
- `codexdev_skills_list`
- `codexdev_skills_show(skill_name)`
- `codexdev_skills_suggest(requirement)`
- `codexdev_skills_create(skill_name, requirement)`

V1 兼容命令（在会话中或显式调用）：

- `/codexdev ask <prompt>`
- `/codexdev files`
- `/codexdev cat <file>`
- `/codexdev test`
- `/codexdev apply`

自动修复示例（自然语言触发）：
- `自己看是哪个插件并修复`
- `根据日志自动修复`

日志检查示例（默认只分析不修复）：
- `检查一下日志`
- `看日志`

新建 skill 对话示例：
- `添加一个新的skill`
- （机器人追问功能描述）
- `用于分析支付失败日志并输出分级建议`
- （机器人给出计划卡）
- `确认创建 skill payment-log-triage`

Skill 建议与确认示例：
- `这个流程我经常重复，帮我沉淀成 skill`
- `确认创建 skill log-diagnosis`

说明：
- `skills create` 会在同名 skill 已存在时自动备份到 `data/local_skills/_backup/`
- 会话内通过“确认创建 skill <name>”创建成功后，会尝试自动重载 `astrbot_plugin_self_code`

兼容别名：

- `inspect` 等价于 `files`
- `abort` 等价于 `stop`

## Dev Mode 使用方式

1. 启动会话：

```text
/codexdev start weather_plugin
```

2. 然后直接说自然语言（无需 `ask`）：

```text
写一个天气查询命令
增加缓存
测试
部署
```

3. 退出会话：

```text
/codexdev stop
/codexdev stop confirm
```

4. 后续继续：

```text
/codexdev resume weather_plugin
```

## 部署行为

当执行 `/codexdev apply`（或自然语言“部署”）时：

1. 先按配置决定是否自动测试（默认开启）
2. 把 workspace 复制到 AstrBot 插件目录

目标目录为：

`<AstrBot项目根目录>/data/plugins/<plugin_name>`

说明：
- 是复制（copy），不是移动（move）
- 复制前会删除同名旧目录并覆盖
- 然后尝试调用 AstrBot 的插件重载
- 该目录由当前插件所在目录自动推导（`plugin_root.parent / <plugin_name>`）

## 官方开发原则（AstrBot 推荐）

开发插件请遵守以下原则：

1. 功能需经过测试。
2. 需包含良好的注释。
3. 持久化数据请存储于 `data` 目录下，而非插件自身目录，防止更新/重装插件时数据被覆盖。
4. 需要良好的错误处理机制，不要让插件因单个错误崩溃。
5. 提交前请使用 `ruff` 格式化与检查代码。
6. 不要使用 `requests` 库进行网络请求，优先使用异步库（如 `aiohttp`、`httpx`）。
7. 如果是对某个插件进行功能扩增，优先向原插件提交 PR（除非原作者已停止维护）。

## 配置项（`_conf_schema.json`）

- `single_user_id`：仅允许该用户使用（空字符串表示不限制）
- `codex_timeout`：`codex exec` 超时秒数（默认 300）
- `auto_test_before_apply`：部署前自动测试（默认 `true`）
- `dev_mode_timeout`：Dev Mode 会话等待超时（默认 1800 秒）
- `skills_update_hint`：提示性配置，说明 skills 为手动更新模式（默认 `manual`）
- `autofix_scope_hint`：提示性配置，说明自动修复仅作用当前会话插件（默认 `current_session_only`）
- `self_authoring_skill_hint`：提示性配置，说明支持自动建议+确认创建 skill（默认 `enabled_with_confirm`）
- `log_inspection_mode_hint`：提示性配置，说明日志检查默认只读分析（默认 `analyze_only`）
- `skill_creation_flow_hint`：提示性配置，说明 skill 创建流程为“先计划后确认再创建并自动重载”（默认 `plan_then_confirm_then_reload`）

## plugin_name 规则

`/codexdev start <plugin_name>` 中的名称必须满足：

- 仅允许：英文大小写、数字、下划线（`[a-zA-Z0-9_]`）
- 长度：3-50

## 依赖与环境

1. Python 3.10+
2. AstrBot 正常运行
3. 可执行的 `codex` CLI 在 AstrBot 进程 `PATH` 中

建议安装后重启 AstrBot，确保新 PATH 生效。

## 常见问题

### 1) `Codex CLI not found`

原因：AstrBot 进程环境中找不到 `codex`。  
处理：

- 在启动 AstrBot 的同一终端执行 `which codex`
- 确认该路径对 AstrBot 进程可见
- 如在 AstrBot 启动后才安装 CLI，重启 AstrBot

### 2) 发送 `/codexdev stop` 后仍在开发

这是两步确认设计，防误退：

```text
/codexdev stop
/codexdev stop confirm
```

### 3) 命令被其他插件抢占或冲突

当前插件固定命令是 `/codexdev`。  
如仍冲突，请检查是否有其他插件注册了同名命令。

## 安全设计

- Workspace 路径限制在 `data/plugin_data/astrbot_plugin_self_code/runtime/workspaces/` 下，防止路径逃逸
- 文件读写全部使用 `pathlib.Path` + 路径校验
- `subprocess.run` 使用 `shell=False`（列表参数调用）
- 会话/文件操作有异常捕获与日志输出

## 开发与检查

```bash
uv run ruff format .
uv run ruff check .
```

## License

`LICENSE` 文件为 GNU GPL v3.0。

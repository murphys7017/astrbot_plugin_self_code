# Log Inspection Skill

## Purpose
When the user asks to check logs (for example: "看一下日志", "检查日志", "看报错"), guide Codex CLI to inspect AstrBot runtime logs first.

## Primary log files
- `data/logs/astrbot.log`
- `data/logs/astrbot.trace.log`

## Workflow
1. Confirm whether both files exist before reading.
2. Read the latest part of logs first, then search for key error patterns.
3. Summarize findings with timestamp, component/module, error type, and likely root cause.
4. If no clear error is found, state that explicitly and suggest the next diagnostic step.

## Codex CLI action hints
- Prefer checking file existence and size first.
- For large files, inspect recent lines instead of dumping full file.
- Search keywords: `ERROR`, `Exception`, `Traceback`, `CRITICAL`, `failed`, `timeout`.
- If the expected paths are missing, search under `data/logs/` for similar log files.

## Output style
- Keep report concise and structured.
- Include concrete evidence lines or snippets when available.
- Avoid guessing without log evidence.

# Plugin Autofix Skill

## Purpose
When the user asks to identify which plugin failed and fix it, run a log-first diagnosis and only modify the current session plugin workspace.

## Trigger examples
- "自己看是哪个插件并修复"
- "检查哪个插件报错并修复"
- "根据日志自动修复"

## Primary logs
- `data/logs/astrbot.log`
- `data/logs/astrbot.trace.log`

## Required constraints
1. Diagnose plugin/module owner from logs before editing anything.
2. Compare inferred owner with current session plugin.
3. If owner is different, do not modify files and report mismatch.
4. If evidence is insufficient, do not modify files and report insufficient evidence.
5. Only when owner matches current session plugin: perform minimal targeted edits.
6. After edits, run test; if test passes, trigger apply/reload.

## Decision protocol
Return one of these markers in the first output line:
- `AUTOFIX_DECISION: proceed`
- `AUTOFIX_DECISION: mismatch`
- `AUTOFIX_DECISION: insufficient`

## Output requirements
- Include log evidence (timestamp/module/error snippet).
- Explain what was changed and why.
- If blocked by mismatch/insufficient evidence, provide next step suggestions without cross-plugin writes.

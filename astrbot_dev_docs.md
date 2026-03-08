# AstrBot Plugin Dev Guide (LLM-Oriented)

This guide is a compact, generation-friendly reference for building correct AstrBot plugins.

## 1. Plugin Structure

### Required Files

```text
your_plugin/
├── main.py                 # Plugin entry (required)
├── metadata.yaml           # Plugin metadata (required by plugin packaging flow)
├── requirements.txt        # Python deps (recommended/usually required)
└── _conf_schema.json       # Optional: config schema for AstrBot UI/config system
```

### Runtime Data Location

- Do **not** write mutable data into plugin source directory.
- Use:
  - KV storage APIs (`context.provider`)
  - plugin data directory (e.g. `data/plugin_data/<plugin_name>/`) for larger files.

## 2. Minimal Plugin Example

```python
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register


@register("hello_plugin", "your_name", "Minimal AstrBot plugin", "0.1.0")
class HelloPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent):
        yield event.plain_result("hello")
```

## 3. Core APIs

### Main Imports

```python
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
```

### Core Concepts

- `Star`: plugin base class. Your plugin class must inherit this.
- `@register(name, author, desc, version)`: registers plugin metadata.
- `AstrMessageEvent`: incoming message/event object.
- `event.plain_result(text)`: plain text response payload.
- Handler outputs usually use `yield ...` in command handlers.

### Context/Provider Utilities

- `context.provider` is used for storage and framework capabilities.
- KV storage methods (common):
  - `put_kv_data(namespace, key, value)`
  - `get_kv_data(namespace, key)`
  - `delete_kv_data(namespace, key)`

## 4. Command Registration

### Basic Command

```python
@filter.command("ping")
async def ping(self, event: AstrMessageEvent):
    yield event.plain_result("pong")
```

### Command With Parameters

```python
@filter.command("echo")
async def echo(self, event: AstrMessageEvent, text: str):
    yield event.plain_result(text)
```

### Alias

```python
@filter.command("weather", alias={"w", "天气"})
async def weather(self, event: AstrMessageEvent, city: str):
    yield event.plain_result(f"querying: {city}")
```

### Command Group (for subcommands)

```python
dev = filter.command_group("dev")

@dev.command("status")
async def dev_status(self, event: AstrMessageEvent):
    yield event.plain_result("ok")
```

## 5. Event Handling

### Rule

- Use `@filter.*` decorators to match message/event conditions.
- Multiple filters are combined with logical AND.

### Typical Listener Pattern

```python
@filter.event_message_type(...)
@filter.permission_type(...)
async def on_message(self, event: AstrMessageEvent):
    yield event.plain_result("matched")
```

### Sending Messages

- In normal command handlers: usually `yield event.plain_result(...)`.
- In some async callback contexts (e.g. session waiter callback): use `await event.send(...)`.

## 6. Configuration System

### `_conf_schema.json`

Defines plugin config schema shown in AstrBot management/UI and persisted as config.

Typical schema fields include:

- `type` (e.g. `string`, `int`, `bool`, etc.)
- `description`
- `default`
- enum/options related fields
- special selectors (`_special`) when needed by framework features.

### Using Config in Plugin

```python
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, register


@register("cfg_demo", "you", "config demo", "0.1.0")
class CfgDemo(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "")

    async def save_something(self):
        self.config["last_status"] = "ok"
        self.config.save_config()
```

## 7. Development Rules

### Mandatory Rules

1. Plugin class must inherit `Star`.
2. Plugin class must be decorated by `@register(...)`.
3. Command handlers must use `@filter.command(...)` (or command-group subcommands).
4. `main.py` must be valid Python and importable.

### Engineering Rules

1. Keep plugin async-friendly.
2. Add dependencies to `requirements.txt`.
3. Include error handling for external API/network/file operations.
4. Keep code formatted/linted (e.g. `ruff format .` and `ruff check .`).
5. Use clear comments only where necessary.

## 8. Best Practices

1. Prefer async HTTP clients (`httpx`, `aiohttp`) over blocking `requests`.
2. Validate user input and command parameters.
3. Isolate side effects (file writes, external commands) behind helper functions.
4. Use path-safe operations (`pathlib.Path`, resolved path checks) if touching files.
5. Keep handlers small; move logic to services/modules.
6. Return clear user-facing error messages on failure.
7. For multi-turn workflows, use session control instead of ad-hoc global state.

### Session Control Pattern (Important)

```python
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import session_waiter
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.star.filter.permission_type import PermissionType
from astrbot.core.star.session import SessionController


@filter.command("survey")
async def survey(self, event: AstrMessageEvent):
    yield event.plain_result("Your age?")

    @session_waiter(timeout=60)
    async def wait_age(controller: SessionController, event: AstrMessageEvent):
        if not event.message_str.isdigit():
            await event.send(event.plain_result("Please input a number"))
            controller.keep(timeout=60)
            return
        await event.send(event.plain_result(f"Age={event.message_str}"))
        controller.stop()
```

Key rule:
- In waiter callback, use `await event.send(...)` and control lifecycle with `controller.keep()` / `controller.stop()`.

## 9. Common Mistakes

1. Missing `@register(...)` or wrong class not inheriting `Star`.
2. Defining handlers without `@filter.command(...)`.
3. Using blocking calls (`requests`, long sync I/O) in async handlers.
4. Writing runtime data into plugin code directory.
5. Not declaring third-party deps in `requirements.txt`.
6. Incorrect reply style in callback contexts (`yield` where `await event.send` is required).
7. Omitting input validation and exception handling.
8. Hardcoding secrets/API keys in source code.
9. Creating overly complex state machines when session control already solves multi-turn chat.

---

## Quick Generation Checklist (for LLM)

Use this checklist before outputting a plugin:

1. `main.py` exists and is importable.
2. Correct imports from `astrbot.api.star` and `astrbot.api.event`.
3. Class inherits `Star`.
4. Class decorated with `@register`.
5. At least one `@filter.command` handler.
6. Handler returns response via `yield event.plain_result(...)`.
7. Async-safe external calls and robust `try/except`.
8. `requirements.txt` includes all non-stdlib packages.
9. Optional `_conf_schema.json` + `AstrBotConfig` usage if config needed.
10. No unsafe path handling or unmanaged runtime writes.

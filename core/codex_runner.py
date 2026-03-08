"""Utilities for executing Codex CLI inside a workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict

from astrbot.api import logger


class CodexRunResult(TypedDict):
    """Structured result returned by `run_codex`."""

    stdout: str
    stderr: str
    exit_code: int


def run_codex(
    prompt: str,
    workspace_path: Path,
    timeout: int = 300,
    codex_bin: str = "codex",
) -> CodexRunResult:
    """Run `codex exec` in a workspace and return stdout/stderr/exit_code."""
    workspace = Path(workspace_path).resolve()
    if not workspace.exists() or not workspace.is_dir():
        message = f"Invalid workspace path: {workspace}"
        logger.error(message)
        return {"stdout": "", "stderr": message, "exit_code": 2}

    logger.info("Running Codex CLI in workspace: %s (bin=%s)", workspace, codex_bin)
    logger.debug("Codex prompt: %s", prompt)

    try:
        process = subprocess.run(
            [codex_bin, "exec", prompt],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        logger.info("Codex CLI finished with exit code: %s", process.returncode)
        if process.stderr:
            logger.warning("Codex CLI stderr: %s", process.stderr.strip())
        return {
            "stdout": process.stdout,
            "stderr": process.stderr,
            "exit_code": process.returncode,
        }
    except subprocess.TimeoutExpired as exc:
        stdout_text = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
        timeout_message = (
            f"Codex CLI timed out after {timeout} seconds in workspace: {workspace}"
        )
        logger.error(timeout_message)
        return {
            "stdout": stdout_text,
            "stderr": f"{stderr_text}\n{timeout_message}".strip(),
            "exit_code": 124,
        }
    except FileNotFoundError:
        message = (
            f"Codex CLI not found: {codex_bin}. "
            "Ensure it is installed and in PATH, or set plugin config `codex_bin`."
        )
        logger.exception(message)
        return {"stdout": "", "stderr": message, "exit_code": 127}
    except Exception as exc:  # pragma: no cover - defensive runtime safety.
        message = f"Unexpected error while running Codex CLI: {exc}"
        logger.exception(message)
        return {"stdout": "", "stderr": message, "exit_code": 1}

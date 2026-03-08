"""Sandbox testing utilities for AI-generated AstrBot plugins."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path
from typing import TypedDict

from astrbot.api import logger

from .utils import get_runtime_root, validate_plugin_name


class TestResult(TypedDict):
    """Standard return type for tester functions."""

    success: bool
    error_message: str


def copy_to_sandbox(plugin_name: str, plugin_root: Path) -> TestResult:
    """Copy a workspace plugin into runtime sandbox path for isolated checks."""
    try:
        safe_name = validate_plugin_name(plugin_name)
        runtime_root = get_runtime_root(plugin_root)
        workspace_path = runtime_root / "workspaces" / safe_name
        sandbox_root = runtime_root / "sandbox_plugins"
        sandbox_path = sandbox_root / safe_name

        if not workspace_path.exists() or not workspace_path.is_dir():
            return {
                "success": False,
                "error_message": f"Workspace not found: {workspace_path}",
            }

        sandbox_root.mkdir(parents=True, exist_ok=True)
        if sandbox_path.exists():
            shutil.rmtree(sandbox_path)
        shutil.copytree(workspace_path, sandbox_path)
        logger.info(
            "Copied workspace to sandbox: %s -> %s", workspace_path, sandbox_path
        )
        return {"success": True, "error_message": ""}
    except Exception as exc:  # pragma: no cover - defensive runtime safety.
        logger.exception("Failed to copy workspace to sandbox: %s", exc)
        return {"success": False, "error_message": str(exc)}


def reload_plugin(plugin_name: str, plugin_root: Path, context=None) -> TestResult:
    """Reload plugin via AstrBot manager when available, otherwise do import validation."""
    try:
        safe_name = validate_plugin_name(plugin_name)
        runtime_root = get_runtime_root(plugin_root)
        sandbox_path = runtime_root / "sandbox_plugins" / safe_name
        main_file = sandbox_path / "main.py"

        if context is not None and hasattr(context, "_star_manager"):
            manager = getattr(context, "_star_manager")
            if manager is not None and hasattr(manager, "reload"):
                reload_result = manager.reload(safe_name)
                if asyncio.iscoroutine(reload_result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(reload_result)
                        logger.info(
                            "Scheduled async plugin reload task for %s", safe_name
                        )
                    except RuntimeError:
                        asyncio.run(reload_result)
                        logger.info("Executed async plugin reload for %s", safe_name)
                logger.info("Plugin reloaded via star manager: %s", safe_name)
                return {"success": True, "error_message": ""}

        if not main_file.exists():
            return {
                "success": False,
                "error_message": f"main.py not found: {main_file}",
            }

        module_name = f"sandbox_plugins.{safe_name}.main"
        spec = importlib.util.spec_from_file_location(module_name, main_file)
        if spec is None or spec.loader is None:
            return {"success": False, "error_message": "Failed to create import spec"}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        logger.info("Plugin import validation passed: %s", safe_name)
        return {"success": True, "error_message": ""}
    except Exception as exc:  # pragma: no cover - defensive runtime safety.
        logger.exception("Failed to reload plugin %s: %s", plugin_name, exc)
        return {"success": False, "error_message": str(exc)}


def run_basic_test(plugin_name: str, plugin_root: Path, context=None) -> TestResult:
    """Run basic sandbox test flow: copy -> file checks -> import/reload."""
    safe_name = validate_plugin_name(plugin_name)
    copy_result = copy_to_sandbox(safe_name, plugin_root)
    if not copy_result["success"]:
        return copy_result

    runtime_root = get_runtime_root(plugin_root)
    sandbox_path = runtime_root / "sandbox_plugins" / safe_name
    main_file = sandbox_path / "main.py"
    metadata_file = sandbox_path / "metadata.yaml"

    if not main_file.exists():
        error_message = f"main.py is missing in sandbox plugin: {main_file}"
        logger.error(error_message)
        return {"success": False, "error_message": error_message}
    if not metadata_file.exists():
        error_message = f"metadata.yaml is missing in sandbox plugin: {metadata_file}"
        logger.error(error_message)
        return {"success": False, "error_message": error_message}

    return reload_plugin(safe_name, plugin_root, context=context)


class Tester:
    """Object-oriented wrapper for sandbox test functions."""

    def __init__(self, plugin_root: Path, context=None) -> None:
        """Store plugin root path and optional AstrBot context for reload operation."""
        self.plugin_root = plugin_root
        self.context = context

    def copy_to_sandbox(self, plugin_name: str) -> TestResult:
        """Copy workspace plugin files into sandbox directory."""
        return copy_to_sandbox(plugin_name, self.plugin_root)

    def reload_plugin(self, plugin_name: str) -> TestResult:
        """Reload plugin from sandbox using context manager when available."""
        return reload_plugin(plugin_name, self.plugin_root, context=self.context)

    def run_basic_test(self, plugin_name: str) -> TestResult:
        """Execute full basic test procedure and return final result."""
        return run_basic_test(plugin_name, self.plugin_root, context=self.context)

    def run(self, plugin_name: str) -> TestResult:
        """Compatibility entrypoint for V2 dev session test action."""
        return self.run_basic_test(plugin_name)

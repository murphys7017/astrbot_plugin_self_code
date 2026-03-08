"""Workspace management for AI-generated plugin development files."""

from __future__ import annotations

import shutil
from pathlib import Path

from .utils import get_runtime_root, validate_plugin_name


class WorkspaceManager:
    """Manage plugin workspaces under plugin data runtime/workspaces."""

    def __init__(self, plugin_root: Path) -> None:
        """Initialize workspace root paths relative to current plugin root."""
        self.plugin_root = plugin_root
        self.runtime_dir = get_runtime_root(self.plugin_root)
        self.workspaces_dir = self.runtime_dir / "workspaces"
        self.template_dir = self.plugin_root / "core" / "plugin_template"

    def ensure_runtime_structure(self) -> None:
        """Ensure runtime and workspace root directories always exist."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "sandbox_plugins").mkdir(parents=True, exist_ok=True)
        sessions_file = self.runtime_dir / "sessions.json"
        if not sessions_file.exists():
            sessions_file.write_text("[]\n", encoding="utf-8")

    def create_workspace(self, plugin_name: str) -> Path:
        """Create a workspace and required base files for one plugin name."""
        workspace_path = self.get_workspace(plugin_name)
        self._ensure_base_files(workspace_path, plugin_name)
        return workspace_path

    def get_workspace(self, plugin_name: str) -> Path:
        """Return workspace path for plugin name, creating the directory if needed."""
        safe_name = validate_plugin_name(plugin_name)
        self.ensure_runtime_structure()
        workspace_path = self.workspaces_dir / safe_name
        workspace_path.mkdir(parents=True, exist_ok=True)
        return workspace_path

    def list_files(self, plugin_name: str) -> list[str]:
        """List all files in a workspace using relative POSIX-style paths."""
        workspace_path = self.get_workspace(plugin_name)
        files = [
            path.relative_to(workspace_path).as_posix()
            for path in workspace_path.rglob("*")
            if path.is_file()
        ]
        return sorted(files)

    def read_file(self, plugin_name: str, path: str) -> str:
        """Read a file in workspace and block any path outside workspace."""
        workspace_path = self.get_workspace(plugin_name)
        target_path = self._resolve_inside_workspace(workspace_path, path)
        if not target_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not target_path.is_file():
            raise IsADirectoryError(f"Path is not a file: {path}")
        return target_path.read_text(encoding="utf-8")

    def write_file(self, plugin_name: str, path: str, content: str) -> Path:
        """Write content into a workspace file while preventing path traversal."""
        workspace_path = self.get_workspace(plugin_name)
        target_path = self._resolve_inside_workspace(workspace_path, path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        return target_path

    def _resolve_inside_workspace(self, workspace_path: Path, user_path: str) -> Path:
        """Resolve user path and ensure the resolved target remains in workspace."""
        if not user_path.strip():
            raise ValueError("path must not be empty")
        resolved_workspace = workspace_path.resolve()
        resolved_target = (workspace_path / user_path).resolve()
        try:
            resolved_target.relative_to(resolved_workspace)
        except ValueError as exc:
            raise ValueError("Access outside workspace is not allowed") from exc
        return resolved_target

    def _ensure_base_files(self, workspace_path: Path, plugin_name: str) -> None:
        """Create default plugin scaffold files when they do not exist."""
        main_file = workspace_path / "main.py"
        metadata_file = workspace_path / "metadata.yaml"
        requirements_file = workspace_path / "requirements.txt"

        # Prefer plugin template files when they are available.
        template_main = self.template_dir / "main.py"
        template_metadata = self.template_dir / "metadata.yaml"
        if template_main.exists() and not main_file.exists():
            shutil.copy2(template_main, main_file)
        if template_metadata.exists() and not metadata_file.exists():
            shutil.copy2(template_metadata, metadata_file)

        if not main_file.exists():
            main_file.write_text(
                (
                    '"""Main module for generated plugin."""\n'
                    "\n"
                    "# Generated workspace entry file.\n"
                ),
                encoding="utf-8",
            )
        if not metadata_file.exists():
            metadata_file.write_text(
                (
                    f"name: {plugin_name}\n"
                    "display_name: Generated Plugin\n"
                    "desc: Generated by dev workspace\n"
                    "version: v0.1.0\n"
                    "author: unknown\n"
                    'repo: ""\n'
                ),
                encoding="utf-8",
            )
        if not requirements_file.exists():
            requirements_file.write_text("", encoding="utf-8")

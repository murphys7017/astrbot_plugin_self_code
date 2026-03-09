"""Manual cache manager for AstrBot-Skill content."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from astrbot.api import logger

from .utils import get_runtime_root

SOURCE_REPO = "xunxiing/AstrBot-Skill"
SOURCE_BRANCH = "v4"
SOURCE_URL = f"https://github.com/{SOURCE_REPO}"


class SkillsCacheManager:
    """Manage local runtime cache for external skill documents."""

    def __init__(self, plugin_root: Path) -> None:
        self.plugin_root = plugin_root
        self.runtime_dir = get_runtime_root(self.plugin_root)
        self.cache_dir = self.runtime_dir / "skills_cache"
        self.snapshot_dir = self.cache_dir / "snapshot"
        self.meta_file = self.cache_dir / "meta.json"
        self.index_file = self.cache_dir / "index.json"

    def ensure_structure(self) -> None:
        """Ensure cache root directory exists."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def update_from_remote(self) -> dict[str, Any]:
        """Download remote skills repository and refresh cache snapshot."""
        self.ensure_structure()
        previous_meta = self.get_status()
        now = _utc_now()
        try:
            index_data = self._download_and_build_index()
            skills_count = len(index_data["skills"])
            files_count = sum(1 for path in self.snapshot_dir.rglob("*") if path.is_file())
            meta = {
                "enabled": True,
                "mode": "manual",
                "source_repo": SOURCE_REPO,
                "source_branch": SOURCE_BRANCH,
                "source_url": SOURCE_URL,
                "updated_at": now,
                "revision": self._fetch_branch_revision(),
                "files_count": files_count,
                "skills_count": skills_count,
                "last_error": "",
                "failed_at": "",
                "status": "ok",
            }
            self._write_json(self.index_file, index_data)
            self._write_json(self.meta_file, meta)
            return {
                "success": True,
                "message": (
                    f"Skills cache updated: {skills_count} skills, {files_count} files."
                ),
                "meta": meta,
            }
        except Exception as exc:  # pragma: no cover - runtime defensive path.
            logger.exception("Failed to update skills cache: %s", exc)
            failed_meta = {
                **previous_meta,
                "source_repo": previous_meta.get("source_repo", SOURCE_REPO),
                "source_branch": previous_meta.get("source_branch", SOURCE_BRANCH),
                "source_url": previous_meta.get("source_url", SOURCE_URL),
                "last_error": str(exc),
                "failed_at": now,
                "status": "error",
            }
            self._write_json(self.meta_file, failed_meta)
            return {
                "success": False,
                "message": f"Skills cache update failed: {exc}",
                "meta": failed_meta,
            }

    def get_status(self) -> dict[str, Any]:
        """Return current cache status metadata."""
        self.ensure_structure()
        raw_meta = self._read_json(self.meta_file, default={})
        if not isinstance(raw_meta, dict):
            raw_meta = {}
        defaults: dict[str, Any] = {
            "enabled": True,
            "mode": "manual",
            "source_repo": SOURCE_REPO,
            "source_branch": SOURCE_BRANCH,
            "source_url": SOURCE_URL,
            "updated_at": "",
            "revision": "",
            "files_count": 0,
            "skills_count": 0,
            "last_error": "",
            "failed_at": "",
            "status": "empty",
        }
        merged = {**defaults, **raw_meta}
        if self.snapshot_dir.exists():
            merged["files_count"] = sum(
                1 for path in self.snapshot_dir.rglob("*") if path.is_file()
            )
        else:
            merged["files_count"] = 0
        if self.index_file.exists():
            index_data = self._read_json(self.index_file, default={"skills": []})
            if isinstance(index_data, dict):
                skills = index_data.get("skills", [])
                if isinstance(skills, list):
                    merged["skills_count"] = len(skills)
        return merged

    def render_status_text(self) -> str:
        """Format status as human-readable text for command response."""
        status = self.get_status()
        updated_at = status.get("updated_at") or "未更新"
        failed_at = status.get("failed_at") or "-"
        revision = status.get("revision") or "-"
        error_text = status.get("last_error") or "-"
        return (
            "Skills 缓存状态\n"
            f"模式: {status.get('mode', 'manual')}\n"
            f"来源: {status.get('source_repo')}@{status.get('source_branch')}\n"
            f"最后成功更新时间: {updated_at}\n"
            f"最近失败时间: {failed_at}\n"
            f"Revision: {revision}\n"
            f"Skills 数量: {status.get('skills_count', 0)}\n"
            f"缓存文件数: {status.get('files_count', 0)}\n"
            f"最近错误: {error_text}\n"
            "更新命令: /codexdev skills update"
        )

    def build_prompt_summary(
        self,
        max_skills: int = 20,
        max_chars: int = 2800,
    ) -> str:
        """Build compact prompt summary from cached skills index."""
        status = self.get_status()
        if not self.index_file.exists():
            return (
                "Skills cache unavailable. Use `/codexdev skills update` to refresh "
                "AstrBot-Skill references."
            )
        index_data = self._read_json(self.index_file, default={"skills": []})
        if not isinstance(index_data, dict):
            return "Skills cache index is invalid."
        skills = index_data.get("skills", [])
        if not isinstance(skills, list) or not skills:
            return "Skills cache has no indexed skills yet."

        header = (
            "AstrBot-Skill cached references:\n"
            f"- source: {status.get('source_repo')}@{status.get('source_branch')}\n"
            f"- updated_at: {status.get('updated_at') or 'unknown'}\n"
            f"- revision: {status.get('revision') or 'unknown'}\n"
            f"- indexed_skills: {len(skills)}\n"
        )
        lines = [header, "Top skills summary:"]
        budget = max(200, max_chars - len(header))
        used = 0
        for item in skills[:max_skills]:
            if not isinstance(item, dict):
                continue
            rel_path = str(item.get("path", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            line = f"- {rel_path}: {snippet}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)

        remaining = max(0, len(skills) - max_skills)
        if remaining:
            lines.append(f"- ... and {remaining} more skills in cache.")
        return "\n".join(lines).strip()

    def _download_and_build_index(self) -> dict[str, Any]:
        """Download zip snapshot, replace local snapshot, then build index."""
        zip_url = (
            f"https://codeload.github.com/{SOURCE_REPO}/zip/refs/heads/{SOURCE_BRANCH}"
        )
        logger.info("Downloading skills zip from %s", zip_url)

        with tempfile.TemporaryDirectory(prefix="skills_cache_") as temp_root:
            temp_root_path = Path(temp_root)
            zip_path = temp_root_path / "skills.zip"
            extract_dir = temp_root_path / "extract"
            new_snapshot = temp_root_path / "snapshot"

            self._download_file(zip_url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(extract_dir)

            repo_root = self._detect_repo_root(extract_dir)
            source_skills_root = repo_root / "data" / "skills"
            if not source_skills_root.exists():
                raise FileNotFoundError(
                    "Missing `data/skills` in downloaded AstrBot-Skill snapshot."
                )
            shutil.copytree(source_skills_root, new_snapshot)

            if self.snapshot_dir.exists():
                shutil.rmtree(self.snapshot_dir)
            new_snapshot.replace(self.snapshot_dir)
            return self._build_index(self.snapshot_dir)

    def _build_index(self, snapshot_root: Path) -> dict[str, Any]:
        """Build compact index from all cached SKILL.md files."""
        skills: list[dict[str, str]] = []
        for skill_file in sorted(snapshot_root.rglob("SKILL.md")):
            rel_path = skill_file.relative_to(snapshot_root).as_posix()
            content = skill_file.read_text(encoding="utf-8", errors="replace")
            snippet = _compact_text(content, limit=220)
            skills.append({"path": rel_path, "snippet": snippet})
        return {"skills": skills}

    def _fetch_branch_revision(self) -> str:
        """Best-effort fetch of current branch head SHA via GitHub API."""
        api_url = f"https://api.github.com/repos/{SOURCE_REPO}/commits/{SOURCE_BRANCH}"
        request = Request(
            api_url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "astrbot"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            sha = payload.get("sha")
            if isinstance(sha, str) and sha:
                return sha[:12]
        except Exception:
            return ""
        return ""

    def _download_file(self, url: str, output_path: Path) -> None:
        """Download a URL to file path with explicit error mapping."""
        request = Request(url, headers={"User-Agent": "astrbot"})
        try:
            with urlopen(request, timeout=30) as response, output_path.open("wb") as fd:
                fd.write(response.read())
        except URLError as exc:
            raise RuntimeError(f"Network error while downloading {url}: {exc}") from exc

    def _detect_repo_root(self, extract_dir: Path) -> Path:
        """Detect repository root from extracted zip directory layout."""
        candidates = [path for path in extract_dir.iterdir() if path.is_dir()]
        if len(candidates) != 1:
            raise RuntimeError("Unexpected zip layout while reading AstrBot-Skill.")
        return candidates[0]

    def _read_json(self, path: Path, default: Any) -> Any:
        """Read JSON file safely and return default on failure."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, value: Any) -> None:
        """Write JSON using atomic replace."""
        temp_file = path.with_suffix(path.suffix + ".tmp")
        temp_file.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_file.replace(path)


def _utc_now() -> str:
    """Return current UTC timestamp string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _compact_text(text: str, limit: int) -> str:
    """Compact text into one-line snippet with max char limit."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."

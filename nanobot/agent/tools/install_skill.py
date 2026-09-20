"""Install a completed skill into the profile's personal skill library."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig
from nanobot.agent.tools.path_utils import resolve_workspace_path
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.security.workspace_access import current_tool_workspace
from nanobot.security.workspace_policy import is_path_within


@tool_parameters(tool_parameters_schema(
    source_dir=StringSchema("Completed skill folder in the current project, containing SKILL.md and any resources"),
    required=["source_dir"],
))
class InstallSkillTool(Tool):
    """Narrow write capability for My Skills, independent of the active project."""

    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.file.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            Path(ctx.workspace), restrict_to_workspace=ctx.config.restrict_to_workspace,
            sandbox_restricts_workspace=bool(ctx.config.exec.sandbox),
        )

    def __init__(
        self, workspace: Path, restrict_to_workspace: bool = False,
        sandbox_restricts_workspace: bool = False,
    ):
        self.workspace = workspace.expanduser().resolve()
        self.restrict_to_workspace = restrict_to_workspace
        self.sandbox_restricts_workspace = sandbox_restricts_workspace

    @property
    def name(self) -> str:
        return "install_skill"

    @property
    def description(self) -> str:
        return (
            "Install a completed user-created skill into My Skills, the personal library shared "
            "across projects. Use this by default after creating a skill, unless the user explicitly "
            "wants only project files or an export. Copies SKILL.md and all bundled resources, "
            "preserves the source, and refuses to overwrite existing or built-in skills. "
            "The installed skill appears in Toolbox > Skills > My Skills; project bindings remain unchanged."
        )

    def _plan(self, source_dir: str) -> tuple[Path, Path, list[Path]]:
        if not isinstance(source_dir, str) or not source_dir.strip():
            raise ValueError("source_dir is required")
        access = current_tool_workspace(
            self.workspace, restrict_to_workspace=self.restrict_to_workspace,
            sandbox_restricts_workspace=self.sandbox_restricts_workspace,
        )
        source = resolve_workspace_path(
            source_dir, access.project_path, access.allowed_root, include_media_dir=False,
        )
        if not source.is_dir():
            raise ValueError("source_dir must be an existing skill directory")
        name = source.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("Invalid skill directory name")
        library = self.workspace / "skills"
        if library.is_symlink():
            raise ValueError("My Skills directory must not be a symlink")
        target = library / name
        if not is_path_within(target, library) or target.is_symlink():
            raise ValueError("Skill destination escapes My Skills")
        if (BUILTIN_SKILLS_DIR / name).exists():
            raise ValueError(f"A built-in skill named {name} already exists; use a different name")
        if target.exists():
            raise ValueError(f"My Skills already contains {name}; existing files were preserved")
        files = []
        total_size = 0
        for item in source.rglob("*"):
            if item.is_symlink() or not is_path_within(item, source):
                raise ValueError("Skill packages must not contain symlinks")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ValueError("Skill packages may contain only regular files")
            files.append(item.relative_to(source))
            total_size += item.stat().st_size
            if len(files) > 200 or total_size > 20 * 1024 * 1024:
                raise ValueError("Skill package exceeds 200 files or 20 MiB")
        if Path("SKILL.md") not in files:
            raise ValueError("Skill package must contain SKILL.md")
        content = (source / "SKILL.md").read_text(encoding="utf-8-sig")
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.DOTALL)
        try:
            metadata = yaml.safe_load(match.group(1)) if match else None
        except yaml.YAMLError as exc:
            raise ValueError("SKILL.md contains invalid YAML frontmatter") from exc
        if not isinstance(metadata, dict) or metadata.get("name") != name:
            raise ValueError("SKILL.md frontmatter name must match its directory name")
        if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            raise ValueError("SKILL.md needs a non-empty description")
        return source, target, files

    def destination_paths(self, source_dir: str) -> list[Path]:
        """Expose exact write targets for the existing safety and audit policy."""
        _, target, files = self._plan(source_dir)
        return [target, *(target / relative for relative in files)]

    async def execute(self, source_dir: str, **_: Any) -> str:
        try:
            source, target, files = self._plan(source_dir)
            target.mkdir(parents=True, exist_ok=False)
            try:
                # Publish SKILL.md last so discovery cannot load an incomplete package.
                for relative in sorted(files, key=lambda path: path == Path("SKILL.md")):
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / relative, destination)
            except Exception:
                shutil.rmtree(target)
                raise
            return json.dumps({
                "ok": True, "name": target.name, "source": "workspace",
                "path": str(target / "SKILL.md"),
                "message": "Installed in Toolbox > Skills > My Skills. Select it in a conversation or bind it to a project to use it.",
            }, ensure_ascii=False)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return f"Error installing skill: {exc}"

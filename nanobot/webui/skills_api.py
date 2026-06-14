"""WebUI API helpers for nanobot skills."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.config.loader import load_config, save_config

QueryParams = dict[str, list[str]]

_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WebUISkillsError(ValueError):
    """User-facing skills API validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def webui_skills_payload(
    workspace_path: Path,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any]:
    """Return agent skills without leaking local filesystem paths."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = sorted(
        loader.list_skills(filter_unavailable=False),
        key=lambda entry: (entry.get("source") != "workspace", entry["name"]),
    )
    return {"skills": [_skill_payload(loader, entry) for entry in entries]}


def webui_skill_detail_payload(
    workspace_path: Path,
    name: str,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return a single skill's safe detail payload."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = loader.list_skills(filter_unavailable=False)
    entry = next((item for item in entries if item["name"] == name), None)
    if entry is None:
        return None
    return {
        **_skill_payload(loader, entry),
        "requirements": loader.get_skill_requirements(name),
        "raw_markdown": loader.load_skill(name) or "",
    }


def _skill_payload(loader: SkillsLoader, entry: dict[str, str]) -> dict[str, Any]:
    name = entry["name"]
    metadata = loader.get_skill_metadata(name)
    available, unavailable_reason = loader.get_skill_availability(name)
    return {
        "name": name,
        "description": _description(metadata, name),
        "source": entry.get("source", "unknown"),
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


def _description(metadata: dict[str, Any] | None, fallback: str) -> str:
    if metadata is None:
        return fallback
    value = metadata.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _skill_name(query: QueryParams) -> str:
    name = (_query_first(query, "name") or "").strip()
    if not _SKILL_NAME_RE.match(name):
        raise WebUISkillsError("invalid skill name")
    return name


def _loader() -> SkillsLoader:
    config = load_config()
    return SkillsLoader(
        config.workspace_path,
        disabled_skills=set(config.agents.defaults.disabled_skills),
    )


def _metadata_tags(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags")
    if isinstance(tags, list):
        return [str(tag) for tag in tags if str(tag).strip()]
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    return []


def _skill_info(loader: SkillsLoader, entry: dict[str, str]) -> dict[str, Any]:
    name = entry["name"]
    meta = loader.get_skill_metadata(name) or {}
    nanobot_meta = loader._get_skill_meta(name)
    available = loader._check_requirements(nanobot_meta)
    missing = loader._get_missing_requirements(nanobot_meta)
    return {
        "name": name,
        "description": str(meta.get("description") or loader._get_skill_description(name)),
        "path": entry["path"],
        "source": entry["source"],
        "enabled": name not in loader.disabled_skills,
        "available": available,
        "missing": missing,
        "user_invocable": bool(meta.get("user_invocable", True)),
        "always": bool(meta.get("always") or nanobot_meta.get("always")),
        "tags": _metadata_tags(meta),
        "metadata": meta,
    }


def skills_payload(*, include_content: bool = False, name: str | None = None) -> dict[str, Any]:
    loader = _loader()
    entries = loader.list_skills(filter_unavailable=False)
    skills = [_skill_info(loader, entry) for entry in entries]
    skills.sort(key=lambda item: (item["source"] != "builtin", item["name"]))
    if name is not None:
        skills = [skill for skill in skills if skill["name"] == name]
        if not skills:
            raise WebUISkillsError("unknown skill", status=404)
    if include_content:
        for skill in skills:
            skill["content"] = loader.load_skill(skill["name"]) or ""
    return {
        "skills": skills,
        "disabled": sorted(loader.disabled_skills),
        "installed_count": sum(1 for skill in skills if skill["enabled"]),
    }


def skills_action(action: str, query: QueryParams) -> dict[str, Any]:
    name = _skill_name(query)
    config = load_config()
    disabled = set(config.agents.defaults.disabled_skills)

    if action == "detail":
        return skills_payload(include_content=True, name=name)

    if action == "enable":
        loader = _loader()
        if not any(entry["name"] == name for entry in loader.list_skills(filter_unavailable=False)):
            raise WebUISkillsError("unknown skill", status=404)
        disabled.discard(name)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        payload = skills_payload()
        payload["last_action"] = {"ok": True, "message": f"Enabled skill: {name}"}
        return payload

    if action == "disable":
        loader = _loader()
        if not any(entry["name"] == name for entry in loader.list_skills(filter_unavailable=False)):
            raise WebUISkillsError("unknown skill", status=404)
        disabled.add(name)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        payload = skills_payload()
        payload["last_action"] = {"ok": True, "message": f"Disabled skill: {name}"}
        return payload

    if action == "delete":
        loader = _loader()
        entries = [entry for entry in loader.list_skills(filter_unavailable=False) if entry["name"] == name]
        if not entries:
            raise WebUISkillsError("unknown skill", status=404)
        entry = entries[0]
        if entry["source"] != "workspace":
            raise WebUISkillsError("builtin skills cannot be deleted", status=400)
        skill_path = Path(entry["path"]).resolve()
        workspace_skills = loader.workspace_skills.resolve()
        if workspace_skills not in skill_path.parents:
            raise WebUISkillsError("refusing to delete skill outside workspace", status=400)
        shutil.rmtree(skill_path.parent)
        disabled.discard(name)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        payload = skills_payload()
        payload["last_action"] = {"ok": True, "message": f"Deleted skill: {name}"}
        return payload

    if action == "save":
        content = _query_first(query, "content")
        if not content or not content.strip():
            raise WebUISkillsError("skill content is required")
        loader = _loader()
        target_dir = loader.workspace_skills / name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(content, encoding="utf-8")
        disabled.discard(name)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        payload = skills_payload()
        payload["last_action"] = {"ok": True, "message": f"Saved skill: {name}"}
        return payload

    raise WebUISkillsError(f"unknown skills action '{action}'", status=404)

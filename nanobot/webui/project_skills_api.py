"""WebUI project-level skill grants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.config.loader import load_config

QueryParams = dict[str, list[str]]


class WebUIProjectSkillsError(ValueError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _project_key(raw: str | None) -> str:
    if not raw or not raw.strip():
        raise WebUIProjectSkillsError("project_path is required")
    return str(Path(raw).expanduser().resolve(strict=False))


def _store_path(workspace: Path) -> Path:
    path = workspace / ".nanobot" / "project-skill-grants.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_store(workspace: Path) -> dict[str, list[str]]:
    path = _store_path(workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): [str(item) for item in value if str(item).strip()]
        for key, value in payload.items()
        if isinstance(value, list)
    }


def _write_store(workspace: Path, store: dict[str, list[str]]) -> None:
    _store_path(workspace).write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _workspace_skill_names(workspace: Path) -> set[str]:
    loader = SkillsLoader(workspace)
    return {
        entry["name"]
        for entry in loader.list_skills(filter_unavailable=False)
        if entry.get("source") == "workspace"
    }


def project_skill_grants(workspace: Path, project_path: str | Path | None) -> list[str]:
    if project_path is None:
        return []
    store = _read_store(workspace)
    names = store.get(_project_key(str(project_path)), [])
    existing = _workspace_skill_names(workspace)
    return [name for name in names if name in existing]


def remove_project_skill_grant_everywhere(workspace: Path, skill_name: str) -> None:
    store = _read_store(workspace)
    next_store = {
        project: [name for name in names if name != skill_name]
        for project, names in store.items()
    }
    if next_store != store:
        _write_store(workspace, next_store)


def project_skills_payload(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    project = _project_key(_query_first(query, "project_path"))
    return {
        "project_path": project,
        "skills": project_skill_grants(config.workspace_path, project),
    }


def project_skills_save(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    project = _project_key(_query_first(query, "project_path"))
    raw_skills = _query_first(query, "skills") or "[]"
    try:
        requested = json.loads(raw_skills)
    except json.JSONDecodeError as exc:
        raise WebUIProjectSkillsError("invalid skills payload") from exc
    if not isinstance(requested, list):
        raise WebUIProjectSkillsError("skills must be a list")

    existing = _workspace_skill_names(config.workspace_path)
    names = []
    seen = set()
    for item in requested:
        name = str(item).strip()
        if not name or name in seen or name not in existing:
            continue
        seen.add(name)
        names.append(name)

    store = _read_store(config.workspace_path)
    store[project] = names
    _write_store(config.workspace_path, store)
    return {"project_path": project, "skills": names}

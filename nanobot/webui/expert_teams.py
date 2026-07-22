"""Expert-team resource discovery and runtime prompt adaptation."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

EXPERT_TEAM_SESSION_KEY = "expert_team"
_TEAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ExpertTeamError(ValueError):
    """Safe user-facing expert-team validation error."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _teams_root() -> Path | None:
    raw = os.environ.get("NANOBOT_EXPERT_TEAMS_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    return root if root.is_dir() else None


def _safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ExpertTeamError("expert team resource escapes its package")
    return path


@lru_cache(maxsize=32)
def _load_team(team_id: str, root_text: str) -> dict[str, Any]:
    root = Path(root_text)
    manifest_path = _safe_child(root, f"{team_id}/team.yaml")
    if not manifest_path.is_file():
        raise ExpertTeamError("unknown expert team", status=404)
    try:
        parsed = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExpertTeamError("invalid expert team manifest", status=500) from exc
    if not isinstance(parsed, dict) or parsed.get("id") != team_id:
        raise ExpertTeamError("invalid expert team manifest", status=500)
    return parsed


def _manifest(team_id: str) -> tuple[Path, dict[str, Any]]:
    if not _TEAM_ID_RE.fullmatch(team_id):
        raise ExpertTeamError("invalid expert team id")
    root = _teams_root()
    if root is None:
        raise ExpertTeamError("expert teams are unavailable", status=503)
    return root, _load_team(team_id, str(root))


def _members(raw: Any, source_root: Path | None = None) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        member_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not member_id or not name:
            continue
        member = {
            "id": member_id,
            "name": name,
            "framework": str(item.get("framework") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "phase": str(item.get("phase") or "").strip(),
            "phase_label": str(item.get("phase_label") or "").strip(),
        }
        playbook = str(item.get("playbook") or "").strip()
        if source_root is not None and playbook:
            try:
                text = _safe_child(source_root, playbook).read_text(encoding="utf-8").strip()
            except (ExpertTeamError, OSError):
                text = ""
            if text:
                member["instructions"] = text
        out.append(member)
    return out


def _lead_playbooks(source_root: Path, runtime: Mapping[str, Any]) -> list[str]:
    raw = runtime.get("lead_playbooks")
    if not isinstance(raw, list):
        return []
    playbooks: list[str] = []
    for relative in raw[:6]:
        if not isinstance(relative, str) or not relative.strip():
            continue
        try:
            path = _safe_child(source_root, relative)
            text = path.read_text(encoding="utf-8").strip()
        except (ExpertTeamError, OSError):
            continue
        if text:
            playbooks.append(text)
    return playbooks


def _workflows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        workflow_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        source = str(item.get("source") or "").strip()
        if not workflow_id or not name or not source:
            continue
        out.append({
            "id": workflow_id,
            "name": name,
            "description": str(item.get("description") or "").strip(),
            "source": source,
            "mode": "team" if item.get("mode") == "team" else "lead",
            "featured": item.get("featured") is True,
        })
    return out


def _data_sources(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        skill = str(item.get("skill") or "").strip()
        if not source_id or not name or not skill:
            continue
        raw_assignments = item.get("assignments")
        assignments = {
            str(role).strip(): str(description).strip()
            for role, description in raw_assignments.items()
            if str(role).strip() and str(description).strip()
        } if isinstance(raw_assignments, dict) else {}
        out.append({
            "id": source_id,
            "name": name,
            "skill": skill,
            "priority": "primary" if item.get("priority") == "primary" else "supplemental",
            "required": item.get("required") is True,
            "description": str(item.get("description") or "").strip(),
            "assignments": assignments,
        })
    return out


def _completion(runtime: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = runtime.get("completion")
    if not isinstance(raw, Mapping):
        return None
    required_tools = [
        str(item).strip()
        for item in raw.get("required_tools", [])
        if isinstance(item, str) and str(item).strip()
    ][:8]
    required_artifacts = [
        str(item).strip().lower().lstrip(".")
        for item in raw.get("required_artifacts", [])
        if isinstance(item, str) and str(item).strip()
    ][:8]
    instruction = str(raw.get("instruction") or "").strip()
    if not required_tools:
        return None
    return {
        "required_tools": list(dict.fromkeys(required_tools)),
        "required_artifacts": list(dict.fromkeys(required_artifacts)),
        **({"instruction": instruction} if instruction else {}),
    }


def _availability(team_root: Path, manifest: Mapping[str, Any]) -> tuple[bool, str]:
    source_root = str(manifest.get("source_root") or "").strip()
    adapter = str((manifest.get("runtime") or {}).get("adapter") or "").strip()
    required = ["team.yaml", source_root, adapter]
    for relative in required:
        if not relative or not _safe_child(team_root, relative).exists():
            return False, f"missing resource: {relative or 'unknown'}"
    return True, ""


def _summary(team_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    source_root = _safe_child(team_root, str(manifest.get("source_root") or ""))
    members = _members(manifest.get("members"), source_root)
    workflows = _workflows(manifest.get("workflows"))
    available, reason = _availability(team_root, manifest)
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    return {
        "id": str(manifest.get("id") or ""),
        "name": str(manifest.get("name") or ""),
        "description": str(manifest.get("description") or ""),
        "version": str(manifest.get("version") or "1.0.0"),
        "enabled": manifest.get("enabled") is not False,
        "available": available,
        "unavailable_reason": reason,
        "cover": str(manifest.get("cover") or ""),
        "member_count": len(members),
        "workflow_count": len(workflows),
        "data_source_count": len(_data_sources(manifest.get("data_sources"))),
        "tags": [str(tag) for tag in manifest.get("tags", []) if str(tag).strip()],
        "requested_concurrency": max(1, min(4, int(runtime.get("requested_concurrency") or 1))),
    }


def expert_teams_payload() -> dict[str, Any]:
    root = _teams_root()
    if root is None:
        return {"teams": []}
    teams: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/team.yaml")):
        team_id = manifest_path.parent.name
        if not _TEAM_ID_RE.fullmatch(team_id):
            continue
        try:
            _, manifest = _manifest(team_id)
            teams.append(_summary(manifest_path.parent, manifest))
        except ExpertTeamError:
            continue
    return {"teams": teams}


def expert_team_detail_payload(team_id: str) -> dict[str, Any]:
    root, manifest = _manifest(team_id)
    team_root = _safe_child(root, team_id)
    summary = _summary(team_root, manifest)
    source_root = _safe_child(team_root, str(manifest.get("source_root") or ""))
    optional = [{
        "name": "Playwright（雪球观点抓取）",
        "available": False,
        "reason": "可选依赖，未检测运行时浏览器；不影响核心投研工作流",
    }]
    try:
        import playwright  # type: ignore  # noqa: F401
        optional[0] = {"name": "Playwright（雪球观点抓取）", "available": True}
    except ImportError:
        pass
    return {
        **summary,
        "members": _members(manifest.get("members")),
        "workflows": _workflows(manifest.get("workflows")),
        "data_sources": _data_sources(manifest.get("data_sources")),
        "optional_dependencies": optional,
        "source_available": source_root.is_dir(),
    }


def normalize_expert_team_binding(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ExpertTeamError("invalid expert team binding")
    team_id = str(raw.get("id") or "").strip()
    root, manifest = _manifest(team_id)
    team_root = _safe_child(root, team_id)
    summary = _summary(team_root, manifest)
    source_root = _safe_child(team_root, str(manifest.get("source_root") or ""))
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    completion = _completion(runtime)
    if not summary["enabled"] or not summary["available"]:
        raise ExpertTeamError(summary["unavailable_reason"] or "expert team is unavailable", status=409)
    return {
        "id": team_id,
        "name": summary["name"],
        "version": summary["version"],
        "requested_concurrency": summary["requested_concurrency"],
        "members": [
            {
                "id": member["id"],
                "name": member["name"],
                "framework": member["framework"],
                "description": member["description"],
                "phase": member["phase"],
                "phase_label": member["phase_label"],
                **({"instructions": member["instructions"]} if member.get("instructions") else {}),
            }
            for member in _members(manifest.get("members"), source_root)
        ],
        "data_sources": _data_sources(manifest.get("data_sources")),
        **({"completion": completion} if completion is not None else {}),
    }


def public_expert_team_binding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    team_id = raw.get("id")
    if not isinstance(team_id, str):
        return None
    try:
        binding = normalize_expert_team_binding({"id": team_id})
    except ExpertTeamError:
        return None
    if binding is None:
        return None
    return {
        "id": binding["id"],
        "name": binding["name"],
        "version": binding["version"],
        "member_count": len(binding["members"]),
    }


def expert_team_system_prompt(session_metadata: Mapping[str, Any] | None) -> str:
    if not isinstance(session_metadata, Mapping):
        return ""
    raw = session_metadata.get(EXPERT_TEAM_SESSION_KEY)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
        return ""
    try:
        root, manifest = _manifest(raw["id"])
        team_root = _safe_child(root, raw["id"])
        runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
        adapter_path = _safe_child(team_root, str(runtime.get("adapter") or ""))
        source_root = _safe_child(team_root, str(manifest.get("source_root") or ""))
        workflow_id = str(manifest.get("entry_workflow") or "")
        workflow = next((item for item in _workflows(manifest.get("workflows")) if item["id"] == workflow_id), None)
        if workflow is None:
            return ""
        workflow_path = _safe_child(source_root, workflow["source"])
        adapter = adapter_path.read_text(encoding="utf-8")
        workflow_text = workflow_path.read_text(encoding="utf-8")
        lead_playbooks = _lead_playbooks(source_root, runtime)
    except (ExpertTeamError, OSError):
        return ""
    return (
        f"# Active Expert Team: {manifest.get('name')}\n\n"
        f"Team resource root (read-only): `{source_root}`\n\n"
        f"# Canonical Entry Workflow: {workflow['name']}\n\n{workflow_text}\n\n"
        f"---\n\n# Lead Method Playbooks\n\n{'\n\n---\n\n'.join(lead_playbooks)}\n\n"
        f"---\n\n# Nanobot Runtime Compatibility Overrides (higher priority)\n\n{adapter}\n\n"
        "The compatibility overrides above are authoritative for runtime/tool/permission semantics. "
        "Do not perform Claude Code permission checks from the canonical workflow."
    )

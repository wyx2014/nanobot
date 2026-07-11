"""Turn-local skill access scope."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping

_CURRENT_ALLOWED_WORKSPACE_SKILLS: ContextVar[set[str] | None] = ContextVar(
    "nanobot_allowed_workspace_skills",
    default=None,
)


def allowed_workspace_skills_from_scope(scope: Mapping[str, Any] | None) -> set[str] | None:
    if not isinstance(scope, Mapping):
        return None
    names: set[str] = set()
    raw = scope.get("project_bound_user_skills")
    if not isinstance(raw, list):
        return names
    names.update(str(item).strip() for item in raw if str(item).strip())
    return names


def explicit_skills_from_scope(scope: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(scope, Mapping):
        return []
    raw = scope.get("explicit_skills")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def bind_allowed_workspace_skills(scope: Mapping[str, Any] | None) -> Token[set[str] | None]:
    return _CURRENT_ALLOWED_WORKSPACE_SKILLS.set(allowed_workspace_skills_from_scope(scope))


def reset_allowed_workspace_skills(token: Token[set[str] | None]) -> None:
    _CURRENT_ALLOWED_WORKSPACE_SKILLS.reset(token)


def current_allowed_workspace_skills() -> set[str] | None:
    return _CURRENT_ALLOWED_WORKSPACE_SKILLS.get()

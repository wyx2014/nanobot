"""Stable project identity bound to one agent execution context."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_CONTEXT_METADATA_KEY = "_project_context"

_CURRENT_PROJECT_CONTEXT: ContextVar["ProjectContext | None"] = ContextVar(
    "nanobot_project_context",
    default=None,
)


@dataclass(frozen=True)
class ProjectContext:
    """Database identity and filesystem root for one project-scoped session."""

    project_id: str
    session_id: str
    session_key: str
    root_path: Path

    def metadata(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "session_id": self.session_id,
            "session_key": self.session_key,
        }


def project_context_from_metadata(
    raw: Any,
    *,
    session_key: str,
    root_path: str | Path,
) -> ProjectContext | None:
    if not isinstance(raw, dict):
        return None
    project_id = raw.get("project_id")
    session_id = raw.get("session_id")
    stored_session_key = raw.get("session_key")
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if stored_session_key not in (None, session_key):
        return None
    return ProjectContext(
        project_id=project_id.strip(),
        session_id=session_id.strip(),
        session_key=session_key,
        root_path=Path(root_path).expanduser().resolve(strict=False),
    )


def bind_project_context(
    context: ProjectContext | None,
) -> Token[ProjectContext | None]:
    return _CURRENT_PROJECT_CONTEXT.set(context)


def reset_project_context(token: Token[ProjectContext | None]) -> None:
    _CURRENT_PROJECT_CONTEXT.reset(token)


def current_project_context() -> ProjectContext | None:
    return _CURRENT_PROJECT_CONTEXT.get()


def require_project_context() -> ProjectContext:
    context = current_project_context()
    if context is None:
        raise RuntimeError("project context is required")
    return context

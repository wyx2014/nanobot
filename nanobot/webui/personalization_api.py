"""WebUI API helpers for workspace personalization files (SOUL.md / USER.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.config.loader import load_config
from nanobot.utils.helpers import load_bundled_template

# Keep SOUL.md / USER.md bounded so the per-turn bootstrap context stays small.
_MAX_PERSONALIZATION_CHARS = 32_000
# Mirror the skills values header limit used by other settings payloads.
_MAX_PERSONALIZATION_HEADER_BYTES = 512 * 1024


class PersonalizationError(ValueError):
    """User-facing personalization API validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _workspace() -> Path:
    config = load_config()
    return Path(config.workspace_path).expanduser().resolve()


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def personalization_payload() -> dict[str, Any]:
    """Return the current SOUL.md / USER.md contents."""
    workspace = _workspace()
    return {
        "soul": _read_file(workspace / "SOUL.md"),
        "user": _read_file(workspace / "USER.md"),
    }


def save_personalization(*, soul: str | None = None, user: str | None = None) -> dict[str, Any]:
    """Write SOUL.md / USER.md (only the provided files are touched)."""
    workspace = _workspace()
    if soul is not None:
        if len(soul) > _MAX_PERSONALIZATION_CHARS:
            raise PersonalizationError(
                f"SOUL.md is too large (max {_MAX_PERSONALIZATION_CHARS} characters)"
            )
        (workspace / "SOUL.md").write_text(soul, encoding="utf-8")
    if user is not None:
        if len(user) > _MAX_PERSONALIZATION_CHARS:
            raise PersonalizationError(
                f"USER.md is too large (max {_MAX_PERSONALIZATION_CHARS} characters)"
            )
        (workspace / "USER.md").write_text(user, encoding="utf-8")
    return personalization_payload()


def restore_personalization(kind: str) -> dict[str, Any]:
    """Restore the bundled template for one personalization file."""
    filename = "SOUL.md" if kind == "soul" else "USER.md" if kind == "user" else None
    if filename is None:
        raise PersonalizationError("unknown personalization file", status=400)
    template = load_bundled_template(filename)
    if template is None:
        raise PersonalizationError("bundled template unavailable", status=500)
    (workspace := _workspace() / filename).write_text(template, encoding="utf-8")
    return personalization_payload()

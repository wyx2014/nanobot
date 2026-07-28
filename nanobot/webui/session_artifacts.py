"""Workspace-bound artifact discovery and delivery for WebUI sessions."""

from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from nanobot.security.workspace_access import WorkspaceScope
from nanobot.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from nanobot.webui.transcript import read_transcript_lines

if TYPE_CHECKING:
    from nanobot.storage.state import ArtifactRecord

MAX_SESSION_ARTIFACTS = 100
MAX_ARTIFACT_CONTENT_BYTES = 64 * 1024 * 1024
MAX_SCANNED_ARTIFACT_FILES = 20_000
MAX_SCANNED_ARTIFACT_DIRS = 4_000
_SESSION_CLOCK_TOLERANCE_S = 2.0
_EXCLUDED_DIR_NAMES = frozenset({
    ".git",
    ".hg",
    ".nanobot",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "embedded-python",
    "memory",
    "node_modules",
    "sessions",
    "skills",
    "venv",
})
_DOCUMENT_EXTENSIONS = frozenset({
    ".doc",
    ".docx",
    ".epub",
    ".md",
    ".odt",
    ".pdf",
    ".rtf",
    ".txt",
})
_SPREADSHEET_EXTENSIONS = frozenset({".csv", ".ods", ".tsv", ".xls", ".xlsx"})
_PRESENTATION_EXTENSIONS = frozenset({".odp", ".ppt", ".pptx"})
_IMAGE_EXTENSIONS = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})
_ARCHIVE_EXTENSIONS = frozenset({".7z", ".gz", ".rar", ".tar", ".tgz", ".zip"})
_CODE_EXTENSIONS = frozenset({
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
})


class SessionArtifactError(ValueError):
    """Raised when a session artifact request is invalid or unsafe."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def discover_session_artifacts(
    session_key: str,
    session_data: dict[str, Any],
    *,
    scope: WorkspaceScope,
    max_items: int = MAX_SESSION_ARTIFACTS,
) -> dict[str, Any]:
    """Return files created/changed during a session plus explicit output paths.

    The scan is intentionally session-scoped rather than a workspace browser:
    old files are omitted unless a durable tool/file-edit record explicitly
    references them. Internal/build/vendor directories are always pruned.
    """

    root = scope.project_path.expanduser().resolve(strict=False)
    if not root.is_dir():
        return {"artifacts": [], "truncated": False}

    threshold = _session_started_timestamp(session_data)
    explicit = _explicit_session_paths(session_key, session_data, root)
    candidates: dict[str, tuple[Path, os.stat_result]] = {}
    scanned_files = 0
    scanned_dirs = 0
    scan_truncated = False

    for candidate in explicit:
        _add_candidate(candidates, candidate, root)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        scanned_dirs += 1
        if scanned_dirs > MAX_SCANNED_ARTIFACT_DIRS:
            scan_truncated = True
            break
        dirnames[:] = [
            name
            for name in dirnames
            if not _excluded_name(name)
        ]
        parent = Path(dirpath)
        for filename in filenames:
            scanned_files += 1
            if scanned_files > MAX_SCANNED_ARTIFACT_FILES:
                scan_truncated = True
                break
            if _excluded_name(filename):
                continue
            candidate = parent / filename
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if stat.st_mtime + _SESSION_CLOCK_TOLERANCE_S < threshold:
                continue
            _add_candidate(candidates, candidate, root, stat=stat)
        if scan_truncated:
            break

    ordered = sorted(
        candidates.values(),
        key=lambda item: (item[1].st_mtime, item[0].as_posix()),
        reverse=True,
    )
    truncated = scan_truncated or len(ordered) > max_items
    rows = [
        artifact_row(path, stat, root=root, session_key=session_key)
        for path, stat in ordered[:max_items]
    ]
    return {"artifacts": rows, "truncated": truncated}


def explicit_artifact_row(
    raw_path: str | None,
    *,
    scope: WorkspaceScope,
    session_key: str,
) -> dict[str, Any] | None:
    """Return a safe row for one file explicitly produced by a tool event."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    root = scope.project_path.expanduser().resolve(strict=False)
    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidates: dict[str, tuple[Path, os.stat_result]] = {}
    _add_candidate(candidates, candidate, root)
    if not candidates:
        return None
    path, stat = next(iter(candidates.values()))
    return artifact_row(path, stat, root=root, session_key=session_key)


def resolve_session_artifact(
    raw_path: str | None,
    *,
    session_key: str,
    session_data: dict[str, Any],
    scope: WorkspaceScope,
) -> Path:
    """Resolve one listed artifact, rechecking eligibility and containment."""

    relative = (raw_path or "").strip()
    if not relative:
        raise SessionArtifactError(400, "missing path")
    if len(relative) > 4096 or "\0" in relative:
        raise SessionArtifactError(400, "invalid path")

    root = scope.project_path.expanduser().resolve(strict=False)
    try:
        resolved = resolve_allowed_path(
            relative,
            workspace=root,
            allowed_root=root,
            strict=True,
        )
    except FileNotFoundError as exc:
        raise SessionArtifactError(404, "artifact not found") from exc
    except WorkspaceBoundaryError as exc:
        raise SessionArtifactError(403, "artifact is outside the current workspace") from exc
    except OSError as exc:
        raise SessionArtifactError(400, "invalid path") from exc

    if not resolved.is_file() or _path_is_excluded(resolved, root):
        raise SessionArtifactError(404, "artifact not found")

    payload = discover_session_artifacts(session_key, session_data, scope=scope)
    eligible = {
        str(row.get("path"))
        for row in payload["artifacts"]
        if isinstance(row, dict)
    }
    try:
        display_path = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise SessionArtifactError(403, "artifact is outside the current workspace") from exc
    if display_path not in eligible:
        raise SessionArtifactError(404, "artifact not found")
    return resolved


def read_session_artifact(
    path: Path,
    *,
    root: Path,
    max_bytes: int = MAX_ARTIFACT_CONTENT_BYTES,
) -> bytes:
    """Read a bounded artifact body for the embedded HTTP server."""

    try:
        safe_root = root.expanduser().resolve(strict=True)
        resolved = path.expanduser().resolve(strict=True)
        resolved.relative_to(safe_root)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(resolved, flags)
    except (OSError, ValueError) as exc:
        raise SessionArtifactError(404, "artifact not found") from exc
    try:
        with os.fdopen(fd, "rb") as artifact_file:
            stat = os.fstat(artifact_file.fileno())
            if stat.st_size > max_bytes:
                raise SessionArtifactError(
                    413,
                    "artifact is too large to preview or download",
                )
            body = artifact_file.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SessionArtifactError(
                    413,
                    "artifact is too large to preview or download",
                )
            return body
    except SessionArtifactError:
        raise
    except OSError as exc:
        raise SessionArtifactError(500, "failed to read artifact") from exc


def artifact_row(
    path: Path,
    stat: os.stat_result,
    *,
    root: Path,
    session_key: str,
) -> dict[str, Any]:
    """Serialize one artifact without exposing its absolute filesystem path."""

    relative = path.relative_to(root).as_posix()
    encoded_key = quote(session_key, safe="")
    encoded_path = quote(relative, safe="")
    content_url = (
        f"/api/sessions/{encoded_key}/artifacts/content?path={encoded_path}"
    )
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {
        "path": relative,
        "name": path.name,
        "kind": artifact_kind(path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "mime_type": mime_type,
        "preview_url": content_url,
        "download_url": f"{content_url}&download=1",
        "reveal_path": relative,
    }


def registered_artifact_row(artifact: ArtifactRecord) -> dict[str, Any]:
    """Serialize one explicitly registered artifact for WebUI clients."""

    encoded_id = quote(artifact.id, safe="")
    encoded_session_key = quote(artifact.session_key, safe="")
    content_url = (
        f"/api/artifacts/{encoded_id}/content"
        f"?session={encoded_session_key}"
    )
    modified_at_ms = artifact.ready_at or artifact.updated_at
    return {
        "id": artifact.id,
        "project_id": artifact.project_id,
        "session_id": artifact.session_id,
        "status": artifact.status,
        "path": artifact.relative_path,
        "name": artifact.display_name,
        "kind": artifact.artifact_kind,
        "size": artifact.byte_size,
        "sha256": artifact.sha256,
        "relation": artifact.relation_type,
        "error_code": artifact.validation.get("error_code"),
        "error_message": artifact.validation.get("error_message"),
        "modified_at": datetime.fromtimestamp(
            modified_at_ms / 1000,
            tz=timezone.utc,
        ).isoformat(),
        "mime_type": artifact.mime_type,
        "preview_url": content_url,
        "download_url": f"{content_url}&download=1",
        "reveal_path": artifact.relative_path,
    }


def artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    if suffix in _PRESENTATION_EXTENSIONS:
        return "presentation"
    if suffix in _DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in _ARCHIVE_EXTENSIONS:
        return "archive"
    if suffix in _CODE_EXTENSIONS:
        return "code"
    return "file"


def artifact_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _session_started_timestamp(session_data: dict[str, Any]) -> float:
    raw = session_data.get("created_at")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.timestamp()
        except ValueError:
            pass
    messages = session_data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            raw_timestamp = message.get("timestamp") if isinstance(message, dict) else None
            if not isinstance(raw_timestamp, str):
                continue
            try:
                parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                return parsed.timestamp()
            except ValueError:
                continue
    return float("inf")


def _explicit_session_paths(
    session_key: str,
    session_data: dict[str, Any],
    root: Path,
) -> set[Path]:
    paths: set[Path] = set()
    messages = session_data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.lstrip().startswith("{"):
                continue
            try:
                payload = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("files", "artifacts"):
                entries = payload.get(key)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    raw_path = entry.get("path") if isinstance(entry, dict) else None
                    _collect_explicit_path(paths, raw_path, root)

    for record in read_transcript_lines(session_key):
        if record.get("event") == "file_edit":
            edits = record.get("edits")
            if isinstance(edits, list):
                for edit in edits:
                    if not isinstance(edit, dict):
                        continue
                    _collect_explicit_path(
                        paths,
                        edit.get("absolute_path") or edit.get("path"),
                        root,
                    )
        if record.get("event") == "message":
            media = record.get("media")
            if isinstance(media, list):
                for raw_path in media:
                    _collect_explicit_path(paths, raw_path, root)
    return paths


def _collect_explicit_path(paths: set[Path], value: Any, root: Path) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    paths.add(candidate)


def _add_candidate(
    candidates: dict[str, tuple[Path, os.stat_result]],
    candidate: Path,
    root: Path,
    *,
    stat: os.stat_result | None = None,
) -> None:
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
        resolved_stat = stat if stat is not None and resolved == candidate else resolved.stat()
    except (FileNotFoundError, OSError, ValueError):
        return
    if not resolved.is_file() or _path_is_excluded(resolved, root):
        return
    candidates[relative.as_posix()] = (resolved, resolved_stat)


def _excluded_name(name: str) -> bool:
    return name.startswith(".") or name in _EXCLUDED_DIR_NAMES


def _path_is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(_excluded_name(part) for part in relative.parts)

"""Crash-safe lifecycle tombstones for rebuildable gateway state.

``state.sqlite`` is deliberately a rebuildable query projection.  User intent
such as archiving or permanently deleting a conversation must therefore live
outside that database, otherwise a projection rebuild can resurrect disk-backed
sessions.  This append-only journal is the durable source of truth for those
lifecycle decisions.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


LifecycleState = Literal["archived", "purged"]
LifecycleKind = Literal["session", "project"]


@dataclass(frozen=True)
class LifecycleEntry:
    kind: LifecycleKind
    key: str
    state: LifecycleState
    updated_at: int
    metadata: dict[str, Any]


class LifecycleRegistry:
    """Append-only archive/purge registry that survives projection rebuilds."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, LifecycleEntry] = {}
        self._projects: dict[str, LifecycleEntry] = {}
        self._load()

    def session(self, session_key: str) -> LifecycleEntry | None:
        with self._lock:
            return self._sessions.get(session_key)

    def project(self, project_id: str) -> LifecycleEntry | None:
        with self._lock:
            return self._projects.get(project_id)

    def session_state(self, session_key: str) -> LifecycleState | None:
        entry = self.session(session_key)
        return entry.state if entry is not None else None

    def project_state(self, project_id: str) -> LifecycleState | None:
        entry = self.project(project_id)
        return entry.state if entry is not None else None

    def project_for_path(self, root_path: str | Path) -> LifecycleEntry | None:
        canonical = _canonical_path(root_path)
        with self._lock:
            for entry in self._projects.values():
                recorded = entry.metadata.get("canonical_root_path")
                if isinstance(recorded, str) and _canonical_path(recorded) == canonical:
                    return entry
        return None

    def session_entries(self) -> list[LifecycleEntry]:
        with self._lock:
            return list(self._sessions.values())

    def project_entries(self) -> list[LifecycleEntry]:
        with self._lock:
            return list(self._projects.values())

    def archive_session(self, session_key: str, **metadata: Any) -> LifecycleEntry:
        return self._record("session", session_key, "archived", metadata)

    def restore_session(self, session_key: str, **metadata: Any) -> None:
        self._record("session", session_key, None, metadata)

    def purge_session(self, session_key: str, **metadata: Any) -> LifecycleEntry:
        return self._record("session", session_key, "purged", metadata)

    def archive_project(self, project_id: str, **metadata: Any) -> LifecycleEntry:
        return self._record("project", project_id, "archived", metadata)

    def restore_project(self, project_id: str, **metadata: Any) -> None:
        self._record("project", project_id, None, metadata)

    def purge_project(self, project_id: str, **metadata: Any) -> LifecycleEntry:
        return self._record("project", project_id, "purged", metadata)

    def _record(
        self,
        kind: LifecycleKind,
        key: str,
        state: LifecycleState | None,
        metadata: dict[str, Any],
    ) -> LifecycleEntry | None:
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("lifecycle key is required")
        updated_at = time.time_ns() // 1_000_000
        payload = {
            "schema_version": self._SCHEMA_VERSION,
            "operation_id": uuid.uuid4().hex,
            "kind": kind,
            "key": normalized_key,
            "state": state,
            "updated_at": updated_at,
            "metadata": _safe_metadata(metadata),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            existed = self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(raw + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                _fsync_directory(self.path.parent)
            return self._apply(payload)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        valid_lines: list[str] = []
        invalid_tail: list[str] = []
        with self._lock:
            for index, raw in enumerate(lines):
                text = raw.strip()
                if not text:
                    valid_lines.append(raw)
                    continue
                try:
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError("lifecycle entry must be an object")
                    self._apply(payload)
                except (json.JSONDecodeError, TypeError, ValueError):
                    invalid_tail = lines[index:]
                    break
                valid_lines.append(raw)
            if invalid_tail:
                self._quarantine_invalid_tail(valid_lines, invalid_tail)

    def _apply(self, payload: dict[str, Any]) -> LifecycleEntry | None:
        if int(payload.get("schema_version") or 0) != self._SCHEMA_VERSION:
            raise ValueError("unsupported lifecycle schema")
        kind = payload.get("kind")
        key = payload.get("key")
        state = payload.get("state")
        if kind not in {"session", "project"} or not isinstance(key, str) or not key:
            raise ValueError("invalid lifecycle identity")
        if state not in {None, "archived", "purged"}:
            raise ValueError("invalid lifecycle state")
        target = self._sessions if kind == "session" else self._projects
        if state is None:
            target.pop(key, None)
            return None
        metadata = payload.get("metadata")
        entry = LifecycleEntry(
            kind=kind,
            key=key,
            state=state,
            updated_at=int(payload.get("updated_at") or 0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
        target[key] = entry
        return entry

    def _quarantine_invalid_tail(
        self,
        valid_lines: list[str],
        invalid_tail: list[str],
    ) -> None:
        backup = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with backup.open("w", encoding="utf-8") as handle:
                handle.write("".join(invalid_tail))
                handle.flush()
                os.fsync(handle.fileno())
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write("".join(valid_lines))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized = str(key)
        if isinstance(value, str):
            safe[normalized] = value[:4_096]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[normalized] = value
    return safe


def _canonical_path(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    value = os.path.normcase(os.path.normpath(str(resolved)))
    return value.rstrip(os.sep) or os.sep


def _fsync_directory(path: Path) -> None:
    """Persist a newly-created/replaced directory entry where supported."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Windows and some network filesystems do not support directory fsync.
        pass
    finally:
        os.close(descriptor)

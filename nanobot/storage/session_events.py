"""Canonical business-event commit boundary for one conversation.

Transport and UI adapters submit facts here.  The underlying journal remains
responsible for append-first durability and idempotent SQLite projection; this
service prevents callers from bypassing identity and envelope validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.state import StateStore


class InvalidSessionEvent(ValueError):
    """Raised before persistence when an event has no stable public type."""


class SessionEventFileStore:
    """Append-only canonical JSONL files, independent from WebUI cache files."""

    _MAX_RECORD_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        root: str | Path,
        *,
        legacy_reader: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._legacy_reader = legacy_reader
        self._lock = threading.RLock()

    def path_for(self, session_key: str) -> Path:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.jsonl"

    def append(self, session_key: str, record: dict[str, Any]) -> None:
        raw = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(raw.encode("utf-8")) > self._MAX_RECORD_BYTES:
            raise ValueError("session event record is too large")
        with self._lock:
            path = self.path_for(session_key)
            if not path.exists():
                self._migrate_legacy_locked(session_key, path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(raw + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read(self, session_key: str) -> list[dict[str, Any]]:
        with self._lock:
            path = self.path_for(session_key)
            if not path.exists():
                self._migrate_legacy_locked(session_key, path)
            if not path.exists():
                return []
            return self._read_locked(path)

    def _migrate_legacy_locked(self, session_key: str, path: Path) -> None:
        if self._legacy_reader is None:
            return
        records = self._legacy_reader(session_key)
        if not records:
            return
        self._write_atomic(path, records)

    def _read_locked(self, path: Path) -> list[dict[str, Any]]:
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        valid_lines: list[str] = []
        corrupt_from: int | None = None
        for index, raw_line in enumerate(raw_lines):
            text = raw_line.strip()
            if not text:
                valid_lines.append(raw_line)
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                corrupt_from = index
                break
            valid_lines.append(raw_line)
            if isinstance(value, dict):
                records.append(value)
        if corrupt_from is not None:
            backup = path.with_name(
                f"{path.name}.corrupt-{time.time_ns()}"
            )
            try:
                backup.write_text("".join(raw_lines[corrupt_from:]), encoding="utf-8")
                self._replace_text(path, "".join(valid_lines))
            except OSError:
                # The valid prefix remains usable in memory. Recovery of a
                # damaged diagnostics tail must not prevent session reads.
                pass
        return records

    def _write_atomic(self, path: Path, records: list[dict[str, Any]]) -> None:
        lines = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
            if isinstance(record, dict)
        )
        self._replace_text(path, lines)

    @staticmethod
    def _replace_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class SessionEventService:
    """Single write API for durable Session events.

    ``append`` remains as a compatibility alias during the migration window;
    all new code should call ``commit``.  Both paths share exactly one journal
    append and one SQLite projection transaction.
    """

    def __init__(self, journal: SessionEventJournal) -> None:
        self._journal = journal

    @property
    def state(self) -> StateStore:
        return self._journal.state

    def commit(
        self,
        session_key: str,
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_key = str(session_key).strip()
        if not normalized_key:
            raise InvalidSessionEvent("session_key is required")
        payload = dict(event)
        event_type = payload.get("event")
        if (
            not isinstance(event_type, str)
            or not event_type.strip()
            or len(event_type.strip()) > 120
        ):
            raise InvalidSessionEvent("event type is required")
        payload["event"] = event_type.strip()
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str):
            normalized_turn_id = turn_id.strip()
            if normalized_turn_id:
                payload["turn_id"] = normalized_turn_id
            else:
                payload.pop("turn_id", None)
        return self._journal.append(normalized_key, payload)

    def append(self, session_key: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibility alias; do not add another persistence path."""
        return self.commit(session_key, event)

    def ensure_recovered(self, session_key: str) -> int:
        return self._journal.ensure_recovered(session_key)

    def recover_session(self, session_key: str) -> int:
        return self._journal.recover_session(session_key)

    def recover_all(self) -> dict[str, int]:
        return self._journal.recover_all()

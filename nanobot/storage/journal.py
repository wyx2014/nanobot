"""Append-only session event journal with an idempotent SQLite projector."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from nanobot.storage.logs import StructuredLogStore
from nanobot.storage.state import EventProjectionError, StateStore

EVENT_SCHEMA_VERSION = 3
_EVENT_NAMESPACE = uuid.UUID("c3012948-955b-485f-8f07-7d6670e827e7")


class SessionEventJournal:
    """Write JSONL first, project second, and recover idempotently after crashes."""

    def __init__(
        self,
        *,
        state: StateStore,
        logs: StructuredLogStore,
        append_record: Callable[[str, dict[str, Any]], None],
        read_records: Callable[[str], list[dict[str, Any]]],
    ) -> None:
        self.state = state
        self.logs = logs
        self._append_record = append_record
        self._read_records = read_records
        self._lock = threading.RLock()
        self._recovered_sessions: set[str] = set()

    def append(self, session_key: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._recover_session_locked(session_key)
            watermark = self.state.projector_watermark(session_key)
            if watermark is not None and watermark.get("error") is not None:
                raise EventProjectionError(
                    "cannot append while the session event projection is incomplete"
                )
            session = self.state.get_session(session_key)
            if session is None:
                raise EventProjectionError(
                    f"cannot append event for an unregistered session: {session_key}"
                )
            event_seq = self.state.next_event_sequence(session_key)
            envelope = dict(event)
            requested_event_id = envelope.get("event_id")
            event_id = (
                requested_event_id.strip()
                if isinstance(requested_event_id, str) and requested_event_id.strip()
                else f"evt_{uuid.uuid4().hex}"
            )
            envelope.update(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "event_id": event_id,
                    "event_seq": event_seq,
                    "recorded_at": time.time_ns() // 1_000_000,
                    "project_id": session.project_id,
                    "session_id": session.id,
                    "session_key": session.session_key,
                }
            )
            turn_payload = envelope.get("turn")
            if isinstance(turn_payload, dict):
                trace_id = turn_payload.get("trace_id")
                runtime_epoch = turn_payload.get("runtime_epoch")
                if isinstance(trace_id, str) and trace_id.strip():
                    envelope["trace_id"] = trace_id.strip()
                if isinstance(runtime_epoch, str) and runtime_epoch.strip():
                    envelope["runtime_epoch"] = runtime_epoch.strip()
            turn_id = envelope.get("turn_id")
            if isinstance(turn_id, str) and turn_id.strip():
                runtime_identity = self.state.turn_runtime_identity(
                    session_key,
                    turn_id.strip(),
                )
                if runtime_identity:
                    if runtime_identity.get("trace_id"):
                        envelope.setdefault("trace_id", runtime_identity["trace_id"])
                    if runtime_identity.get("runtime_epoch"):
                        envelope.setdefault(
                            "runtime_epoch",
                            runtime_identity["runtime_epoch"],
                        )
            envelope.setdefault("visibility", "public")
            envelope.setdefault(
                "payload",
                {
                    key: value
                    for key, value in envelope.items()
                    if key
                    not in {
                        "schema_version",
                        "event_id",
                        "event_seq",
                        "recorded_at",
                        "project_id",
                        "session_id",
                        "session_key",
                        "turn_id",
                        "trace_id",
                        "runtime_epoch",
                        "visibility",
                        "payload",
                    }
                },
            )
            self._append_record(session_key, envelope)
            try:
                self.state.project_event(session_key, envelope)
            except Exception as exc:
                self.logs.write(
                    level="error",
                    component="projector",
                    event_name="event_projection_failed",
                    message="durable event append succeeded but SQLite projection failed",
                    project_id=session.project_id,
                    session_id=session.id,
                    error_code="EVENT_PROJECTION_FAILED",
                    details={
                        "session_key": session_key,
                        "event_id": envelope["event_id"],
                        "event_seq": event_seq,
                        "event_type": envelope.get("event"),
                        "exception_type": type(exc).__name__,
                    },
                )
                raise
            return envelope

    def recover_session(self, session_key: str) -> int:
        with self._lock:
            return self._recover_session_locked(session_key, force=True)

    def ensure_recovered(self, session_key: str) -> int:
        with self._lock:
            return self._recover_session_locked(session_key)

    def recover_all(self) -> dict[str, int]:
        recovered: dict[str, int] = {}
        for session in self.state.list_sessions(include_archived=True):
            count = self.recover_session(session.session_key)
            if count:
                recovered[session.session_key] = count
        return recovered

    def _recover_session_locked(self, session_key: str, *, force: bool = False) -> int:
        if not force and session_key in self._recovered_sessions:
            return 0
        session = self.state.get_session(session_key)
        if session is None:
            return 0
        rows = self._read_records(session_key)
        explicit_sequences = {
            int(row["event_seq"])
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("event_seq"), int)
            and not isinstance(row.get("event_seq"), bool)
            and int(row["event_seq"]) > 0
        }
        assigned_sequences: set[int] = set()
        next_legacy_seq = 1
        normalized_events: list[dict[str, Any]] = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                continue
            event = dict(raw)
            raw_seq = event.get("event_seq")
            if (
                isinstance(raw_seq, int)
                and not isinstance(raw_seq, bool)
                and raw_seq > 0
            ):
                event_seq = raw_seq
            else:
                while (
                    next_legacy_seq in explicit_sequences
                    or next_legacy_seq in assigned_sequences
                ):
                    next_legacy_seq += 1
                event_seq = next_legacy_seq
                next_legacy_seq += 1
            assigned_sequences.add(event_seq)
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                event_id = self._legacy_event_id(session_key, index, raw)
            recorded_at = event.get("recorded_at")
            if isinstance(recorded_at, bool) or not isinstance(recorded_at, int):
                occurred = event.get("occurred_at")
                recorded_at = (
                    occurred
                    if isinstance(occurred, int) and not isinstance(occurred, bool)
                    else session.created_at + index
                )
            event.update(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "event_id": event_id,
                    "event_seq": event_seq,
                    "recorded_at": recorded_at,
                    "project_id": session.project_id,
                    "session_id": session.id,
                    "session_key": session.session_key,
                }
            )
            turn_payload = event.get("turn")
            if isinstance(turn_payload, dict):
                trace_id = turn_payload.get("trace_id")
                runtime_epoch = turn_payload.get("runtime_epoch")
                if isinstance(trace_id, str) and trace_id.strip():
                    event["trace_id"] = trace_id.strip()
                if isinstance(runtime_epoch, str) and runtime_epoch.strip():
                    event["runtime_epoch"] = runtime_epoch.strip()
            event.setdefault("visibility", "public")
            event.setdefault(
                "payload",
                {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "schema_version",
                        "event_id",
                        "event_seq",
                        "recorded_at",
                        "project_id",
                        "session_id",
                        "session_key",
                        "turn_id",
                        "trace_id",
                        "runtime_epoch",
                        "visibility",
                        "payload",
                    }
                },
            )
            normalized_events.append(event)

        # Schema v7 stores the full envelope in projected_events.  Backfill
        # rows below the existing projector watermark from their append-only
        # source without replaying business projections or changing sequence.
        self.state.backfill_projected_event_payloads(session_key, normalized_events)

        # SQLite already records the last successfully projected journal
        # envelope.  A restarted gateway only needs to project the suffix
        # after that watermark.  Re-checking every historical event used to
        # open a write transaction per row and could block startup for tens of
        # seconds on established conversations.
        watermark = self.state.projector_watermark(session_key)
        projected_through = 0
        if watermark:
            candidate_seq = int(watermark.get("last_event_seq") or 0)
            candidate_id = watermark.get("last_event_id")
            if candidate_seq > 0 and isinstance(candidate_id, str):
                matching_event = next(
                    (
                        event
                        for event in normalized_events
                        if event["event_seq"] == candidate_seq
                    ),
                    None,
                )
                if (
                    matching_event is not None
                    and matching_event["event_id"] == candidate_id
                ):
                    projected_through = candidate_seq

        recovered = 0
        quarantined = 0
        complete = True
        for event in normalized_events:
            if event["event_seq"] <= projected_through:
                continue
            event_id = event["event_id"]
            event_seq = event["event_seq"]
            try:
                if self.state.project_event(session_key, event):
                    recovered += 1
            except EventProjectionError as exc:
                surrogate = self._legacy_progress_surrogate(event, str(exc))
                if surrogate is not None:
                    try:
                        if self.state.project_event(session_key, surrogate):
                            recovered += 1
                        quarantined += 1
                        self.logs.write(
                            level="warning",
                            component="projector",
                            event_name="legacy_progress_event_quarantined",
                            message=(
                                "an invalid legacy progress snapshot was ignored "
                                "while later durable events continued replaying"
                            ),
                            project_id=session.project_id,
                            session_id=session.id,
                            error_code="LEGACY_PROGRESS_QUARANTINED",
                            details={
                                "session_key": session_key,
                                "event_id": event_id,
                                "event_seq": event_seq,
                                "reason": str(exc),
                            },
                        )
                        continue
                    except EventProjectionError:
                        pass
                complete = False
                self.state.record_projector_error(
                    session_key,
                    event_id=event_id,
                    event_seq=event_seq,
                    message=str(exc),
                )
                self.logs.write(
                    level="error",
                    component="projector",
                    event_name="event_recovery_failed",
                    message="session journal recovery stopped at an invalid event",
                    project_id=session.project_id,
                    session_id=session.id,
                    error_code="EVENT_RECOVERY_FAILED",
                    details={
                        "session_key": session_key,
                        "event_id": event_id,
                        "event_seq": event_seq,
                        "line_index": max(0, event_seq - 1),
                        "reason": str(exc),
                    },
                )
                break
        self._recovered_sessions.add(session_key)
        if recovered:
            self.logs.write(
                level="info",
                component="projector",
                event_name="session_events_recovered",
                message="session journal events projected into SQLite",
                project_id=session.project_id,
                session_id=session.id,
                details={
                    "session_key": session_key,
                    "recovered_events": recovered,
                    "quarantined_legacy_progress_events": quarantined,
                    "journal_rows": len(rows),
                    "complete": complete,
                },
            )
        return recovered

    @staticmethod
    def _legacy_progress_surrogate(
        event: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        """Advance past a known-invalid legacy progress-only snapshot.

        Older desktop builds could persist a serial plan between steps with no
        running item.  Modern validation correctly rejects that shape, but a
        rebuild must not stop before later terminal events.  Project a private,
        no-op envelope at the same sequence instead of inventing progress.
        """
        agent_ui = event.get("agent_ui")
        if (
            str(event.get("event") or "") != "message"
            or not isinstance(agent_ui, dict)
            or agent_ui.get("kind") != "task_progress"
        ):
            return None
        known_progress_errors = (
            "task progress ",
            "workflow progress ",
        )
        if not reason.startswith(known_progress_errors):
            return None
        envelope_keys = {
            "schema_version",
            "event_id",
            "event_seq",
            "recorded_at",
            "project_id",
            "session_id",
            "session_key",
        }
        surrogate = {
            key: value
            for key, value in event.items()
            if key in envelope_keys
        }
        surrogate.update({
            "event": "legacy_progress_quarantined",
            "visibility": "private",
            "payload": {
                "reason": reason[:1_000],
                "original_event": "message",
            },
        })
        return surrogate

    @staticmethod
    def _legacy_event_id(
        session_key: str,
        index: int,
        event: dict[str, Any],
    ) -> str:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        value = f"{session_key}:{index}:{digest}"
        return f"evt_{uuid.uuid5(_EVENT_NAMESPACE, value).hex}"

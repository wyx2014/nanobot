"""SQLite-backed query state for projects, sessions and artifacts.

The existing session JSONL files remain the durable conversation transcript.
This module supplies stable identities, project-scoped relationships and a
rebuildable query projection without moving file bytes into SQLite.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Literal

STATE_SCHEMA_VERSION = 7
_ID_NAMESPACE = uuid.UUID("8b77d594-7dc0-4f27-b76c-7b96e80743c9")
_ARTIFACT_RELATIONS = {
    "generated",
    "modified",
    "attached",
    "referenced",
    "intermediate",
    "final",
}


class StateStoreError(RuntimeError):
    """Base error for the gateway state projection."""


class SessionProjectMismatch(StateStoreError):
    """Raised when a persisted session is presented with another project."""

    def __init__(self, session_key: str, expected_project_id: str, got_project_id: str) -> None:
        super().__init__(
            f"session {session_key!r} belongs to {expected_project_id}, not {got_project_id}"
        )
        self.session_key = session_key
        self.expected_project_id = expected_project_id
        self.got_project_id = got_project_id


class EventProjectionError(StateStoreError):
    """Raised when an append-only session event cannot be safely projected."""


@dataclass(frozen=True)
class StateStoreRecovery:
    """Result of opening the state projection with corruption recovery."""

    store: "StateStore"
    backup_dir: Path | None
    reason: str | None


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    kind: str
    name: str
    root_path: str
    canonical_root_path: str
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    session_key: str
    title: str
    status: str
    event_log_path: str | None
    artifact_indexed_at: int | None
    artifact_revision: int
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    project_id: str
    session_id: str
    session_key: str
    status: str
    storage_kind: str
    relative_path: str
    display_name: str
    artifact_kind: str
    mime_type: str
    byte_size: int
    sha256: str
    relation_type: str
    validation: dict[str, Any]
    created_at: int
    ready_at: int | None
    updated_at: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL
                        CHECK (kind IN ('workspace', 'inbox', 'legacy_quarantine')),
    name                TEXT NOT NULL,
    root_path           TEXT NOT NULL,
    canonical_root_path TEXT NOT NULL,
    filesystem_identity TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'missing', 'detached', 'archived')),
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    last_opened_at      INTEGER,
    settings_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS projects_canonical_root_unique
ON projects(canonical_root_path)
WHERE status != 'archived';

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_key         TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN (
                            'active', 'completed', 'failed',
                            'cancelled', 'archived'
                        )),
    event_log_path      TEXT,
    next_event_seq      INTEGER NOT NULL DEFAULT 1,
    active_turn_id      TEXT,
    model_provider      TEXT,
    model_name          TEXT,
    artifact_indexed_at INTEGER,
    artifact_revision   INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    completed_at        INTEGER,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS sessions_project_updated
ON sessions(project_id, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS sessions_project_immutable
BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'session project_id is immutable');
END;

CREATE TABLE IF NOT EXISTS turns (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    turn_index          INTEGER NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'queued', 'running', 'completed',
                            'failed', 'cancelled'
                        )),
    user_message_id     TEXT,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    error_code          TEXT,
    error_message       TEXT,
    usage_json          TEXT,
    trace_id            TEXT,
    UNIQUE (id, project_id),
    UNIQUE (session_id, turn_index),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    turn_id             TEXT,
    sequence_no         INTEGER NOT NULL,
    role                TEXT NOT NULL
                        CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    message_kind        TEXT NOT NULL DEFAULT 'answer',
    content_json        TEXT NOT NULL,
    is_final            INTEGER NOT NULL DEFAULT 1,
    event_id            TEXT,
    segment_id          TEXT,
    revision            INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    UNIQUE (session_id, sequence_no),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (turn_id, project_id)
        REFERENCES turns(id, project_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    turn_id             TEXT NOT NULL,
    parent_tool_call_id TEXT,
    tool_name           TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'pending', 'running', 'succeeded',
                            'failed', 'cancelled'
                        )),
    input_json          TEXT,
    output_summary_json TEXT,
    started_at          INTEGER,
    ended_at            INTEGER,
    error_code          TEXT,
    error_message       TEXT,
    UNIQUE (id, project_id),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (parent_tool_call_id, project_id)
        REFERENCES tool_calls(id, project_id)
);

CREATE TABLE IF NOT EXISTS turn_progress (
    turn_id             TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    plan_id             TEXT,
    kind                TEXT NOT NULL DEFAULT 'dynamic',
    owner               TEXT NOT NULL DEFAULT 'agent',
    policy              TEXT NOT NULL DEFAULT 'required',
    execution           TEXT NOT NULL DEFAULT 'serial',
    revision            INTEGER NOT NULL DEFAULT 0,
    current_step_key    TEXT,
    active_step_ids_json TEXT NOT NULL DEFAULT '[]',
    signature_version   INTEGER NOT NULL DEFAULT 1,
    note                TEXT,
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'pending', 'running', 'completed',
                            'failed', 'cancelled'
                        )),
    terminalized_at     INTEGER,
    terminalization_reason TEXT,
    updated_at          INTEGER NOT NULL,
    UNIQUE (turn_id, project_id),
    FOREIGN KEY (turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE TABLE IF NOT EXISTS turn_steps (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    turn_id             TEXT NOT NULL,
    step_key            TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    title               TEXT NOT NULL,
    step_kind           TEXT,
    stage_key           TEXT,
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'pending', 'running', 'completed',
                            'failed', 'cancelled'
                        )),
    detail              TEXT,
    warning             TEXT,
    started_at          INTEGER,
    ended_at            INTEGER,
    updated_at          INTEGER NOT NULL,
    UNIQUE (turn_id, step_key),
    FOREIGN KEY (turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE TABLE IF NOT EXISTS expert_team_runs (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    turn_id               TEXT NOT NULL,
    team_id               TEXT NOT NULL,
    plan_id               TEXT NOT NULL,
    status                TEXT NOT NULL,
    current_stage_key     TEXT,
    warning_count         INTEGER NOT NULL DEFAULT 0,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    terminalized_at       INTEGER,
    terminalization_reason TEXT,
    UNIQUE(turn_id, team_id),
    FOREIGN KEY(session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY(turn_id, project_id)
        REFERENCES turns(id, project_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    status                  TEXT NOT NULL
                            CHECK (status IN (
                                'staging', 'ready', 'failed',
                                'missing', 'quarantined'
                            )),
    storage_kind            TEXT NOT NULL
                            CHECK (storage_kind IN ('project_file', 'managed_copy')),
    relative_path           TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    artifact_kind           TEXT NOT NULL,
    mime_type               TEXT,
    byte_size               INTEGER,
    sha256                  TEXT,
    created_by_session_id   TEXT,
    created_by_turn_id      TEXT,
    created_by_tool_call_id TEXT,
    supersedes_artifact_id  TEXT,
    validation_json         TEXT NOT NULL DEFAULT '{}',
    created_at              INTEGER NOT NULL,
    ready_at                INTEGER,
    updated_at              INTEGER NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (created_by_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (created_by_turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (created_by_tool_call_id, project_id)
        REFERENCES tool_calls(id, project_id),
    FOREIGN KEY (supersedes_artifact_id, project_id)
        REFERENCES artifacts(id, project_id)
);

CREATE INDEX IF NOT EXISTS artifacts_project_created
ON artifacts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifact_links (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    artifact_id         TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    turn_id             TEXT,
    tool_call_id        TEXT,
    relation_type       TEXT NOT NULL
                        CHECK (relation_type IN (
                            'generated', 'modified', 'attached',
                            'referenced', 'intermediate', 'final'
                        )),
    origin_event_id     TEXT,
    created_at          INTEGER NOT NULL,
    UNIQUE (artifact_id, session_id, relation_type),
    FOREIGN KEY (artifact_id, project_id)
        REFERENCES artifacts(id, project_id),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (tool_call_id, project_id)
        REFERENCES tool_calls(id, project_id)
);

CREATE INDEX IF NOT EXISTS artifact_links_session
ON artifact_links(project_id, session_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS artifact_links_origin_event_unique
ON artifact_links(origin_event_id)
WHERE origin_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_edges (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    parent_session_id   TEXT NOT NULL,
    parent_turn_id      TEXT NOT NULL,
    child_session_id    TEXT NOT NULL,
    spawn_tool_call_id  TEXT,
    created_at          INTEGER NOT NULL,
    FOREIGN KEY (parent_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (parent_turn_id, project_id)
        REFERENCES turns(id, project_id),
    FOREIGN KEY (child_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (spawn_tool_call_id, project_id)
        REFERENCES tool_calls(id, project_id)
);

CREATE TABLE IF NOT EXISTS project_memories (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    content             TEXT NOT NULL,
    source_session_id   TEXT,
    source_turn_id      TEXT,
    confidence          REAL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (source_turn_id, project_id)
        REFERENCES turns(id, project_id)
);

CREATE TABLE IF NOT EXISTS project_memory_stage1 (
    id                          TEXT PRIMARY KEY,
    project_id                  TEXT NOT NULL,
    source_session_id           TEXT NOT NULL,
    source_rollout_revision     TEXT NOT NULL,
    raw_memory                  TEXT NOT NULL,
    rollout_summary             TEXT NOT NULL,
    rollout_slug                TEXT,
    generated_at                INTEGER NOT NULL,
    usage_count                 INTEGER NOT NULL DEFAULT 0,
    last_used_at                INTEGER,
    selected_for_consolidation  INTEGER NOT NULL DEFAULT 0,
    selected_source_revision    TEXT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, source_session_id, source_rollout_revision),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE INDEX IF NOT EXISTS project_memory_stage1_selection
ON project_memory_stage1(
    project_id, selected_for_consolidation,
    usage_count DESC, last_used_at DESC, generated_at DESC
);

CREATE TABLE IF NOT EXISTS project_memory_jobs (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL,
    phase                TEXT NOT NULL
                         CHECK (phase IN ('phase1', 'phase2')),
    job_key              TEXT NOT NULL,
    status               TEXT NOT NULL
                         CHECK (status IN (
                             'queued', 'running', 'succeeded',
                             'succeeded_no_output', 'failed'
                         )),
    lease_owner          TEXT,
    lease_expires_at     INTEGER,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    retry_at             INTEGER,
    input_watermark      INTEGER,
    completed_watermark  INTEGER,
    error_json           TEXT,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL,
    completed_at         INTEGER,
    UNIQUE (project_id, phase, job_key),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS project_memory_jobs_status
ON project_memory_jobs(project_id, phase, status, retry_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS project_memory_sources (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    memory_id           TEXT NOT NULL,
    stage1_id           TEXT,
    source_session_id   TEXT NOT NULL,
    source_turn_id      TEXT,
    source_event_id     TEXT,
    evidence_locator    TEXT,
    created_at          INTEGER NOT NULL,
    UNIQUE (
        project_id, memory_id, source_session_id,
        source_turn_id, source_event_id, evidence_locator
    ),
    FOREIGN KEY (memory_id, project_id)
        REFERENCES project_memories(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (stage1_id, project_id)
        REFERENCES project_memory_stage1(id, project_id),
    FOREIGN KEY (source_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (source_turn_id, project_id)
        REFERENCES turns(id, project_id)
);

CREATE INDEX IF NOT EXISTS project_memory_sources_memory
ON project_memory_sources(project_id, memory_id);

CREATE TABLE IF NOT EXISTS project_documents (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    source_artifact_id  TEXT,
    relative_path       TEXT NOT NULL,
    content_hash        TEXT,
    indexing_status     TEXT NOT NULL DEFAULT 'pending',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_artifact_id, project_id)
        REFERENCES artifacts(id, project_id)
);

CREATE TABLE IF NOT EXISTS project_chunks (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    text                TEXT NOT NULL,
    embedding_ref       TEXT,
    UNIQUE (document_id, ordinal),
    FOREIGN KEY (document_id, project_id)
        REFERENCES project_documents(id, project_id)
);

CREATE TABLE IF NOT EXISTS project_cache (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    cache_key           TEXT NOT NULL,
    value_json          TEXT NOT NULL,
    expires_at          INTEGER,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (project_id, namespace, cache_key),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS project_cache_lookup
ON project_cache(project_id, namespace, cache_key);

CREATE TABLE IF NOT EXISTS schedules (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    name                TEXT NOT NULL,
    cron                TEXT NOT NULL,
    prompt              TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_session_id  TEXT,
    last_run_session_id TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (created_session_id, project_id)
        REFERENCES sessions(id, project_id),
    FOREIGN KEY (last_run_session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE TABLE IF NOT EXISTS projector_state (
    session_id          TEXT PRIMARY KEY,
    event_log_path      TEXT NOT NULL,
    last_event_seq      INTEGER NOT NULL DEFAULT 0,
    last_event_id       TEXT,
    last_projected_at   INTEGER,
    lease_owner         TEXT,
    lease_expires_at    INTEGER,
    error_json          TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS projected_events (
    event_id            TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    event_seq           INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
    payload_json        TEXT,
    recorded_at         INTEGER NOT NULL,
    projected_at        INTEGER NOT NULL,
    UNIQUE (session_id, event_seq),
    FOREIGN KEY (session_id, project_id)
        REFERENCES sessions(id, project_id)
);

CREATE INDEX IF NOT EXISTS projected_events_session_seq
ON projected_events(project_id, session_id, event_seq);

"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_timestamp_ms(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    timestamp = int(value)
    return timestamp if timestamp > 0 else fallback


def _coerce_nonnegative_int(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{uuid.uuid5(_ID_NAMESPACE, value).hex}"


def _canonical_path(value: str | Path) -> str:
    resolved = Path(value).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def _filesystem_identity(value: str | Path) -> str | None:
    try:
        stat = Path(value).expanduser().resolve(strict=True).stat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}"


def _json_object(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("token", "authorization", "cookie", "api_key", "password")
            ) and normalized not in {
                "prompt_tokens",
                "completion_tokens",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cached_input_tokens",
                "cache_read_input_tokens",
                "total_tokens",
                "new_tokens",
                "confirmed_new_tokens",
            }:
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact_json_value(item)
        return output
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value[:200]]
    if isinstance(value, str):
        return value[:16_000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:16_000]


def _safe_json(value: Any) -> str:
    return json.dumps(
        _redact_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
    )[:64_000]


def _redact_event_value(value: Any) -> Any:
    """Redact credentials while preserving complete committed message text."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:1_000]:
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in (
                    "authorization",
                    "cookie",
                    "api_key",
                    "password",
                    "secret",
                )
            ):
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact_event_value(item)
        return output
    if isinstance(value, list):
        return [_redact_event_value(item) for item in value[:10_000]]
    if isinstance(value, str):
        return value[:4_000_000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:64_000]


def _event_json(value: Any) -> str:
    encoded = json.dumps(
        _redact_event_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
        raise EventProjectionError("event payload exceeds 8 MiB")
    return encoded


class StateStore:
    """Own the SQLite projection used by the local gateway."""

    def __init__(self, path: str | Path, *, default_workspace: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.default_workspace = _canonical_path(default_workspace)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.executescript(_SCHEMA)
            self._ensure_column(
                connection,
                "project_memories",
                "title",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "project_memories",
                "status",
                "TEXT NOT NULL DEFAULT 'active'",
            )
            self._ensure_column(
                connection,
                "project_memories",
                "usage_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "project_memories",
                "last_used_at",
                "INTEGER",
            )
            self._ensure_column(connection, "turns", "runtime_epoch", "TEXT")
            self._ensure_column(connection, "turns", "trace_id", "TEXT")
            self._ensure_column(connection, "turns", "finish_reason", "TEXT")
            self._ensure_column(connection, "turns", "terminal_event_id", "TEXT")
            self._ensure_column(connection, "turn_progress", "plan_id", "TEXT")
            self._ensure_column(
                connection, "turn_progress", "kind", "TEXT NOT NULL DEFAULT 'dynamic'"
            )
            self._ensure_column(
                connection, "turn_progress", "owner", "TEXT NOT NULL DEFAULT 'agent'"
            )
            self._ensure_column(
                connection, "turn_progress", "policy", "TEXT NOT NULL DEFAULT 'required'"
            )
            self._ensure_column(
                connection, "turn_progress", "execution", "TEXT NOT NULL DEFAULT 'serial'"
            )
            self._ensure_column(
                connection,
                "turn_progress",
                "active_step_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "turn_progress",
                "signature_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(connection, "turn_progress", "terminalized_at", "INTEGER")
            self._ensure_column(
                connection, "turn_progress", "terminalization_reason", "TEXT"
            )
            self._ensure_column(connection, "turn_steps", "step_kind", "TEXT")
            self._ensure_column(connection, "turn_steps", "stage_key", "TEXT")
            self._ensure_column(connection, "turn_steps", "warning", "TEXT")
            self._ensure_column(
                connection,
                "sessions",
                "artifact_revision",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "messages", "event_id", "TEXT")
            self._ensure_column(connection, "messages", "segment_id", "TEXT")
            self._ensure_column(
                connection,
                "messages",
                "revision",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "projected_events", "payload_json", "TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS messages_origin_event_unique
                ON messages(event_id)
                WHERE event_id IS NOT NULL
                """
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS artifact_links_revision_insert
                AFTER INSERT ON artifact_links
                BEGIN
                    UPDATE sessions
                    SET artifact_revision = artifact_revision + 1,
                        updated_at = MAX(updated_at, NEW.created_at)
                    WHERE id = NEW.session_id;
                END;

                CREATE TRIGGER IF NOT EXISTS artifact_links_revision_delete
                AFTER DELETE ON artifact_links
                BEGIN
                    UPDATE sessions
                    SET artifact_revision = artifact_revision + 1,
                        updated_at = MAX(
                            updated_at,
                            CAST(strftime('%s','now') AS INTEGER) * 1000
                        )
                    WHERE id = OLD.session_id;
                END;

                CREATE TRIGGER IF NOT EXISTS artifacts_revision_update
                AFTER UPDATE OF status, relative_path, display_name,
                    validation_json, updated_at ON artifacts
                BEGIN
                    UPDATE sessions
                    SET artifact_revision = artifact_revision + 1,
                        updated_at = MAX(updated_at, NEW.updated_at)
                    WHERE id IN (
                        SELECT session_id FROM artifact_links
                        WHERE artifact_id = NEW.id
                          AND project_id = NEW.project_id
                    );
                END;
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS turns_terminal_event_unique
                ON turns(terminal_event_id)
                WHERE terminal_event_id IS NOT NULL
                """
            )
            for version in range(1, STATE_SCHEMA_VERSION + 1):
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now_ms()),
                )
            connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def quick_check(self) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and row[0] == "ok")

    def ensure_project(
        self,
        root_path: str | Path,
        *,
        name: str | None = None,
        kind: Literal["workspace", "inbox", "legacy_quarantine"] | None = None,
    ) -> ProjectRecord:
        canonical = _canonical_path(root_path)
        resolved_kind = kind or ("inbox" if canonical == self.default_workspace else "workspace")
        project_id = _stable_id("prj", f"{resolved_kind}:{canonical}")
        filesystem_identity = _filesystem_identity(root_path)
        display_name = (name or Path(canonical).name or canonical).strip()
        now = _now_ms()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM projects
                WHERE canonical_root_path = ?
                   OR (? IS NOT NULL AND filesystem_identity = ?)
                ORDER BY
                    CASE WHEN canonical_root_path = ? THEN 0 ELSE 1 END,
                    CASE WHEN status = 'archived' THEN 1 ELSE 0 END,
                    updated_at DESC
                LIMIT 1
                """,
                (canonical, filesystem_identity, filesystem_identity, canonical),
            ).fetchone()
            if row is None:
                id_owner = connection.execute(
                    "SELECT canonical_root_path FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if id_owner is not None:
                    project_id = _stable_id(
                        "prj",
                        (
                            f"{resolved_kind}:{canonical}:collision:"
                            f"{id_owner['canonical_root_path']}"
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO projects(
                        id, kind, name, root_path, canonical_root_path,
                        filesystem_identity, status,
                        created_at, updated_at, last_opened_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        project_id,
                        resolved_kind,
                        display_name,
                        str(Path(root_path).expanduser().resolve(strict=False)),
                        canonical,
                        filesystem_identity,
                        now,
                        now,
                        now,
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
            else:
                connection.execute(
                    """
                    UPDATE projects
                    SET root_path = ?, canonical_root_path = ?,
                        filesystem_identity = COALESCE(?, filesystem_identity),
                        status = 'active', last_opened_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(Path(root_path).expanduser().resolve(strict=False)),
                        canonical,
                        filesystem_identity,
                        now,
                        now,
                        row["id"],
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM projects WHERE id = ?",
                    (row["id"],),
                ).fetchone()
        assert row is not None
        return self._project_record(row)

    def bind_session(
        self,
        session_key: str,
        project_id: str,
        *,
        event_log_path: str | Path | None = None,
        title: str = "",
        metadata: dict[str, Any] | None = None,
        artifact_index_initialized: bool = False,
        allow_draft_rebind: bool = False,
    ) -> SessionRecord:
        normalized_key = session_key.strip()
        if not normalized_key:
            raise ValueError("session_key is required")
        session_id = _stable_id("ses", normalized_key)
        now = _now_ms()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                if not allow_draft_rebind or self._session_has_dependencies(
                    connection, existing["id"]
                ):
                    connection.rollback()
                    raise SessionProjectMismatch(
                        normalized_key,
                        str(existing["project_id"]),
                        project_id,
                    )
                connection.execute("DELETE FROM sessions WHERE id = ?", (existing["id"],))
                existing = None

            if existing is None:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, project_id, session_key, title, status,
                        event_log_path, artifact_indexed_at,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        project_id,
                        normalized_key,
                        title.strip(),
                        str(event_log_path) if event_log_path is not None else None,
                        now if artifact_index_initialized else None,
                        now,
                        now,
                        _json_object(metadata),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE sessions
                    SET title = CASE WHEN ? != '' THEN ? ELSE title END,
                        event_log_path = COALESCE(?, event_log_path),
                        artifact_indexed_at = CASE
                            WHEN ? = 1 THEN COALESCE(artifact_indexed_at, ?)
                            ELSE artifact_indexed_at
                        END,
                        updated_at = ?,
                        metadata_json = CASE
                            WHEN ? != '{}' THEN ?
                            ELSE metadata_json
                        END
                    WHERE id = ?
                    """,
                    (
                        title.strip(),
                        title.strip(),
                        str(event_log_path) if event_log_path is not None else None,
                        1 if artifact_index_initialized else 0,
                        now,
                        now,
                        _json_object(metadata),
                        _json_object(metadata),
                        existing["id"],
                    ),
                )
                session_id = str(existing["id"])
            connection.commit()
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return self._session_record(row)

    @staticmethod
    def _session_has_dependencies(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> bool:
        for table in ("turns", "messages", "tool_calls", "artifact_links"):
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                return True
        return False

    def ensure_session_for_project(
        self,
        session_key: str,
        root_path: str | Path,
        *,
        project_name: str | None = None,
        event_log_path: str | Path | None = None,
        title: str = "",
        metadata: dict[str, Any] | None = None,
        artifact_index_initialized: bool = False,
        allow_draft_rebind: bool = False,
    ) -> tuple[ProjectRecord, SessionRecord]:
        project = self.ensure_project(root_path, name=project_name)
        session = self.bind_session(
            session_key,
            project.id,
            event_log_path=event_log_path,
            title=title,
            metadata=metadata,
            artifact_index_initialized=artifact_index_initialized,
            allow_draft_rebind=allow_draft_rebind,
        )
        return project, session

    def get_session(self, session_key: str) -> SessionRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return self._session_record(row) if row is not None else None

    def get_session_by_id(self, session_id: str) -> SessionRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return self._session_record(row) if row is not None else None

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return self._project_record(row) if row is not None else None

    def list_projects(self, *, include_archived: bool = False) -> list[ProjectRecord]:
        where = "" if include_archived else "WHERE status != 'archived'"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM projects
                {where}
                ORDER BY COALESCE(last_opened_at, updated_at) DESC, name COLLATE NOCASE
                """
            ).fetchall()
        return [self._project_record(row) for row in rows]

    def archive_project(self, project_id: str) -> ProjectRecord:
        """Archive a project registration without deleting user files."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        if project.kind == "inbox":
            raise StateStoreError("the Inbox project cannot be archived")
        now = _now_ms()
        with self._lock, self._connection() as connection:
            active_schedule = connection.execute(
                """
                SELECT id FROM schedules
                WHERE project_id = ? AND status IN ('active', 'running', 'enabled')
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active_schedule is not None:
                raise StateStoreError(
                    "project has active schedules; pause them before archiving"
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE projects SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, project_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET status = CASE
                    WHEN status IN ('active', 'completed') THEN 'archived'
                    ELSE status
                END,
                    updated_at = ?
                WHERE project_id = ?
                """,
                (now, project_id),
            )
            connection.commit()
        archived = self.get_project(project_id)
        assert archived is not None
        return archived

    def restore_project(self, project_id: str) -> ProjectRecord:
        """Restore an archived project and its archived sessions."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        canonical = _canonical_path(project.canonical_root_path)
        now = _now_ms()
        with self._lock, self._connection() as connection:
            conflict = connection.execute(
                """
                SELECT id FROM projects
                WHERE canonical_root_path = ? AND status != 'archived' AND id != ?
                LIMIT 1
                """,
                (canonical, project_id),
            ).fetchone()
            if conflict is not None:
                raise StateStoreError("another active project already uses this path")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE projects
                SET status = ?, updated_at = ?, last_opened_at = ?
                WHERE id = ?
                """,
                ("active" if Path(canonical).exists() else "missing", now, now, project_id),
            )
            connection.execute(
                """
                UPDATE sessions SET status = 'active', updated_at = ?
                WHERE project_id = ? AND status = 'archived'
                """,
                (now, project_id),
            )
            connection.commit()
        restored = self.get_project(project_id)
        assert restored is not None
        return restored

    def relocate_project(
        self,
        project_id: str,
        new_root_path: str | Path,
    ) -> ProjectRecord:
        """Move a project registration to another root while preserving its ID."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        if project.kind != "workspace":
            raise StateStoreError("only workspace projects can be relocated")
        new_root = Path(new_root_path).expanduser().resolve(strict=False)
        if not new_root.exists() or not new_root.is_dir():
            raise StateStoreError("new project path is not an existing directory")
        canonical = _canonical_path(new_root)
        filesystem_identity = _filesystem_identity(new_root)
        now = _now_ms()
        with self._lock, self._connection() as connection:
            conflict = connection.execute(
                """
                SELECT id FROM projects
                WHERE canonical_root_path = ? AND status != 'archived' AND id != ?
                LIMIT 1
                """,
                (canonical, project_id),
            ).fetchone()
            if conflict is not None:
                raise StateStoreError("another active project already uses this path")
            connection.execute(
                """
                UPDATE projects
                SET root_path = ?, canonical_root_path = ?,
                    filesystem_identity = ?, status = 'active',
                    updated_at = ?, last_opened_at = ?
                WHERE id = ?
                """,
                (
                    str(new_root),
                    canonical,
                    filesystem_identity,
                    now,
                    now,
                    project_id,
                ),
            )
            connection.commit()
        relocated = self.get_project(project_id)
        assert relocated is not None
        return relocated

    def project_export_manifest(self, project_id: str) -> dict[str, Any]:
        """Return a portable relationship snapshot; artifact bytes stay on disk."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        with self._lock, self._connection() as connection:
            sessions = connection.execute(
                "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            artifacts = connection.execute(
                """
                SELECT a.*, l.session_id, l.turn_id, l.tool_call_id,
                       l.relation_type, l.origin_event_id
                FROM artifacts a
                LEFT JOIN artifact_links l
                  ON l.artifact_id = a.id AND l.project_id = a.project_id
                WHERE a.project_id = ?
                ORDER BY a.created_at, a.id
                """,
                (project_id,),
            ).fetchall()
            memories = connection.execute(
                """
                SELECT id, kind, content, source_session_id, source_turn_id,
                       confidence, created_at, updated_at
                FROM project_memories
                WHERE project_id = ?
                ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "exported_at": _now_ms(),
            "project": {
                "id": project.id,
                "kind": project.kind,
                "name": project.name,
                "root_path": project.root_path,
                "status": project.status,
                "created_at": project.created_at,
                "updated_at": project.updated_at,
            },
            "sessions": [
                {
                    "id": str(row["id"]),
                    "session_key": str(row["session_key"]),
                    "title": str(row["title"]),
                    "status": str(row["status"]),
                    "event_log_path": row["event_log_path"],
                    "created_at": int(row["created_at"]),
                    "updated_at": int(row["updated_at"]),
                }
                for row in sessions
            ],
            "artifacts": [
                {
                    "id": str(row["id"]),
                    "status": str(row["status"]),
                    "storage_kind": str(row["storage_kind"]),
                    "relative_path": str(row["relative_path"]),
                    "display_name": str(row["display_name"]),
                    "artifact_kind": str(row["artifact_kind"]),
                    "mime_type": row["mime_type"],
                    "byte_size": row["byte_size"],
                    "sha256": row["sha256"],
                    "session_id": row["session_id"],
                    "turn_id": row["turn_id"],
                    "tool_call_id": row["tool_call_id"],
                    "relation_type": row["relation_type"],
                    "origin_event_id": row["origin_event_id"],
                }
                for row in artifacts
            ],
            "memories": [dict(row) for row in memories],
        }

    def upsert_project_memory(
        self,
        project_id: str,
        *,
        kind: str,
        content: str,
        title: str = "",
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        confidence: float | None = None,
        status: str = "active",
        memory_key: str | None = None,
    ) -> str:
        """Persist a project-owned memory snapshot in the relational index."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        normalized_kind = kind.strip() or "long_term"
        normalized_title = title.strip()[:500]
        normalized_content = content.strip()[:64_000]
        if not normalized_content:
            raise StateStoreError("project memory content is required")
        if status not in {"active", "stale", "deleted"}:
            raise StateStoreError("invalid project memory status")
        if confidence is not None:
            confidence = max(0.0, min(float(confidence), 1.0))
        memory_id = _stable_id(
            "mem",
            (
                f"{project_id}:{normalized_kind}:{source_session_id or ''}:"
                f"{source_turn_id or ''}:{memory_key or ''}"
            ),
        )
        now = _now_ms()
        with self._lock, self._connection() as connection:
            self._validate_project_memory_source(
                connection,
                project_id=project_id,
                source_session_id=source_session_id,
                source_turn_id=source_turn_id,
            )
            connection.execute(
                """
                INSERT INTO project_memories(
                    id, project_id, kind, title, content, source_session_id,
                    source_turn_id, confidence, status, usage_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_id,
                    project_id,
                    normalized_kind,
                    normalized_title,
                    normalized_content,
                    source_session_id,
                    source_turn_id,
                    confidence,
                    status,
                    now,
                    now,
                ),
            )
            connection.commit()
        return memory_id

    def list_project_memories(
        self,
        project_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        status_filter = "" if include_deleted else "AND m.status != 'deleted'"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT m.id, m.project_id, m.kind, m.title, m.content,
                       m.source_session_id, m.source_turn_id, m.confidence,
                       m.status, m.usage_count, m.last_used_at,
                       m.created_at, m.updated_at,
                       COUNT(DISTINCT s.id) AS source_count
                FROM project_memories m
                LEFT JOIN project_memory_sources s
                  ON s.memory_id = m.id AND s.project_id = m.project_id
                WHERE m.project_id = ? {status_filter}
                GROUP BY m.id
                ORDER BY m.updated_at DESC, m.id
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_project_memory(self, project_id: str, memory_id: str) -> bool:
        """Soft-delete one project memory and remove its online sources."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM project_memories WHERE id = ? AND project_id = ?",
                (memory_id, project_id),
            ).fetchone()
            if row is None:
                return False
            source_rows = connection.execute(
                """
                SELECT DISTINCT stage1_id
                FROM project_memory_sources
                WHERE memory_id = ? AND project_id = ? AND stage1_id IS NOT NULL
                """,
                (memory_id, project_id),
            ).fetchall()
            connection.execute(
                "DELETE FROM project_memory_sources WHERE memory_id = ? AND project_id = ?",
                (memory_id, project_id),
            )
            for source in source_rows:
                connection.execute(
                    """
                    DELETE FROM project_memory_stage1
                    WHERE id = ? AND project_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM project_memory_sources
                          WHERE stage1_id = ? AND project_id = ?
                      )
                    """,
                    (
                        source["stage1_id"],
                        project_id,
                        source["stage1_id"],
                        project_id,
                    ),
                )
            connection.execute(
                """
                UPDATE project_memories
                SET status = 'deleted', content = '[forgotten]', updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (_now_ms(), memory_id, project_id),
            )
            connection.execute(
                "DELETE FROM project_cache WHERE project_id = ? AND namespace = 'memory'",
                (project_id,),
            )
            connection.commit()
        return True

    def clear_project_memories(self, project_id: str) -> int:
        """Forget one project's derived memories while preserving session journals."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM project_memories
                WHERE project_id = ? AND status != 'deleted'
                """,
                (project_id,),
            ).fetchone()
            count = int(row["count"]) if row is not None else 0
            connection.execute(
                "DELETE FROM project_memory_sources WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM project_memory_stage1 WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM project_memory_jobs WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM project_memories WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM project_cache WHERE project_id = ? AND namespace = 'memory'",
                (project_id,),
            )
            connection.commit()
        return count

    def record_project_memory_usage(
        self,
        project_id: str,
        memory_ids: list[str],
    ) -> int:
        """Record successful online use without accepting cross-project IDs."""
        normalized = list(dict.fromkeys(item.strip() for item in memory_ids if item.strip()))[:50]
        if not normalized:
            return 0
        now = _now_ms()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE project_memories
                SET usage_count = usage_count + 1, last_used_at = ?, updated_at = updated_at
                WHERE project_id = ? AND status = 'active' AND id IN ({placeholders})
                """,
                (now, project_id, *normalized),
            )
            stage1_rows = connection.execute(
                f"""
                SELECT DISTINCT stage1_id
                FROM project_memory_sources
                WHERE project_id = ?
                  AND memory_id IN ({placeholders})
                  AND stage1_id IS NOT NULL
                """,
                (project_id, *normalized),
            ).fetchall()
            stage1_ids = [str(row["stage1_id"]) for row in stage1_rows]
            if stage1_ids:
                stage1_placeholders = ",".join("?" for _ in stage1_ids)
                connection.execute(
                    f"""
                    UPDATE project_memory_stage1
                    SET usage_count = usage_count + 1, last_used_at = ?
                    WHERE project_id = ? AND id IN ({stage1_placeholders})
                    """,
                    (now, project_id, *stage1_ids),
                )
            connection.commit()
        return max(int(cursor.rowcount), 0)

    def search_project_memories(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Run bounded lexical lookup over active memories in one project."""
        terms = list(
            dict.fromkeys(
                term.lower()
                for term in query.strip().split()
                if len(term.strip()) >= 2
            )
        )[:5]
        if not terms:
            return []
        clauses = " OR ".join(
            "(LOWER(m.title) LIKE ? OR LOWER(m.content) LIKE ?)" for _ in terms
        )
        pattern_params: list[str] = []
        for term in terms:
            pattern_params.extend((f"%{term}%", f"%{term}%"))
        bounded_limit = max(1, min(int(limit), 20))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT m.id, m.project_id, m.kind, m.title, m.content,
                       m.confidence, m.usage_count, m.last_used_at,
                       m.created_at, m.updated_at,
                       GROUP_CONCAT(DISTINCT s.source_session_id) AS source_session_ids,
                       GROUP_CONCAT(DISTINCT s.evidence_locator) AS evidence_locators
                FROM project_memories m
                LEFT JOIN project_memory_sources s
                  ON s.memory_id = m.id AND s.project_id = m.project_id
                WHERE m.project_id = ? AND m.status = 'active'
                  AND ({clauses})
                GROUP BY m.id
                ORDER BY
                    m.usage_count DESC,
                    COALESCE(m.last_used_at, m.updated_at) DESC,
                    m.id
                LIMIT ?
                """,
                (project_id, *pattern_params, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_project_memory_stage1(
        self,
        project_id: str,
        *,
        source_session_id: str,
        source_rollout_revision: str,
        raw_memory: str,
        rollout_summary: str,
        rollout_slug: str | None = None,
    ) -> str:
        """Persist one idempotent high-signal extraction from a session rollout."""
        revision = source_rollout_revision.strip()
        normalized_raw = raw_memory.strip()[:64_000]
        normalized_summary = rollout_summary.strip()[:24_000]
        if not revision:
            raise StateStoreError("source rollout revision is required")
        if not normalized_raw and not normalized_summary:
            raise StateStoreError("stage1 memory output is empty")
        stage1_id = _stable_id(
            "m1",
            f"{project_id}:{source_session_id}:{revision}",
        )
        now = _now_ms()
        with self._lock, self._connection() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
                (source_session_id, project_id),
            ).fetchone()
            if session is None:
                raise StateStoreError("memory source session does not belong to project")
            connection.execute(
                """
                INSERT INTO project_memory_stage1(
                    id, project_id, source_session_id, source_rollout_revision,
                    raw_memory, rollout_summary, rollout_slug, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source_session_id, source_rollout_revision)
                DO UPDATE SET
                    raw_memory = excluded.raw_memory,
                    rollout_summary = excluded.rollout_summary,
                    rollout_slug = excluded.rollout_slug,
                    generated_at = excluded.generated_at
                """,
                (
                    stage1_id,
                    project_id,
                    source_session_id,
                    revision,
                    normalized_raw,
                    normalized_summary,
                    (rollout_slug or "").strip()[:160] or None,
                    now,
                ),
            )
            connection.commit()
        return stage1_id

    def get_project_memory_stage1(
        self,
        project_id: str,
        *,
        source_session_id: str,
        source_rollout_revision: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM project_memory_stage1
                WHERE project_id = ? AND source_session_id = ?
                  AND source_rollout_revision = ?
                """,
                (project_id, source_session_id, source_rollout_revision),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_project_memory_stage1(
        self,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.*
                FROM project_memory_stage1 m
                WHERE m.project_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM project_memory_stage1 newer
                      WHERE newer.project_id = m.project_id
                        AND newer.source_session_id = m.source_session_id
                        AND (
                            newer.generated_at > m.generated_at
                            OR (
                                newer.generated_at = m.generated_at
                                AND newer.rowid > m.rowid
                            )
                        )
                  )
                ORDER BY
                    m.usage_count DESC,
                    COALESCE(m.last_used_at, m.generated_at) DESC,
                    m.generated_at DESC,
                    m.id
                LIMIT ?
                """,
                (project_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_project_memory_sources(
        self,
        project_id: str,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        """List provenance edges for exactly one project-owned memory."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.project_id, s.memory_id, s.stage1_id,
                       s.source_session_id, session.session_key AS source_session_key,
                       s.source_turn_id, s.source_event_id, s.evidence_locator,
                       s.created_at
                FROM project_memory_sources s
                JOIN sessions session
                  ON session.id = s.source_session_id
                 AND session.project_id = s.project_id
                WHERE s.project_id = ? AND s.memory_id = ?
                ORDER BY s.created_at DESC, s.id
                """,
                (project_id, memory_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def project_memory_source_summaries(
        self,
        project_id: str,
        memory_ids: list[str],
        *,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return a few rollout summaries behind selected project memories."""
        normalized = list(dict.fromkeys(item for item in memory_ids if item))[:20]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT m1.id, m1.source_session_id,
                       session.session_key AS source_session_key,
                       m1.rollout_slug, m1.rollout_summary,
                       m1.generated_at, m1.usage_count, m1.last_used_at
                FROM project_memory_sources source
                JOIN project_memory_stage1 m1
                  ON m1.id = source.stage1_id
                 AND m1.project_id = source.project_id
                JOIN sessions session
                  ON session.id = m1.source_session_id
                 AND session.project_id = m1.project_id
                WHERE source.project_id = ?
                  AND source.memory_id IN ({placeholders})
                ORDER BY
                    m1.usage_count DESC,
                    COALESCE(m1.last_used_at, m1.generated_at) DESC,
                    m1.id
                LIMIT ?
                """,
                (project_id, *normalized, max(1, min(int(limit), 5))),
            ).fetchall()
        return [dict(row) for row in rows]

    def project_memory_input_watermark(self, project_id: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT MAX(
                    generated_at * 1000000 + (rowid % 1000000)
                ) AS watermark
                FROM project_memory_stage1
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        if row is None or row["watermark"] is None:
            return 0
        return int(row["watermark"])

    def claim_project_memory_job(
        self,
        project_id: str,
        *,
        phase: Literal["phase1", "phase2"],
        job_key: str,
        lease_owner: str,
        lease_ms: int = 300_000,
        input_watermark: int | None = None,
    ) -> dict[str, Any] | None:
        """Claim one memory job, returning None when an equivalent lease is active/done."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        normalized_key = job_key.strip()
        normalized_owner = lease_owner.strip()
        if not normalized_key or not normalized_owner:
            raise StateStoreError("memory job key and lease owner are required")
        now = _now_ms()
        lease_expires_at = now + max(1_000, int(lease_ms))
        job_id = _stable_id("mjob", f"{project_id}:{phase}:{normalized_key}")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM project_memory_jobs
                WHERE project_id = ? AND phase = ? AND job_key = ?
                """,
                (project_id, phase, normalized_key),
            ).fetchone()
            if row is not None:
                active_lease = (
                    str(row["status"]) == "running"
                    and row["lease_expires_at"] is not None
                    and int(row["lease_expires_at"]) > now
                )
                same_success = (
                    str(row["status"]) in {"succeeded", "succeeded_no_output"}
                    and (
                        input_watermark is None
                        or int(row["completed_watermark"] or 0) >= int(input_watermark)
                    )
                )
                retry_blocked = row["retry_at"] is not None and int(row["retry_at"]) > now
                if active_lease or same_success or retry_blocked:
                    connection.rollback()
                    return None
            connection.execute(
                """
                INSERT INTO project_memory_jobs(
                    id, project_id, phase, job_key, status, lease_owner,
                    lease_expires_at, attempt_count, input_watermark,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, 1, ?, ?, ?)
                ON CONFLICT(project_id, phase, job_key) DO UPDATE SET
                    status = 'running',
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    attempt_count = project_memory_jobs.attempt_count + 1,
                    retry_at = NULL,
                    input_watermark = excluded.input_watermark,
                    error_json = NULL,
                    updated_at = excluded.updated_at,
                    completed_at = NULL
                """,
                (
                    job_id,
                    project_id,
                    phase,
                    normalized_key,
                    normalized_owner,
                    lease_expires_at,
                    input_watermark,
                    now,
                    now,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM project_memory_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        return dict(claimed) if claimed is not None else None

    def finish_project_memory_job(
        self,
        project_id: str,
        *,
        phase: Literal["phase1", "phase2"],
        job_key: str,
        lease_owner: str,
        status: Literal["succeeded", "succeeded_no_output", "failed"],
        completed_watermark: int | None = None,
        error: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> bool:
        """Finish only the lease owned by this worker."""
        now = _now_ms()
        retry_at = (
            now + max(1_000, int(retry_after_ms))
            if status == "failed" and retry_after_ms is not None
            else None
        )
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE project_memory_jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = ?, completed_watermark = ?,
                    error_json = ?, updated_at = ?, completed_at = ?
                WHERE project_id = ? AND phase = ? AND job_key = ?
                  AND status = 'running' AND lease_owner = ?
                """,
                (
                    status,
                    retry_at,
                    completed_watermark,
                    _safe_json(error) if error is not None else None,
                    now,
                    now,
                    project_id,
                    phase,
                    job_key,
                    lease_owner,
                ),
            )
            connection.commit()
        return cursor.rowcount > 0

    def project_memory_job_status(self, project_id: str) -> dict[str, Any]:
        """Return the latest observable job state for each memory phase."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        phases: dict[str, dict[str, Any] | None] = {"phase1": None, "phase2": None}
        with self._lock, self._connection() as connection:
            for phase in phases:
                row = connection.execute(
                    """
                    SELECT *
                    FROM project_memory_jobs
                    WHERE project_id = ? AND phase = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (project_id, phase),
                ).fetchone()
                if row is not None:
                    payload = dict(row)
                    raw_error = payload.get("error_json")
                    if isinstance(raw_error, str) and raw_error:
                        try:
                            payload["error"] = json.loads(raw_error)
                        except json.JSONDecodeError:
                            payload["error"] = {"message": raw_error[:1_000]}
                    else:
                        payload["error"] = None
                    payload.pop("error_json", None)
                    phases[phase] = payload
        return {
            "project_id": project_id,
            "input_watermark": self.project_memory_input_watermark(project_id),
            **phases,
        }

    def replace_consolidated_project_memories(
        self,
        project_id: str,
        entries: list[dict[str, Any]],
        *,
        selected_stage1_ids: list[str],
    ) -> list[str]:
        """Atomically replace structured consolidated entries for one project."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        selected = list(dict.fromkeys(selected_stage1_ids))
        now = _now_ms()
        created_ids: list[str] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stage1_by_id = {
                str(row["id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM project_memory_stage1 WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
            if any(stage1_id not in stage1_by_id for stage1_id in selected):
                connection.rollback()
                raise StateStoreError("selected stage1 memory does not belong to project")
            connection.execute(
                """
                DELETE FROM project_memory_sources
                WHERE project_id = ? AND memory_id IN (
                    SELECT id FROM project_memories
                    WHERE project_id = ?
                )
                """,
                (project_id, project_id),
            )
            connection.execute(
                "DELETE FROM project_memories WHERE project_id = ?",
                (project_id,),
            )
            for entry in entries[:200]:
                kind = str(entry.get("kind") or "reference").strip()[:80]
                title = str(entry.get("title") or "").strip()[:500]
                content = str(entry.get("content") or "").strip()[:64_000]
                if not content:
                    continue
                requested_source_ids = [
                    str(item)
                    for item in entry.get("stage1_ids", [])
                    if str(item).strip()
                ]
                if any(item not in stage1_by_id for item in requested_source_ids):
                    connection.rollback()
                    raise StateStoreError(
                        "consolidated memory source does not belong to project"
                    )
                source_ids = requested_source_ids
                source_ids = list(dict.fromkeys(source_ids))
                memory_key = str(entry.get("key") or title or hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest())
                memory_id = _stable_id(
                    "mem",
                    f"{project_id}:{kind}:consolidated:{memory_key}",
                )
                confidence_raw = entry.get("confidence")
                confidence = None
                if confidence_raw is not None:
                    confidence = max(0.0, min(float(confidence_raw), 1.0))
                first_source = stage1_by_id[source_ids[0]] if source_ids else None
                source_session_id = (
                    str(first_source["source_session_id"]) if first_source is not None else None
                )
                connection.execute(
                    """
                    INSERT INTO project_memories(
                        id, project_id, kind, title, content,
                        source_session_id, confidence, status,
                        usage_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)
                    """,
                    (
                        memory_id,
                        project_id,
                        kind,
                        title,
                        content,
                        source_session_id,
                        confidence,
                        now,
                        now,
                    ),
                )
                for stage1_id in source_ids:
                    source = stage1_by_id[stage1_id]
                    source_id = _stable_id(
                        "msrc",
                        f"{project_id}:{memory_id}:{stage1_id}",
                    )
                    connection.execute(
                        """
                        INSERT INTO project_memory_sources(
                            id, project_id, memory_id, stage1_id,
                            source_session_id, evidence_locator, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            project_id,
                            memory_id,
                            stage1_id,
                            str(source["source_session_id"]),
                            f"stage1:{stage1_id}",
                            now,
                        ),
                    )
                created_ids.append(memory_id)
            connection.execute(
                """
                UPDATE project_memory_stage1
                SET selected_for_consolidation = 0,
                    selected_source_revision = NULL
                WHERE project_id = ?
                """,
                (project_id,),
            )
            if selected:
                placeholders = ",".join("?" for _ in selected)
                connection.execute(
                    f"""
                    UPDATE project_memory_stage1
                    SET selected_for_consolidation = 1,
                        selected_source_revision = source_rollout_revision
                    WHERE project_id = ? AND id IN ({placeholders})
                    """,
                    (project_id, *selected),
                )
            connection.execute(
                "DELETE FROM project_cache WHERE project_id = ? AND namespace = 'memory'",
                (project_id,),
            )
            connection.commit()
        return created_ids

    @staticmethod
    def _validate_project_memory_source(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        source_session_id: str | None,
        source_turn_id: str | None,
    ) -> None:
        if source_session_id is not None:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
                (source_session_id, project_id),
            ).fetchone()
            if session is None:
                raise StateStoreError("memory source session does not belong to project")
        if source_turn_id is not None:
            turn = connection.execute(
                "SELECT id FROM turns WHERE id = ? AND project_id = ?",
                (source_turn_id, project_id),
            ).fetchone()
            if turn is None:
                raise StateStoreError("memory source turn does not belong to project")

    def replace_project_document(
        self,
        project_id: str,
        *,
        relative_path: str,
        chunks: list[str],
        content_hash: str | None = None,
        source_artifact_id: str | None = None,
    ) -> str:
        """Replace one project's RAG document and chunks atomically."""
        project = self.get_project(project_id)
        if project is None:
            raise StateStoreError("project not found")
        normalized = Path(relative_path).as_posix().lstrip("/")
        if not normalized or normalized == "." or ".." in Path(normalized).parts:
            raise StateStoreError("invalid project document path")
        document_id = _stable_id("doc", f"{project_id}:{normalized}")
        now = _now_ms()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if source_artifact_id is not None:
                artifact = connection.execute(
                    "SELECT id FROM artifacts WHERE id = ? AND project_id = ?",
                    (source_artifact_id, project_id),
                ).fetchone()
                if artifact is None:
                    connection.rollback()
                    raise StateStoreError("source artifact does not belong to project")
            connection.execute(
                """
                INSERT INTO project_documents(
                    id, project_id, source_artifact_id, relative_path,
                    content_hash, indexing_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_artifact_id = excluded.source_artifact_id,
                    content_hash = excluded.content_hash,
                    indexing_status = 'ready',
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    project_id,
                    source_artifact_id,
                    normalized,
                    content_hash,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM project_chunks WHERE document_id = ? AND project_id = ?",
                (document_id, project_id),
            )
            for ordinal, text in enumerate(chunks):
                chunk_id = _stable_id("chk", f"{document_id}:{ordinal}")
                connection.execute(
                    """
                    INSERT INTO project_chunks(
                        id, project_id, document_id, ordinal, text
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk_id, project_id, document_id, ordinal, str(text)),
                )
            connection.commit()
        return document_id

    def search_project_chunks(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search only the requested project's indexed text chunks."""
        terms = [term for term in query.strip().split() if term]
        if not terms:
            return []
        clauses = " AND ".join("LOWER(c.text) LIKE ?" for _ in terms)
        params: list[Any] = [project_id, *(f"%{term.lower()}%" for term in terms)]
        params.append(max(1, min(int(limit), 100)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, c.project_id, c.document_id, c.ordinal, c.text,
                       d.relative_path
                FROM project_chunks c
                JOIN project_documents d
                  ON d.id = c.document_id AND d.project_id = c.project_id
                WHERE c.project_id = ? AND {clauses}
                ORDER BY d.updated_at DESC, c.ordinal
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def reindex_project_text_artifacts(self, project_id: str) -> dict[str, int]:
        """Rebuild the bounded lexical index for ready text artifacts in one project."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM artifacts
                WHERE project_id = ? AND status = 'ready'
                ORDER BY updated_at DESC, id
                """,
                (project_id,),
            ).fetchall()
        artifact_ids = [str(row["id"]) for row in rows]
        indexed = 0
        skipped = 0
        missing = 0
        for artifact_id in artifact_ids:
            try:
                artifact, path = self.resolve_artifact_path(artifact_id)
            except StateStoreError:
                missing += 1
                continue
            before = self._project_document_count(project_id, artifact.id)
            self._index_text_artifact(artifact, path)
            after = self._project_document_count(project_id, artifact.id)
            if after > 0:
                indexed += 1
            elif before == 0:
                skipped += 1
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM project_cache WHERE project_id = ? AND namespace = 'rag'",
                (project_id,),
            )
            connection.commit()
        return {
            "artifacts_seen": len(artifact_ids),
            "indexed": indexed,
            "skipped": skipped,
            "missing": missing,
        }

    def _project_document_count(self, project_id: str, artifact_id: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM project_documents
                WHERE project_id = ? AND source_artifact_id = ?
                """,
                (project_id, artifact_id),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def put_project_cache(
        self,
        project_id: str,
        namespace: str,
        cache_key: str,
        value: Any,
        *,
        ttl_ms: int | None = None,
    ) -> str:
        """Write a cache entry whose uniqueness is namespaced by project."""
        if self.get_project(project_id) is None:
            raise StateStoreError("project not found")
        normalized_namespace = namespace.strip()
        normalized_key = cache_key.strip()
        if not normalized_namespace or not normalized_key:
            raise StateStoreError("cache namespace and key are required")
        entry_id = _stable_id(
            "cache",
            f"{project_id}:{normalized_namespace}:{normalized_key}",
        )
        now = _now_ms()
        expires_at = now + ttl_ms if ttl_ms is not None and ttl_ms > 0 else None
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO project_cache(
                    id, project_id, namespace, cache_key, value_json,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, namespace, cache_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    project_id,
                    normalized_namespace,
                    normalized_key,
                    _safe_json(value),
                    expires_at,
                    now,
                    now,
                ),
            )
            connection.commit()
        return entry_id

    def get_project_cache(
        self,
        project_id: str,
        namespace: str,
        cache_key: str,
    ) -> Any | None:
        now = _now_ms()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT value_json FROM project_cache
                WHERE project_id = ? AND namespace = ? AND cache_key = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (project_id, namespace.strip(), cache_key.strip(), now),
            ).fetchone()
        return json.loads(str(row["value_json"])) if row is not None else None

    def sync_project_schedules(self, jobs: list[Any]) -> int:
        """Mirror project-owned CronService jobs into the relational graph."""
        now = _now_ms()
        synced_ids: set[str] = set()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job in jobs:
                payload = getattr(job, "payload", None)
                project_id = getattr(payload, "project_id", None)
                if not isinstance(project_id, str) or not project_id:
                    continue
                if connection.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (project_id,)
                ).fetchone() is None:
                    continue
                schedule_id = str(getattr(job, "id", "")).strip()
                if not schedule_id:
                    continue
                existing_schedule = connection.execute(
                    "SELECT project_id FROM schedules WHERE id = ?",
                    (schedule_id,),
                ).fetchone()
                if (
                    existing_schedule is not None
                    and str(existing_schedule["project_id"]) != project_id
                ):
                    connection.rollback()
                    raise StateStoreError(
                        "schedule id is already bound to another project"
                    )
                created_session_id = getattr(payload, "created_session_id", None)
                if created_session_id and connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ? AND project_id = ?",
                    (created_session_id, project_id),
                ).fetchone() is None:
                    created_session_id = None
                schedule = getattr(job, "schedule", None)
                schedule_json = _safe_json({
                    "kind": getattr(schedule, "kind", None),
                    "at_ms": getattr(schedule, "at_ms", None),
                    "every_ms": getattr(schedule, "every_ms", None),
                    "expr": getattr(schedule, "expr", None),
                    "tz": getattr(schedule, "tz", None),
                })
                connection.execute(
                    """
                    INSERT INTO schedules(
                        id, project_id, name, cron, prompt, status,
                        created_session_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        cron = excluded.cron,
                        prompt = excluded.prompt,
                        status = excluded.status,
                        created_session_id = COALESCE(
                            schedules.created_session_id,
                            excluded.created_session_id
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        schedule_id,
                        project_id,
                        str(getattr(job, "name", "")),
                        schedule_json,
                        str(getattr(payload, "message", "")),
                        "active" if bool(getattr(job, "enabled", False)) else "paused",
                        created_session_id,
                        int(getattr(job, "created_at_ms", now) or now),
                        int(getattr(job, "updated_at_ms", now) or now),
                    ),
                )
                synced_ids.add(schedule_id)
            for row in connection.execute("SELECT id FROM schedules").fetchall():
                schedule_id = str(row["id"])
                if schedule_id not in synced_ids:
                    connection.execute(
                        "UPDATE schedules SET status = 'deleted', updated_at = ? WHERE id = ?",
                        (now, schedule_id),
                    )
            connection.commit()
        return len(synced_ids)

    def list_project_sessions(self, project_id: str) -> list[SessionRecord]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE project_id = ? AND status != 'archived'
                ORDER BY updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._session_record(row) for row in rows]

    def list_sessions(self, *, include_archived: bool = False) -> list[SessionRecord]:
        where = "" if include_archived else "WHERE status != 'archived'"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM sessions
                {where}
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [self._session_record(row) for row in rows]

    def next_event_sequence(self, session_key: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT next_event_seq FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            raise StateStoreError(f"session is not registered: {session_key}")
        return int(row["next_event_seq"])

    def project_event(self, session_key: str, event: dict[str, Any]) -> bool:
        """Idempotently project one durable WebUI journal event into query tables."""
        session = self.get_session(session_key)
        if session is None:
            raise EventProjectionError(f"session is not registered: {session_key}")
        event_id = str(event.get("event_id") or "").strip()
        event_type = str(event.get("event") or event.get("type") or "").strip()
        raw_seq = event.get("event_seq")
        if not event_id or not event_type or isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
            raise EventProjectionError("event envelope is incomplete")
        if event.get("project_id") != session.project_id or event.get("session_id") != session.id:
            raise EventProjectionError("event identity does not match its session")
        recorded_at = event.get("recorded_at")
        if isinstance(recorded_at, bool) or not isinstance(recorded_at, int):
            recorded_at = _now_ms()

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM projected_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if duplicate is not None:
                connection.rollback()
                return False
            sequence_owner = connection.execute(
                """
                SELECT event_id FROM projected_events
                WHERE session_id = ? AND event_seq = ?
                """,
                (session.id, raw_seq),
            ).fetchone()
            if sequence_owner is not None:
                connection.rollback()
                raise EventProjectionError(
                    f"event sequence {raw_seq} is already owned by another event"
                )
            sequence_state = connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) AS last_event_seq
                FROM projected_events WHERE session_id = ?
                """,
                (session.id,),
            ).fetchone()
            expected_seq = int(sequence_state["last_event_seq"]) + 1
            if raw_seq != expected_seq:
                connection.rollback()
                raise EventProjectionError(
                    f"event sequence gap: expected {expected_seq}, got {raw_seq}"
                )

            self._project_event_row(
                connection,
                session=session,
                event=event,
                event_type=event_type,
                event_seq=raw_seq,
                recorded_at=recorded_at,
            )
            now = _now_ms()
            connection.execute(
                """
                INSERT INTO projected_events(
                    event_id, project_id, session_id, event_seq,
                    event_type, payload_json, recorded_at, projected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session.project_id,
                    session.id,
                    raw_seq,
                    event_type,
                    _event_json(event),
                    recorded_at,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO projector_state(
                    session_id, event_log_path, last_event_seq,
                    last_event_id, last_projected_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    event_log_path = excluded.event_log_path,
                    last_event_seq = MAX(projector_state.last_event_seq, excluded.last_event_seq),
                    last_event_id = CASE
                        WHEN excluded.last_event_seq >= projector_state.last_event_seq
                        THEN excluded.last_event_id ELSE projector_state.last_event_id
                    END,
                    last_projected_at = excluded.last_projected_at,
                    error_json = NULL
                """,
                (
                    session.id,
                    session.event_log_path or "",
                    raw_seq,
                    event_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET next_event_seq = MAX(next_event_seq, ?), updated_at = ?
                WHERE id = ?
                """,
                (raw_seq + 1, now, session.id),
            )
            connection.commit()
        return True

    def _project_event_row(
        self,
        connection: sqlite3.Connection,
        *,
        session: SessionRecord,
        event: dict[str, Any],
        event_type: str,
        event_seq: int,
        recorded_at: int,
    ) -> None:
        external_turn_id = event.get("turn_id")
        turn_id = (
            str(external_turn_id).strip()
            if isinstance(external_turn_id, str) and external_turn_id.strip()
            else None
        )
        if turn_id is not None:
            self._ensure_projected_turn(
                connection,
                session=session,
                turn_id=turn_id,
                started_at=recorded_at,
            )

        if event_type == "user":
            if turn_id is None:
                turn_id = _stable_id("turn", f"{session.id}:{event_seq}")
                self._ensure_projected_turn(
                    connection,
                    session=session,
                    turn_id=turn_id,
                    started_at=recorded_at,
                )
            self._insert_projected_message(
                connection,
                session=session,
                turn_id=turn_id,
                event=event,
                event_seq=event_seq,
                role="user",
                message_kind="user",
                recorded_at=recorded_at,
            )
        elif event_type == "message" and event.get("kind") not in {"reasoning"}:
            kind = str(event.get("kind") or "answer")
            self._insert_projected_message(
                connection,
                session=session,
                turn_id=turn_id,
                event=event,
                event_seq=event_seq,
                role="assistant",
                message_kind=("trace" if kind in {"progress", "tool_hint"} else kind),
                recorded_at=recorded_at,
                is_final=kind not in {"progress", "tool_hint"},
            )
            self._project_tool_events(
                connection,
                session=session,
                turn_id=turn_id,
                events=event.get("tool_events"),
                recorded_at=recorded_at,
            )
            self._project_task_progress(
                connection,
                session=session,
                turn_id=turn_id,
                agent_ui=event.get("agent_ui"),
                recorded_at=recorded_at,
            )
        elif event_type == "turn_started" and turn_id is not None:
            turn = event.get("turn")
            turn_payload = turn if isinstance(turn, dict) else {}
            connection.execute(
                """
                UPDATE turns
                SET status = 'running',
                    started_at = COALESCE(?, started_at),
                    runtime_epoch = COALESCE(?, runtime_epoch),
                    trace_id = COALESCE(?, trace_id),
                    ended_at = NULL,
                    finish_reason = NULL,
                    terminal_event_id = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE id = ? AND project_id = ?
                """,
                (
                    _coerce_timestamp_ms(turn_payload.get("started_at"), recorded_at),
                    _optional_text(turn_payload.get("runtime_epoch")),
                    _optional_text(
                        turn_payload.get("trace_id") or event.get("trace_id")
                    ),
                    turn_id,
                    session.project_id,
                ),
            )
            connection.execute(
                "UPDATE sessions SET active_turn_id = ?, updated_at = ? WHERE id = ?",
                (turn_id, recorded_at, session.id),
            )
        elif event_type in {"turn_completed", "turn_end"} and turn_id is not None:
            turn = event.get("turn")
            turn_payload = turn if isinstance(turn, dict) else {}
            finish_reason = str(event.get("finish_reason") or "completed")
            if event_type == "turn_completed":
                finish_reason = str(turn_payload.get("finish_reason") or finish_reason)
                public_status = str(turn_payload.get("status") or "")
                status = {
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(public_status, "failed")
            else:
                status = {
                    "cancelled": "cancelled",
                    "error": "failed",
                    "failed": "failed",
                }.get(finish_reason, "completed")
            terminal_event_id = str(event.get("event_id") or "")
            existing_terminal = connection.execute(
                "SELECT terminal_event_id FROM turns WHERE id = ? AND project_id = ?",
                (turn_id, session.project_id),
            ).fetchone()
            if (
                existing_terminal is not None
                and existing_terminal["terminal_event_id"]
                and existing_terminal["terminal_event_id"] != terminal_event_id
            ):
                if event_type == "turn_end":
                    # Legacy terminal notification follows the authoritative
                    # v2 turn_completed during the compatibility window. It is
                    # journaled for old clients but cannot overwrite lifecycle
                    # state or count as a conflicting terminal.
                    return
                raise EventProjectionError(
                    f"turn {turn_id!r} already has another terminal event"
                )
            ended_at = _coerce_timestamp_ms(
                turn_payload.get("completed_at"),
                recorded_at,
            )
            turn_error = turn_payload.get("error")
            error_payload = turn_error if isinstance(turn_error, dict) else {}
            turn_usage = turn_payload.get("usage")
            usage_payload = turn_usage if isinstance(turn_usage, dict) else event.get("usage")
            usage_json = (
                json.dumps(usage_payload, ensure_ascii=False, separators=(",", ":"))
                if isinstance(usage_payload, dict) and usage_payload
                else None
            )
            connection.execute(
                """
                UPDATE turns
                SET status = ?, ended_at = ?,
                    runtime_epoch = COALESCE(?, runtime_epoch),
                    trace_id = COALESCE(?, trace_id),
                    finish_reason = ?,
                    terminal_event_id = ?,
                    usage_json = COALESCE(?, usage_json),
                    error_code = CASE
                        WHEN ? = 'failed' THEN COALESCE(?, error_code, 'TURN_FAILED')
                        ELSE error_code
                    END,
                    error_message = CASE
                        WHEN ? = 'failed' THEN COALESCE(?, error_message)
                        ELSE error_message
                    END
                WHERE id = ? AND project_id = ?
                """,
                (
                    status,
                    ended_at,
                    _optional_text(turn_payload.get("runtime_epoch")),
                    _optional_text(
                        turn_payload.get("trace_id") or event.get("trace_id")
                    ),
                    finish_reason,
                    terminal_event_id,
                    usage_json,
                    status,
                    _optional_text(error_payload.get("code")),
                    status,
                    _optional_text(error_payload.get("message")),
                    turn_id,
                    session.project_id,
                ),
            )
            tool_terminal = "cancelled" if status == "cancelled" else "failed"
            connection.execute(
                """
                UPDATE tool_calls
                SET status = ?, ended_at = ?,
                    error_code = COALESCE(error_code, 'INCOMPLETE_TOOL_EVENT')
                WHERE turn_id = ? AND project_id = ?
                  AND status IN ('pending', 'running')
                """,
                (tool_terminal, ended_at, turn_id, session.project_id),
            )
            progress_terminal = (
                "cancelled" if status == "cancelled"
                else "failed" if status == "failed"
                else "completed"
            )
            connection.execute(
                """
                UPDATE turn_progress
                SET status = ?,
                    revision = revision + 1,
                    current_step_key = NULL,
                    active_step_ids_json = '[]',
                    terminalized_at = COALESCE(terminalized_at, ?),
                    terminalization_reason = COALESCE(
                        terminalization_reason,
                        ?
                    ),
                    updated_at = ?
                WHERE turn_id = ? AND project_id = ?
                  AND status IN ('pending', 'running')
                """,
                (
                    progress_terminal,
                    ended_at,
                    finish_reason,
                    ended_at,
                    turn_id,
                    session.project_id,
                ),
            )
            step_terminal = "cancelled" if status == "cancelled" else (
                "failed" if status == "failed" else "completed"
            )
            connection.execute(
                """
                UPDATE turn_steps
                SET status = CASE
                        WHEN ? IN ('completed', 'failed') AND status = 'pending'
                        THEN 'cancelled'
                        ELSE ?
                    END,
                    ended_at = COALESCE(ended_at, ?), updated_at = ?
                WHERE turn_id = ? AND project_id = ?
                  AND status IN ('pending', 'running')
                """,
                (
                    status,
                    step_terminal,
                    ended_at,
                    ended_at,
                    turn_id,
                    session.project_id,
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET active_turn_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (ended_at, session.id),
            )
            connection.execute(
                """
                UPDATE expert_team_runs
                SET status = ?,
                    current_stage_key = NULL,
                    terminalized_at = COALESCE(terminalized_at, ?),
                    terminalization_reason = COALESCE(
                        terminalization_reason,
                        ?
                    ),
                    updated_at = ?
                WHERE turn_id = ? AND project_id = ?
                  AND status IN ('pending', 'running')
                """,
                (
                    progress_terminal,
                    ended_at,
                    finish_reason,
                    ended_at,
                    turn_id,
                    session.project_id,
                ),
            )

    @staticmethod
    def _ensure_projected_turn(
        connection: sqlite3.Connection,
        *,
        session: SessionRecord,
        turn_id: str,
        started_at: int,
    ) -> None:
        existing = connection.execute(
            "SELECT 1 FROM turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        if existing is not None:
            return
        row = connection.execute(
            "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM turns WHERE session_id = ?",
            (session.id,),
        ).fetchone()
        turn_index = int(row[0]) if row is not None else 1
        connection.execute(
            """
            INSERT INTO turns(
                id, project_id, session_id, turn_index,
                status, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (
                turn_id,
                session.project_id,
                session.id,
                turn_index,
                started_at,
            ),
        )
        connection.execute(
            """
            UPDATE sessions
            SET active_turn_id = ?,
                status = CASE WHEN status = 'archived' THEN status ELSE 'active' END,
                updated_at = ?
            WHERE id = ?
            """,
            (turn_id, started_at, session.id),
        )

    @staticmethod
    def _insert_projected_message(
        connection: sqlite3.Connection,
        *,
        session: SessionRecord,
        turn_id: str | None,
        event: dict[str, Any],
        event_seq: int,
        role: str,
        message_kind: str,
        recorded_at: int,
        is_final: bool = True,
    ) -> None:
        message_id = _stable_id(
            "msg",
            f"{session.id}:{event.get('event_id')}:{role}",
        )
        content = {
            key: value
            for key, value in event.items()
            if key not in {
                "event_id",
                "event_seq",
                "schema_version",
                "recorded_at",
                "project_id",
                "session_id",
            }
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, project_id, session_id, turn_id, sequence_no,
                role, message_kind, content_json, is_final,
                event_id, segment_id, revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                session.project_id,
                session.id,
                turn_id,
                event_seq,
                role,
                message_kind[:64],
                _safe_json(content),
                1 if is_final else 0,
                str(event.get("event_id") or "") or None,
                str(event.get("segment_id") or event.get("stream_id") or "") or None,
                _coerce_nonnegative_int(
                    event.get("revision")
                    or event.get("snapshot_revision")
                    or 0
                ),
                recorded_at,
            ),
        )

    @staticmethod
    def _project_tool_events(
        connection: sqlite3.Connection,
        *,
        session: SessionRecord,
        turn_id: str | None,
        events: Any,
        recorded_at: int,
    ) -> None:
        if turn_id is None or not isinstance(events, list):
            return
        for raw in events[:100]:
            if not isinstance(raw, dict):
                continue
            call_id = str(raw.get("call_id") or "").strip()
            tool_name = str(raw.get("name") or "").strip()
            if not call_id or not tool_name:
                continue
            tool_call_id = _stable_id("tool", f"{session.id}:{call_id}")
            phase = str(raw.get("phase") or "start")
            status = {
                "start": "running",
                "end": "succeeded",
                "error": "failed",
            }.get(phase, "running")
            ended_at = recorded_at if status in {"succeeded", "failed"} else None
            connection.execute(
                """
                INSERT INTO tool_calls(
                    id, project_id, session_id, turn_id, tool_name,
                    status, input_json, output_summary_json,
                    started_at, ended_at, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    output_summary_json = COALESCE(
                        excluded.output_summary_json,
                        tool_calls.output_summary_json
                    ),
                    ended_at = COALESCE(excluded.ended_at, tool_calls.ended_at),
                    error_code = COALESCE(excluded.error_code, tool_calls.error_code),
                    error_message = COALESCE(excluded.error_message, tool_calls.error_message)
                """,
                (
                    tool_call_id,
                    session.project_id,
                    session.id,
                    turn_id,
                    tool_name[:128],
                    status,
                    _safe_json(raw.get("arguments")),
                    _safe_json(raw.get("result")) if raw.get("result") is not None else None,
                    int(raw.get("occurred_at") or recorded_at),
                    ended_at,
                    "TOOL_FAILED" if status == "failed" else None,
                    str(raw.get("error") or "")[:2_000] or None,
                ),
            )

    @staticmethod
    def _project_task_progress(
        connection: sqlite3.Connection,
        *,
        session: SessionRecord,
        turn_id: str | None,
        agent_ui: Any,
        recorded_at: int,
    ) -> None:
        if (
            turn_id is None
            or not isinstance(agent_ui, dict)
            or agent_ui.get("kind") != "task_progress"
        ):
            return
        bound_turn_id = _optional_text(agent_ui.get("turn_id"))
        if bound_turn_id is not None and bound_turn_id != turn_id:
            raise EventProjectionError(
                f"task progress turn {bound_turn_id!r} does not match event turn {turn_id!r}"
            )
        current_progress = connection.execute(
            "SELECT revision, kind, status FROM turn_progress WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        supplied_revision = agent_ui.get("revision")
        if (
            isinstance(supplied_revision, int)
            and not isinstance(supplied_revision, bool)
            and supplied_revision > 0
        ):
            revision = supplied_revision
            if (
                current_progress is not None
                and revision <= int(current_progress["revision"])
            ):
                return
        else:
            revision = (
                int(current_progress["revision"]) + 1
                if current_progress is not None
                else 1
            )
        steps = agent_ui.get("steps")
        if not isinstance(steps, list):
            return
        plan_kind = (
            "workflow"
            if agent_ui.get("plan_kind") == "workflow"
            or agent_ui.get("team_id")
            or agent_ui.get("team_run_id")
            else "dynamic"
        )
        execution = str(agent_ui.get("execution") or (
            "staged" if plan_kind == "workflow" else "serial"
        ))
        if (
            current_progress is not None
            and str(current_progress["kind"] or "dynamic") == "workflow"
            and plan_kind != "workflow"
        ):
            # A model-authored dynamic plan can never replace a runtime-owned
            # expert-team workflow plan.
            return
        workflow_takeover = (
            current_progress is not None
            and str(current_progress["kind"] or "dynamic") != "workflow"
            and plan_kind == "workflow"
        )
        normalized: list[
            tuple[str, str, str, str | None, str | None, str | None, str | None]
        ] = []
        for raw in steps[:100]:
            if not isinstance(raw, dict):
                continue
            step_key = str(raw.get("id") or "").strip()
            title = str(raw.get("title") or "").strip()
            raw_status = str(raw.get("status") or "pending")
            status = {
                "error": "failed",
                "done": "completed",
                "skipped": "cancelled",
                "interrupted": "cancelled",
            }.get(raw_status, raw_status)
            if not step_key or not title or status not in {
                "pending", "running", "completed", "failed", "cancelled"
            }:
                continue
            detail = str(raw.get("detail") or "").strip() or None
            step_kind = str(raw.get("kind") or "").strip() or None
            stage_key = str(raw.get("stage_key") or "").strip() or None
            warning = str(raw.get("warning") or "").strip() or None
            normalized.append(
                (step_key, title, status, detail, step_kind, stage_key, warning)
            )
        if not normalized:
            return
        running_count = sum(1 for step in normalized if step[2] == "running")
        non_terminal = any(
            step[2] in {"pending", "running"} for step in normalized
        )
        if (
            execution == "serial"
            and ((non_terminal and running_count != 1) or running_count > 1)
        ):
            raise EventProjectionError(
                "task progress must have exactly one running step until terminal"
            )
        existing_steps = connection.execute(
            """
            SELECT step_key, title
            FROM turn_steps
            WHERE turn_id = ? AND project_id = ?
            ORDER BY ordinal
            """,
            (turn_id, session.project_id),
        ).fetchall()
        if existing_steps and not workflow_takeover:
            previous_signature = [
                (str(row["step_key"]), str(row["title"])) for row in existing_steps
            ]
            next_signature = [(step[0], step[1]) for step in normalized]
            if previous_signature != next_signature:
                raise EventProjectionError(
                    "task progress ids, order, and titles are immutable within a turn"
                )
        if workflow_takeover:
            connection.execute(
                "DELETE FROM turn_steps WHERE turn_id = ? AND project_id = ?",
                (turn_id, session.project_id),
            )
        current_step = str(agent_ui.get("current_step_id") or "").strip() or None
        active_step_ids = [
            str(value).strip()
            for value in (
                agent_ui.get("active_step_ids")
                if isinstance(agent_ui.get("active_step_ids"), list)
                else [step[0] for step in normalized if step[2] == "running"]
            )
            if str(value).strip()
        ]
        if current_step is None and len(active_step_ids) == 1:
            current_step = active_step_ids[0]
        overall = (
            "failed" if any(step[2] == "failed" for step in normalized)
            else "running" if any(step[2] == "running" for step in normalized)
            else "completed" if all(step[2] == "completed" for step in normalized)
            else "pending"
        )
        if (
            current_progress is not None
            and str(current_progress["status"]) in {"completed", "failed", "cancelled"}
            and overall in {"pending", "running"}
        ):
            # A delayed model/tool frame cannot resurrect a terminal plan.
            return
        connection.execute(
            """
            INSERT INTO turn_progress(
                turn_id, project_id, session_id, plan_id,
                kind, owner, policy, execution, revision,
                current_step_key, active_step_ids_json, signature_version,
                note, status, terminalized_at, terminalization_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                kind = excluded.kind,
                owner = excluded.owner,
                policy = excluded.policy,
                execution = excluded.execution,
                revision = excluded.revision,
                current_step_key = excluded.current_step_key,
                active_step_ids_json = excluded.active_step_ids_json,
                signature_version = excluded.signature_version,
                note = excluded.note,
                status = excluded.status,
                terminalized_at = COALESCE(
                    excluded.terminalized_at,
                    turn_progress.terminalized_at
                ),
                terminalization_reason = COALESCE(
                    excluded.terminalization_reason,
                    turn_progress.terminalization_reason
                ),
                updated_at = excluded.updated_at
            """,
            (
                turn_id,
                session.project_id,
                session.id,
                str(agent_ui.get("plan_id") or f"plan:{turn_id}"),
                plan_kind,
                str(agent_ui.get("owner") or (
                    f"expert_team:{agent_ui.get('team_id')}"
                    if plan_kind == "workflow"
                    else "agent"
                )),
                str(agent_ui.get("policy") or "required"),
                execution,
                revision,
                current_step,
                _safe_json(active_step_ids),
                int(agent_ui.get("signature_version") or 1),
                str(agent_ui.get("note") or "")[:2_000] or None,
                overall,
                recorded_at if overall in {"completed", "failed", "cancelled"} else None,
                (
                    str(agent_ui.get("terminalization_reason") or "")[:500] or None
                    if overall in {"completed", "failed", "cancelled"}
                    else None
                ),
                recorded_at,
            ),
        )
        for ordinal, (
            step_key,
            title,
            status,
            detail,
            step_kind,
            stage_key,
            warning,
        ) in enumerate(normalized):
            step_id = _stable_id("step", f"{turn_id}:{step_key}")
            connection.execute(
                """
                INSERT INTO turn_steps(
                    id, project_id, session_id, turn_id, step_key,
                    ordinal, title, step_kind, stage_key, status, detail,
                    warning, started_at, ended_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, step_key) DO UPDATE SET
                    ordinal = excluded.ordinal,
                    title = excluded.title,
                    step_kind = excluded.step_kind,
                    stage_key = excluded.stage_key,
                    status = excluded.status,
                    detail = excluded.detail,
                    warning = excluded.warning,
                    started_at = COALESCE(turn_steps.started_at, excluded.started_at),
                    ended_at = excluded.ended_at,
                    updated_at = excluded.updated_at
                """,
                (
                    step_id,
                    session.project_id,
                    session.id,
                    turn_id,
                    step_key,
                    ordinal,
                    title[:500],
                    step_kind,
                    stage_key,
                    status,
                    detail,
                    warning,
                    recorded_at if status == "running" else None,
                    recorded_at if status in {"completed", "failed", "cancelled"} else None,
                    recorded_at,
                ),
            )
        team_run_id = _optional_text(agent_ui.get("team_run_id"))
        team_id = _optional_text(agent_ui.get("team_id"))
        if plan_kind == "workflow" and team_run_id and team_id:
            stage_key = _optional_text(agent_ui.get("stage_key"))
            connection.execute(
                """
                INSERT INTO expert_team_runs(
                    id, project_id, session_id, turn_id, team_id, plan_id,
                    status, current_stage_key, warning_count,
                    created_at, updated_at, terminalized_at,
                    terminalization_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    current_stage_key = excluded.current_stage_key,
                    warning_count = excluded.warning_count,
                    updated_at = excluded.updated_at,
                    terminalized_at = excluded.terminalized_at,
                    terminalization_reason = excluded.terminalization_reason
                """,
                (
                    team_run_id,
                    session.project_id,
                    session.id,
                    turn_id,
                    team_id,
                    str(agent_ui.get("plan_id") or f"plan:{turn_id}"),
                    overall,
                    stage_key,
                    sum(1 for step in normalized if step[6]),
                    recorded_at,
                    recorded_at,
                    recorded_at if overall in {"completed", "failed", "cancelled"} else None,
                    (
                        str(agent_ui.get("terminalization_reason") or "")[:500] or None
                        if overall in {"completed", "failed", "cancelled"}
                        else None
                    ),
                ),
            )

    def reconcile_incomplete_runs(self, *, error_code: str = "GATEWAY_RESTARTED") -> int:
        """Close stale running projections during startup so clients never spin forever."""
        now = _now_ms()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn_rows = connection.execute(
                "SELECT id, project_id FROM turns WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in turn_rows:
                connection.execute(
                    """
                    UPDATE turns SET status = 'failed', ended_at = ?,
                        error_code = ?, error_message = 'gateway restarted before terminal event'
                    WHERE id = ? AND project_id = ?
                    """,
                    (now, error_code, row["id"], row["project_id"]),
                )
            connection.execute(
                """
                UPDATE tool_calls SET status = 'failed', ended_at = ?,
                    error_code = COALESCE(error_code, ?)
                WHERE status IN ('pending', 'running')
                """,
                (now, error_code),
            )
            connection.execute(
                """
                UPDATE turn_progress
                SET status = 'failed',
                    revision = revision + 1,
                    current_step_key = NULL,
                    active_step_ids_json = '[]',
                    terminalized_at = COALESCE(terminalized_at, ?),
                    terminalization_reason = COALESCE(
                        terminalization_reason,
                        ?
                    ),
                    updated_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now, error_code, now),
            )
            connection.execute(
                """
                UPDATE turn_steps SET status = 'failed',
                    ended_at = COALESCE(ended_at, ?), updated_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE expert_team_runs
                SET status = 'failed',
                    current_stage_key = NULL,
                    terminalized_at = COALESCE(terminalized_at, ?),
                    terminalization_reason = COALESCE(
                        terminalization_reason,
                        ?
                    ),
                    updated_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now, error_code, now),
            )
            connection.execute("UPDATE sessions SET active_turn_id = NULL")
            connection.commit()
        return len(turn_rows)

    def incomplete_turn_snapshots(self) -> list[dict[str, Any]]:
        """List stale durable running rows for journal-first startup recovery."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    t.id AS turn_id,
                    t.runtime_epoch,
                    t.started_at,
                    s.session_key,
                    s.id AS session_id,
                    s.project_id
                FROM turns AS t
                JOIN sessions AS s ON s.id = t.session_id
                WHERE t.status IN ('queued', 'running')
                ORDER BY t.started_at, t.id
                """
            ).fetchall()
        return [
            {
                "turn_id": str(row["turn_id"]),
                "runtime_epoch": row["runtime_epoch"],
                "started_at": int(row["started_at"]),
                "session_key": str(row["session_key"]),
                "session_id": str(row["session_id"]),
                "project_id": str(row["project_id"]),
            }
            for row in rows
        ]

    def projection_counts(self, session_key: str) -> dict[str, int]:
        session = self.get_session(session_key)
        if session is None:
            return {}
        with self._lock, self._connection() as connection:
            counts = {}
            for table in (
                "projected_events",
                "turns",
                "messages",
                "tool_calls",
                "turn_steps",
            ):
                counts[table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
                        (session.id,),
                    ).fetchone()[0]
                )
        return counts

    def projector_watermark(self, session_key: str) -> dict[str, Any] | None:
        session = self.get_session(session_key)
        if session is None:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT last_event_seq, last_event_id, last_projected_at, error_json
                FROM projector_state
                WHERE session_id = ?
                """,
                (session.id,),
            ).fetchone()
        if row is None:
            return None
        error: Any = None
        if row["error_json"]:
            try:
                error = json.loads(str(row["error_json"]))
            except json.JSONDecodeError:
                error = {"message": str(row["error_json"])}
        return {
            "last_event_seq": int(row["last_event_seq"]),
            "last_event_id": row["last_event_id"],
            "last_projected_at": row["last_projected_at"],
            "error": error,
        }

    def backfill_projected_event_payloads(
        self,
        session_key: str,
        events: list[dict[str, Any]],
    ) -> int:
        """Attach canonical payloads to already-projected legacy envelopes.

        Schema v6 recorded only identity/type metadata.  During lazy journal
        recovery we already have the exact source envelopes, so upgrading the
        read model does not require re-projecting business rows.
        """
        session = self.get_session(session_key)
        if session is None or not events:
            return 0
        updated = 0
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for event in events:
                event_id = _optional_text(event.get("event_id"))
                event_seq = event.get("event_seq")
                if (
                    event_id is None
                    or isinstance(event_seq, bool)
                    or not isinstance(event_seq, int)
                ):
                    continue
                cursor = connection.execute(
                    """
                    UPDATE projected_events
                    SET payload_json = ?
                    WHERE event_id = ? AND project_id = ? AND session_id = ?
                      AND event_seq = ?
                      AND (payload_json IS NULL OR payload_json = '')
                    """,
                    (
                        _event_json(event),
                        event_id,
                        session.project_id,
                        session.id,
                        event_seq,
                    ),
                )
                updated += max(0, int(cursor.rowcount))
            connection.commit()
        return updated

    def session_event_envelopes(
        self,
        session_key: str,
        *,
        after_event_seq: int = 0,
        limit: int = 2_000,
    ) -> list[dict[str, Any]]:
        """Return canonical envelopes from the SQLite read model in order."""
        session = self.get_session(session_key)
        if session is None:
            return []
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_seq, event_type, recorded_at, payload_json
                FROM projected_events
                WHERE project_id = ? AND session_id = ? AND event_seq > ?
                ORDER BY event_seq
                LIMIT ?
                """,
                (
                    session.project_id,
                    session.id,
                    max(0, int(after_event_seq)),
                    max(1, min(int(limit), 10_000)),
                ),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] = {}
            raw = row["payload_json"]
            if raw:
                try:
                    decoded = json.loads(str(raw))
                    if isinstance(decoded, dict):
                        payload = decoded
                except json.JSONDecodeError:
                    payload = {}
            payload.update(
                {
                    "schema_version": int(payload.get("schema_version") or 3),
                    "event_id": str(row["event_id"]),
                    "event_seq": int(row["event_seq"]),
                    "event": str(payload.get("event") or row["event_type"]),
                    "recorded_at": int(row["recorded_at"]),
                    "project_id": session.project_id,
                    "session_id": session.id,
                    "session_key": session.session_key,
                }
            )
            events.append(payload)
        return events

    def session_display_event_envelopes(
        self,
        session_key: str,
        *,
        limit: int = 200,
        before_event_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return only recent message-bearing events for Thread rendering.

        A Thread snapshot must not scan every token/progress event ever written
        by a long conversation.  The messages projection already identifies
        the durable user-visible rows, while ``projected_events.payload_json``
        retains the exact rich WebUI envelope needed by the compatibility
        renderer.
        """
        session = self.get_session(session_key)
        if session is None:
            return []
        before_clause = ""
        params: list[Any] = [session.project_id, session.id]
        if before_event_seq is not None:
            before_clause = "AND m.sequence_no < ?"
            params.append(max(0, int(before_event_seq)))
        params.append(max(1, min(int(limit), 1_001)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    pe.event_id,
                    pe.event_seq,
                    pe.event_type,
                    pe.recorded_at,
                    pe.payload_json
                FROM messages AS m
                JOIN projected_events AS pe
                  ON pe.project_id = m.project_id
                 AND pe.session_id = m.session_id
                 AND pe.event_seq = m.sequence_no
                WHERE m.project_id = ? AND m.session_id = ?
                {before_clause}
                ORDER BY m.sequence_no DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload: dict[str, Any] = {}
            raw = row["payload_json"]
            if raw:
                try:
                    decoded = json.loads(str(raw))
                    if isinstance(decoded, dict):
                        payload = decoded
                except json.JSONDecodeError:
                    payload = {}
            payload.update(
                {
                    "schema_version": int(payload.get("schema_version") or 3),
                    "event_id": str(row["event_id"]),
                    "event_seq": int(row["event_seq"]),
                    "event": str(payload.get("event") or row["event_type"]),
                    "recorded_at": int(row["recorded_at"]),
                    "project_id": session.project_id,
                    "session_id": session.id,
                    "session_key": session.session_key,
                }
            )
            events.append(payload)
        return events

    def session_artifact_revision(self, session_key: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT artifact_revision FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return int(row["artifact_revision"] or 0) if row is not None else 0

    def mark_artifact_indexed(self, session_key: str) -> None:
        now = _now_ms()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET artifact_indexed_at = COALESCE(artifact_indexed_at, ?),
                    updated_at = ?
                WHERE session_key = ?
                """,
                (now, now, session_key),
            )
            connection.commit()

    def register_artifact(
        self,
        session_key: str,
        path: str | Path,
        *,
        relation_type: str = "generated",
        artifact_kind: str = "file",
        mime_type: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ArtifactRecord:
        if relation_type not in _ARTIFACT_RELATIONS:
            raise ValueError(f"unsupported artifact relation: {relation_type}")
        session = self.get_session(session_key)
        if session is None:
            raise StateStoreError(f"session is not registered: {session_key}")
        project = self.get_project(session.project_id)
        if project is None:
            raise StateStoreError(f"project is not registered: {session.project_id}")

        root = Path(project.canonical_root_path).resolve(strict=True)
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise StateStoreError("artifact is outside the session project") from exc
        if not resolved.is_file():
            raise StateStoreError("artifact is not a file")

        stat = resolved.stat()
        digest = self._hash_file(resolved)
        content_artifact_id = _stable_id(
            "art",
            f"{session.project_id}:{session.id}:{relative}:{stat.st_mtime_ns}:{stat.st_size}:{digest}",
        )
        now = _now_ms()
        resolved_mime = mime_type or mimetypes.guess_type(resolved.name)[0]
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            linked_turn_id = None
            if turn_id:
                turn = connection.execute(
                    """
                    SELECT id FROM turns
                    WHERE id = ? AND session_id = ? AND project_id = ?
                    """,
                    (turn_id, session.id, session.project_id),
                ).fetchone()
                if turn is not None:
                    linked_turn_id = str(turn["id"])
            linked_tool_call_id = None
            if tool_call_id and linked_turn_id is not None:
                candidate_tool_id = _stable_id(
                    "tool",
                    f"{session.id}:{tool_call_id}",
                )
                tool = connection.execute(
                    """
                    SELECT id FROM tool_calls
                    WHERE id = ? AND turn_id = ? AND project_id = ?
                    """,
                    (candidate_tool_id, linked_turn_id, session.project_id),
                ).fetchone()
                if tool is not None:
                    linked_tool_call_id = str(tool["id"])
            staged = connection.execute(
                """
                SELECT a.id
                FROM artifacts a
                JOIN artifact_links l
                  ON l.artifact_id = a.id
                 AND l.project_id = a.project_id
                WHERE a.project_id = ?
                  AND l.session_id = ?
                  AND a.relative_path = ?
                  AND a.status = 'staging'
                  AND (? IS NULL OR a.created_by_turn_id = ?)
                  AND (? IS NULL OR a.created_by_tool_call_id = ?)
                ORDER BY a.created_at DESC
                LIMIT 1
                """,
                (
                    session.project_id,
                    session.id,
                    relative,
                    linked_turn_id,
                    linked_turn_id,
                    linked_tool_call_id,
                    linked_tool_call_id,
                ),
            ).fetchone()
            artifact_id = (
                str(staged["id"])
                if staged is not None
                else content_artifact_id
            )
            link_id = _stable_id("alink", f"{artifact_id}:{session.id}:{relation_type}")
            previous = connection.execute(
                """
                SELECT id FROM artifacts
                WHERE project_id = ? AND relative_path = ? AND id != ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session.project_id, relative, artifact_id),
            ).fetchone()
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    id, project_id, status, storage_kind, relative_path,
                    display_name, artifact_kind, mime_type, byte_size, sha256,
                    created_by_session_id, created_by_turn_id,
                    created_by_tool_call_id, supersedes_artifact_id,
                    validation_json, created_at, ready_at, updated_at
                ) VALUES (
                    ?, ?, 'ready', 'project_file', ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, '{"exists":true}', ?, ?, ?
                )
                """,
                (
                    artifact_id,
                    session.project_id,
                    relative,
                    resolved.name,
                    artifact_kind,
                    resolved_mime or "application/octet-stream",
                    stat.st_size,
                    digest,
                    session.id,
                    linked_turn_id,
                    linked_tool_call_id,
                    previous["id"] if previous is not None else None,
                    now,
                    now,
                    now,
                ),
            )
            if staged is not None:
                connection.execute(
                    """
                    UPDATE artifacts
                    SET status = 'ready',
                        display_name = ?,
                        artifact_kind = ?,
                        mime_type = ?,
                        byte_size = ?,
                        sha256 = ?,
                        supersedes_artifact_id = ?,
                        validation_json = '{"exists":true}',
                        ready_at = ?,
                        updated_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (
                        resolved.name,
                        artifact_kind,
                        resolved_mime or "application/octet-stream",
                        stat.st_size,
                        digest,
                        previous["id"] if previous is not None else None,
                        now,
                        now,
                        artifact_id,
                        session.project_id,
                    ),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_links(
                    id, project_id, artifact_id, session_id, turn_id,
                    tool_call_id, relation_type, origin_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    session.project_id,
                    artifact_id,
                    session.id,
                    linked_turn_id,
                    linked_tool_call_id,
                    relation_type,
                    f"artifact:{artifact_id}:{session.id}:{relation_type}",
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session.id),
            )
            connection.commit()
        record = self.get_artifact(artifact_id, session_key=session_key)
        assert record is not None
        self._index_text_artifact(record, resolved)
        return record

    def _index_text_artifact(self, artifact: ArtifactRecord, path: Path) -> None:
        """Best-effort project-scoped indexing for small textual artifacts."""
        textual = (
            artifact.mime_type.startswith("text/")
            or path.suffix.lower() in {
                ".md", ".markdown", ".txt", ".json", ".jsonl", ".csv",
                ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
                ".yaml", ".yml", ".toml", ".sql",
            }
        )
        if not textual or artifact.byte_size > 2 * 1024 * 1024:
            return
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        chunks = [
            content[offset:offset + 2_000]
            for offset in range(0, len(content), 2_000)
            if content[offset:offset + 2_000].strip()
        ]
        self.replace_project_document(
            artifact.project_id,
            relative_path=artifact.relative_path,
            chunks=chunks,
            content_hash=artifact.sha256,
            source_artifact_id=artifact.id,
        )

    def stage_artifact(
        self,
        session_key: str,
        path: str | Path,
        *,
        relation_type: str = "generated",
        artifact_kind: str = "file",
        mime_type: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ArtifactRecord:
        if relation_type not in _ARTIFACT_RELATIONS:
            raise ValueError(f"unsupported artifact relation: {relation_type}")
        session = self.get_session(session_key)
        if session is None:
            raise StateStoreError(f"session is not registered: {session_key}")
        project = self.get_project(session.project_id)
        if project is None:
            raise StateStoreError(f"project is not registered: {session.project_id}")
        root = Path(project.canonical_root_path).resolve(strict=False)
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve(strict=False)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise StateStoreError("artifact is outside the session project") from exc
        if not relative or relative == ".":
            raise StateStoreError("artifact path is invalid")
        now = _now_ms()
        external_tool_id = str(tool_call_id or "").strip()
        artifact_id = _stable_id(
            "art",
            f"staging:{session.id}:{turn_id or ''}:{external_tool_id}:{relative}",
        )
        link_id = _stable_id("alink", f"{artifact_id}:{session.id}:{relation_type}")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            linked_turn_id = None
            if turn_id:
                turn = connection.execute(
                    """
                    SELECT id FROM turns
                    WHERE id = ? AND session_id = ? AND project_id = ?
                    """,
                    (turn_id, session.id, session.project_id),
                ).fetchone()
                if turn is not None:
                    linked_turn_id = str(turn["id"])
            linked_tool_call_id = None
            if external_tool_id and linked_turn_id is not None:
                candidate_tool_id = _stable_id(
                    "tool",
                    f"{session.id}:{external_tool_id}",
                )
                tool = connection.execute(
                    """
                    SELECT id FROM tool_calls
                    WHERE id = ? AND turn_id = ? AND project_id = ?
                    """,
                    (candidate_tool_id, linked_turn_id, session.project_id),
                ).fetchone()
                if tool is not None:
                    linked_tool_call_id = str(tool["id"])
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, status, storage_kind, relative_path,
                    display_name, artifact_kind, mime_type,
                    created_by_session_id, created_by_turn_id,
                    created_by_tool_call_id, validation_json,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'staging', 'project_file', ?, ?, ?, ?,
                    ?, ?, ?, '{"exists":false}', ?, ?
                )
                ON CONFLICT(id) DO UPDATE SET
                    status = 'staging',
                    validation_json = '{"exists":false}',
                    ready_at = NULL,
                    created_by_turn_id = excluded.created_by_turn_id,
                    created_by_tool_call_id = excluded.created_by_tool_call_id,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    session.project_id,
                    relative,
                    resolved.name,
                    artifact_kind,
                    mime_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                    session.id,
                    linked_turn_id,
                    linked_tool_call_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_links(
                    id, project_id, artifact_id, session_id,
                    turn_id, tool_call_id, relation_type,
                    origin_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    session.project_id,
                    artifact_id,
                    session.id,
                    linked_turn_id,
                    linked_tool_call_id,
                    relation_type,
                    f"artifact-stage:{artifact_id}:{session.id}:{relation_type}",
                    now,
                ),
            )
            connection.commit()
        record = self.get_artifact(artifact_id, session_key=session_key)
        assert record is not None
        return record

    def fail_artifact(
        self,
        artifact_id: str,
        *,
        session_key: str,
        error_code: str,
        error_message: str,
    ) -> ArtifactRecord:
        artifact = self.get_artifact(artifact_id, session_key=session_key)
        if artifact is None:
            raise StateStoreError("artifact not found")
        now = _now_ms()
        validation = _safe_json(
            {
                "exists": False,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE artifacts
                SET status = 'failed', validation_json = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (validation, now, artifact.id, artifact.project_id),
            )
            connection.commit()
        failed = self.get_artifact(artifact_id, session_key=session_key)
        assert failed is not None
        return failed

    def list_session_artifacts(self, session_key: str) -> list[ArtifactRecord]:
        self.reconcile_stale_artifacts(session_key=session_key)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    a.*, l.session_id, l.relation_type, s.session_key
                FROM sessions s
                JOIN artifact_links l
                  ON l.session_id = s.id
                 AND l.project_id = s.project_id
                JOIN artifacts a
                  ON a.id = l.artifact_id
                 AND a.project_id = l.project_id
                WHERE s.session_key = ?
                ORDER BY
                    COALESCE(a.ready_at, a.updated_at) DESC,
                    CASE l.relation_type
                        WHEN 'final' THEN 0
                        WHEN 'generated' THEN 1
                        WHEN 'modified' THEN 2
                        WHEN 'intermediate' THEN 3
                        WHEN 'attached' THEN 4
                        ELSE 5
                    END,
                    a.id
                """,
                (session_key,),
            ).fetchall()
        # A failed edit attempt doesn't invalidate the last materialized file
        # at the same path. Pick the newest non-failed revision when one
        # exists, and surface a failed revision only if the path has no usable
        # staging/ready/missing/quarantined record.
        rows_by_path: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            relative_path = str(row["relative_path"])
            rows_by_path.setdefault(relative_path, []).append(row)
        selected_rows = [
            next(
                (
                    row
                    for row in candidates
                    if str(row["status"]) != "failed"
                ),
                candidates[0],
            )
            for candidates in rows_by_path.values()
        ]
        relation_order = {
            "final": 0,
            "generated": 1,
            "modified": 2,
            "intermediate": 3,
            "attached": 4,
        }
        selected_rows.sort(
            key=lambda row: (
                -int(row["ready_at"] or row["updated_at"]),
                relation_order.get(str(row["relation_type"]), 5),
                str(row["id"]),
            )
        )

        records: list[ArtifactRecord] = []
        for row in selected_rows:
            record = self._artifact_record(row)
            if record.status in {"ready", "missing"}:
                project = self.get_project(record.project_id)
                exists = (
                    project is not None
                    and self._artifact_file_exists(project, record.relative_path)
                )
                reconciled_status = "ready" if exists else "missing"
                if record.status != reconciled_status:
                    self._set_artifact_status(record.id, reconciled_status)
                    record = replace(
                        record,
                        status=reconciled_status,
                        updated_at=_now_ms(),
                    )
            records.append(record)
        return records

    def reconcile_stale_artifacts(
        self,
        *,
        session_key: str | None = None,
        max_age_ms: int = 10 * 60 * 1_000,
    ) -> int:
        cutoff = _now_ms() - max(1, max_age_ms)
        session_filter = ""
        if session_key is not None:
            session_filter = """
                AND id IN (
                    SELECT l.artifact_id
                    FROM artifact_links l
                    JOIN sessions s ON s.id = l.session_id
                    WHERE s.session_key = ?
                )
            """
        now = _now_ms()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE artifacts
                SET status = 'failed',
                    validation_json = '{{"error_code":"ARTIFACT_TIMEOUT","exists":false}}',
                    updated_at = ?
                WHERE status = 'staging'
                  AND updated_at < ?
                  {session_filter}
                """,
                (
                    now,
                    cutoff,
                    *(
                        [session_key]
                        if session_key is not None
                        else []
                    ),
                ),
            )
            connection.commit()
            return max(0, int(cursor.rowcount))

    def get_artifact(
        self,
        artifact_id: str,
        *,
        session_key: str | None = None,
    ) -> ArtifactRecord | None:
        params: list[Any] = [artifact_id]
        session_filter = ""
        if session_key is not None:
            session_filter = "AND s.session_key = ?"
            params.append(session_key)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    a.*, l.session_id, l.relation_type, s.session_key
                FROM artifacts a
                JOIN artifact_links l
                  ON l.artifact_id = a.id
                 AND l.project_id = a.project_id
                JOIN sessions s
                  ON s.id = l.session_id
                 AND s.project_id = l.project_id
                WHERE a.id = ?
                  {session_filter}
                ORDER BY l.created_at
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._artifact_record(row) if row is not None else None

    def resolve_artifact_path(
        self,
        artifact_id: str,
        *,
        session_key: str | None = None,
    ) -> tuple[ArtifactRecord, Path]:
        artifact = self.get_artifact(artifact_id, session_key=session_key)
        if artifact is None:
            raise StateStoreError("artifact not found")
        if artifact.status != "ready":
            raise StateStoreError(f"artifact is not ready: {artifact.status}")
        project = self.get_project(artifact.project_id)
        if project is None:
            raise StateStoreError("artifact project not found")
        root = Path(project.canonical_root_path).resolve(strict=False)
        resolved = (root / artifact.relative_path).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise StateStoreError("artifact is outside the session project") from exc
        if not resolved.is_file():
            self._set_artifact_status(artifact.id, "missing")
            raise StateStoreError("artifact file is missing")
        if artifact.status == "missing":
            self._set_artifact_status(artifact.id, "ready")
            artifact = replace(artifact, status="ready", updated_at=_now_ms())
        return artifact, resolved

    @staticmethod
    def _artifact_file_exists(project: ProjectRecord, relative_path: str) -> bool:
        root = Path(project.canonical_root_path).resolve(strict=False)
        resolved = (root / relative_path).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        return resolved.is_file()

    def _set_artifact_status(self, artifact_id: str, status: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE artifacts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now_ms(), artifact_id),
            )
            connection.commit()

    def archive_session(self, session_key: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET status = 'archived', updated_at = ? WHERE session_key = ?",
                (_now_ms(), session_key),
            )
            connection.commit()

    def restore_session(self, session_key: str) -> SessionRecord:
        now = _now_ms()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET status = 'active', updated_at = ?, completed_at = NULL
                WHERE session_key = ? AND status = 'archived'
                """,
                (now, session_key),
            )
            connection.commit()
        if cursor.rowcount <= 0:
            raise StateStoreError("archived session not found")
        restored = self.get_session(session_key)
        assert restored is not None
        return restored

    def complete_session(
        self,
        session_key: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
    ) -> None:
        now = _now_ms()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?, completed_at = ?
                WHERE session_key = ?
                """,
                (status, now, now, session_key),
            )
            connection.commit()

    def active_turn_id(self, session_key: str) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT active_turn_id FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None or row["active_turn_id"] is None:
            return None
        return str(row["active_turn_id"])

    def turn_runtime_identity(
        self,
        session_key: str,
        turn_id: str,
    ) -> dict[str, str | None] | None:
        """Resolve durable Trace/runtime identity for one session-owned Turn."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT t.trace_id, t.runtime_epoch
                FROM turns AS t
                JOIN sessions AS s
                  ON s.id = t.session_id AND s.project_id = t.project_id
                WHERE s.session_key = ? AND t.id = ?
                """,
                (session_key, turn_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "trace_id": str(row["trace_id"]) if row["trace_id"] else None,
            "runtime_epoch": (
                str(row["runtime_epoch"]) if row["runtime_epoch"] else None
            ),
        }

    def latest_turn_snapshot(self, session_key: str) -> dict[str, Any] | None:
        """Return the latest durable turn as a public lifecycle resource.

        This is historical recovery data only. Callers must never infer a live
        ActiveTurn from this projection.
        """
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT t.*
                FROM turns AS t
                JOIN sessions AS s ON s.id = t.session_id
                WHERE s.session_key = ?
                ORDER BY t.turn_index DESC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
        if row is None:
            return None
        status = {
            "running": "inProgress",
            "queued": "queued",
            "cancelled": "interrupted",
        }.get(str(row["status"]), str(row["status"]))
        payload: dict[str, Any] = {
            "id": str(row["id"]),
            "runtime_epoch": row["runtime_epoch"],
            "trace_id": row["trace_id"],
            "project_id": str(row["project_id"]),
            "session_id": str(row["session_id"]),
            "status": status,
            "started_at": int(row["started_at"]),
            "completed_at": (
                int(row["ended_at"]) if row["ended_at"] is not None else None
            ),
            "duration_ms": (
                max(0, int(row["ended_at"]) - int(row["started_at"]))
                if row["ended_at"] is not None
                else None
            ),
            "finish_reason": row["finish_reason"],
        }
        if row["usage_json"]:
            try:
                usage = json.loads(str(row["usage_json"]))
            except json.JSONDecodeError:
                usage = None
            if isinstance(usage, dict):
                payload["usage"] = usage
        if row["error_code"] or row["error_message"]:
            payload["error"] = {
                "code": str(row["error_code"] or "TURN_FAILED"),
                "message": str(row["error_message"] or row["error_code"] or "turn failed"),
                "retryable": False,
            }
        return payload

    def turn_plan_snapshot(
        self,
        *,
        session_key: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            session = connection.execute(
                "SELECT id, project_id FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if session is None:
                return None
            progress = connection.execute(
                """
                SELECT
                    p.plan_id,
                    p.kind,
                    p.owner,
                    p.policy,
                    p.execution,
                    p.revision,
                    p.current_step_key,
                    p.active_step_ids_json,
                    p.signature_version,
                    p.note,
                    p.status,
                    p.terminalized_at,
                    p.terminalization_reason,
                    p.updated_at,
                    t.status AS turn_status
                FROM turn_progress AS p
                JOIN turns AS t
                  ON t.id = p.turn_id AND t.project_id = p.project_id
                WHERE p.turn_id = ? AND p.session_id = ? AND p.project_id = ?
                """,
                (turn_id, session["id"], session["project_id"]),
            ).fetchone()
            if progress is None:
                return None
            step_rows = connection.execute(
                """
                SELECT
                    step_key, ordinal, title, step_kind, stage_key,
                    status, detail, warning, started_at, ended_at, updated_at
                FROM turn_steps
                WHERE turn_id = ? AND session_id = ? AND project_id = ?
                ORDER BY ordinal
                """,
                (turn_id, session["id"], session["project_id"]),
            ).fetchall()
            team_run = (
                connection.execute(
                    """
                    SELECT id, team_id, current_stage_key
                    FROM expert_team_runs
                    WHERE turn_id = ? AND session_id = ? AND project_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (turn_id, session["id"], session["project_id"]),
                ).fetchone()
                if str(progress["kind"] or "") == "workflow"
                else None
            )
        public_status = {
            "running": "inProgress",
            "cancelled": "interrupted",
        }.get(str(progress["status"]), str(progress["status"]))
        try:
            active_step_ids = json.loads(str(progress["active_step_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            active_step_ids = []
        if not isinstance(active_step_ids, list):
            active_step_ids = []
        return {
            "id": str(progress["plan_id"] or f"plan:{turn_id}"),
            "project_id": str(session["project_id"]),
            "session_id": str(session["id"]),
            "turn_id": turn_id,
            "kind": str(progress["kind"] or "dynamic"),
            "owner": str(progress["owner"] or "agent"),
            "policy": str(progress["policy"] or "required"),
            "execution": str(progress["execution"] or "serial"),
            "revision": int(progress["revision"]),
            "signature_version": int(progress["signature_version"] or 1),
            "status": public_status,
            "active_step_ids": [
                str(value) for value in active_step_ids if str(value).strip()
            ],
            "current_step_id": (
                progress["current_step_key"]
                if public_status in {"pending", "inProgress"}
                else None
            ),
            "note": progress["note"],
            "updated_at": int(progress["updated_at"]),
            **(
                {
                    "team_run_id": str(team_run["id"]),
                    "team_id": str(team_run["team_id"]),
                    **(
                        {"stage_key": str(team_run["current_stage_key"])}
                        if team_run["current_stage_key"] is not None
                        else {}
                    ),
                }
                if team_run is not None
                else {}
            ),
            **(
                {"terminalized_at": int(progress["terminalized_at"])}
                if progress["terminalized_at"] is not None
                else {}
            ),
            **(
                {"terminalization_reason": str(progress["terminalization_reason"])}
                if progress["terminalization_reason"] is not None
                else {}
            ),
            "steps": [
                {
                    "id": str(row["step_key"]),
                    "key": str(row["step_key"]),
                    "ordinal": int(row["ordinal"]),
                    "title": str(row["title"]),
                    **(
                        {"kind": str(row["step_kind"])}
                        if row["step_kind"] is not None
                        else {}
                    ),
                    **(
                        {"stage_key": str(row["stage_key"])}
                        if row["stage_key"] is not None
                        else {}
                    ),
                    "status": {
                        "running": "inProgress",
                        "failed": "error",
                        "cancelled": (
                            "skipped"
                            if str(progress["turn_status"]) in {"completed", "failed"}
                            else "interrupted"
                        ),
                    }.get(str(row["status"]), str(row["status"])),
                    **(
                        {"detail": str(row["detail"])}
                        if row["detail"] is not None
                        else {}
                    ),
                    **(
                        {"warning": str(row["warning"])}
                        if row["warning"] is not None
                        else {}
                    ),
                    **(
                        {"started_at": int(row["started_at"])}
                        if row["started_at"] is not None
                        else {}
                    ),
                    **(
                        {"ended_at": int(row["ended_at"])}
                        if row["ended_at"] is not None
                        else {}
                    ),
                    "updated_at": int(row["updated_at"]),
                }
                for row in step_rows
            ],
        }

    def record_agent_edge(
        self,
        *,
        project_id: str,
        parent_session_key: str,
        child_session_key: str,
        parent_turn_id: str,
        spawn_tool_call_id: str | None = None,
    ) -> str:
        """Persist a same-project parent/child agent relationship."""
        parent = self.get_session(parent_session_key)
        child = self.get_session(child_session_key)
        if (
            parent is None
            or child is None
            or parent.project_id != project_id
            or child.project_id != project_id
        ):
            raise StateStoreError("agent edge sessions must belong to the same project")
        edge_id = _stable_id(
            "edge",
            f"{project_id}:{parent.id}:{parent_turn_id}:{child.id}",
        )
        linked_tool_id = None
        if spawn_tool_call_id:
            linked_tool_id = _stable_id(
                "tool",
                f"{parent.id}:{spawn_tool_call_id}",
            )
        with self._lock, self._connection() as connection:
            turn = connection.execute(
                """
                SELECT id FROM turns
                WHERE id = ? AND session_id = ? AND project_id = ?
                """,
                (parent_turn_id, parent.id, project_id),
            ).fetchone()
            if turn is None:
                raise StateStoreError("parent turn is not registered")
            if linked_tool_id is not None:
                tool = connection.execute(
                    "SELECT id FROM tool_calls WHERE id = ? AND project_id = ?",
                    (linked_tool_id, project_id),
                ).fetchone()
                if tool is None:
                    linked_tool_id = None
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_edges(
                    id, project_id, parent_session_id, parent_turn_id,
                    child_session_id, spawn_tool_call_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    project_id,
                    parent.id,
                    parent_turn_id,
                    child.id,
                    linked_tool_id,
                    _now_ms(),
                ),
            )
            connection.commit()
        return edge_id

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _project_record(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            root_path=str(row["root_path"]),
            canonical_root_path=str(row["canonical_root_path"]),
            status=str(row["status"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _session_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            session_key=str(row["session_key"]),
            title=str(row["title"]),
            status=str(row["status"]),
            event_log_path=str(row["event_log_path"]) if row["event_log_path"] else None,
            artifact_indexed_at=(
                int(row["artifact_indexed_at"])
                if row["artifact_indexed_at"] is not None
                else None
            ),
            artifact_revision=int(row["artifact_revision"] or 0),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            session_id=str(row["session_id"]),
            session_key=str(row["session_key"]),
            status=str(row["status"]),
            storage_kind=str(row["storage_kind"]),
            relative_path=str(row["relative_path"]),
            display_name=str(row["display_name"]),
            artifact_kind=str(row["artifact_kind"]),
            mime_type=str(row["mime_type"] or "application/octet-stream"),
            byte_size=int(row["byte_size"] or 0),
            sha256=str(row["sha256"] or ""),
            relation_type=str(row["relation_type"]),
            validation=(
                json.loads(str(row["validation_json"]))
                if row["validation_json"]
                else {}
            ),
            created_at=int(row["created_at"]),
            ready_at=int(row["ready_at"]) if row["ready_at"] is not None else None,
            updated_at=int(row["updated_at"]),
        )


def open_state_store_with_recovery(
    path: str | Path,
    *,
    default_workspace: str | Path,
) -> StateStoreRecovery:
    """Open state.sqlite, backing up corrupt files before rebuilding projection."""
    database_path = Path(path).expanduser()
    try:
        store = StateStore(database_path, default_workspace=default_workspace)
        if not store.quick_check():
            raise sqlite3.DatabaseError("PRAGMA quick_check failed")
        return StateStoreRecovery(store=store, backup_dir=None, reason=None)
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = database_path.parent / "recovery" / f"state-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{database_path}{suffix}")
            if source.exists():
                shutil.move(str(source), str(backup_dir / source.name))
        store = StateStore(database_path, default_workspace=default_workspace)
        return StateStoreRecovery(
            store=store,
            backup_dir=backup_dir,
            reason=f"{type(exc).__name__}: {exc}",
        )

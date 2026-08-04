"""SQLite persistence for end-to-end Turn / Agent Run traces.

Trace data shares ``logs.sqlite`` with structured diagnostics, but remains
separate from ``state.sqlite`` because it is best-effort operational data and
has an independent retention policy.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT,
    session_id            TEXT,
    turn_id               TEXT,
    runtime_epoch         TEXT NOT NULL,
    status                TEXT NOT NULL,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER,
    duration_ms           INTEGER,
    root_run_id           TEXT,
    provider              TEXT,
    model                 TEXT,
    model_preset          TEXT,
    prompt_version        TEXT,
    toolset_version       TEXT,
    skillset_version      TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cached_input_tokens   INTEGER,
    total_tokens          INTEGER,
    error_code            TEXT,
    error_json            TEXT,
    created_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS traces_turn_time
ON traces(turn_id, started_at DESC);

CREATE INDEX IF NOT EXISTS traces_session_time
ON traces(session_id, started_at DESC);

CREATE INDEX IF NOT EXISTS traces_project_time
ON traces(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                    TEXT PRIMARY KEY,
    trace_id              TEXT NOT NULL,
    parent_run_id         TEXT,
    parent_span_id        TEXT,
    agent_kind            TEXT NOT NULL,
    agent_label           TEXT,
    project_id            TEXT,
    session_id            TEXT,
    turn_id               TEXT,
    status                TEXT NOT NULL,
    provider              TEXT,
    model                 TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER,
    duration_ms           INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cached_input_tokens   INTEGER,
    total_tokens          INTEGER,
    stop_reason           TEXT,
    error_json            TEXT,
    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS agent_runs_trace
ON agent_runs(trace_id, started_at);

CREATE TABLE IF NOT EXISTS trace_spans (
    id                    TEXT PRIMARY KEY,
    trace_id              TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    parent_span_id        TEXT,
    sequence_no           INTEGER NOT NULL,
    kind                  TEXT NOT NULL,
    name                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER,
    duration_ms           INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cached_input_tokens   INTEGER,
    total_tokens          INTEGER,
    ttft_ms               INTEGER,
    retry_count           INTEGER,
    error_code            TEXT,
    attributes_json       TEXT NOT NULL DEFAULT '{}',
    error_json            TEXT,
    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS trace_spans_run_sequence
ON trace_spans(run_id, sequence_no);

CREATE INDEX IF NOT EXISTS trace_spans_trace_parent
ON trace_spans(trace_id, parent_span_id, sequence_no);

CREATE TABLE IF NOT EXISTS trace_context_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id              TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    item_kind             TEXT NOT NULL,
    source_id             TEXT,
    source_locator        TEXT,
    content_hash          TEXT,
    token_estimate        INTEGER,
    selected_reason       TEXT,
    rank                  REAL,
    metadata_json         TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS trace_context_trace
ON trace_context_items(trace_id, run_id, item_kind);
"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


_TRACE_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|cookie|password|secret)"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
)


def _redact_text(value: str) -> str:
    output = value
    for pattern in _TRACE_SECRET_PATTERNS:
        output = pattern.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]"
                if match.lastindex
                else "[REDACTED]"
            ),
            output,
        )
    return output[:16_000]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in (
                    "token",
                    "authorization",
                    "cookie",
                    "api_key",
                    "password",
                    "secret",
                )
            ) and normalized not in {
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "total_tokens",
                "token_estimate",
            }:
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact(item)
        return output
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value[:200]]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:16_000]


def _safe_json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, sort_keys=True)[:64_000]


def _decode_json(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None


class TraceStore:
    """WAL-backed Trace read/write store sharing the diagnostics database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.executescript(_TRACE_SCHEMA)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "logs" in tables:
                self._ensure_column(connection, "logs", "trace_id", "TEXT")
                self._ensure_column(connection, "logs", "run_id", "TEXT")
                self._ensure_column(connection, "logs", "span_id", "TEXT")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS logs_trace_time "
                    "ON logs(trace_id, timestamp DESC)"
                )
            connection.commit()

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

    def begin_trace(
        self,
        *,
        trace_id: str,
        project_id: str | None,
        session_id: str | None,
        turn_id: str,
        runtime_epoch: str,
        started_at: int,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        attrs = attributes or {}
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO traces(
                    id, project_id, session_id, turn_id, runtime_epoch,
                    status, started_at, provider, model, model_preset,
                    prompt_version, toolset_version, skillset_version, created_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    trace_id,
                    project_id,
                    session_id,
                    turn_id,
                    runtime_epoch,
                    started_at,
                    attrs.get("provider"),
                    attrs.get("model"),
                    attrs.get("model_preset"),
                    attrs.get("prompt_version"),
                    attrs.get("toolset_version"),
                    attrs.get("skillset_version"),
                    started_at,
                ),
            )
            connection.commit()

    def finish_trace(
        self,
        *,
        trace_id: str,
        status: str,
        ended_at: int,
        error_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            usage = connection.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM agent_runs WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE traces
                SET status = ?, ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    input_tokens = ?, output_tokens = ?,
                    cached_input_tokens = ?, total_tokens = ?,
                    error_code = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    ended_at,
                    ended_at,
                    int(usage["input_tokens"]),
                    int(usage["output_tokens"]),
                    int(usage["cached_input_tokens"]),
                    int(usage["total_tokens"]),
                    error_code,
                    _safe_json(error) if error else None,
                    trace_id,
                ),
            )
            connection.commit()

    def begin_run(
        self,
        *,
        run_id: str,
        trace_id: str,
        parent_run_id: str | None,
        parent_span_id: str | None,
        agent_kind: str,
        agent_label: str | None,
        project_id: str | None,
        session_id: str | None,
        turn_id: str | None,
        provider: str | None,
        model: str | None,
        started_at: int,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, trace_id, parent_run_id, parent_span_id,
                    agent_kind, agent_label, project_id, session_id, turn_id,
                    status, provider, model, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    run_id,
                    trace_id,
                    parent_run_id,
                    parent_span_id,
                    agent_kind,
                    agent_label,
                    project_id,
                    session_id,
                    turn_id,
                    provider,
                    model,
                    started_at,
                ),
            )
            connection.execute(
                """
                UPDATE traces
                SET root_run_id = COALESCE(root_run_id, ?),
                    provider = COALESCE(provider, ?),
                    model = COALESCE(model, ?)
                WHERE id = ?
                """,
                (run_id, provider, model, trace_id),
            )
            connection.commit()

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        ended_at: int,
        stop_reason: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            usage = connection.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM trace_spans
                WHERE run_id = ? AND kind = 'llm'
                """,
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    input_tokens = ?, output_tokens = ?,
                    cached_input_tokens = ?, total_tokens = ?,
                    stop_reason = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    ended_at,
                    ended_at,
                    int(usage["input_tokens"]),
                    int(usage["output_tokens"]),
                    int(usage["cached_input_tokens"]),
                    int(usage["total_tokens"]),
                    stop_reason,
                    _safe_json(error) if error else None,
                    run_id,
                ),
            )
            connection.commit()

    def begin_span(
        self,
        *,
        span_id: str,
        trace_id: str,
        run_id: str,
        parent_span_id: str | None,
        kind: str,
        name: str,
        started_at: int,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_seq "
                "FROM trace_spans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO trace_spans(
                    id, trace_id, run_id, parent_span_id, sequence_no,
                    kind, name, status, started_at, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    span_id,
                    trace_id,
                    run_id,
                    parent_span_id,
                    int(row["next_seq"]),
                    kind,
                    name,
                    started_at,
                    _safe_json(attributes or {}),
                ),
            )
            connection.commit()

    def finish_span(
        self,
        *,
        span_id: str,
        status: str,
        ended_at: int,
        usage: dict[str, int] | None = None,
        ttft_ms: int | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        usage = usage or {}
        input_tokens = max(0, int(usage.get("input_tokens", 0)))
        output_tokens = max(0, int(usage.get("output_tokens", 0)))
        cached = max(0, int(usage.get("cached_input_tokens", 0)))
        total = max(0, int(usage.get("total_tokens", input_tokens + output_tokens)))
        with self._lock, self._connection() as connection:
            current = connection.execute(
                "SELECT attributes_json FROM trace_spans WHERE id = ?",
                (span_id,),
            ).fetchone()
            merged_attributes = _decode_json(current["attributes_json"]) if current else {}
            if not isinstance(merged_attributes, dict):
                merged_attributes = {}
            merged_attributes.update(attributes or {})
            connection.execute(
                """
                UPDATE trace_spans
                SET status = ?, ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    input_tokens = ?, output_tokens = ?,
                    cached_input_tokens = ?, total_tokens = ?,
                    ttft_ms = ?, retry_count = ?, error_code = ?,
                    attributes_json = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    ended_at,
                    ended_at,
                    input_tokens,
                    output_tokens,
                    cached,
                    total,
                    ttft_ms,
                    retry_count,
                    error_code,
                    _safe_json(merged_attributes),
                    _safe_json(error) if error else None,
                    span_id,
                ),
            )
            connection.commit()

    def add_context_item(
        self,
        *,
        trace_id: str,
        run_id: str,
        item_kind: str,
        source_id: str | None,
        source_locator: str | None,
        content_hash: str | None,
        token_estimate: int | None,
        selected_reason: str | None,
        rank: float | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO trace_context_items(
                    trace_id, run_id, item_kind, source_id, source_locator,
                    content_hash, token_estimate, selected_reason, rank, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    run_id,
                    item_kind,
                    source_id,
                    source_locator,
                    content_hash,
                    token_estimate,
                    selected_reason,
                    rank,
                    _safe_json(metadata or {}),
                ),
            )
            connection.commit()

    def recover_abandoned(self, *, runtime_epoch: str, ended_at: int | None = None) -> int:
        now = ended_at or _now_ms()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM traces WHERE status = 'running' AND runtime_epoch != ?",
                (runtime_epoch,),
            ).fetchall()
            trace_ids = [str(row["id"]) for row in rows]
            if not trace_ids:
                return 0
            placeholders = ",".join("?" for _ in trace_ids)
            connection.execute(
                f"""
                UPDATE trace_spans
                SET status = 'abandoned', ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    error_code = COALESCE(error_code, 'GATEWAY_RESTARTED')
                WHERE trace_id IN ({placeholders}) AND status = 'running'
                """,
                (now, now, *trace_ids),
            )
            connection.execute(
                f"""
                UPDATE agent_runs
                SET status = 'abandoned', ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    stop_reason = COALESCE(stop_reason, 'gateway_restarted')
                WHERE trace_id IN ({placeholders}) AND status = 'running'
                """,
                (now, now, *trace_ids),
            )
            connection.execute(
                f"""
                UPDATE traces
                SET status = 'abandoned', ended_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    error_code = COALESCE(error_code, 'GATEWAY_RESTARTED')
                WHERE id IN ({placeholders})
                """,
                (now, now, *trace_ids),
            )
            connection.commit()
            return len(trace_ids)

    def list_traces(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("status", status),
        ):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM traces {where} ORDER BY started_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_payload(row) for row in rows]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            trace = connection.execute(
                "SELECT * FROM traces WHERE id = ?",
                (trace_id,),
            ).fetchone()
            if trace is None:
                return None
            runs = connection.execute(
                "SELECT * FROM agent_runs WHERE trace_id = ? ORDER BY started_at, id",
                (trace_id,),
            ).fetchall()
        return {
            **self._row_payload(trace),
            "runs": [self._row_payload(row) for row in runs],
        }

    def list_spans(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM trace_spans
                WHERE trace_id = ? ORDER BY started_at, run_id, sequence_no
                """,
                (trace_id,),
            ).fetchall()
        return [self._row_payload(row) for row in rows]

    def context_manifest(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM trace_context_items
                WHERE trace_id = ? ORDER BY run_id, id
                """,
                (trace_id,),
            ).fetchall()
        return [self._row_payload(row) for row in rows]

    def export_trace(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.get_trace(trace_id)
        if trace is None:
            return None
        return _redact({
            "schema_version": 1,
            "trace": trace,
            "spans": self.list_spans(trace_id),
            "context_manifest": self.context_manifest(trace_id),
        })

    def prune(
        self,
        *,
        success_older_than_ms: int,
        failure_older_than_ms: int,
        keep_latest: int = 1_000,
    ) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM traces
                WHERE id NOT IN (
                    SELECT id FROM traces ORDER BY started_at DESC LIMIT ?
                )
                  AND (
                    (status = 'completed' AND started_at < ?)
                    OR (status != 'completed' AND started_at < ?)
                  )
                """,
                (
                    max(0, int(keep_latest)),
                    int(success_older_than_ms),
                    int(failure_older_than_ms),
                ),
            )
            removed = max(0, int(cursor.rowcount))
            connection.commit()
            if removed:
                connection.execute("PRAGMA incremental_vacuum(200)")
            return removed

    @staticmethod
    def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = {str(key): row[key] for key in row.keys()}
        for key in ("attributes_json", "error_json", "metadata_json"):
            if key not in payload:
                continue
            decoded = _decode_json(payload.pop(key))
            payload[key.removesuffix("_json")] = decoded or {}
        return payload

"""Structured operational logs stored separately from core gateway state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class StructuredLogRecord:
    id: int
    timestamp: int
    level: str
    component: str
    event_name: str | None
    message: str
    request_id: str | None
    project_id: str | None
    session_id: str | None
    turn_id: str | None
    trace_id: str | None
    run_id: str | None
    span_id: str | None
    tool_call_id: str | None
    artifact_id: str | None
    error_code: str | None
    duration_ms: int | None
    details: dict[str, Any]


_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       INTEGER NOT NULL,
    level           TEXT NOT NULL,
    component       TEXT NOT NULL,
    event_name      TEXT,
    message         TEXT NOT NULL,
    request_id      TEXT,
    project_id      TEXT,
    session_id      TEXT,
    turn_id         TEXT,
    trace_id        TEXT,
    run_id          TEXT,
    span_id         TEXT,
    tool_call_id    TEXT,
    artifact_id     TEXT,
    error_code      TEXT,
    duration_ms     INTEGER,
    details_json    TEXT
);

CREATE INDEX IF NOT EXISTS logs_session_time
ON logs(session_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS logs_artifact_time
ON logs(artifact_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS logs_error_time
ON logs(error_code, timestamp DESC);

"""


class StructuredLogStore:
    """Small WAL-backed sink for queryable gateway diagnostics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.executescript(_LOG_SCHEMA)
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

    def write(
        self,
        *,
        level: str,
        component: str,
        message: str,
        event_name: str | None = None,
        request_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        span_id: str | None = None,
        tool_call_id: str | None = None,
        artifact_id: str | None = None,
        error_code: str | None = None,
        duration_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        if trace_id is None or run_id is None or span_id is None:
            # Import lazily so logs remain usable by low-level recovery code.
            from nanobot.runtime.trace_context import current_trace_context

            trace_context = current_trace_context()
            if trace_context is not None:
                trace_id = trace_id or trace_context.trace_id
                run_id = run_id or trace_context.run_id
                span_id = span_id or trace_context.span_id
        timestamp = time.time_ns() // 1_000_000
        safe_details = self._redact(details or {})
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO logs(
                    timestamp, level, component, event_name, message,
                    request_id, project_id, session_id, turn_id,
                    trace_id, run_id, span_id,
                    tool_call_id, artifact_id, error_code, duration_ms,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    level.lower(),
                    component,
                    event_name,
                    message,
                    request_id,
                    project_id,
                    session_id,
                    turn_id,
                    trace_id,
                    run_id,
                    span_id,
                    tool_call_id,
                    artifact_id,
                    error_code,
                    duration_ms,
                    json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def query(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        artifact_id: str | None = None,
        error_code: str | None = None,
        trace_id: str | None = None,
        limit: int = 200,
    ) -> list[StructuredLogRecord]:
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("session_id", session_id),
            ("artifact_id", artifact_id),
            ("error_code", error_code),
            ("trace_id", trace_id),
        ):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        bounded_limit = max(1, min(int(limit), 500))
        params.append(bounded_limit)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM logs
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._record(row) for row in rows]

    def prune(self, *, older_than_ms: int, keep_latest: int = 1_000) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM logs
                WHERE timestamp < ?
                  AND id NOT IN (
                    SELECT id FROM logs ORDER BY timestamp DESC, id DESC LIMIT ?
                  )
                """,
                (older_than_ms, max(0, keep_latest)),
            )
            connection.commit()
            return max(0, int(cursor.rowcount))

    def delete_session(self, session_id: str) -> int:
        """Remove diagnostic rows owned by a permanently deleted session."""
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM logs WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()
            return max(0, int(cursor.rowcount))

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if any(
                    secret in normalized
                    for secret in ("token", "authorization", "cookie", "api_key", "password")
                ):
                    output[str(key)] = "[REDACTED]"
                else:
                    output[str(key)] = cls._redact(item)
            return output
        if isinstance(value, list):
            return [cls._redact(item) for item in value[:100]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _record(row: sqlite3.Row) -> StructuredLogRecord:
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        return StructuredLogRecord(
            id=int(row["id"]),
            timestamp=int(row["timestamp"]),
            level=str(row["level"]),
            component=str(row["component"]),
            event_name=str(row["event_name"]) if row["event_name"] else None,
            message=str(row["message"]),
            request_id=str(row["request_id"]) if row["request_id"] else None,
            project_id=str(row["project_id"]) if row["project_id"] else None,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            trace_id=str(row["trace_id"]) if row["trace_id"] else None,
            run_id=str(row["run_id"]) if row["run_id"] else None,
            span_id=str(row["span_id"]) if row["span_id"] else None,
            tool_call_id=str(row["tool_call_id"]) if row["tool_call_id"] else None,
            artifact_id=str(row["artifact_id"]) if row["artifact_id"] else None,
            error_code=str(row["error_code"]) if row["error_code"] else None,
            duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            details=details if isinstance(details, dict) else {},
        )

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

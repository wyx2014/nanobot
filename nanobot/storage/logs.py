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

from nanobot.security.audit import redact_security_details, redact_security_text


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


@dataclass(frozen=True)
class SecurityAuditRecord:
    id: int
    timestamp: int
    category: str
    action: str
    decision: str
    result: str
    risk: str
    rule_id: str | None
    project_id: str | None
    session_id: str | None
    turn_id: str | None
    tool_call_id: str | None
    tool_name: str | None
    target: str | None
    summary: str
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

CREATE TABLE IF NOT EXISTS security_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       INTEGER NOT NULL,
    category        TEXT NOT NULL,
    action          TEXT NOT NULL,
    decision        TEXT NOT NULL,
    result          TEXT NOT NULL,
    risk            TEXT NOT NULL,
    rule_id         TEXT,
    project_id      TEXT,
    session_id      TEXT,
    turn_id         TEXT,
    tool_call_id    TEXT,
    tool_name       TEXT,
    target          TEXT,
    summary         TEXT NOT NULL,
    duration_ms     INTEGER,
    details_json    TEXT
);

CREATE INDEX IF NOT EXISTS security_events_time
ON security_events(timestamp DESC, id DESC);

CREATE INDEX IF NOT EXISTS security_events_session_time
ON security_events(session_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS security_events_category_result_time
ON security_events(category, result, timestamp DESC);

CREATE INDEX IF NOT EXISTS security_events_category_result_time_v2
ON security_events(category, result, timestamp DESC, id DESC);

CREATE INDEX IF NOT EXISTS security_events_visible_category_time
ON security_events(category, timestamp DESC, id DESC)
WHERE target IS NOT NULL AND target != '';

CREATE INDEX IF NOT EXISTS security_events_task
ON security_events(session_id, turn_id, category, action, result);

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

    def prune_operations(self) -> None:
        """Bound only operational rows; retain existing audit/Trace policies."""
        with self._lock, self._connection() as connection:
            connection.execute(
                """DELETE FROM logs WHERE component LIKE 'operations.%'
                   AND (timestamp < ? OR id NOT IN (
                       SELECT id FROM logs WHERE component LIKE 'operations.%'
                       ORDER BY id DESC LIMIT 100000
                   ))""",
                (time.time_ns() // 1_000_000 - 30 * 86400 * 1000,),
            )
            connection.commit()

    def delete_session(self, session_id: str) -> int:
        """Remove diagnostic rows owned by a permanently deleted session."""
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM logs WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()
            return max(0, int(cursor.rowcount))

    def begin_security_event(
        self,
        *,
        category: str,
        action: str,
        decision: str,
        risk: str,
        summary: str,
        rule_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        target: str | None = None,
        result: str = "pending",
        details: dict[str, Any] | None = None,
    ) -> int:
        timestamp = time.time_ns() // 1_000_000
        safe_details = redact_security_details(details or {})
        safe_details["lifecycle"] = [{"timestamp": timestamp, "result": result, "decision": decision}]
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO security_events(
                    timestamp, category, action, decision, result, risk,
                    rule_id, project_id, session_id, turn_id, tool_call_id,
                    tool_name, target, summary, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    category,
                    action,
                    decision,
                    result,
                    risk,
                    rule_id,
                    project_id,
                    session_id,
                    turn_id,
                    tool_call_id,
                    tool_name,
                    redact_security_text(target) if target else None,
                    redact_security_text(summary),
                    json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def complete_security_event(
        self,
        event_id: int,
        *,
        result: str,
        decision: str | None = None,
        duration_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM security_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise LookupError("security audit record no longer exists")
            try:
                current = json.loads(row["details_json"] or "{}")
            except json.JSONDecodeError:
                current = {}
            if not isinstance(current, dict):
                current = {}
            current.update(redact_security_details(details or {}))
            now = time.time_ns() // 1_000_000
            lifecycle = current.get("lifecycle", [])
            stage = {"timestamp": now, "result": result, "decision": decision or row["decision"]}
            if not lifecycle or any(lifecycle[-1].get(key) != stage[key] for key in ("result", "decision")):
                lifecycle = [*lifecycle[-15:], stage]
            current["lifecycle"] = lifecycle
            current["last_timestamp"] = now
            aggregate_key = current.get("aggregate_key")
            # Successful routine work is summarized within its owning task. Failures,
            # approvals and in-flight operations always retain their own record.
            if result == "succeeded" and aggregate_key and row["result"] != "succeeded":
                previous = connection.execute(
                    """SELECT * FROM security_events
                       WHERE session_id IS ? AND turn_id = ? AND category = ? AND action = ?
                         AND result = 'succeeded' AND id != ?
                         AND json_extract(details_json, '$.aggregate_key') = ?
                       ORDER BY id DESC LIMIT 1""",
                    (row["session_id"], row["turn_id"], row["category"], row["action"], event_id, aggregate_key),
                ).fetchone()
                if previous is not None:
                    grouped = json.loads(previous["details_json"])
                    grouped["operation_count"] = grouped.get("operation_count", 1) + 1
                    grouped["last_timestamp"] = now
                    paths = list(dict.fromkeys([*grouped.get("paths", []), *current.get("paths", [])]))
                    grouped["paths"] = paths[:20]
                    grouped["paths_truncated"] = grouped.get("paths_truncated", False) or len(paths) > 20
                    if current.get("http_activity"):
                        old_http = grouped.get("http_activity", {})
                        new_http = current["http_activity"]
                        grouped["http_activity"] = {
                            "request_count": old_http.get("request_count", 0) + new_http["request_count"],
                            "requests": [*old_http.get("requests", []), *new_http.get("requests", [])][:20],
                        }
                    grouped["lifecycle"] = []
                    connection.execute(
                        """UPDATE security_events SET details_json = ?, tool_call_id = NULL,
                           duration_ms = COALESCE(duration_ms, 0) + ? WHERE id = ?""",
                        (json.dumps(grouped, ensure_ascii=False, sort_keys=True), duration_ms or 0, previous["id"]),
                    )
                    connection.execute("DELETE FROM security_events WHERE id = ?", (event_id,))
                    connection.commit()
                    return
            connection.execute(
                """
                UPDATE security_events
                SET result = ?, decision = COALESCE(?, decision),
                    duration_ms = ?, details_json = ?
                WHERE id = ?
                """,
                (
                    result,
                    decision,
                    duration_ms,
                    json.dumps(current, ensure_ascii=False, sort_keys=True),
                    event_id,
                ),
            )
            connection.commit()

    def find_running_security_command(self, process_session_id: str, session_key: str) -> SecurityAuditRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM security_events WHERE category = 'command' AND result = 'running'
                   AND json_extract(details_json, '$.process_session_id') = ?
                   AND json_extract(details_json, '$.session_key') = ? ORDER BY id DESC LIMIT 1""",
                (process_session_id, session_key),
            ).fetchone()
        return self._security_record(row) if row is not None else None

    @staticmethod
    def _security_filters(
        *,
        search: str | None,
        category: str | None,
        categories: list[str] | tuple[str, ...] | None,
        require_target: bool,
        result: str | None,
        start_ms: int | None,
        end_ms: int | None,
        cursor: int | None,
    ) -> tuple[list[str], list[Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if search:
            filters.append(
                "(summary LIKE ? OR target LIKE ? OR tool_name LIKE ? OR rule_id LIKE ? OR details_json LIKE ?)"
            )
            needle = f"%{search[:200]}%"
            params.extend([needle, needle, needle, needle, needle])
        if category == "authorization":
            filters.append("(decision IN ('require_approval', 'block', 'approved', 'approved_for_turn') OR result IN ('blocked', 'blocked_unattended', 'denied', 'timed_out'))")
        elif category == "network":
            filters.append("category IN ('network', 'mcp')")
        elif category:
            filters.append("category = ?")
            params.append(category)
        elif categories:
            normalized_categories = [str(item) for item in categories if str(item)]
            if normalized_categories:
                placeholders = ", ".join("?" for _ in normalized_categories)
                filters.append(f"category IN ({placeholders})")
                params.extend(normalized_categories)
        if require_target:
            filters.append("target IS NOT NULL AND target != ''")
        if result == "approved":
            filters.append("(decision IN ('approved', 'approved_for_turn') OR result = 'approved')")
        elif result:
            filters.append("result = ?")
            params.append(result)
        if start_ms is not None:
            filters.append("timestamp >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            filters.append("timestamp <= ?")
            params.append(int(end_ms))
        if cursor is not None:
            filters.append("id < ?")
            params.append(int(cursor))
        return filters, params

    def query_security_events(
        self,
        *,
        search: str | None = None,
        category: str | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
        require_target: bool = False,
        result: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cursor: int | None = None,
        limit: int = 100,
    ) -> list[SecurityAuditRecord]:
        filters, params = self._security_filters(
            search=search,
            category=category,
            categories=categories,
            require_target=require_target,
            result=result,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor=cursor,
        )
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(int(limit), 100_000)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM security_events
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._security_record(row) for row in rows]

    def count_security_events(
        self,
        *,
        search: str | None = None,
        category: str | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
        require_target: bool = False,
        result: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> int:
        filters, params = self._security_filters(
            search=search,
            category=category,
            categories=categories,
            require_target=require_target,
            result=result,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor=None,
        )
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM security_events {where}",
                params,
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def clear_security_events(self, *, record_admin: bool = False) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM security_events")
            deleted = max(0, int(cursor.rowcount))
            if record_admin:
                connection.execute(
                    """INSERT INTO security_events(timestamp, category, action, decision, result, risk,
                       rule_id, target, summary, details_json) VALUES (?, 'settings', 'clear_audit',
                       'allow', 'succeeded', 'normal', 'security.audit_cleared', 'security-audit', ?, ?)""",
                    (time.time_ns() // 1_000_000, "安全审计记录已由用户清空",
                     json.dumps({"deleted_count": deleted, "actor": "user"})),
                )
            connection.commit()
            return deleted

    def prune_security_events(
        self,
        *,
        older_than_ms: int,
        keep_latest: int = 100_000,
    ) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM security_events
                WHERE timestamp < ?
                   OR id NOT IN (
                        SELECT id FROM security_events
                        ORDER BY timestamp DESC, id DESC LIMIT ?
                   )
                """,
                (int(older_than_ms), max(1, int(keep_latest))),
            )
            connection.commit()
            return max(0, int(cursor.rowcount))

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "token_estimate"} and isinstance(item, (int, float)):
                    output[str(key)] = item
                    continue
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
    def _security_record(row: sqlite3.Row) -> SecurityAuditRecord:
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        return SecurityAuditRecord(
            id=int(row["id"]),
            timestamp=int(row["timestamp"]),
            category=str(row["category"]),
            action=str(row["action"]),
            decision=str(row["decision"]),
            result=str(row["result"]),
            risk=str(row["risk"]),
            rule_id=str(row["rule_id"]) if row["rule_id"] else None,
            project_id=str(row["project_id"]) if row["project_id"] else None,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            tool_call_id=str(row["tool_call_id"]) if row["tool_call_id"] else None,
            tool_name=str(row["tool_name"]) if row["tool_name"] else None,
            target=(
                "web_search" if row["category"] == "network" and row["action"] == "search"
                else redact_security_text(str(row["target"])) if row["target"] else None
            ),
            summary=redact_security_text(str(row["summary"])),
            duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            details=redact_security_details(details) if isinstance(details, dict) else {},
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

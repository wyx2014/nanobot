"""Read-only, bounded projections for desktop diagnostic bundles; never export raw databases."""

from __future__ import annotations

import json
import platform
import sqlite3
import time
from pathlib import Path
from typing import Any

from nanobot.observability.operations import _details, operation_health, valid_id

MAX_BYTES = 8 * 1024 * 1024
TABLES = {
    "logs": (5000, "id timestamp level component event_name request_id project_id session_id turn_id trace_id run_id span_id tool_call_id artifact_id error_code duration_ms"),
    "security_events": (1000, "id timestamp category action decision result risk rule_id project_id session_id turn_id tool_call_id tool_name duration_ms"),
    "traces": (100, "id project_id session_id turn_id runtime_epoch status started_at ended_at duration_ms root_run_id provider model input_tokens output_tokens total_tokens error_code"),
    "agent_runs": (500, "id trace_id parent_run_id parent_span_id agent_kind project_id session_id turn_id status provider model started_at ended_at duration_ms input_tokens output_tokens total_tokens stop_reason"),
    "trace_spans": (3000, "id trace_id run_id parent_span_id sequence_no kind name status started_at ended_at duration_ms input_tokens output_tokens total_tokens ttft_ms retry_count error_code"),
}


def parse_window(query: dict[str, list[str]]) -> tuple[int, int, str | None]:
    start = int(query.get("start_ms", [""])[0])
    end = int(query.get("end_ms", [""])[0])
    if start < 0 or end <= start or end - start > 86400_000 or end > time.time() * 1000 + 60_000:
        raise ValueError("invalid diagnostic time window")
    session = query.get("session_id", [None])[0]
    if session is not None and valid_id(session) is None:
        raise ValueError("invalid diagnostic session")
    return start, end, session


def runtime_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = {key: snapshot.get(key) for key in ("runtime_epoch", "snapshot_revision")}
    thread = snapshot.get("thread_status") or {}
    result["thread_status"] = {key: thread[key] for key in ("type", "active_flags", "error_code") if key in thread}
    for key in ("active_turn", "latest_turn"):
        turn = snapshot.get(key)
        result[key] = {field: turn.get(field) for field in (
            "id", "trace_id", "status", "started_at", "completed_at", "duration_ms", "finish_reason",
        )} if isinstance(turn, dict) else None
    return result


def collect_database(path: Path, start: int, end: int, session: str | None) -> dict[str, Any]:
    """One SQLite read transaction includes WAL and freezes all table watermarks.

    Rows are selected newest first, capped explicitly, then exported chronologically.
    No offset paging or copies of an active SQLite main file are involved.
    """
    result: dict[str, Any] = {"schema_version": 1, "captured_at": int(time.time() * 1000),
                              "window": {"start_ms": start, "end_ms": end}, "sources": {}, "tables": {}}
    used = 0
    deadline = time.monotonic() + 4
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        # Freeze the read snapshot now, before querying per-table watermarks.
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        trace_filter = "started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)"
        trace_params: list[Any] = [end, start]
        if session:
            trace_filter += " AND session_id = ?"
            trace_params.append(session)
        for table, (limit, fields) in TABLES.items():
            info: dict[str, Any] = {"status": "included", "count": 0, "limit": limit,
                                    "malformed_records": 0, "oversized_records": 0}
            result["sources"][table] = info
            result["tables"][table] = []
            try:
                info["cutoff_rowid"] = connection.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
                columns = ", ".join(fields.split())
                if table == "logs":
                    columns += ", substr(details_json, 1, 16000) AS details_json, length(details_json) AS details_length"
                if table in {"logs", "security_events"}:
                    where = "timestamp >= ? AND timestamp <= ?"
                    params: list[Any] = [start, end]
                    if session:
                        where += " AND (session_id = ? OR session_id IS NULL)"
                        params.append(session)
                        if table == "logs":
                            where += " AND (trace_id IS NULL OR trace_id IN (SELECT id FROM traces WHERE session_id = ?))"
                            params.append(session)
                    order = "timestamp DESC, id DESC"
                elif table == "traces":
                    where, params, order = trace_filter, trace_params, "started_at DESC, id DESC"
                else:
                    where = f"trace_id IN (SELECT id FROM traces WHERE {trace_filter})"
                    params, order = trace_params, "started_at DESC, id DESC"
                rows = connection.execute(f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {order} LIMIT ?", [*params, limit + 1])
                for index, row in enumerate(rows):
                    if index == limit:
                        info["status"], info["reason"] = "truncated", "ROW_LIMIT"
                        break
                    item = dict(row)
                    if table == "logs":
                        raw = item.pop("details_json")
                        length = item.pop("details_length") or 0
                        if length > 16000:
                            info["oversized_records"] += 1
                            item["details"] = {}
                        else:
                            try:
                                details = json.loads(raw or "{}")
                                item["details"] = _details(details) if isinstance(details, dict) else {}
                            except (ValueError, TypeError):
                                info["malformed_records"] += 1
                                item["details"] = {}
                    size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                    if used + size > MAX_BYTES:
                        info["status"], info["reason"] = "truncated", "BYTE_LIMIT"
                        break
                    used += size
                    result["tables"][table].append(item)
                result["tables"][table].reverse()
                info["count"] = len(result["tables"][table])
                if info["malformed_records"] or info["oversized_records"]:
                    info["status"], info["reason"] = "truncated", "INVALID_DETAILS"
            except sqlite3.Error as exc:
                info.update(status="unavailable", reason=type(exc).__name__)
            finally:
                info["count"] = len(result["tables"][table])
    finally:
        connection.close()
    result["completed_at"] = int(time.time() * 1000)
    return result


async def collect_snapshot(handler: Any, session_id: str | None) -> dict[str, Any]:
    import asyncio

    snapshot: dict[str, Any] = {
        "captured_at": int(time.time() * 1000), "python_version": platform.python_version(),
        "collection": operation_health(),
        "ready": bool(handler.runtime_ready()) if handler.runtime_ready else None,
        "mcp_status": handler.runtime_mcp_status() if handler.runtime_mcp_status else None,
        "runtime": {"status": "excluded", "reason": "NO_SESSION_SELECTED"},
    }
    if session_id:
        session = await asyncio.to_thread(handler.state.get_session_by_id, session_id)
        if session is None:
            raise LookupError("diagnostic session not found")
        if handler.thread_runtime_registry is not None:
            current = await handler.thread_runtime_registry.snapshot(session.session_key)
            snapshot["runtime"] = {"status": "included", **runtime_projection(current.payload())}
        else:
            snapshot["runtime"] = {"status": "unavailable", "reason": "RUNTIME_UNAVAILABLE"}
        watermark = await asyncio.to_thread(handler.state.projector_watermark, session.session_key)
        snapshot["projection"] = {key: value for key, value in (watermark or {}).items()
                                  if key in {"last_event_seq", "last_event_id", "last_projected_at"}}
    return snapshot

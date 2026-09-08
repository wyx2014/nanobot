"""Bounded MCP connection diagnostics, separate from temporary connection tests."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from nanobot.observability.operations import record_operation

_live: dict[str, dict[str, Any]] = {}
_capture: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar("mcp_diagnostics", default=None)


def record_connection(name: str, status: str, message: str, tools: list[str] | None = None) -> None:
    records = _capture.get()
    if records is None:
        records = _live
    previous = records.get(name, {})
    now = time.monotonic()
    started = now if status == "connecting" else previous.get("started_monotonic", now)
    record_operation("mcp.connection", status={"connecting": "started", "connected": "completed", "failed": "failed", "needs_auth": "failed"}.get(status),
                     duration_ms=round((now - started) * 1000) if status != "connecting" else None,
                     error_code="MCP_AUTH_FAILED" if status == "needs_auth" else "MCP_CONNECTION_FAILED" if status == "failed" else None,
                     details={"server_id": name, "stage": "probe" if _capture.get() is not None else "runtime", "reason": status, "tool_count": len(tools or [])})
    checked_at = datetime.now(timezone.utc).isoformat()
    history = [*previous.get("history", []), {
        "time": checked_at, "status": status, "message": message,
    }][-40:]
    records[name] = {
        "started_monotonic": started,
        "status": status, "message": message, "checked_at": checked_at,
        "tool_names": tools if tools is not None else previous.get("tool_names", []),
        "history": history,
    }
    if len(records) > 256:
        records.pop(next(iter(records)))


def connection_snapshot(name: str) -> dict[str, Any] | None:
    records = _capture.get()
    result = deepcopy((_live if records is None else records).get(name))
    if result is not None:
        result.pop("started_monotonic", None)
    return result


@contextmanager
def capture_connections():
    records: dict[str, dict[str, Any]] = {}
    token = _capture.set(records)
    try:
        yield records
    finally:
        _capture.reset(token)

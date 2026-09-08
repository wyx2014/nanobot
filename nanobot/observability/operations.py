"""Bounded operational events using the gateway's existing diagnostic store."""

from __future__ import annotations

import asyncio
import atexit
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from nanobot.security.audit import redact_security_text

_PROCESS_ID = "gateway_" + uuid.uuid4().hex
_CONTEXT: ContextVar[dict[str, str]] = ContextVar("operation_context", default={})
_RECORDER: OperationRecorder | None = None
_DETAIL_KEYS = frozenset({
    "stage", "route", "method", "status_code", "attempt", "timeout_ms", "duration_ms",
    "error_type", "error_code", "stack", "template_id", "document_id", "page_count",
    "warning_count", "artifact_count", "bytes", "exit_code", "transport", "server_id",
    "tool_name", "tool_count", "provider", "model", "input_tokens", "output_tokens",
    "ttft_ms", "finish_reason", "runtime_epoch", "session_key", "snapshot_revision",
    "event_seq", "event_type", "queue_depth", "cache_hit", "cancelled", "operation_id",
    "parent_operation_id", "app_launch_id", "process_instance_id", "process_seq",
    "event_id", "timestamp", "schema_version", "status", "client_action_id", "reason",
    "delay_ms",
})
_ACTIVE_OPERATION: ContextVar[Operation | None] = ContextVar("active_operation", default=None)


def valid_id(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else None


def safe_route(raw: str) -> str:
    segments = raw.split("?", 1)[0].split("/")
    resources = {"sessions", "projects", "traces", "artifacts", "providers", "skills", "runs", "jobs"}
    return "/".join(":id" if index and segments[index - 1] in resources else part
                    for index, part in enumerate(segments))[:300]


def _details(value: dict[str, Any]) -> dict[str, Any]:
    output = {}
    remaining = 8000
    for key, item in list(value.items())[:64]:
        if key not in _DETAIL_KEYS:
            continue
        if isinstance(item, str):
            if remaining <= 0:
                break
            output[key] = re.sub(r"/(?:Users|home)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+",
                                 "<home>", redact_security_text(item))[:remaining]
            remaining -= len(output[key])
        elif item is None or isinstance(item, (bool, int, float)):
            output[key] = item
    return output


class OperationRecorder:
    def __init__(self, store: Any, *, max_queue: int = 2048):
        self.store = store
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.write_failures = 0
        self._sequence = 0
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._write, name="nanobot-operations", daemon=True)
        self._thread.start()

    def record(self, event: dict[str, Any]) -> None:
        if self._closing.is_set():
            return
        critical = event.get("level") == "error" or event["details"].get("status") in {"completed", "failed", "cancelled", "abandoned"}
        reserve = min(32, self.queue.maxsize // 4)
        if not critical and self.queue.qsize() >= self.queue.maxsize - reserve:
            self.dropped += 1
            return
        self._sequence += 1
        event["details"].update({
            "schema_version": 1, "event_id": "evt_" + uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "process_instance_id": _PROCESS_ID, "process_seq": self._sequence,
            "app_launch_id": valid_id(os.environ.get("NANOBOT_APP_LAUNCH_ID")),
        })
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _write(self) -> None:
        written = 0
        while not self._closing.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.store.write(**event)
                written += 1
                if written % 1000 == 0:
                    self.store.prune_operations()
            except Exception:
                self.write_failures += 1
                self.dropped += 1
            finally:
                self.queue.task_done()

    def close(self, timeout: float = 1.0) -> None:
        self._closing.set()
        self._thread.join(timeout)


def configure_operations(store: Any) -> OperationRecorder:
    global _RECORDER
    if _RECORDER is not None:
        _RECORDER.close()
    _RECORDER = OperationRecorder(store)
    atexit.register(_RECORDER.close)
    return _RECORDER


def operation_health() -> dict[str, Any]:
    recorder = _RECORDER
    return {"enabled": recorder is not None, "dropped": recorder.dropped if recorder else 0,
            "write_failures": recorder.write_failures if recorder else 0,
            "queue_depth": recorder.queue.qsize() if recorder else 0,
            "process_instance_id": _PROCESS_ID}


def fail_current_operation(code: str) -> None:
    scope = _ACTIVE_OPERATION.get()
    if scope is not None:
        scope.fail(valid_id(code) or "REQUEST_REJECTED")


@contextmanager
def operation_context(**identities: str | None):
    context = {**_CONTEXT.get(), **{key: value for key, raw in identities.items()
                                  if (value := valid_id(raw))}}
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def record_operation(event_name: str, *, status: str | None = None,
                     duration_ms: int | None = None, error_code: str | None = None,
                     details: dict[str, Any] | None = None, **identities: Any) -> None:
    try:
        recorder = _RECORDER
        if recorder is None:
            return
        from nanobot.agent.tools.context import current_request_context
        from nanobot.runtime.trace_context import current_trace_context

        context = {**_CONTEXT.get(), **identities}
        request = current_request_context()
        trace = current_trace_context()
        if request:
            context.setdefault("client_action_id", request.metadata.get("client_action_id"))
        if trace:
            context.setdefault("trace_id", trace.trace_id)
            context.setdefault("run_id", trace.run_id)
            context.setdefault("span_id", trace.span_id)
        recorder.record({
            "level": "error" if status == "failed" else "info",
            "component": "operations." + event_name.split(".", 1)[0],
            "event_name": event_name, "message": event_name,
            "duration_ms": duration_ms, "error_code": valid_id(error_code),
            **{key: valid_id(context.get(key)) for key in (
                "request_id", "project_id", "session_id", "turn_id", "trace_id",
                "run_id", "span_id", "tool_call_id", "artifact_id",
            )},
            "details": _details({**(details or {}), "status": status,
                                  "client_action_id": valid_id(context.get("client_action_id")),
                                  "operation_id": context.get("operation_id"),
                                  "parent_operation_id": context.get("parent_operation_id")}),
        })
    except Exception:
        # Never turn a diagnostics failure into a tool or transport failure.
        return


class Operation:
    def __init__(self, event_name: str, details: dict[str, Any]):
        self.event_name = event_name
        self.details = details
        self.error_code: str | None = None

    def fail(self, code: str) -> None:
        self.error_code = code


@contextmanager
def operation(event_name: str, **details: Any):
    scope = Operation(event_name, details)
    started = time.perf_counter()
    status = "completed"
    with operation_context(operation_id="op_" + uuid.uuid4().hex,
                           parent_operation_id=_CONTEXT.get().get("operation_id")):
        token = _ACTIVE_OPERATION.set(scope)
        record_operation(event_name, status="started", details=details)
        try:
            yield scope
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            scope.error_code = type(exc).__name__
            scope.details["error_type"] = type(exc).__name__
            import traceback
            frames = traceback.extract_tb(exc.__traceback__, limit=12)
            scope.details["stack"] = "\n".join(f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames)
            raise
        finally:
            record_operation(event_name, status="failed" if scope.error_code and status == "completed" else status,
                             error_code=scope.error_code, duration_ms=round((time.perf_counter() - started) * 1000),
                             details=scope.details)
            _ACTIVE_OPERATION.reset(token)

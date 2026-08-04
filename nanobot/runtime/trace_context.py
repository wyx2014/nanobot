"""Task-local identity for one observable agent execution tree.

The trace context deliberately carries identities only.  It never stores model
reasoning or prompt bodies, and ContextVar propagation makes child asyncio
tasks inherit their parent run without a process-global "current trace".
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    run_id: str | None = None
    span_id: str | None = None


_CURRENT_TRACE: ContextVar[TraceContext | None] = ContextVar(
    "nanobot_current_trace",
    default=None,
)
_PENDING_CONTEXT_ITEMS: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "nanobot_pending_trace_context_items",
    default=(),
)


def current_trace_context() -> TraceContext | None:
    return _CURRENT_TRACE.get()


def set_trace_context(context: TraceContext | None) -> Token[TraceContext | None]:
    return _CURRENT_TRACE.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    _CURRENT_TRACE.reset(token)


def activate_turn_trace(trace_id: str) -> None:
    """Bind a turn trace to the current task.

    Turn dispatch tasks are short lived.  Rebinding (rather than retaining a
    token on ActiveTurn) also supports internal continuations that resume the
    same turn from a different asyncio task.
    """
    _CURRENT_TRACE.set(TraceContext(trace_id=trace_id))
    _PENDING_CONTEXT_ITEMS.set(())


def clear_trace_context() -> None:
    _CURRENT_TRACE.set(None)
    _PENDING_CONTEXT_ITEMS.set(())


@contextmanager
def bind_trace_context(context: TraceContext) -> Iterator[TraceContext]:
    token = set_trace_context(context)
    try:
        yield context
    finally:
        reset_trace_context(token)


def content_fingerprint(value: str | bytes) -> str:
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def record_pending_context_item(
    *,
    item_kind: str,
    source_id: str | None = None,
    source_locator: str | None = None,
    content: str | bytes | None = None,
    token_estimate: int | None = None,
    selected_reason: str | None = None,
    rank: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Stage a redacted manifest item until the next Agent Run is created."""
    if current_trace_context() is None:
        return
    content_length = len(content) if content is not None else None
    item = {
        "item_kind": str(item_kind)[:80],
        "source_id": str(source_id)[:300] if source_id else None,
        "source_locator": str(source_locator)[:1_000] if source_locator else None,
        "content_hash": content_fingerprint(content) if content is not None else None,
        "token_estimate": (
            max(0, int(token_estimate))
            if token_estimate is not None
            else max(0, int((content_length or 0) / 4))
        ),
        "selected_reason": str(selected_reason)[:300] if selected_reason else None,
        "rank": float(rank) if rank is not None else None,
        "metadata": {
            **dict(metadata or {}),
            **({"content_length": content_length} if content_length is not None else {}),
        },
    }
    pending = _PENDING_CONTEXT_ITEMS.get()
    _PENDING_CONTEXT_ITEMS.set((*pending, item))


def drain_pending_context_items() -> list[dict[str, Any]]:
    pending = list(_PENDING_CONTEXT_ITEMS.get())
    _PENDING_CONTEXT_ITEMS.set(())
    return pending

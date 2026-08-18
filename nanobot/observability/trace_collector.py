"""Best-effort, single-writer collection of runtime trace records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanobot.observability.trace_store import TraceStore
from nanobot.runtime.trace_context import (
    TraceContext,
    current_trace_context,
    drain_pending_context_items,
)


@dataclass(slots=True)
class _WriteOperation:
    method: str | None
    kwargs: dict[str, Any]
    future: asyncio.Future[Any] | None = None


class TraceCollector:
    """Serialize TraceStore writes without making Trace a business dependency."""

    def __init__(
        self,
        store: TraceStore,
        *,
        max_queue: int = 2_048,
        flush_timeout_s: float = 2.0,
    ) -> None:
        self.store = store
        self.max_queue = max(32, int(max_queue))
        self.flush_timeout_s = max(0.1, float(flush_timeout_s))
        self._queue: asyncio.Queue[_WriteOperation | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._dropped = 0

    @staticmethod
    def new_trace_id() -> str:
        return f"trc_{uuid.uuid4().hex}"

    @staticmethod
    def new_run_id() -> str:
        return f"run_{uuid.uuid4().hex}"

    @staticmethod
    def new_span_id() -> str:
        return f"spn_{uuid.uuid4().hex}"

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        self._worker = asyncio.create_task(
            self._writer_loop(),
            name="nanobot-trace-writer",
        )

    async def _writer_loop(self) -> None:
        assert self._queue is not None
        while True:
            operation = await self._queue.get()
            try:
                if operation is None:
                    return
                if operation.method is None:
                    if operation.future is not None and not operation.future.done():
                        operation.future.set_result(None)
                    continue
                method = getattr(self.store, operation.method)
                result = await asyncio.to_thread(method, **operation.kwargs)
                if operation.future is not None and not operation.future.done():
                    operation.future.set_result(result)
            except BaseException as exc:
                if operation is not None and operation.future is not None:
                    if not operation.future.done():
                        operation.future.set_exception(exc)
                else:
                    logger.warning("Trace writer operation failed: {}", exc)
            finally:
                self._queue.task_done()

    async def _submit(
        self,
        method: str | None,
        *,
        wait: bool = False,
        terminal: bool = False,
        **kwargs: Any,
    ) -> Any:
        try:
            self._ensure_worker()
            assert self._queue is not None
            loop = asyncio.get_running_loop()
            future = loop.create_future() if wait else None
            operation = _WriteOperation(method=method, kwargs=kwargs, future=future)
            try:
                self._queue.put_nowait(operation)
            except asyncio.QueueFull:
                if not terminal:
                    self._dropped += 1
                    return None
                await asyncio.wait_for(
                    self._queue.put(operation),
                    timeout=self.flush_timeout_s,
                )
            if future is not None:
                return await asyncio.wait_for(future, timeout=self.flush_timeout_s)
        except BaseException as exc:
            # Cancellation must still cancel the business task; every other
            # Trace failure is deliberately degraded to diagnostics only.
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning(
                "Trace collector degraded during {} ({}): {}",
                method,
                type(exc).__name__,
                exc,
            )
            return None

    async def begin_trace(
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
        await self._submit(
            "begin_trace",
            wait=True,
            trace_id=trace_id,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            runtime_epoch=runtime_epoch,
            started_at=started_at,
            attributes=attributes,
        )

    async def end_trace(
        self,
        *,
        trace_id: str,
        status: str,
        error_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        await self._submit(
            "finish_trace",
            wait=True,
            terminal=True,
            trace_id=trace_id,
            status=status,
            ended_at=time.time_ns() // 1_000_000,
            error_code=error_code,
            error=error,
        )

    async def begin_run(
        self,
        *,
        trace_id: str,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        parent_span_id: str | None = None,
        agent_kind: str = "main",
        agent_label: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        resolved = run_id or self.new_run_id()
        await self._submit(
            "begin_run",
            wait=True,
            run_id=resolved,
            trace_id=trace_id,
            parent_run_id=parent_run_id,
            parent_span_id=parent_span_id,
            agent_kind=agent_kind,
            agent_label=agent_label,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            provider=provider,
            model=model,
            started_at=time.time_ns() // 1_000_000,
        )
        return resolved

    async def end_run(
        self,
        *,
        run_id: str,
        status: str,
        stop_reason: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        await self._submit(
            "finish_run",
            wait=True,
            terminal=True,
            run_id=run_id,
            status=status,
            ended_at=time.time_ns() // 1_000_000,
            stop_reason=stop_reason,
            error=error,
        )

    async def begin_span(
        self,
        *,
        kind: str,
        name: str,
        attributes: dict[str, Any] | None = None,
        context: TraceContext | None = None,
    ) -> str | None:
        current = context or current_trace_context()
        if current is None or current.run_id is None:
            return None
        span_id = self.new_span_id()
        await self._submit(
            "begin_span",
            wait=True,
            span_id=span_id,
            trace_id=current.trace_id,
            run_id=current.run_id,
            parent_span_id=current.span_id,
            kind=kind,
            name=name,
            started_at=time.time_ns() // 1_000_000,
            attributes=attributes,
        )
        return span_id

    async def end_span(
        self,
        span_id: str | None,
        *,
        status: str,
        usage: dict[str, int] | None = None,
        ttft_ms: int | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if span_id is None:
            return
        await self._submit(
            "finish_span",
            wait=False,
            terminal=True,
            span_id=span_id,
            status=status,
            ended_at=time.time_ns() // 1_000_000,
            usage=usage,
            ttft_ms=ttft_ms,
            retry_count=retry_count,
            error_code=error_code,
            attributes=attributes,
            error=error,
        )

    async def flush_pending_context(self, *, trace_id: str, run_id: str) -> None:
        for item in drain_pending_context_items():
            await self._submit(
                "add_context_item",
                trace_id=trace_id,
                run_id=run_id,
                item_kind=item.get("item_kind") or "unknown",
                source_id=item.get("source_id"),
                source_locator=item.get("source_locator"),
                content_hash=item.get("content_hash"),
                token_estimate=item.get("token_estimate"),
                selected_reason=item.get("selected_reason"),
                rank=item.get("rank"),
                metadata=item.get("metadata"),
            )

    async def record_prompt_manifest(
        self,
        *,
        trace_id: str,
        run_id: str,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]] | None,
    ) -> None:
        await self.flush_pending_context(trace_id=trace_id, run_id=run_id)
        role_counts: dict[str, int] = {}
        canonical_messages: list[dict[str, Any]] = []
        char_count = 0
        for message in messages:
            role = str(message.get("role") or "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
            content = message.get("content")
            if isinstance(content, str):
                char_count += len(content)
                canonical_content: Any = content
            elif isinstance(content, list):
                text_parts = [
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                char_count += sum(len(item) for item in text_parts)
                canonical_content = text_parts
            else:
                canonical_content = str(content or "")
            canonical_messages.append({"role": role, "content": canonical_content})
        raw = json.dumps(canonical_messages, ensure_ascii=False, sort_keys=True)
        await self._submit(
            "add_context_item",
            trace_id=trace_id,
            run_id=run_id,
            item_kind="message_history",
            source_id=None,
            source_locator=None,
            content_hash="sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            token_estimate=max(0, char_count // 4),
            selected_reason="agent_run_input",
            rank=None,
            metadata={"message_count": len(messages), "role_counts": role_counts},
        )
        tools_raw = json.dumps(tool_definitions or [], ensure_ascii=False, sort_keys=True)
        await self._submit(
            "add_context_item",
            trace_id=trace_id,
            run_id=run_id,
            item_kind="tool_contract",
            source_id=None,
            source_locator=None,
            content_hash="sha256:" + hashlib.sha256(tools_raw.encode("utf-8")).hexdigest(),
            token_estimate=max(0, len(tools_raw) // 4),
            selected_reason="available_toolset",
            rank=None,
            metadata={"tool_count": len(tool_definitions or [])},
        )

    async def recover_abandoned(self, *, runtime_epoch: str) -> int:
        result = await self._submit(
            "recover_abandoned",
            wait=True,
            terminal=True,
            runtime_epoch=runtime_epoch,
        )
        return int(result or 0)

    async def apply_retention(self, *, startup_delay_s: float = 0.0) -> int:
        if startup_delay_s > 0:
            await asyncio.sleep(startup_delay_s)
        now = time.time_ns() // 1_000_000
        day = 24 * 60 * 60 * 1_000
        result = await self._submit(
            "prune",
            wait=True,
            success_older_than_ms=now - 30 * day,
            failure_older_than_ms=now - 90 * day,
            keep_latest=1_000,
        )
        return int(result or 0)

    async def flush(self) -> None:
        await self._submit(None, wait=True, terminal=True)

    async def close(self) -> None:
        if self._worker is None or self._worker.done() or self._queue is None:
            return
        await self.flush()
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._worker, timeout=self.flush_timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._worker.cancel()
        finally:
            self._worker = None
            self._queue = None

    @staticmethod
    def summarize_tool_arguments(arguments: Any) -> dict[str, Any]:
        try:
            raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            raw = str(type(arguments).__name__)
        keys = sorted(str(key) for key in arguments) if isinstance(arguments, dict) else []
        return {
            "argument_keys": keys[:50],
            "argument_bytes": len(raw.encode("utf-8", errors="replace")),
            "argument_hash": "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }

    @property
    def dropped_operations(self) -> int:
        return self._dropped

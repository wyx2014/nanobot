"""Codex-style authoritative Thread / ActiveTurn lifecycle.

Live ``active`` state belongs to this process-local registry. Durable SQLite
rows are history/projections and must never resurrect a live turn by
themselves.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from nanobot.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventContext,
    ThreadRuntimeStatusChanged,
    TurnLifecycleCompleted,
    TurnLifecycleStarted,
)


class TurnStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
        }


class FinishReason(StrEnum):
    SUCCESS = "success"
    MODEL_ERROR = "modelError"
    TOOL_ERROR = "toolError"
    USER_INTERRUPTED = "userInterrupted"
    REPLACED = "replaced"
    GATEWAY_RESTARTED = "gatewayRestarted"
    SHUTDOWN = "shutdown"
    INTERNAL_ERROR = "internalError"


class TurnLifecycleError(RuntimeError):
    """Raised when a turn transition violates lifecycle invariants."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TurnError:
    code: str
    message: str
    retryable: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass
class ActiveTurn:
    id: str
    context: RuntimeEventContext
    runtime_epoch: str
    project_id: str | None
    session_id: str | None
    started_at: float
    task: asyncio.Task[Any] | None = None
    final_answer_committed: bool = False
    same_turn_children: set[str] = field(default_factory=set)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runtime_epoch": self.runtime_epoch,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "status": TurnStatus.IN_PROGRESS.value,
            "started_at": int(self.started_at * 1000),
            "completed_at": None,
            "duration_ms": None,
            "finish_reason": None,
        }


@dataclass(frozen=True)
class TerminalTurn:
    id: str
    runtime_epoch: str
    project_id: str | None
    session_id: str | None
    status: TurnStatus
    started_at: float
    completed_at: float
    finish_reason: FinishReason
    error: TurnError | None = None

    def payload(self) -> dict[str, Any]:
        duration_ms = max(0, round((self.completed_at - self.started_at) * 1000))
        return {
            "id": self.id,
            "runtime_epoch": self.runtime_epoch,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "started_at": int(self.started_at * 1000),
            "completed_at": int(self.completed_at * 1000),
            "duration_ms": duration_ms,
            "finish_reason": self.finish_reason.value,
            **({"error": self.error.payload()} if self.error is not None else {}),
        }


@dataclass
class _RuntimeFacts:
    is_loaded: bool = False
    active_turn: ActiveTurn | None = None
    latest_turn: TerminalTurn | None = None
    pending_approval_count: int = 0
    pending_user_input_count: int = 0
    system_error_code: str | None = None
    snapshot_revision: int = 0
    terminalizing_turn_id: str | None = None


@dataclass(frozen=True)
class ThreadRuntimeSnapshot:
    session_key: str
    runtime_epoch: str
    snapshot_revision: int
    thread_status: dict[str, Any]
    active_turn: dict[str, Any] | None
    latest_turn: dict[str, Any] | None

    def payload(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "runtime_epoch": self.runtime_epoch,
            "snapshot_revision": self.snapshot_revision,
            "thread_status": dict(self.thread_status),
            "active_turn": (
                dict(self.active_turn) if self.active_turn is not None else None
            ),
            "latest_turn": (
                dict(self.latest_turn) if self.latest_turn is not None else None
            ),
        }


SnapshotListener = Callable[[ThreadRuntimeSnapshot], Awaitable[None] | None]


class ThreadRuntimeRegistry:
    """Single authority for live Thread and ActiveTurn state."""

    def __init__(
        self,
        *,
        runtime_events: RuntimeEventBus | None = None,
        runtime_epoch: str | None = None,
    ) -> None:
        self.runtime_epoch = runtime_epoch or uuid.uuid4().hex
        self._runtime_events = runtime_events
        self._facts: dict[str, _RuntimeFacts] = {}
        self._lock = asyncio.Lock()
        self._listeners: set[SnapshotListener] = set()

    def subscribe(self, listener: SnapshotListener) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def start_turn(
        self,
        *,
        context: RuntimeEventContext,
        turn_id: str,
        project_id: str | None = None,
        session_id: str | None = None,
        started_at: float | None = None,
        task: asyncio.Task[Any] | None = None,
    ) -> ActiveTurn:
        normalized_turn_id = turn_id.strip()
        if not normalized_turn_id:
            raise TurnLifecycleError("TURN_ID_REQUIRED", "turn id is required")
        async with self._lock:
            facts = self._facts.setdefault(context.session_key, _RuntimeFacts())
            current = facts.active_turn
            if current is not None:
                if current.id == normalized_turn_id:
                    if task is not None:
                        current.task = task
                    return current
                raise TurnLifecycleError(
                    "TURN_ALREADY_ACTIVE",
                    f"session {context.session_key!r} already has active turn {current.id!r}",
                )
            active = ActiveTurn(
                id=normalized_turn_id,
                context=context,
                runtime_epoch=self.runtime_epoch,
                project_id=project_id,
                session_id=session_id,
                started_at=started_at or time.time(),
                task=task,
            )
            facts.is_loaded = True
            facts.active_turn = active
            facts.system_error_code = None
            facts.terminalizing_turn_id = None
            facts.snapshot_revision += 1
            snapshot = self._snapshot_locked(context.session_key, facts)
        if self._runtime_events is not None:
            await self._runtime_events.publish(
                TurnLifecycleStarted(
                    context=context,
                    turn=active.payload(),
                    snapshot_revision=snapshot.snapshot_revision,
                )
            )
        logger.bind(
            runtime_epoch=self.runtime_epoch,
            project_id=project_id,
            session_id=session_id,
            session_key=context.session_key,
            turn_id=active.id,
            previous_status="idle",
            next_status=TurnStatus.IN_PROGRESS.value,
            snapshot_revision=snapshot.snapshot_revision,
            task_name=active.task.get_name() if active.task is not None else None,
        ).info("turn lifecycle started")
        await self._publish_snapshot(snapshot)
        return active

    async def attach_current_task(self, session_key: str, turn_id: str) -> None:
        task = asyncio.current_task()
        async with self._lock:
            facts = self._facts.get(session_key)
            active = facts.active_turn if facts is not None else None
            if active is None or active.id != turn_id:
                raise TurnLifecycleError(
                    "TURN_ID_MISMATCH",
                    f"active turn for {session_key!r} does not match {turn_id!r}",
                )
            active.task = task

    async def commit_final_answer(self, session_key: str, turn_id: str) -> None:
        """Atomically guard the single user-visible final answer for a turn."""
        async with self._lock:
            facts = self._facts.get(session_key)
            active = facts.active_turn if facts is not None else None
            if active is None or active.id != turn_id:
                raise TurnLifecycleError(
                    "TURN_ID_MISMATCH",
                    f"active turn for {session_key!r} does not match {turn_id!r}",
                )
            if active.final_answer_committed:
                raise TurnLifecycleError(
                    "FINAL_ANSWER_ALREADY_COMMITTED",
                    f"turn {turn_id!r} already committed its final answer",
                )
            active.final_answer_committed = True

    async def finish_turn(
        self,
        *,
        session_key: str,
        expected_turn_id: str,
        status: TurnStatus,
        finish_reason: FinishReason,
        error: TurnError | None = None,
        completed_at: float | None = None,
    ) -> TerminalTurn:
        if not status.terminal:
            raise TurnLifecycleError(
                "TURN_TERMINAL_STATUS_REQUIRED",
                f"{status.value!r} is not a terminal turn status",
            )
        async with self._lock:
            facts = self._facts.get(session_key)
            active = facts.active_turn if facts is not None else None
            if active is None:
                latest = facts.latest_turn if facts is not None else None
                if (
                    latest is not None
                    and latest.id == expected_turn_id
                    and latest.status == status
                ):
                    return latest
                raise TurnLifecycleError(
                    "TURN_NOT_ACTIVE",
                    f"session {session_key!r} has no active turn",
                )
            if active.id != expected_turn_id:
                raise TurnLifecycleError(
                    "TURN_ID_MISMATCH",
                    f"active turn {active.id!r} does not match {expected_turn_id!r}",
                )
            if facts.terminalizing_turn_id is not None:
                raise TurnLifecycleError(
                    "TURN_TERMINALIZING",
                    f"turn {expected_turn_id!r} is already committing its terminal event",
                )
            terminal = TerminalTurn(
                id=active.id,
                runtime_epoch=active.runtime_epoch,
                project_id=active.project_id,
                session_id=active.session_id,
                status=status,
                started_at=active.started_at,
                completed_at=completed_at or time.time(),
                finish_reason=finish_reason,
                error=error,
            )
            facts.terminalizing_turn_id = active.id
            terminal_revision = facts.snapshot_revision + 1
            context = active.context
        try:
            if self._runtime_events is not None:
                await self._runtime_events.publish(
                    TurnLifecycleCompleted(
                        context=context,
                        turn=terminal.payload(),
                        snapshot_revision=terminal_revision,
                    )
                )
        except Exception:
            async with self._lock:
                current = self._facts.get(session_key)
                if (
                    current is not None
                    and current.terminalizing_turn_id == expected_turn_id
                ):
                    current.terminalizing_turn_id = None
            raise
        async with self._lock:
            facts = self._facts.get(session_key)
            active = facts.active_turn if facts is not None else None
            if (
                facts is None
                or active is None
                or active.id != expected_turn_id
                or facts.terminalizing_turn_id != expected_turn_id
            ):
                raise TurnLifecycleError(
                    "TURN_TERMINAL_COMMIT_LOST",
                    f"turn {expected_turn_id!r} changed during terminal commit",
                )
            facts.active_turn = None
            facts.latest_turn = terminal
            facts.pending_approval_count = 0
            facts.pending_user_input_count = 0
            facts.terminalizing_turn_id = None
            facts.snapshot_revision = terminal_revision
            snapshot = self._snapshot_locked(session_key, facts)
        logger.bind(
            runtime_epoch=terminal.runtime_epoch,
            project_id=terminal.project_id,
            session_id=terminal.session_id,
            session_key=session_key,
            turn_id=terminal.id,
            previous_status=TurnStatus.IN_PROGRESS.value,
            next_status=terminal.status.value,
            finish_reason=terminal.finish_reason.value,
            event_id=f"terminal_{terminal.runtime_epoch}_{terminal.id}",
            snapshot_revision=snapshot.snapshot_revision,
            task_name=active.task.get_name() if active.task is not None else None,
        ).info("turn lifecycle completed")
        await self._publish_snapshot(snapshot)
        return terminal

    async def set_system_error(
        self,
        session_key: str,
        *,
        error_code: str,
    ) -> ThreadRuntimeSnapshot:
        async with self._lock:
            facts = self._facts.setdefault(session_key, _RuntimeFacts())
            facts.is_loaded = True
            facts.active_turn = None
            facts.pending_approval_count = 0
            facts.pending_user_input_count = 0
            facts.system_error_code = error_code
            facts.terminalizing_turn_id = None
            facts.snapshot_revision += 1
            snapshot = self._snapshot_locked(session_key, facts)
        await self._publish_snapshot(snapshot)
        return snapshot

    async def unload_thread(self, session_key: str) -> ThreadRuntimeSnapshot:
        async with self._lock:
            facts = self._facts.setdefault(session_key, _RuntimeFacts())
            facts.is_loaded = False
            facts.active_turn = None
            facts.pending_approval_count = 0
            facts.pending_user_input_count = 0
            facts.terminalizing_turn_id = None
            facts.snapshot_revision += 1
            snapshot = self._snapshot_locked(session_key, facts)
        await self._publish_snapshot(snapshot)
        return snapshot

    async def snapshot(self, session_key: str) -> ThreadRuntimeSnapshot:
        async with self._lock:
            facts = self._facts.get(session_key)
            if facts is None:
                facts = _RuntimeFacts()
            return self._snapshot_locked(session_key, facts)

    async def active_turn(self, session_key: str) -> ActiveTurn | None:
        async with self._lock:
            facts = self._facts.get(session_key)
            return facts.active_turn if facts is not None else None

    async def running_turn_count(self) -> int:
        async with self._lock:
            return sum(1 for facts in self._facts.values() if facts.active_turn is not None)

    def _snapshot_locked(
        self,
        session_key: str,
        facts: _RuntimeFacts,
    ) -> ThreadRuntimeSnapshot:
        if not facts.is_loaded:
            status: dict[str, Any] = {"type": "notLoaded"}
        elif facts.active_turn is not None or (
            facts.pending_approval_count > 0 or facts.pending_user_input_count > 0
        ):
            active_flags: list[str] = []
            if facts.pending_approval_count > 0:
                active_flags.append("waitingOnApproval")
            if facts.pending_user_input_count > 0:
                active_flags.append("waitingOnUserInput")
            status = {"type": "active", "active_flags": active_flags}
        elif facts.system_error_code is not None:
            status = {
                "type": "systemError",
                "error_code": facts.system_error_code,
            }
        else:
            status = {"type": "idle"}
        return ThreadRuntimeSnapshot(
            session_key=session_key,
            runtime_epoch=self.runtime_epoch,
            snapshot_revision=facts.snapshot_revision,
            thread_status=status,
            active_turn=(
                facts.active_turn.payload() if facts.active_turn is not None else None
            ),
            latest_turn=(
                facts.latest_turn.payload() if facts.latest_turn is not None else None
            ),
        )

    async def _publish_snapshot(self, snapshot: ThreadRuntimeSnapshot) -> None:
        for listener in tuple(self._listeners):
            result = listener(snapshot)
            if asyncio.iscoroutine(result):
                await result
        if self._runtime_events is not None:
            await self._runtime_events.publish(
                ThreadRuntimeStatusChanged(
                    session_key=snapshot.session_key,
                    snapshot=snapshot.payload(),
                )
            )


class TurnLifecycleManager:
    """Small transition facade used by AgentLoop and interrupt handlers."""

    def __init__(self, registry: ThreadRuntimeRegistry) -> None:
        self.registry = registry

    def turn_scope(
        self,
        *,
        context: RuntimeEventContext,
        turn_id: str,
        project_id: str | None = None,
        session_id: str | None = None,
        started_at: float | None = None,
    ) -> "TurnScope":
        return TurnScope(
            lifecycle=self,
            context=context,
            turn_id=turn_id,
            project_id=project_id,
            session_id=session_id,
            started_at=started_at,
        )

    async def start_turn(
        self,
        *,
        context: RuntimeEventContext,
        turn_id: str,
        project_id: str | None = None,
        session_id: str | None = None,
        started_at: float | None = None,
    ) -> ActiveTurn:
        return await self.registry.start_turn(
            context=context,
            turn_id=turn_id,
            project_id=project_id,
            session_id=session_id,
            started_at=started_at,
            task=asyncio.current_task(),
        )

    async def finish_turn(
        self,
        *,
        session_key: str,
        expected_turn_id: str,
        status: TurnStatus,
        finish_reason: FinishReason,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
    ) -> TerminalTurn:
        error = (
            TurnError(
                code=error_code,
                message=error_message or error_code,
                retryable=retryable,
            )
            if error_code
            else None
        )
        return await self.registry.finish_turn(
            session_key=session_key,
            expected_turn_id=expected_turn_id,
            status=status,
            finish_reason=finish_reason,
            error=error,
        )

    async def commit_final_answer(
        self,
        *,
        session_key: str,
        expected_turn_id: str,
    ) -> None:
        await self.registry.commit_final_answer(session_key, expected_turn_id)


class TurnScope:
    """Exception-safe lifecycle boundary for new runner entry points."""

    def __init__(
        self,
        *,
        lifecycle: TurnLifecycleManager,
        context: RuntimeEventContext,
        turn_id: str,
        project_id: str | None,
        session_id: str | None,
        started_at: float | None,
    ) -> None:
        self.lifecycle = lifecycle
        self.context = context
        self.turn_id = turn_id
        self.project_id = project_id
        self.session_id = session_id
        self.started_at = started_at
        self._terminal = False

    async def __aenter__(self) -> "TurnScope":
        await self.lifecycle.start_turn(
            context=self.context,
            turn_id=self.turn_id,
            project_id=self.project_id,
            session_id=self.session_id,
            started_at=self.started_at,
        )
        return self

    async def complete(self) -> TerminalTurn:
        terminal = await self.lifecycle.finish_turn(
            session_key=self.context.session_key,
            expected_turn_id=self.turn_id,
            status=TurnStatus.COMPLETED,
            finish_reason=FinishReason.SUCCESS,
        )
        self._terminal = True
        return terminal

    async def interrupt(
        self,
        reason: FinishReason = FinishReason.USER_INTERRUPTED,
    ) -> TerminalTurn:
        terminal = await self.lifecycle.finish_turn(
            session_key=self.context.session_key,
            expected_turn_id=self.turn_id,
            status=TurnStatus.INTERRUPTED,
            finish_reason=reason,
        )
        self._terminal = True
        return terminal

    async def fail(
        self,
        *,
        reason: FinishReason = FinishReason.INTERNAL_ERROR,
        error_code: str = "TURN_INTERNAL_ERROR",
        error_message: str | None = None,
    ) -> TerminalTurn:
        terminal = await self.lifecycle.finish_turn(
            session_key=self.context.session_key,
            expected_turn_id=self.turn_id,
            status=TurnStatus.FAILED,
            finish_reason=reason,
            error_code=error_code,
            error_message=error_message,
        )
        self._terminal = True
        return terminal

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: Any,
    ) -> bool:
        if self._terminal:
            return False
        if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
            await self.interrupt(FinishReason.USER_INTERRUPTED)
            return False
        await self.fail(
            error_code=(
                "TURN_SCOPE_EXIT_WITHOUT_TERMINAL"
                if exc is None
                else "TURN_INTERNAL_ERROR"
            ),
            error_message=(
                "turn scope exited without an explicit terminal"
                if exc is None
                else str(exc)
            ),
        )
        return False

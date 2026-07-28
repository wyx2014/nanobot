import asyncio

import pytest

from nanobot.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventContext,
    ThreadRuntimeStatusChanged,
    TurnLifecycleCompleted,
    TurnLifecycleStarted,
)
from nanobot.runtime.turn_lifecycle import (
    FinishReason,
    ThreadRuntimeRegistry,
    TurnLifecycleError,
    TurnLifecycleManager,
    TurnStatus,
)


def _context(session_key: str = "websocket:chat-a") -> RuntimeEventContext:
    return RuntimeEventContext(
        channel="websocket",
        chat_id="chat-a",
        session_key=session_key,
        metadata={"webui": True},
    )


@pytest.mark.asyncio
async def test_active_turn_is_the_only_source_of_active_status() -> None:
    registry = ThreadRuntimeRegistry(runtime_epoch="epoch-a")
    lifecycle = TurnLifecycleManager(registry)

    before = await registry.snapshot("websocket:chat-a")
    assert before.thread_status == {"type": "notLoaded"}

    await lifecycle.start_turn(
        context=_context(),
        turn_id="turn-a",
        project_id="project-a",
        session_id="session-a",
        started_at=10.0,
    )
    active = await registry.snapshot("websocket:chat-a")
    assert active.thread_status == {"type": "active", "active_flags": []}
    assert active.active_turn == {
        "id": "turn-a",
        "runtime_epoch": "epoch-a",
        "project_id": "project-a",
        "session_id": "session-a",
        "status": "inProgress",
        "started_at": 10_000,
        "completed_at": None,
        "duration_ms": None,
        "finish_reason": None,
    }

    await lifecycle.finish_turn(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
        status=TurnStatus.COMPLETED,
        finish_reason=FinishReason.SUCCESS,
    )
    completed = await registry.snapshot("websocket:chat-a")
    assert completed.thread_status == {"type": "idle"}
    assert completed.active_turn is None
    assert completed.latest_turn is not None
    assert completed.latest_turn["status"] == "completed"


@pytest.mark.asyncio
async def test_start_rejects_second_active_turn_and_expected_id_guards_finish() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)
    await lifecycle.start_turn(context=_context(), turn_id="turn-a")

    with pytest.raises(TurnLifecycleError) as start_error:
        await lifecycle.start_turn(context=_context(), turn_id="turn-b")
    assert start_error.value.code == "TURN_ALREADY_ACTIVE"

    with pytest.raises(TurnLifecycleError) as finish_error:
        await lifecycle.finish_turn(
            session_key="websocket:chat-a",
            expected_turn_id="turn-b",
            status=TurnStatus.INTERRUPTED,
            finish_reason=FinishReason.USER_INTERRUPTED,
        )
    assert finish_error.value.code == "TURN_ID_MISMATCH"
    assert (await registry.snapshot("websocket:chat-a")).thread_status["type"] == "active"


@pytest.mark.asyncio
async def test_terminal_transition_is_idempotent_but_conflicts_are_rejected() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)
    await lifecycle.start_turn(context=_context(), turn_id="turn-a")
    first = await lifecycle.finish_turn(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
        status=TurnStatus.INTERRUPTED,
        finish_reason=FinishReason.USER_INTERRUPTED,
    )
    second = await lifecycle.finish_turn(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
        status=TurnStatus.INTERRUPTED,
        finish_reason=FinishReason.USER_INTERRUPTED,
    )
    assert second is first

    with pytest.raises(TurnLifecycleError) as conflict:
        await lifecycle.finish_turn(
            session_key="websocket:chat-a",
            expected_turn_id="turn-a",
            status=TurnStatus.COMPLETED,
            finish_reason=FinishReason.SUCCESS,
        )
    assert conflict.value.code == "TURN_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_system_error_clears_running_task_and_pending_state() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)
    await lifecycle.start_turn(context=_context(), turn_id="turn-a")

    snapshot = await registry.set_system_error(
        "websocket:chat-a",
        error_code="TURN_TERMINAL_PERSIST_FAILED",
    )
    assert snapshot.thread_status == {
        "type": "systemError",
        "error_code": "TURN_TERMINAL_PERSIST_FAILED",
    }
    assert snapshot.active_turn is None
    assert await registry.running_turn_count() == 0


@pytest.mark.asyncio
async def test_registry_publishes_revisioned_lifecycle_and_snapshot_events() -> None:
    bus = RuntimeEventBus()
    seen: list[object] = []
    bus.subscribe(seen.append)
    registry = ThreadRuntimeRegistry(runtime_events=bus, runtime_epoch="epoch-a")
    lifecycle = TurnLifecycleManager(registry)

    await lifecycle.start_turn(context=_context(), turn_id="turn-a", started_at=10.0)
    await lifecycle.finish_turn(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
        status=TurnStatus.FAILED,
        finish_reason=FinishReason.MODEL_ERROR,
        error_code="MODEL_FAILED",
        error_message="provider failed",
        retryable=True,
    )

    assert [type(event) for event in seen] == [
        TurnLifecycleStarted,
        ThreadRuntimeStatusChanged,
        TurnLifecycleCompleted,
        ThreadRuntimeStatusChanged,
    ]
    started = seen[0]
    completed = seen[2]
    assert isinstance(started, TurnLifecycleStarted)
    assert started.snapshot_revision == 1
    assert isinstance(completed, TurnLifecycleCompleted)
    assert completed.snapshot_revision == 2
    assert completed.turn["status"] == "failed"
    assert completed.turn["error"] == {
        "code": "MODEL_FAILED",
        "message": "provider failed",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_start_is_idempotent_for_internal_continuation_of_same_turn() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)
    first = await lifecycle.start_turn(context=_context(), turn_id="turn-a")
    second = await lifecycle.start_turn(context=_context(), turn_id="turn-a")

    assert second is first
    assert await registry.running_turn_count() == 1
    assert (await registry.snapshot("websocket:chat-a")).snapshot_revision == 1
    assert isinstance(first.task, asyncio.Task)


@pytest.mark.asyncio
async def test_final_answer_can_only_be_committed_once() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)
    await lifecycle.start_turn(context=_context(), turn_id="turn-a")

    await lifecycle.commit_final_answer(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
    )
    with pytest.raises(TurnLifecycleError) as duplicate:
        await lifecycle.commit_final_answer(
            session_key="websocket:chat-a",
            expected_turn_id="turn-a",
        )
    assert duplicate.value.code == "FINAL_ANSWER_ALREADY_COMMITTED"


@pytest.mark.asyncio
async def test_turn_scope_terminalizes_unhandled_exception() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)

    with pytest.raises(RuntimeError, match="boom"):
        async with lifecycle.turn_scope(
            context=_context(),
            turn_id="turn-scope",
        ):
            raise RuntimeError("boom")

    snapshot = await registry.snapshot("websocket:chat-a")
    assert snapshot.thread_status == {"type": "idle"}
    assert snapshot.latest_turn is not None
    assert snapshot.latest_turn["status"] == "failed"
    assert snapshot.latest_turn["finish_reason"] == "internalError"


@pytest.mark.asyncio
async def test_turn_scope_exit_without_complete_is_failed_not_running() -> None:
    registry = ThreadRuntimeRegistry()
    lifecycle = TurnLifecycleManager(registry)

    async with lifecycle.turn_scope(
        context=_context(),
        turn_id="turn-scope",
    ):
        pass

    snapshot = await registry.snapshot("websocket:chat-a")
    assert snapshot.thread_status == {"type": "idle"}
    assert snapshot.latest_turn is not None
    assert snapshot.latest_turn["error"]["code"] == "TURN_SCOPE_EXIT_WITHOUT_TERMINAL"


@pytest.mark.asyncio
async def test_terminal_barrier_failure_does_not_publish_false_idle() -> None:
    bus = RuntimeEventBus()

    async def fail_terminal(event: object) -> None:
        if isinstance(event, TurnLifecycleCompleted):
            raise RuntimeError("disk full")

    bus.subscribe(fail_terminal, required=True)
    registry = ThreadRuntimeRegistry(runtime_events=bus)
    lifecycle = TurnLifecycleManager(registry)
    await lifecycle.start_turn(context=_context(), turn_id="turn-a")

    with pytest.raises(RuntimeError, match="disk full"):
        await lifecycle.finish_turn(
            session_key="websocket:chat-a",
            expected_turn_id="turn-a",
            status=TurnStatus.COMPLETED,
            finish_reason=FinishReason.SUCCESS,
        )

    snapshot = await registry.snapshot("websocket:chat-a")
    assert snapshot.thread_status["type"] == "active"
    assert snapshot.active_turn is not None

from pathlib import Path

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventBus, RuntimeEventContext
from nanobot.runtime.turn_lifecycle import (
    FinishReason,
    ThreadRuntimeRegistry,
    TurnLifecycleManager,
    TurnStatus,
)
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WebuiTurnCoordinator
from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.logs import StructuredLogStore
from nanobot.storage.state import StateStore
from nanobot.webui.transcript import WebUITranscriptRecorder


@pytest.mark.asyncio
async def test_terminal_barrier_persists_before_registry_becomes_idle(
    tmp_path: Path,
) -> None:
    state = StateStore(
        tmp_path / "state.sqlite",
        default_workspace=tmp_path,
    )
    project = state.ensure_project(tmp_path)
    session = state.bind_session("websocket:chat-a", project.id)
    rows: list[dict] = []
    journal = SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "logs.sqlite"),
        append_record=lambda _key, row: rows.append(dict(row)),
        read_records=lambda _key: list(rows),
    )
    recorder = WebUITranscriptRecorder(journal=journal)
    message_bus = MessageBus()
    runtime_events = RuntimeEventBus()
    WebuiTurnCoordinator(
        bus=message_bus,
        sessions=SessionManager(tmp_path),
        schedule_background=lambda _coro: None,
        transcripts=recorder,
    ).subscribe(runtime_events)
    registry = ThreadRuntimeRegistry(
        runtime_events=runtime_events,
        runtime_epoch="epoch-a",
    )
    lifecycle = TurnLifecycleManager(registry)
    context = RuntimeEventContext(
        channel="websocket",
        chat_id="chat-a",
        session_key=session.session_key,
        metadata={
            "webui": True,
            "webui_turn_id": "turn-a",
            "_runtime_turn_id": "turn-a",
        },
    )

    await lifecycle.start_turn(
        context=context,
        turn_id="turn-a",
        project_id=project.id,
        session_id=session.id,
        started_at=1.0,
    )
    await lifecycle.finish_turn(
        session_key=session.session_key,
        expected_turn_id="turn-a",
        status=TurnStatus.COMPLETED,
        finish_reason=FinishReason.SUCCESS,
        usage={
            "prompt_tokens": 1200,
            "completion_tokens": 34,
            "total_tokens": 1234,
        },
    )

    assert [row["event"] for row in rows] == [
        "turn_started",
        "turn_completed",
    ]
    assert rows[-1]["event_id"] == "terminal_epoch-a_turn-a"
    assert rows[-1]["turn"]["usage"] == {
        "prompt_tokens": 1200,
        "completion_tokens": 34,
        "total_tokens": 1234,
    }
    assert state.active_turn_id(session.session_key) is None
    latest = state.latest_turn_snapshot(session.session_key)
    assert latest is not None
    assert latest["status"] == "completed"
    assert (await registry.snapshot(session.session_key)).thread_status == {
        "type": "idle"
    }

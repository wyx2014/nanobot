from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.logs import StructuredLogStore
from nanobot.storage.session_events import (
    InvalidSessionEvent,
    SessionEventFileStore,
    SessionEventService,
)
from nanobot.storage.state import StateStore


def _state(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    state = StateStore(
        tmp_path / "runtime" / "state.sqlite",
        default_workspace=tmp_path / "inbox",
    )
    project = state.ensure_project(project_path)
    session = state.bind_session("websocket:chat-a", project.id)
    return state, session


def test_canonical_event_files_migrate_legacy_once_and_survive_cache_deletion(
    tmp_path: Path,
) -> None:
    legacy = [{"event": "user", "turn_id": "turn-a", "text": "hello"}]
    files = SessionEventFileStore(
        tmp_path / "runtime" / "session-events",
        legacy_reader=lambda _key: list(legacy),
    )

    assert files.read("websocket:chat-a") == legacy
    canonical_path = files.path_for("websocket:chat-a")
    assert canonical_path.is_file()

    legacy.clear()
    assert files.read("websocket:chat-a") == [
        {"event": "user", "turn_id": "turn-a", "text": "hello"}
    ]


def test_session_event_service_is_the_single_commit_boundary(tmp_path: Path) -> None:
    state, session = _state(tmp_path)
    files = SessionEventFileStore(tmp_path / "runtime" / "session-events")
    service = SessionEventService(SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "runtime" / "logs.sqlite"),
        append_record=files.append,
        read_records=files.read,
    ))

    committed = service.commit(
        session.session_key,
        {"event": "user", "turn_id": "turn-a", "text": "hello"},
    )
    assert committed["event_seq"] == 1
    assert committed["project_id"] == session.project_id
    assert state.projection_counts(session.session_key)["messages"] == 1
    assert files.read(session.session_key) == [committed]

    display = state.session_display_event_envelopes(session.session_key)
    assert [event["event"] for event in display] == ["user"]
    assert display[0]["text"] == "hello"

    with pytest.raises(InvalidSessionEvent):
        service.commit(session.session_key, {"event": ""})


def test_display_event_envelopes_page_backwards_by_event_sequence(
    tmp_path: Path,
) -> None:
    state, session = _state(tmp_path)
    service = SessionEventService(SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "runtime" / "logs.sqlite"),
        append_record=SessionEventFileStore(
            tmp_path / "runtime" / "session-events"
        ).append,
        read_records=SessionEventFileStore(
            tmp_path / "runtime" / "session-events"
        ).read,
    ))
    for index in range(1, 6):
        service.commit(
            session.session_key,
            {
                "event": "user",
                "turn_id": f"turn-{index}",
                "text": f"message-{index}",
            },
        )

    latest = state.session_display_event_envelopes(session.session_key, limit=2)
    assert [event["event_seq"] for event in latest] == [4, 5]
    older = state.session_display_event_envelopes(
        session.session_key,
        limit=2,
        before_event_seq=4,
    )
    assert [event["event_seq"] for event in older] == [2, 3]


def test_display_event_envelopes_keep_only_latest_task_progress_per_plan(
    tmp_path: Path,
) -> None:
    state, session = _state(tmp_path)
    files = SessionEventFileStore(tmp_path / "runtime" / "session-events")
    service = SessionEventService(SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "runtime" / "logs.sqlite"),
        append_record=files.append,
        read_records=files.read,
    ))

    service.commit(
        session.session_key,
        {"event": "user", "turn_id": "turn-a", "text": "research"},
    )
    service.commit(
        session.session_key,
        {
            "event": "message",
            "turn_id": "turn-a",
            "kind": "progress",
            "text": "",
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": "plan-a",
                "revision": 1,
                "steps": [{
                    "id": "research",
                    "title": "Research",
                    "status": "running",
                }],
            },
        },
    )
    service.commit(
        session.session_key,
        {
            "event": "message",
            "turn_id": "turn-a",
            "kind": "tool_hint",
            "text": "searching",
        },
    )
    service.commit(
        session.session_key,
        {
            "event": "message",
            "turn_id": "turn-a",
            "kind": "progress",
            "text": "",
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": "plan-a",
                "revision": 2,
                "steps": [{
                    "id": "research",
                    "title": "Research",
                    "status": "completed",
                }],
            },
        },
    )
    service.commit(
        session.session_key,
        {
            "event": "message",
            "turn_id": "turn-a",
            "kind": "progress",
            "text": "",
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": "plan-b",
                "revision": 1,
                "steps": [{
                    "id": "delivery",
                    "title": "Delivery",
                    "status": "running",
                }],
            },
        },
    )

    display = state.session_display_event_envelopes(session.session_key)
    assert [event["event_seq"] for event in display] == [1, 3, 4, 5]
    progress = [
        event for event in display
        if event.get("agent_ui", {}).get("kind") == "task_progress"
    ]
    assert len(progress) == 2
    assert [event["agent_ui"]["plan_id"] for event in progress] == [
        "plan-a",
        "plan-b",
    ]
    assert progress[0]["agent_ui"]["revision"] == 2

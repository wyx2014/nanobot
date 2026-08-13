from __future__ import annotations

import json
import sqlite3
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


def test_large_projected_message_remains_valid_json(tmp_path: Path) -> None:
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
        {
            "event": "message",
            "turn_id": "turn-large",
            "kind": "progress",
            "text": "",
            "tool_events": [
                {
                    "name": f"large_lookup_{index}",
                    "result": "large-result-" * 2_000,
                }
                for index in range(8)
            ],
        },
    )

    with sqlite3.connect(state.path) as connection:
        content_json, event_json = connection.execute(
            """
            SELECT m.content_json, pe.payload_json
            FROM messages AS m
            JOIN projected_events AS pe
              ON pe.project_id = m.project_id
             AND pe.session_id = m.session_id
             AND pe.event_seq = m.sequence_no
            WHERE m.session_id = ?
            """,
            (session.id,),
        ).fetchone()

    assert len(str(event_json)) > 64_000
    assert len(str(content_json)) <= 64_000
    assert isinstance(json.loads(str(content_json)), dict)
    display = state.session_display_event_envelopes(session.session_key)
    assert len(display) == 1
    assert display[0]["tool_events"][0]["result"].startswith("large-result-")


def test_display_event_envelopes_tolerate_legacy_malformed_json(
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
            "text": "working",
        },
    )
    with sqlite3.connect(state.path) as connection:
        connection.execute(
            "UPDATE messages SET content_json = ? WHERE sequence_no = 2",
            ('{"truncated":',),
        )
        connection.commit()

    display = state.session_display_event_envelopes(session.session_key)

    assert [event["event_seq"] for event in display] == [1, 2]
    assert display[1]["text"] == "working"


def test_schema_upgrade_repairs_legacy_malformed_message_json(
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
        {"event": "user", "turn_id": "turn-a", "text": "repair me"},
    )
    with sqlite3.connect(state.path) as connection:
        connection.execute(
            "UPDATE messages SET content_json = ?",
            ('{"truncated":',),
        )
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = 9",
        )
        connection.execute("PRAGMA user_version = 8")
        connection.commit()

    reopened = StateStore(state.path, default_workspace=tmp_path / "inbox")

    with sqlite3.connect(reopened.path) as connection:
        valid, content_json = connection.execute(
            "SELECT json_valid(content_json), content_json FROM messages",
        ).fetchone()
    assert valid == 1
    assert json.loads(str(content_json))["text"] == "repair me"


def test_recovery_quarantines_invalid_legacy_progress_and_reaches_terminal_event(
    tmp_path: Path,
) -> None:
    state, session = _state(tmp_path)
    files = SessionEventFileStore(tmp_path / "runtime" / "session-events")
    logs = StructuredLogStore(tmp_path / "runtime" / "logs.sqlite")
    service = SessionEventService(SessionEventJournal(
        state=state,
        logs=logs,
        append_record=files.append,
        read_records=files.read,
    ))
    common = {
        "schema_version": 2,
        "project_id": session.project_id,
        "session_id": session.id,
        "session_key": session.session_key,
        "turn_id": "turn-legacy-progress",
    }
    rows = [
        {
            **common,
            "event": "turn_started",
            "event_id": "evt-start",
            "event_seq": 1,
            "recorded_at": 1_000,
            "turn": {"id": "turn-legacy-progress", "started_at": 1_000},
        },
        {
            **common,
            "event": "message",
            "event_id": "evt-invalid-progress",
            "event_seq": 2,
            "recorded_at": 1_100,
            "kind": "progress",
            "text": "between steps",
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": "plan-legacy",
                "execution": "serial",
                "steps": [
                    {"id": "one", "title": "One", "status": "completed"},
                    {"id": "two", "title": "Two", "status": "pending"},
                ],
            },
        },
        {
            **common,
            "event": "message",
            "event_id": "evt-terminal-progress",
            "event_seq": 3,
            "recorded_at": 1_200,
            "kind": "progress",
            "text": "done",
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": "plan-legacy",
                "execution": "serial",
                "steps": [
                    {"id": "one", "title": "One", "status": "completed"},
                    {"id": "two", "title": "Two", "status": "completed"},
                ],
            },
        },
        {
            **common,
            "event": "turn_completed",
            "event_id": "evt-completed",
            "event_seq": 4,
            "recorded_at": 1_300,
            "turn": {
                "id": "turn-legacy-progress",
                "status": "completed",
                "started_at": 1_000,
                "completed_at": 1_300,
            },
        },
    ]
    for row in rows:
        files.append(session.session_key, row)

    assert service.ensure_recovered(session.session_key) == 4

    watermark = state.projector_watermark(session.session_key)
    assert watermark is not None
    assert watermark["last_event_seq"] == 4
    assert watermark["error"] is None
    plan = state.turn_plan_snapshot(
        session_key=session.session_key,
        turn_id="turn-legacy-progress",
    )
    assert plan is not None
    assert plan["status"] == "completed"
    assert state.latest_turn_snapshot(session.session_key)["status"] == "completed"
    assert logs.query(error_code="LEGACY_PROGRESS_QUARANTINED", limit=10)

from pathlib import Path

from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.logs import StructuredLogStore
from nanobot.storage.state import StateStore


def _journal(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state = StateStore(
        tmp_path / "runtime" / "state.sqlite",
        default_workspace=tmp_path / "inbox",
    )
    project_record = state.ensure_project(project)
    session = state.bind_session("websocket:chat-a", project_record.id)
    rows: list[dict] = []
    journal = SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "runtime" / "logs.sqlite"),
        append_record=lambda _key, row: rows.append(dict(row)),
        read_records=lambda _key: list(rows),
    )
    return journal, state, session, rows


def test_journal_appends_envelope_before_projecting(tmp_path: Path) -> None:
    journal, state, session, rows = _journal(tmp_path)

    event = journal.append(
        session.session_key,
        {
            "event": "user",
            "turn_id": "turn-a",
            "text": "hello",
        },
    )

    assert rows == [event]
    assert event["project_id"] == session.project_id
    assert event["session_id"] == session.id
    assert event["event_seq"] == 1
    assert event["event_id"].startswith("evt_")
    assert state.next_event_sequence(session.session_key) == 2
    assert state.projection_counts(session.session_key)["messages"] == 1


def test_journal_preserves_caller_stable_terminal_event_id(tmp_path: Path) -> None:
    journal, _state, session, rows = _journal(tmp_path)

    event = journal.append(
        session.session_key,
        {
            "event": "turn_completed",
            "event_id": "terminal_epoch-a_turn-a",
            "turn_id": "turn-a",
            "turn": {
                "id": "turn-a",
                "runtime_epoch": "epoch-a",
                "status": "completed",
                "started_at": 1_000,
                "completed_at": 1_500,
                "finish_reason": "success",
            },
        },
    )

    assert event["event_id"] == "terminal_epoch-a_turn-a"
    assert rows[0]["event_id"] == "terminal_epoch-a_turn-a"


def test_journal_recovers_legacy_rows_idempotently(tmp_path: Path) -> None:
    journal, state, session, rows = _journal(tmp_path)
    rows.extend([
        {
            "event": "user",
            "turn_id": "turn-a",
            "text": "legacy question",
        },
        {
            "event": "turn_end",
            "turn_id": "turn-a",
        },
    ])

    assert journal.recover_session(session.session_key) == 2
    assert journal.recover_session(session.session_key) == 0
    assert state.projection_counts(session.session_key)["projected_events"] == 2
    assert state.next_event_sequence(session.session_key) == 3


def test_append_lazily_recovers_legacy_rows_before_new_write(tmp_path: Path) -> None:
    journal, state, session, rows = _journal(tmp_path)
    rows.extend([
        {
            "event": "user",
            "turn_id": "turn-a",
            "text": "legacy question",
        },
        {
            "event": "turn_end",
            "turn_id": "turn-a",
        },
    ])

    appended = journal.append(
        session.session_key,
        {
            "event": "user",
            "turn_id": "turn-b",
            "text": "new question",
        },
    )

    assert appended["event_seq"] == 3
    assert state.projection_counts(session.session_key)["projected_events"] == 3
    assert state.next_event_sequence(session.session_key) == 4


def test_restart_recovery_skips_events_at_projector_watermark(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal, state, session, rows = _journal(tmp_path)
    journal.append(
        session.session_key,
        {"event": "user", "turn_id": "turn-a", "text": "question"},
    )
    journal.append(
        session.session_key,
        {"event": "turn_end", "turn_id": "turn-a"},
    )

    restarted = SessionEventJournal(
        state=state,
        logs=StructuredLogStore(tmp_path / "runtime" / "logs.sqlite"),
        append_record=lambda _key, row: rows.append(dict(row)),
        read_records=lambda _key: list(rows),
    )
    original_project_event = state.project_event
    projected_sequences: list[int] = []

    def track_projection(session_key: str, event: dict) -> bool:
        projected_sequences.append(event["event_seq"])
        return original_project_event(session_key, event)

    monkeypatch.setattr(state, "project_event", track_projection)

    assert restarted.ensure_recovered(session.session_key) == 0
    assert projected_sequences == []

    appended = restarted.append(
        session.session_key,
        {"event": "user", "turn_id": "turn-b", "text": "next"},
    )
    assert appended["event_seq"] == 3
    assert projected_sequences == [3]

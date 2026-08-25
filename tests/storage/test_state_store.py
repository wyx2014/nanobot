from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.graph.workflows.asset_research import (
    advance_asset_research_graph,
    new_asset_research_state,
)
from nanobot.storage.state import (
    EventProjectionError,
    SessionProjectMismatch,
    StateStore,
    StateStoreError,
    open_state_store_with_recovery,
)


def _store(tmp_path: Path) -> StateStore:
    return StateStore(
        tmp_path / "runtime" / "state.sqlite",
        default_workspace=tmp_path / "inbox",
    )


def test_schema_initializes_with_wal_and_core_relations(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.quick_check() is True
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "projects",
            "sessions",
            "turns",
            "messages",
            "tool_calls",
            "turn_progress",
            "turn_steps",
            "expert_team_runs",
            "expert_team_checkpoints",
            "artifacts",
            "artifact_links",
            "project_memories",
            "project_memory_stage1",
            "project_memory_jobs",
            "project_memory_sources",
            "project_documents",
            "project_chunks",
            "project_cache",
            "schedules",
            "projector_state",
            "projected_events",
        } <= tables
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_session_display_query_logs_lock_and_sql_timings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.ensure_project(tmp_path / "inbox")
    store.bind_session("websocket:slow-query", project.id)

    with (
        patch("nanobot.storage.state._SLOW_STATE_QUERY_LOG_MS", 0),
        patch("nanobot.storage.state.logger.warning") as log_warning,
    ):
        assert store.session_display_event_envelopes("websocket:slow-query") == []

    args = log_warning.call_args.args
    assert args[0].startswith("slow state query")
    assert args[1] >= 0
    assert args[2] >= 0
    assert args[3] >= 0
    assert args[4] >= 0
    assert args[5] >= 0
    assert args[6] == 0
    assert args[7] == 200


def test_project_and_session_ids_are_stable_and_session_project_is_immutable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_a_path = tmp_path / "project-a"
    project_b_path = tmp_path / "project-b"
    project_a_path.mkdir()
    project_b_path.mkdir()
    project_a = store.ensure_project(project_a_path)
    project_a_again = store.ensure_project(project_a_path)
    project_b = store.ensure_project(project_b_path)

    assert project_a_again.id == project_a.id
    session = store.bind_session("websocket:chat-a", project_a.id)
    assert store.bind_session("websocket:chat-a", project_a.id).id == session.id

    with pytest.raises(SessionProjectMismatch):
        store.bind_session("websocket:chat-a", project_b.id)

    rebound = store.bind_session(
        "websocket:chat-a",
        project_b.id,
        allow_draft_rebind=True,
    )
    assert rebound.id == session.id
    assert rebound.project_id == project_b.id

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE sessions SET project_id = ? WHERE id = ?",
                (project_a.id, rebound.id),
            )


def test_rebuild_preserves_durable_preferred_project_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "migrated-project"
    project_path.mkdir()
    preferred_id = "prj_0123456789abcdef0123456789abcdef"

    project, session = store.ensure_session_for_project(
        "websocket:recovered",
        project_path,
        metadata={
            "project_id": preferred_id,
            "workspace_scope": {"project_path": str(project_path)},
        },
    )

    assert project.id == preferred_id
    assert session.project_id == preferred_id


def test_preferred_project_id_cannot_alias_another_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    preferred_id = "prj_0123456789abcdef0123456789abcdef"
    first = store.ensure_project(first_path, preferred_id=preferred_id)
    second = store.ensure_project(second_path, preferred_id=preferred_id)

    assert first.id == preferred_id
    assert second.id != preferred_id
    assert second.canonical_root_path == str(second_path.resolve())


def test_default_workspace_rename_preserves_inbox_project_and_sessions(
    tmp_path: Path,
) -> None:
    legacy_workspace = tmp_path / "nanobot-workspace"
    legacy_workspace.mkdir()
    store = StateStore(
        legacy_workspace / ".nanobot" / "state.sqlite",
        default_workspace=legacy_workspace,
    )
    legacy_project = store.reconcile_default_workspace_project()
    session = store.bind_session(
        "websocket:existing-chat",
        legacy_project.id,
        title="Existing chat",
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (legacy_workspace / ".nanobot").rename(workspace / ".nanobot")
    legacy_workspace.rmdir()
    reopened = StateStore(
        workspace / ".nanobot" / "state.sqlite",
        default_workspace=workspace,
    )
    project = reopened.reconcile_default_workspace_project()

    assert project.id == legacy_project.id
    assert project.kind == "inbox"
    assert project.name == "workspace"
    assert project.root_path == str(workspace.resolve())
    assert project.canonical_root_path == str(workspace.resolve())
    reopened_session = reopened.get_session(session.session_key)
    assert reopened_session is not None
    assert reopened_session.project_id == project.id


def test_artifacts_are_explicitly_linked_and_project_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_a_path = tmp_path / "project-a"
    project_b_path = tmp_path / "project-b"
    project_a_path.mkdir()
    project_b_path.mkdir()
    project_a = store.ensure_project(project_a_path)
    project_b = store.ensure_project(project_b_path)
    session_a = store.bind_session("websocket:chat-a", project_a.id)
    session_b = store.bind_session("websocket:chat-b", project_b.id)
    report_a = project_a_path / "reports" / "a.pdf"
    report_b = project_b_path / "reports" / "b.pdf"
    report_a.parent.mkdir()
    report_b.parent.mkdir()
    report_a.write_bytes(b"%PDF-A")
    report_b.write_bytes(b"%PDF-B")

    artifact_a = store.register_artifact(
        session_a.session_key,
        report_a,
        relation_type="final",
        artifact_kind="document",
        mime_type="application/pdf",
    )
    artifact_b = store.register_artifact(
        session_b.session_key,
        report_b,
        relation_type="final",
        artifact_kind="document",
        mime_type="application/pdf",
    )

    assert [row.id for row in store.list_session_artifacts(session_a.session_key)] == [
        artifact_a.id
    ]
    assert [row.id for row in store.list_session_artifacts(session_b.session_key)] == [
        artifact_b.id
    ]
    assert store.get_artifact(artifact_b.id, session_key=session_a.session_key) is None
    assert store.resolve_artifact_path(
        artifact_a.id,
        session_key=session_a.session_key,
    )[1] == report_a.resolve()

    with pytest.raises(StateStoreError, match="outside"):
        store.register_artifact(session_a.session_key, report_b)


def test_prune_unverified_referenced_artifacts_repairs_only_legacy_links(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat", project.id)
    explicit = project_path / "explicit.md"
    unrelated = project_path / "unrelated.md"
    generated = project_path / "generated.md"
    explicit.write_text("explicit", encoding="utf-8")
    unrelated.write_text("another conversation", encoding="utf-8")
    generated.write_text("generated here", encoding="utf-8")
    store.register_artifact(session.session_key, explicit, relation_type="referenced")
    store.register_artifact(session.session_key, unrelated, relation_type="referenced")
    store.register_artifact(session.session_key, generated, relation_type="generated")
    revision_before = store.session_artifact_revision(session.session_key)

    pruned = store.prune_unverified_referenced_artifacts(
        session.session_key,
        {"explicit.md"},
    )

    assert pruned == 1
    assert {
        artifact.relative_path
        for artifact in store.list_session_artifacts(session.session_key)
    } == {"explicit.md", "generated.md"}
    assert unrelated.is_file()
    assert store.session_artifact_revision(session.session_key) > revision_before


def test_session_artifacts_can_be_scoped_to_the_latest_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat", project.id)
    for event_seq, turn_id in ((1, "turn-one"), (2, "turn-two")):
        store.project_event(session.session_key, {
            "schema_version": 3,
            "event": "user",
            "event_id": f"evt-{event_seq}",
            "event_seq": event_seq,
            "recorded_at": event_seq * 1_000,
            "project_id": project.id,
            "session_id": session.id,
            "turn_id": turn_id,
            "text": f"task {event_seq}",
        })
    first = project_path / "first.md"
    second = project_path / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    store.register_artifact(
        session.session_key,
        first,
        relation_type="final",
        turn_id="turn-one",
    )
    store.register_artifact(
        session.session_key,
        second,
        relation_type="final",
        turn_id="turn-two",
    )

    assert [
        artifact.relative_path
        for artifact in store.list_session_artifacts(
            session.session_key,
            turn_id="turn-two",
        )
    ] == ["second.md"]
    assert {
        artifact.relative_path
        for artifact in store.list_session_artifacts(session.session_key)
    } == {"first.md", "second.md"}


def test_legacy_reference_turn_can_be_backfilled_without_rehashing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat", project.id)
    store.project_event(session.session_key, {
        "schema_version": 3,
        "event": "user",
        "event_id": "evt-user",
        "event_seq": 1,
        "recorded_at": 1_000,
        "project_id": project.id,
        "session_id": session.id,
        "turn_id": "turn-one",
        "text": "create report",
    })
    report = project_path / "report.md"
    report.write_text("report", encoding="utf-8")
    store.register_artifact(session.session_key, report, relation_type="referenced")

    assert store.list_session_artifacts(session.session_key, turn_id="turn-one") == []
    assert store.assign_referenced_artifact_turn(
        session.session_key,
        "report.md",
        "turn-one",
    ) == 1
    assert [
        artifact.relative_path
        for artifact in store.list_session_artifacts(
            session.session_key,
            turn_id="turn-one",
        )
    ] == ["report.md"]


def test_composite_foreign_keys_reject_cross_project_artifact_links(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_a_path = tmp_path / "project-a"
    project_b_path = tmp_path / "project-b"
    project_a_path.mkdir()
    project_b_path.mkdir()
    project_a = store.ensure_project(project_a_path)
    project_b = store.ensure_project(project_b_path)
    session_a = store.bind_session("websocket:chat-a", project_a.id)
    session_b = store.bind_session("websocket:chat-b", project_b.id)
    report = project_a_path / "a.pdf"
    report.write_bytes(b"%PDF")
    artifact = store.register_artifact(session_a.session_key, report)

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifact_links(
                    id, project_id, artifact_id, session_id,
                    relation_type, created_at
                ) VALUES (?, ?, ?, ?, 'referenced', 1)
                """,
                ("invalid-link", project_b.id, artifact.id, session_b.id),
            )


def test_artifact_status_reconciles_when_file_is_removed_and_restored(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-a", project.id)
    report = project_path / "report.pdf"
    report.write_bytes(b"%PDF")
    artifact = store.register_artifact(session.session_key, report)

    report.unlink()
    [missing] = store.list_session_artifacts(session.session_key)
    assert missing.id == artifact.id
    assert missing.status == "missing"
    with pytest.raises(StateStoreError, match="missing"):
        store.resolve_artifact_path(artifact.id, session_key=session.session_key)

    report.write_bytes(b"%PDF")
    [ready] = store.list_session_artifacts(session.session_key)
    assert ready.status == "ready"


def test_session_artifact_list_returns_only_latest_revision_per_path(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-a", project.id)
    report = project_path / "report.md"
    report.write_text("first", encoding="utf-8")
    first = store.register_artifact(session.session_key, report)
    time.sleep(0.002)
    report.write_text("second", encoding="utf-8")
    second = store.register_artifact(session.session_key, report)

    assert first.id != second.id
    assert [row.id for row in store.list_session_artifacts(session.session_key)] == [
        second.id
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE relative_path = 'report.md'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT supersedes_artifact_id FROM artifacts WHERE id = ?",
            (second.id,),
        ).fetchone()[0] == first.id


def test_failed_edit_does_not_hide_ready_artifact_at_same_path(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-a", project.id)
    report = project_path / "report.md"
    report.write_text("published", encoding="utf-8")
    ready = store.register_artifact(session.session_key, report)

    failed_stage = store.stage_artifact(
        session.session_key,
        report,
        tool_call_id="invalid-edit",
    )
    failed = store.fail_artifact(
        failed_stage.id,
        session_key=session.session_key,
        error_code="ARTIFACT_TIMEOUT",
        error_message="edit never reached a terminal file event",
    )
    assert failed.updated_at >= ready.updated_at

    [current] = store.list_session_artifacts(session.session_key)
    assert current.id == ready.id
    assert current.status == "ready"


def test_event_projection_tracks_turn_messages_tools_and_terminal_progress(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-a", project.id)
    common = {
        "schema_version": 1,
        "project_id": project.id,
        "session_id": session.id,
        "recorded_at": 1_000,
        "chat_id": "chat-a",
        "turn_id": "turn-a",
    }
    events = [
        {
            **common,
            "event": "user",
            "event_id": "evt-user",
            "event_seq": 1,
            "text": "create a report",
        },
        {
            **common,
            "event": "message",
            "event_id": "evt-progress",
            "event_seq": 2,
            "kind": "progress",
            "text": "",
            "tool_events": [{
                "phase": "start",
                "call_id": "call-pdf",
                "name": "convert_to_pdf",
                "arguments": {
                    "path": "report.md",
                    "api_key": "must-not-persist",
                },
            }],
            "agent_ui": {
                "kind": "task_progress",
                "current_step_id": "pdf",
                "steps": [{
                    "id": "pdf",
                    "title": "Convert PDF",
                    "status": "running",
                }],
            },
        },
        {
            **common,
            "event": "turn_end",
            "event_id": "evt-end",
            "event_seq": 3,
        },
    ]

    assert all(store.project_event(session.session_key, event) for event in events)
    assert store.project_event(session.session_key, events[-1]) is False
    report = project_path / "report.pdf"
    report.write_bytes(b"%PDF")
    artifact = store.register_artifact(
        session.session_key,
        report,
        relation_type="final",
        turn_id="turn-a",
        tool_call_id="call-pdf",
    )
    assert store.projection_counts(session.session_key) == {
        "projected_events": 3,
        "turns": 1,
        "messages": 2,
        "tool_calls": 1,
        "turn_steps": 1,
    }
    with sqlite3.connect(store.path) as connection:
        turn = connection.execute(
            "SELECT status FROM turns WHERE id = 'turn-a'"
        ).fetchone()
        tool = connection.execute(
            "SELECT status, input_json FROM tool_calls"
        ).fetchone()
        step = connection.execute(
            "SELECT status FROM turn_steps"
        ).fetchone()
        artifact_owner = connection.execute(
            """
            SELECT created_by_turn_id, created_by_tool_call_id
            FROM artifacts WHERE id = ?
            """,
            (artifact.id,),
        ).fetchone()
        link_owner = connection.execute(
            """
            SELECT turn_id, tool_call_id
            FROM artifact_links WHERE artifact_id = ?
            """,
            (artifact.id,),
        ).fetchone()
    assert turn == ("completed",)
    assert tool[0] == "failed"
    assert "must-not-persist" not in tool[1]
    assert "[REDACTED]" in tool[1]
    assert step == ("completed",)
    assert artifact_owner[0] == "turn-a"
    assert artifact_owner[1] == link_owner[1]
    assert link_owner[0] == "turn-a"
    plan = store.turn_plan_snapshot(
        session_key=session.session_key,
        turn_id="turn-a",
    )
    assert plan is not None
    assert plan["status"] == "completed"
    assert plan["revision"] == 2
    assert plan["current_step_id"] is None
    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "evt-late-progress",
        "event_seq": 4,
        "kind": "progress",
        "text": "",
        "agent_ui": {
            "kind": "task_progress",
            "revision": 99,
            "current_step_id": "pdf",
            "steps": [{
                "id": "pdf",
                "title": "Convert PDF",
                "status": "running",
            }],
        },
    })
    after_late = store.turn_plan_snapshot(
        session_key=session.session_key,
        turn_id="turn-a",
    )
    assert after_late is not None
    assert after_late["status"] == "completed"
    assert after_late["revision"] == 2


def test_expert_team_workflow_replaces_model_plan_as_canonical_plan(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-a", project.id)
    common = {
        "schema_version": 1,
        "project_id": project.id,
        "session_id": session.id,
        "recorded_at": 1_000,
        "chat_id": "chat-a",
        "turn_id": "turn-a",
    }
    assert store.project_event(session.session_key, {
        **common,
        "event": "user",
        "event_id": "evt-user",
        "event_seq": 1,
        "text": "research a company",
    })
    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "evt-plan",
        "event_seq": 2,
        "kind": "progress",
        "text": "",
        "agent_ui": {
            "kind": "task_progress",
            "current_step_id": "research",
            "steps": [{
                "id": "research",
                "title": "Research",
                "status": "running",
            }],
        },
    })
    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "evt-team",
        "event_seq": 3,
        "kind": "progress",
        "text": "financial analyst completed",
        "agent_ui": {
            "kind": "task_progress",
            "plan_kind": "workflow",
            "execution": "staged",
            "team_id": "asset-research-team",
            "team_run_id": "run-1",
            "steps": [{
                "id": "financial-analyst",
                "title": "Financial analyst",
                "status": "completed",
            }],
        },
    })

    with sqlite3.connect(store.path) as connection:
        messages = connection.execute(
            "SELECT turn_id FROM messages ORDER BY sequence_no"
        ).fetchall()
        steps = connection.execute(
            "SELECT step_key, title, status FROM turn_steps"
        ).fetchall()
        progress = connection.execute(
            "SELECT kind, owner, execution FROM turn_progress"
        ).fetchone()
        team_run = connection.execute(
            "SELECT id, team_id, status FROM expert_team_runs"
        ).fetchone()
    assert messages == [("turn-a",), ("turn-a",), ("turn-a",)]
    assert steps == [("financial-analyst", "Financial analyst", "completed")]
    assert progress == ("workflow", "expert_team:asset-research-team", "staged")
    assert team_run == ("run-1", "asset-research-team", "completed")


def test_workflow_plan_allows_parallel_members_and_terminal_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-workflow"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:workflow", project.id)
    common = {
        "schema_version": 3,
        "project_id": project.id,
        "session_id": session.id,
        "recorded_at": 1_000,
        "chat_id": "workflow",
        "turn_id": "turn-workflow",
    }
    assert store.project_event(session.session_key, {
        **common,
        "event": "user",
        "event_id": "workflow-user",
        "event_seq": 1,
        "text": "research",
    })
    steps = [
        {"id": "business", "title": "Business", "status": "running"},
        {"id": "finance", "title": "Finance", "status": "running"},
        {"id": "team-lead", "title": "Synthesis", "status": "pending"},
        {"id": "report-audit", "title": "Audit", "status": "pending"},
    ]
    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "workflow-plan-1",
        "event_seq": 2,
        "kind": "progress",
        "agent_ui": {
            "kind": "task_progress",
            "plan_kind": "workflow",
            "execution": "staged",
            "revision": 1,
            "active_step_ids": ["business", "finance"],
            "team_id": "asset-research-team",
            "team_run_id": "run-workflow",
            "steps": steps,
        },
    })
    first = store.turn_plan_snapshot(
        session_key=session.session_key,
        turn_id="turn-workflow",
    )
    assert first is not None
    assert first["kind"] == "workflow"
    assert first["active_step_ids"] == ["business", "finance"]

    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "workflow-plan-2",
        "event_seq": 3,
        "kind": "progress",
        "agent_ui": {
            "kind": "task_progress",
            "plan_kind": "workflow",
            "execution": "staged",
            "revision": 2,
            "active_step_ids": ["team-lead"],
            "team_id": "asset-research-team",
            "team_run_id": "run-workflow",
            "steps": [
                {**steps[0], "status": "completed"},
                {**steps[1], "status": "completed"},
                {**steps[2], "status": "running"},
                steps[3],
            ],
        },
    })
    second = store.turn_plan_snapshot(
        session_key=session.session_key,
        turn_id="turn-workflow",
    )
    assert second is not None
    assert second["revision"] == 2
    assert second["active_step_ids"] == ["team-lead"]


def test_expert_team_graph_checkpoint_can_resume_from_same_session_artifacts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-resume"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:resume", project.id)
    common = {
        "schema_version": 3,
        "project_id": project.id,
        "session_id": session.id,
        "recorded_at": 1_000,
        "chat_id": "resume",
        "turn_id": "turn-resume",
    }
    assert store.project_event(session.session_key, {
        **common,
        "event": "user",
        "event_id": "resume-user",
        "event_seq": 1,
        "text": "research",
    })

    member_ids = (
        "business-analyst",
        "financial-analyst",
        "industry-researcher",
        "risk-assessor",
    )
    graph = new_asset_research_state(run_id="run-resume", member_ids=member_ids)
    graph = advance_asset_research_graph(graph, "data_package_ready")
    for member_id in member_ids:
        relative = f"reports/.team-runs/run-resume/members/{member_id}.md"
        path = project_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {member_id}\n", encoding="utf-8")
        store.register_artifact(
            session.session_key,
            path,
            relation_type="intermediate",
            artifact_kind="document",
            mime_type="text/markdown",
            turn_id="turn-resume",
        )
        graph = advance_asset_research_graph(
            graph,
            "member_updated",
            {
                "id": member_id,
                "status": "failed" if member_id == "risk-assessor" else "completed",
                "artifact": relative,
            },
        )
    graph = advance_asset_research_graph(
        graph,
        "report_written",
        {"artifact": "reports/resume-report.html"},
    )
    graph = advance_asset_research_graph(graph, "audit_completed")

    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "resume-workflow",
        "event_seq": 2,
        "kind": "progress",
        "agent_ui": {
            "kind": "task_progress",
            "plan_kind": "workflow",
            "execution": "staged",
            "revision": graph["checkpoint_revision"],
            "team_id": "asset-research-team",
            "team_run_id": "run-resume",
            "stage_key": graph["node"],
            "graph_event": "audit_completed",
            "graph_state": graph,
            "steps": [
                {"id": "data-package", "title": "Data", "status": "completed"},
                *[
                    {"id": item, "title": item, "status": "completed"}
                    for item in member_ids
                ],
                {"id": "team-lead", "title": "Lead", "status": "completed"},
                {"id": "report-audit", "title": "Audit", "status": "completed"},
            ],
        },
    })

    resume = store.latest_expert_team_resume(
        session_key=session.session_key,
        team_id="asset-research-team",
    )
    assert resume is not None
    assert resume["run_id"] == "run-resume"
    assert resume["graph_state"]["degraded"] is True
    assert any(path.endswith("risk-assessor.md") for path in resume["artifacts"])
    other_session = store.bind_session("websocket:other", project.id)
    assert store.latest_expert_team_resume(
        session_key=other_session.session_key,
        team_id="asset-research-team",
    ) is None
    with sqlite3.connect(store.path) as connection:
        checkpoint = connection.execute(
            "SELECT node_key, event_type, revision FROM expert_team_checkpoints"
        ).fetchone()
    assert checkpoint == (
        "delivered",
        "audit_completed",
        graph["checkpoint_revision"],
    )


def test_v2_lifecycle_projection_has_one_stable_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project-v2"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat-v2", project.id)
    common = {
        "schema_version": 2,
        "project_id": project.id,
        "session_id": session.id,
        "chat_id": "chat-v2",
        "turn_id": "turn-v2",
    }
    started = {
        **common,
        "event": "turn_started",
        "event_id": "evt-start-v2",
        "event_seq": 1,
        "recorded_at": 1_000,
        "turn": {
            "id": "turn-v2",
            "runtime_epoch": "epoch-a",
            "status": "inProgress",
            "started_at": 900,
        },
    }
    completed = {
        **common,
        "event": "turn_completed",
        "event_id": "terminal_epoch-a_turn-v2",
        "event_seq": 2,
        "recorded_at": 1_600,
        "turn": {
            "id": "turn-v2",
            "runtime_epoch": "epoch-a",
            "status": "interrupted",
            "started_at": 900,
            "completed_at": 1_500,
            "finish_reason": "userInterrupted",
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 34,
                "total_tokens": 1234,
            },
        },
    }

    assert store.project_event(session.session_key, started) is True
    assert store.active_turn_id(session.session_key) == "turn-v2"
    assert store.project_event(session.session_key, completed) is True
    assert store.project_event(session.session_key, completed) is False
    assert store.project_event(
        session.session_key,
        {
            **common,
            "event": "turn_end",
            "event_id": "evt-legacy-end",
            "event_seq": 3,
            "recorded_at": 1_700,
        },
    ) is True
    assert store.active_turn_id(session.session_key) is None
    with sqlite3.connect(store.path) as connection:
        usage_json = connection.execute(
            "SELECT usage_json FROM turns WHERE id = ?",
            ("turn-v2",),
        ).fetchone()[0]
    assert json.loads(usage_json) == {
        "prompt_tokens": 1200,
        "completion_tokens": 34,
        "total_tokens": 1234,
    }
    assert store.latest_turn_snapshot(session.session_key) == {
        "id": "turn-v2",
        "trace_id": None,
        "runtime_epoch": "epoch-a",
        "project_id": project.id,
        "session_id": session.id,
        "status": "interrupted",
        "started_at": 900,
        "completed_at": 1_500,
        "duration_ms": 600,
        "finish_reason": "userInterrupted",
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 34,
            "total_tokens": 1234,
        },
    }
    with pytest.raises(EventProjectionError, match="another terminal event"):
        store.project_event(
            session.session_key,
            {
                **completed,
                "event_id": "terminal_epoch-b_turn-v2",
                "event_seq": 4,
                "turn": {
                    **completed["turn"],
                    "runtime_epoch": "epoch-b",
                    "status": "completed",
                    "finish_reason": "success",
                },
            },
        )


def test_artifact_staging_reaches_ready_or_failed_terminal_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:chat", project.id)
    turn_event = {
        "schema_version": 1,
        "event": "user",
        "event_id": "evt-user",
        "event_seq": 1,
        "recorded_at": 1,
        "project_id": project.id,
        "session_id": session.id,
        "turn_id": "turn-pdf",
        "text": "pdf",
    }
    store.project_event(session.session_key, turn_event)

    staged = store.stage_artifact(
        session.session_key,
        project_path / "report.pdf",
        relation_type="final",
        turn_id="turn-pdf",
    )
    assert staged.status == "staging"
    report = project_path / "report.pdf"
    report.write_bytes(b"%PDF")
    ready = store.register_artifact(
        session.session_key,
        report,
        relation_type="final",
        turn_id="turn-pdf",
    )
    assert ready.id == staged.id
    assert ready.status == "ready"

    failed_stage = store.stage_artifact(
        session.session_key,
        project_path / "broken.pdf",
        turn_id="turn-pdf",
    )
    failed = store.fail_artifact(
        failed_stage.id,
        session_key=session.session_key,
        error_code="PDF_RENDER_FAILED",
        error_message="renderer exited",
    )
    assert failed.status == "failed"
    restarted = store.stage_artifact(
        session.session_key,
        project_path / "broken.pdf",
        turn_id="turn-pdf",
    )
    assert restarted.id == failed.id
    assert restarted.status == "staging"
    failed = store.fail_artifact(
        restarted.id,
        session_key=session.session_key,
        error_code="PDF_RENDER_FAILED",
        error_message="renderer exited again",
    )
    statuses = {
        artifact.relative_path: artifact.status
        for artifact in store.list_session_artifacts(session.session_key)
    }
    assert statuses == {
        "report.pdf": "ready",
        "broken.pdf": "failed",
    }


def test_project_archive_restore_relocate_export_excludes_legacy_memory(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    original = tmp_path / "project"
    original.mkdir()
    project = store.ensure_project(original, name="Example")
    session = store.bind_session("websocket:chat", project.id, title="Analysis")
    store.upsert_project_memory(
        project.id,
        kind="long_term",
        content="Only visible in Example",
        source_session_id=session.id,
    )

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    moved = store.relocate_project(project.id, relocated)
    assert moved.id == project.id
    assert moved.root_path == str(relocated.resolve())
    manifest = store.project_export_manifest(project.id)
    assert manifest["project"]["id"] == project.id
    assert manifest["sessions"][0]["id"] == session.id
    assert "memories" not in manifest

    archived = store.archive_project(project.id)
    assert archived.status == "archived"
    assert store.list_projects() == []
    assert store.list_projects(include_archived=True)[0].id == project.id
    restored = store.restore_project(project.id)
    assert restored.status == "active"
    assert store.list_project_sessions(project.id)[0].status == "active"


def test_readding_archived_or_reused_relocation_path_keeps_identities_safe(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    original = tmp_path / "original"
    destination = tmp_path / "destination"
    original.mkdir()
    destination.mkdir()
    project = store.ensure_project(original)
    store.archive_project(project.id)
    restored = store.ensure_project(original)
    assert restored.id == project.id
    assert restored.status == "active"

    moved = store.relocate_project(project.id, destination)
    replacement = store.ensure_project(original)
    assert moved.id == project.id
    assert replacement.id != moved.id
    assert store.ensure_project(original).id == replacement.id


def test_corrupt_state_database_is_backed_up_and_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "state.sqlite"
    path.parent.mkdir()
    path.write_bytes(b"not-a-sqlite-database")

    recovery = open_state_store_with_recovery(
        path,
        default_workspace=tmp_path / "inbox",
    )

    assert recovery.store.quick_check() is True
    assert recovery.backup_dir is not None
    assert (recovery.backup_dir / "state.sqlite").read_bytes() == b"not-a-sqlite-database"


def test_latency_sensitive_open_can_skip_full_integrity_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "state.sqlite"
    original_quick_check = StateStore.quick_check
    calls = 0

    def counted_quick_check(store: StateStore) -> bool:
        nonlocal calls
        calls += 1
        return original_quick_check(store)

    monkeypatch.setattr(StateStore, "quick_check", counted_quick_check)

    recovery = open_state_store_with_recovery(
        path,
        default_workspace=tmp_path / "inbox",
        verify_integrity=False,
    )

    assert recovery.backup_dir is None
    assert calls == 0
    assert recovery.store.list_projects() == []


def test_skipping_full_scan_still_recovers_database_that_cannot_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "state.sqlite"
    path.parent.mkdir()
    path.write_bytes(b"not-a-sqlite-database")

    recovery = open_state_store_with_recovery(
        path,
        default_workspace=tmp_path / "inbox",
        verify_integrity=False,
    )

    assert recovery.backup_dir is not None
    assert recovery.store.quick_check() is True


def test_event_projection_rejects_sequence_gaps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:gap", project.id)

    with pytest.raises(StateStoreError, match="sequence gap"):
        store.project_event(
            session.session_key,
            {
                "schema_version": 1,
                "event": "user",
                "event_id": "evt-gap",
                "event_seq": 2,
                "recorded_at": 1,
                "project_id": project.id,
                "session_id": session.id,
                "text": "missing event one",
            },
        )


def test_permanent_session_and_project_purge_leave_foreign_keys_consistent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project_path = tmp_path / "purge-project"
    project_path.mkdir()
    project = store.ensure_project(project_path)
    session = store.bind_session("websocket:purge-me", project.id, title="Purge me")
    common = {
        "schema_version": 3,
        "project_id": project.id,
        "session_id": session.id,
        "session_key": session.session_key,
        "turn_id": "turn-purge",
    }
    assert store.project_event(session.session_key, {
        **common,
        "event": "turn_started",
        "event_id": "purge-start",
        "event_seq": 1,
        "recorded_at": 1_000,
        "turn": {"id": "turn-purge", "started_at": 1_000},
    })
    assert store.project_event(session.session_key, {
        **common,
        "event": "message",
        "event_id": "purge-progress",
        "event_seq": 2,
        "recorded_at": 1_100,
        "kind": "progress",
        "text": "working",
        "tool_events": [{
            "id": "tool-purge",
            "name": "read_file",
            "status": "succeeded",
            "result": "ok",
        }],
        "agent_ui": {
            "kind": "task_progress",
            "plan_id": "plan-purge",
            "steps": [{"id": "one", "title": "One", "status": "completed"}],
        },
    })
    assert store.project_event(session.session_key, {
        **common,
        "event": "turn_completed",
        "event_id": "purge-completed",
        "event_seq": 3,
        "recorded_at": 1_200,
        "turn": {
            "id": "turn-purge",
            "status": "completed",
            "started_at": 1_000,
            "completed_at": 1_200,
        },
    })

    store.archive_session(session.session_key)
    result = store.purge_session(session.session_key)

    assert result["purged"] is True
    assert store.get_session(session.session_key) is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    store.archive_project(project.id)
    project_result = store.purge_project(project.id)
    assert project_result["purged"] is True
    assert store.get_project(project.id) is None
    assert project_path.is_dir()


def test_project_rag_and_cache_never_cross_project_boundaries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    project_a = store.ensure_project(root_a)
    project_b = store.ensure_project(root_b)
    store.replace_project_document(
        project_a.id,
        relative_path="notes/customer.txt",
        chunks=["shared-query private-A"],
    )
    store.replace_project_document(
        project_b.id,
        relative_path="notes/customer.txt",
        chunks=["shared-query private-B"],
    )
    store.put_project_cache(project_a.id, "rag", "shared-query", {"answer": "A"})
    store.put_project_cache(project_b.id, "rag", "shared-query", {"answer": "B"})

    assert store.search_project_chunks(project_a.id, "shared-query")[0]["text"].endswith(
        "private-A"
    )
    assert store.search_project_chunks(project_b.id, "shared-query")[0]["text"].endswith(
        "private-B"
    )
    assert store.get_project_cache(project_a.id, "rag", "shared-query") == {"answer": "A"}
    assert store.get_project_cache(project_b.id, "rag", "shared-query") == {"answer": "B"}


def test_project_memory_stage1_jobs_consolidation_and_usage_are_project_scoped(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    root_a = tmp_path / "memory-a"
    root_b = tmp_path / "memory-b"
    root_a.mkdir()
    root_b.mkdir()
    project_a = store.ensure_project(root_a)
    project_b = store.ensure_project(root_b)
    session_a = store.bind_session("websocket:memory-a", project_a.id)
    session_b = store.bind_session("websocket:memory-b", project_b.id)

    stage1_a = store.upsert_project_memory_stage1(
        project_a.id,
        source_session_id=session_a.id,
        source_rollout_revision="rev-a",
        raw_memory="Customer A requires citations.",
        rollout_summary="A preference was confirmed.",
        rollout_slug="customer-a",
    )
    stage1_b = store.upsert_project_memory_stage1(
        project_b.id,
        source_session_id=session_b.id,
        source_rollout_revision="rev-b",
        raw_memory="Customer B requires tables.",
        rollout_summary="B preference was confirmed.",
    )
    assert stage1_a != stage1_b
    assert store.get_project_memory_stage1(
        project_a.id,
        source_session_id=session_a.id,
        source_rollout_revision="rev-a",
    )["raw_memory"].endswith("citations.")

    owner = "worker-a"
    claimed = store.claim_project_memory_job(
        project_a.id,
        phase="phase2",
        job_key="consolidate",
        lease_owner=owner,
        input_watermark=store.project_memory_input_watermark(project_a.id),
    )
    assert claimed is not None
    assert (
        store.claim_project_memory_job(
            project_a.id,
            phase="phase2",
            job_key="consolidate",
            lease_owner="worker-b",
            input_watermark=store.project_memory_input_watermark(project_a.id),
        )
        is None
    )

    memory_ids = store.replace_consolidated_project_memories(
        project_a.id,
        [
            {
                "key": "citations",
                "kind": "project_preference",
                "title": "Citation requirement",
                "content": "Always include source citations.",
                "confidence": 0.9,
                "stage1_ids": [stage1_a],
            }
        ],
        selected_stage1_ids=[stage1_a],
    )
    assert len(memory_ids) == 1
    memories_a = store.list_project_memories(project_a.id)
    assert memories_a[0]["source_count"] == 1
    assert memories_a[0]["content"] == "Always include source citations."
    assert store.list_project_memories(project_b.id) == []
    with pytest.raises(StateStoreError, match="does not belong"):
        store.replace_consolidated_project_memories(
            project_a.id,
            [
                {
                    "kind": "reference",
                    "content": "must reject cross-project source",
                    "stage1_ids": [stage1_b],
                }
            ],
            selected_stage1_ids=[stage1_a],
        )
    assert store.record_project_memory_usage(project_a.id, memory_ids) == 1
    assert store.list_project_memories(project_a.id)[0]["usage_count"] == 1

    watermark = store.project_memory_input_watermark(project_a.id)
    assert store.finish_project_memory_job(
        project_a.id,
        phase="phase2",
        job_key="consolidate",
        lease_owner=owner,
        status="succeeded",
        completed_watermark=watermark,
    )
    status = store.project_memory_job_status(project_a.id)
    assert status["phase2"]["status"] == "succeeded"
    assert status["phase2"]["completed_watermark"] == watermark
    assert (
        store.claim_project_memory_job(
            project_a.id,
            phase="phase2",
            job_key="consolidate",
            lease_owner="worker-c",
            input_watermark=watermark,
        )
        is None
    )


def test_project_memory_forget_does_not_delete_session_journal_registration(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    root = tmp_path / "forget"
    root.mkdir()
    project = store.ensure_project(root)
    session = store.bind_session("websocket:forget", project.id)
    memory_id = store.upsert_project_memory(
        project.id,
        kind="workflow",
        title="Build",
        content="Run focused tests.",
        source_session_id=session.id,
    )

    assert store.delete_project_memory(project.id, memory_id) is True
    assert store.list_project_memories(project.id) == []
    assert store.get_session(session.session_key) is not None
    assert store.clear_project_memories(project.id) == 0
    assert store.get_session(session.session_key) is not None


def test_project_schedule_relationship_is_mirrored_in_sqlite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    project = store.ensure_project(root)
    session = store.bind_session("websocket:owner", project.id)
    job = CronJob(
        id="job-a",
        name="Daily",
        enabled=True,
        schedule=CronSchedule(kind="cron", expr="0 9 * * *"),
        payload=CronPayload(
            message="run report",
            project_id=project.id,
            created_session_id=session.id,
        ),
        created_at_ms=1,
        updated_at_ms=2,
    )

    assert store.sync_project_schedules([job]) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT project_id, created_session_id, status FROM schedules WHERE id = 'job-a'"
        ).fetchone() == (project.id, session.id, "active")
    job.enabled = False
    store.sync_project_schedules([job])
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM schedules WHERE id = 'job-a'"
        ).fetchone() == ("paused",)

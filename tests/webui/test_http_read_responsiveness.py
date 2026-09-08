from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.channels.websocket import WebSocketConfig
from nanobot.session.manager import Session, SessionManager
from nanobot.storage.state import StateStoreError
from nanobot.webui import ws_http
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import write_session_messages_as_transcript

SESSION_KEY = "websocket:read-performance"
SESSION_URL = "/api/sessions/websocket%3Aread-performance"


@pytest.fixture
def http_handler(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_config_path", lambda: tmp_path / "config.json")
    manager = SessionManager(tmp_path)
    session = Session(key=SESSION_KEY)
    session.add_message("user", "Create a report")
    session.add_message("assistant", "Report ready")
    manager.save(session)
    write_session_messages_as_transcript(SESSION_KEY, session.messages)
    gateway = build_gateway_services(
        config=WebSocketConfig(enabled=True),
        bus=MagicMock(),
        session_manager=manager,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=lambda: "test-model",
        runtime_surface="native",
        runtime_capabilities_overrides=None,
    )
    handler = gateway.http
    data = manager.read_session_file(SESSION_KEY)
    scope, state_session = handler._bind_session_read(SESSION_KEY, data)
    return handler, data, scope, state_session


def _authorized_request(handler, path):
    token = handler.tokens.issue_token(handler.config.token_ttl_s, api_token=True)
    return Request(path, Headers({"Authorization": f"Bearer {token}"}))


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "owner", "operation"), [
    ("thread", "session_manager", "read_session_file"),
    ("thread", "handler", "_ensure_state_session"),
    ("thread", "state", "projector_watermark"),
    ("thread", "module", "build_webui_thread_response"),
    ("thread", "state", "latest_turn_snapshot"),
    ("thread", "state", "session_artifact_revision"),
    ("runtime-snapshot", "session_manager", "read_session_metadata"),
    ("runtime-snapshot", "state", "latest_turn_snapshot"),
    ("runtime-diagnostics", "state", "projection_counts"),
    ("artifacts", "session_manager", "read_session_metadata"),
    ("projects", "handler", "_project_lifecycle_entry"),
    ("messages", "session_manager", "read_session_file"),
    ("webui-thread", "session_manager", "read_session_file"),
])
async def test_slow_read_keeps_other_requests_responsive(
    http_handler, monkeypatch, route, owner, operation,
):
    handler, _data, _scope, _state_session = http_handler
    owner_object = {
        "handler": handler,
        "module": ws_http,
        "state": handler.state,
        "session_manager": handler.session_manager,
    }[owner]
    original = getattr(owner_object, operation)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    calls = []

    def slow_read(*args, **kwargs):
        calls.append(threading.get_ident())
        assert threading.get_ident() != loop_thread, "disk read blocked the event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5), "test did not release slow read"
        return original(*args, **kwargs)

    monkeypatch.setattr(owner_object, operation, slow_read)
    path = "/api/projects" if route == "projects" else f"{SESSION_URL}/{route}"
    connection = MagicMock(remote_address=("127.0.0.1", 12345))
    pending = asyncio.create_task(handler.dispatch(connection, _authorized_request(handler, path)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        bootstrap = await asyncio.wait_for(
            handler.dispatch(connection, Request("/webui/bootstrap", Headers())), timeout=1,
        )
        assert bootstrap.status_code == 200
        assert not pending.done()
    finally:
        release.set()
        response = await pending
    assert response.status_code == 200
    assert calls


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["runtime-snapshot", "runtime-diagnostics", "artifacts"])
async def test_status_and_indexed_files_do_not_load_full_history(http_handler, monkeypatch, route):
    handler, _data, _scope, _session = http_handler
    handler.state.mark_artifact_indexed(SESSION_KEY)
    full_read = MagicMock(side_effect=AssertionError("full history read is unnecessary"))
    monkeypatch.setattr(handler.session_manager, "read_session_file", full_read)
    response = await handler.dispatch(
        MagicMock(), _authorized_request(handler, f"{SESSION_URL}/{route}"),
    )
    assert response.status_code == 200
    full_read.assert_not_called()


@pytest.mark.asyncio
async def test_current_artifacts_skip_legacy_scan_but_new_legacy_links_are_repaired(
    http_handler, monkeypatch, tmp_path,
):
    handler, data, scope, _state_session = http_handler
    discover = MagicMock(wraps=ws_http.discover_session_artifacts)
    monkeypatch.setattr(ws_http, "discover_session_artifacts", discover)
    first = await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)
    assert first["migration_failures"] == 0
    assert discover.call_count == 1

    generated = tmp_path / "generated.md"
    generated.write_text("generated in this session", encoding="utf-8")
    handler.state.register_artifact(SESSION_KEY, generated, relation_type="generated")
    for _ in range(3):
        await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)
    assert discover.call_count == 1

    unrelated = tmp_path / "other-session.md"
    unrelated.write_text("belongs to another session", encoding="utf-8")
    handler.state.register_artifact(SESSION_KEY, unrelated, relation_type="referenced")
    repaired = await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)
    assert repaired["pruned_count"] == 1
    assert discover.call_count == 2
    assert [row.relative_path for row in handler.state.list_session_artifacts(SESSION_KEY)] == [
        "generated.md",
    ]
    assert unrelated.is_file()


@pytest.mark.asyncio
async def test_parallel_readers_share_migration_when_one_reader_disconnects(
    http_handler, monkeypatch,
):
    handler, data, scope, _state_session = http_handler
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = handler._index_session_artifacts

    def slow_index(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        return original(*args)

    index = MagicMock(side_effect=slow_index)
    monkeypatch.setattr(handler, "_index_session_artifacts", index)

    async def read():
        return await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)

    first = asyncio.create_task(read())
    readers = []
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        readers = [asyncio.create_task(read()) for _ in range(4)]
        await asyncio.sleep(0)
        assert index.call_count == 1
    finally:
        release.set()
        await asyncio.gather(first, *readers, return_exceptions=True)
        await asyncio.gather(*handler._artifact_index_tasks.values())
    assert index.call_count == 1
    assert all(reader.result()["migration_failures"] == 0 for reader in readers)
    assert handler._artifact_index_tasks == {}


@pytest.mark.asyncio
async def test_failed_legacy_registration_can_retry_on_next_read(http_handler, monkeypatch, tmp_path):
    handler, data, scope, _state_session = http_handler
    report = tmp_path / "legacy.md"
    report.write_text("legacy report", encoding="utf-8")
    data["messages"].append({
        "role": "tool", "content": json.dumps({"files": [{"path": str(report)}]}),
    })
    original = handler.state.register_artifact
    monkeypatch.setattr(handler.state, "register_artifact", MagicMock(
        side_effect=StateStoreError("temporarily unavailable"),
    ))
    failed = await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)
    assert failed["migration_failures"] == 1
    assert handler.state.get_session(SESSION_KEY).artifact_indexed_at is None
    monkeypatch.setattr(handler.state, "register_artifact", original)
    recovered = await handler._ensure_session_artifact_index(SESSION_KEY, data, scope)
    assert recovered["migrated_count"] == 1
    assert handler.state.get_session(SESSION_KEY).artifact_indexed_at is not None


def test_batch_reference_repair_checks_turn_ownership(http_handler, tmp_path: Path):
    handler, _data, _scope, session = http_handler
    state = handler.state
    other = state.bind_session("websocket:other", session.project_id)
    for owner, turn in ((session, "our-turn"), (other, "other-turn")):
        state.project_event(owner.session_key, {
            "schema_version": 3, "event": "user", "event_id": f"event-{turn}",
            "event_seq": 1, "recorded_at": 1000, "project_id": owner.project_id,
            "session_id": owner.id, "turn_id": turn, "text": "create report",
        })
    for name in ("good.md", "bad-turn.md", "unrelated.md"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        state.register_artifact(SESSION_KEY, path, relation_type="referenced")
    revision = state.session_artifact_revision(SESSION_KEY)
    assert state.has_unscoped_artifact_references(SESSION_KEY)
    assert state.prune_unverified_referenced_artifacts(
        SESSION_KEY, {"good.md", "bad-turn.md"},
        turn_by_path={"good.md": "our-turn", "bad-turn.md": "other-turn"},
    ) == 1
    assert [row.relative_path for row in state.list_session_artifacts(
        SESSION_KEY, turn_id="our-turn",
    )] == ["good.md"]
    assert state.list_session_artifacts(SESSION_KEY, turn_id="other-turn") == []
    assert state.session_artifact_revision(SESSION_KEY) > revision
    assert {row.relative_path for row in state.list_session_artifacts(SESSION_KEY)} == {
        "good.md", "bad-turn.md",
    }


def test_artifact_listing_reuses_project_lookup_and_rechecks_missing_files(
    http_handler, monkeypatch, tmp_path,
):
    handler, _data, _scope, _session = http_handler
    state = handler.state
    for index in range(10):
        path = tmp_path / f"report-{index}.md"
        path.write_text(str(index), encoding="utf-8")
        state.register_artifact(SESSION_KEY, path)
    project_query = MagicMock(wraps=state.get_project)
    monkeypatch.setattr(state, "get_project", project_query)
    (tmp_path / "report-0.md").unlink()
    rows = state.list_session_artifacts(SESSION_KEY)
    assert len(rows) == 10
    assert project_query.call_count == 1
    assert next(row for row in rows if row.relative_path == "report-0.md").status == "missing"

    (tmp_path / "report-0.md").write_text("restored", encoding="utf-8")
    project_query.reset_mock()
    assert all(row.status == "ready" for row in state.list_session_artifacts(SESSION_KEY))
    assert project_query.call_count == 1

"""End-to-end tests for the embedded webui's HTTP routes on the WebSocket channel."""

import asyncio
import functools
import io
import json
import random
import socket
import sqlite3
import time
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote, urlencode

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronRunRecord, CronSchedule
from nanobot.session.keys import UNIFIED_SESSION_KEY
from nanobot.session.manager import Session, SessionManager
from nanobot.storage.journal import SessionEventJournal
from nanobot.webui.gateway_services import GatewayServices, build_gateway_services

_PORT = 29900


def _free_port() -> int:
    for _ in range(100):
        port = random.randint(30_000, 60_000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("could not find a free localhost port")


@pytest.mark.asyncio
async def test_presentation_routes_require_auth_and_filter_conversation(bus, tmp_path, monkeypatch):
    handler = _make_handler({"enabled": True}, bus, workspace_path=tmp_path)
    monkeypatch.setattr(handler.http.presentations, "previews", lambda _: ["cover", "page2", "page3"])
    connection = MagicMock()
    connection.remote_address = ("127.0.0.1", 12345)
    denied = await handler.http.dispatch(connection, Request("/api/presentations/templates", Headers()))
    assert denied.status_code == 401
    denied_preview = await handler.http.dispatch(connection, Request("/api/presentations/previews?template_id=kimi-work", Headers()))
    assert denied_preview.status_code == 401
    bootstrap = await handler.http.dispatch(connection, Request("/webui/bootstrap", Headers()))
    headers = Headers({"Authorization": f"Bearer {json.loads(bootstrap.body)['token']}"})
    headers["X-Request-Id"] = "http-presentation-catalog"
    headers["X-Client-Action-Id"] = "action-presentation-picker"
    removed_source = await handler.http.dispatch(connection, Request("/api/presentations/source", headers))
    assert removed_source.status_code == 404
    catalog = await handler.http.dispatch(connection, Request("/api/presentations/templates", headers))
    assert catalog.status_code == 200
    assert catalog.headers["X-Request-Id"] == "http-presentation-catalog"
    assert len(json.loads(catalog.body)["templates"]) == 7
    assert all(template["previews"] == ["cover"] for template in json.loads(catalog.body)["templates"])
    preview = await handler.http.dispatch(connection, Request("/api/presentations/previews?template_id=kimi-work", headers))
    assert json.loads(preview.body)["previews"] == ["cover", "page2", "page3"]
    invalid_preview = await handler.http.dispatch(connection, Request("/api/presentations/previews?template_id=unknown", headers))
    assert invalid_preview.status_code == 400
    handler.http.presentations.bind({"template_id": "taiping-standard", "document_id": "document-001"},
                                    session_key="websocket:chat-a", project_root=tmp_path, title="Annual report")
    for chat, count in (("chat-a", 1), ("other", 0)):
        result = await handler.http.dispatch(connection, Request(f"/api/presentations/documents?chat_id={chat}", headers))
        assert result.status_code == 200
        assert len(json.loads(result.body)["documents"]) == count


@pytest.mark.asyncio
async def test_diagnostic_export_requires_auth_and_valid_scope(bus, tmp_path):
    handler = _make_handler({"enabled": True}, bus, workspace_path=tmp_path)
    connection = MagicMock()
    connection.remote_address = ("127.0.0.1", 12345)
    now = int(time.time() * 1000)
    route = f"/api/diagnostics/export?start_ms={now - 60000}&end_ms={now}"
    denied = await handler.http.dispatch(connection, Request(route, Headers()))
    assert denied.status_code == 401
    bootstrap = await handler.http.dispatch(connection, Request("/webui/bootstrap", Headers()))
    headers = Headers({"Authorization": f"Bearer {json.loads(bootstrap.body)['token']}"})
    result = await handler.http.dispatch(connection, Request(route, headers))
    assert result.status_code == 200
    payload = json.loads(result.body)
    assert payload["schema_version"] == 1
    assert "trace_spans" in payload["tables"]
    assert "captured_at" in payload["snapshot"]
    invalid = await handler.http.dispatch(connection, Request("/api/diagnostics/export?start_ms=0&end_ms=9999999999999", headers))
    assert invalid.status_code == 400
    missing = await handler.http.dispatch(connection, Request(route + "&session_id=missing", headers))
    assert missing.status_code == 404


def _make_handler(
    cfg: dict[str, Any] | WebSocketConfig,
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    workspace_path: Path | None = None,
    runtime_model_name: Any | None = None,
    runtime_ready: Any | None = None,
    runtime_mcp_status: Any | None = None,
    cron_service: CronService | None = None,
    cron_pending_job_ids: Any | None = None,
) -> GatewayServices:
    config = WebSocketConfig.model_validate(cfg) if isinstance(cfg, dict) else cfg
    workspace = workspace_path or Path.cwd()
    return build_gateway_services(
        config=config,
        bus=bus,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        workspace_path=workspace,
        default_restrict_to_workspace=False,
        runtime_model_name=runtime_model_name,
        runtime_ready=runtime_ready,
        runtime_mcp_status=runtime_mcp_status,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
    )


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    workspace_path: Path | None = None,
    port: int = _PORT,
    runtime_model_name: Any | None = None,
    runtime_ready: Any | None = None,
    runtime_mcp_status: Any | None = None,
    cron_service: CronService | None = None,
    cron_pending_job_ids: Any | None = None,
    **extra: Any,
) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    cfg.update(extra)
    gateway = _make_handler(
        cfg, bus,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        workspace_path=workspace_path,
        runtime_model_name=runtime_model_name,
        runtime_ready=runtime_ready,
        runtime_mcp_status=runtime_mcp_status,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _http_get(
    url: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


@pytest.mark.asyncio
async def test_security_policy_updates_over_gateway_compatible_get(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    port = _free_port()
    channel = _ch(bus, workspace_path=tmp_path, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        headers = {
            "Authorization": f"Bearer {boot.json()['token']}",
            "X-Nanobot-Security-Values": json.dumps({"file_allow_paths": []}),
        }

        response = await _http_get(
            f"http://127.0.0.1:{port}/api/security/policy/update",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["file_allow_paths"] == []
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_security_audit_includes_legacy_mcp_and_targetless_records(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    port = _free_port()
    channel = _ch(bus, workspace_path=tmp_path, port=port)
    logs = channel.gateway.http.logs
    logs.begin_security_event(
        category="file",
        action="write",
        decision="allow",
        result="succeeded",
        risk="normal",
        summary="internal progress update",
    )
    logs.begin_security_event(
        category="mcp",
        action="call",
        decision="allow",
        result="succeeded",
        risk="normal",
        summary="internal MCP event",
        target="mcp_internal",
    )
    visible_id = logs.begin_security_event(
        category="network",
        action="fetch",
        decision="allow",
        result="succeeded",
        risk="normal",
        summary="通过 Web 工具访问网络",
        target="https://example.com/report",
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        response = await _http_get(
            f"http://127.0.0.1:{port}/api/security/audit",
            headers={"Authorization": f"Bearer {boot.json()['token']}"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 3
        assert payload["events"][0]["id"] == visible_id
        assert payload["events"][1]["category"] == "network"
        assert payload["events"][1]["action"] == "mcp_call"
        assert payload["events"][2]["target"] is None
        export = await _http_get(
            f"http://127.0.0.1:{port}/api/security/audit/export",
            headers={"Authorization": f"Bearer {boot.json()['token']}"},
        )
        assert export.status_code == 200
        assert len(export.text.splitlines()) == 3
        [admin] = logs.query_security_events(category="settings")
        assert admin.action == "export_audit"
        assert admin.details["exported_count"] == 3
        assert admin.details["export_stage"] == "generated"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_security_audit_cursor_page_skips_recounting(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    port = _free_port()
    channel = _ch(bus, workspace_path=tmp_path, port=port)
    logs = channel.gateway.http.logs
    for index in range(2):
        logs.begin_security_event(
            category="command",
            action="execute",
            decision="allow",
            result="succeeded",
            risk="normal",
            summary="执行命令",
            target=f"echo page-{index}",
        )
    logs.count_security_events = MagicMock(
        side_effect=AssertionError("cursor pages must not recount all records")
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        response = await _http_get(
            f"http://127.0.0.1:{port}/api/security/audit?limit=1&include_total=0",
            headers={"Authorization": f"Bearer {boot.json()['token']}"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] is None
        assert len(payload["events"]) == 1
        assert payload["next_cursor"] is not None
        logs.count_security_events.assert_not_called()
    finally:
        await channel.stop()
        await server_task


def _seed_session(workspace: Path, key: str = "websocket:test") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hi")
    s.add_message("assistant", "hello back")
    sm.save(s)
    return sm


def _seed_many(workspace: Path, keys: list[str]) -> SessionManager:
    sm = SessionManager(workspace)
    for k in keys:
        s = Session(key=k)
        s.add_message("user", f"hi from {k}")
        sm.save(s)
    return sm


def test_session_listing_does_not_eagerly_replay_event_journals(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = _seed_session(tmp_path, key="websocket:large-history")

    def reject_full_recovery(_journal: SessionEventJournal) -> dict[str, int]:
        raise AssertionError("gateway startup must not replay all event journals")

    monkeypatch.setattr(SessionEventJournal, "recover_all", reject_full_recovery)
    gateway = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    gateway.journal.ensure_recovered = MagicMock(
        side_effect=AssertionError(
            "session listing must not replay the session event journal"
        )
    )

    payload = gateway.http._sessions_list_payload()

    assert [row["key"] for row in payload["sessions"]] == [
        "websocket:large-history"
    ]
    assert gateway.state.get_session("websocket:large-history") is not None
    gateway.journal.ensure_recovered.assert_not_called()


def test_archived_session_does_not_resurrect_after_state_database_rebuild(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    sm = _seed_session(tmp_path, key="websocket:durable-archive")
    first = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    first.http._sessions_list_payload()
    state_session = first.state.get_session("websocket:durable-archive")
    assert state_session is not None
    first.lifecycle.archive_session(
        state_session.session_key,
        session_id=state_session.id,
        project_id=state_session.project_id,
    )
    first.state.archive_session(state_session.session_key)
    first.state.checkpoint()
    first.state.path.write_bytes(b"not-a-sqlite-database")

    rebuilt = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )

    assert rebuilt.state.quick_check() is True
    assert rebuilt.http._sessions_list_payload()["sessions"] == []
    recovered = rebuilt.state.get_session("websocket:durable-archive")
    assert recovered is not None
    assert recovered.status == "archived"
    archived_rows = rebuilt.http._sessions_list_payload(include_archived=True)["sessions"]
    assert [row["key"] for row in archived_rows] == ["websocket:durable-archive"]


def test_archived_project_does_not_resurrect_after_state_database_rebuild(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "removed-workspace"
    project_path.mkdir()
    sm = SessionManager(tmp_path)
    session = Session(
        key="websocket:archived-project-chat",
        metadata={
            "title": "Archived project chat",
            "workspace_scope": {"project_path": str(project_path)},
        },
    )
    session.add_message("user", "remember this without showing the workspace")
    sm.save(session)
    first = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    first.http._sessions_list_payload()
    state_session = first.state.get_session("websocket:archived-project-chat")
    assert state_session is not None
    project = first.state.get_project(state_session.project_id)
    assert project is not None
    first.lifecycle.archive_project(
        project.id,
        name=project.name,
        root_path=project.root_path,
        canonical_root_path=project.canonical_root_path,
    )
    first.state.archive_project(project.id)
    project_path.rmdir()
    first.state.checkpoint()
    first.state.path.write_bytes(b"not-a-sqlite-database")

    rebuilt = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )

    assert rebuilt.state.quick_check() is True
    assert rebuilt.http._sessions_list_payload()["sessions"] == []
    recovered_session = rebuilt.state.get_session("websocket:archived-project-chat")
    assert recovered_session is not None
    recovered_project = rebuilt.state.get_project(recovered_session.project_id)
    assert recovered_project is not None
    assert recovered_project.id == project.id
    assert recovered_project.canonical_root_path == str(project_path.resolve())
    assert recovered_project.status == "archived"
    assert [
        row["key"]
        for row in rebuilt.http._sessions_list_payload(include_archived=True)["sessions"]
    ] == ["websocket:archived-project-chat"]


def test_misbound_session_projection_is_quarantined_and_rebuilt(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "durable-project"
    project_path.mkdir()
    durable_project_id = f"prj_{'a' * 32}"
    key = "websocket:misbound-project-chat"
    sm = SessionManager(tmp_path)
    session = Session(
        key=key,
        metadata={
            "title": "Durable project chat",
            "project_id": durable_project_id,
            "workspace_scope": {"project_path": str(project_path)},
        },
    )
    session.add_message("user", "keep me with the durable project")
    sm.save(session)

    first = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    first.http._sessions_list_payload()
    projected = first.state.get_session(key)
    assert projected is not None
    assert projected.project_id == durable_project_id

    inbox = first.state.ensure_project(tmp_path, kind="inbox")
    first.state.bind_session(
        key,
        inbox.id,
        title="Wrong inbox binding",
        metadata=session.metadata,
        allow_draft_rebind=True,
    )
    first.state.checkpoint()

    rebuilt = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )

    recovered = rebuilt.state.get_session(key)
    assert recovered is not None
    assert recovered.project_id == durable_project_id
    recovered_project = rebuilt.state.get_project(recovered.project_id)
    assert recovered_project is not None
    assert recovered_project.canonical_root_path == str(project_path.resolve())
    recovery_dirs = list(
        (tmp_path / ".nanobot" / "recovery").glob("projection-bindings-*")
    )
    assert len(recovery_dirs) == 1
    assert (recovery_dirs[0] / "state.sqlite").is_file()


def test_matching_project_identity_allows_projection_relocation(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "original-project"
    relocated_path = tmp_path / "relocated-project"
    project_path.mkdir()
    relocated_path.mkdir()
    durable_project_id = f"prj_{'b' * 32}"
    key = "websocket:relocated-project-chat"
    sm = SessionManager(tmp_path)
    session = Session(
        key=key,
        metadata={
            "project_id": durable_project_id,
            "workspace_scope": {"project_path": str(project_path)},
        },
    )
    session.add_message("user", "follow an intentional project relocation")
    sm.save(session)

    first = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    first.http._sessions_list_payload()
    first.state.relocate_project(durable_project_id, relocated_path)
    first.state.checkpoint()

    reopened = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )

    project = reopened.state.get_project(durable_project_id)
    assert project is not None
    assert project.canonical_root_path == str(relocated_path.resolve())
    recovery_root = tmp_path / ".nanobot" / "recovery"
    assert not recovery_root.exists() or not list(
        recovery_root.glob("projection-bindings-*")
    )


def test_session_listing_uses_metadata_only_after_sqlite_projection(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = _seed_session(tmp_path, key="websocket:projected-history")
    gateway = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )

    first = gateway.http._sessions_list_payload()
    assert [row["key"] for row in first["sessions"]] == [
        "websocket:projected-history"
    ]

    monkeypatch.setattr(
        sm,
        "read_session_file",
        MagicMock(
            side_effect=AssertionError(
                "projected session listing must not load full transcripts"
            )
        ),
    )
    second = gateway.http._sessions_list_payload()

    assert [row["key"] for row in second["sessions"]] == [
        "websocket:projected-history"
    ]
    sm.read_session_file.assert_not_called()


def test_session_listing_merges_sqlite_session_missing_from_jsonl_index(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = _seed_session(tmp_path, key="websocket:sqlite-only")
    gateway = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    project = gateway.state.ensure_project(tmp_path)
    state_session = gateway.state.bind_session(
        "websocket:sqlite-only",
        project.id,
        title="SQLite session",
    )
    monkeypatch.setattr(
        "nanobot.webui.ws_http.list_webui_sessions",
        lambda _manager: [],
    )

    payload = gateway.http._sessions_list_payload()

    [row] = payload["sessions"]
    assert row["key"] == "websocket:sqlite-only"
    assert isinstance(row["created_at"], str)
    assert isinstance(row["updated_at"], str)
    assert row["title"] == "SQLite session"
    assert row["preview"] == "hi"
    assert row["workspace_scope"]["project_path"] == str(tmp_path)
    assert row["session_id"] == state_session.id
    assert row["project_id"] == project.id


def test_session_listing_hides_reasoning_title_from_sqlite_projection(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    key = "websocket:reasoning-title"
    sm = _seed_session(tmp_path, key=key)
    session = sm.get_or_create(key)
    bad_title = "被截断的自动标题…"
    session.metadata["title"] = bad_title
    sm.save(session)
    gateway = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
    )
    project = gateway.state.ensure_project(tmp_path)
    gateway.state.bind_session(key, project.id, title=bad_title)

    payload = gateway.http._sessions_list_payload()

    [row] = payload["sessions"]
    assert row["title"] == "hi"
    assert row["preview"] == "hi"


@pytest.mark.asyncio
async def test_bootstrap_returns_token_for_localhost(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29901)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29901/webui/bootstrap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"].startswith("nbwt_")
        assert body["ws_path"] == "/"
        assert body["ws_url"] == "ws://127.0.0.1:29901/"
        assert body["expires_in"] > 0
        assert isinstance(body.get("model_name"), str)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_routes_require_bearer_token(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path, key="websocket:abc")
    channel = _ch(bus, session_manager=sm, port=29902)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Unauthenticated → 401.
        deny = await _http_get("http://127.0.0.1:29902/api/sessions")
        assert deny.status_code == 401

        # Mint a token via bootstrap, then call the API with it.
        boot = await _http_get("http://127.0.0.1:29902/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get("http://127.0.0.1:29902/api/sessions", headers=auth)
        assert listing.status_code == 200
        keys = [s["key"] for s in listing.json()["sessions"]]
        assert "websocket:abc" in keys
        # Server stays an opaque source: filesystem paths must not leak to the wire.
        assert all("path" not in s for s in listing.json()["sessions"])

        msgs = await _http_get(
            "http://127.0.0.1:29902/api/sessions/websocket:abc/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        body = msgs.json()
        assert body["key"] == "websocket:abc"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

        runtime = await _http_get(
            "http://127.0.0.1:29902/api/sessions/"
            "websocket%3Aabc/runtime-snapshot",
            headers=auth,
        )
        assert runtime.status_code == 200
        runtime_body = runtime.json()
        assert runtime_body["session_key"] == "websocket:abc"
        assert runtime_body["thread_status"] == {"type": "notLoaded"}
        assert runtime_body["active_turn"] is None
        assert runtime_body["project_id"].startswith("prj_")
        assert runtime_body["session_id"].startswith("ses_")

        diagnostics = await _http_get(
            "http://127.0.0.1:29902/api/sessions/"
            "websocket%3Aabc/runtime-diagnostics",
            headers=auth,
        )
        assert diagnostics.status_code == 200
        assert diagnostics.json()["runtime_snapshot"]["session_key"] == "websocket:abc"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_projects_api_exposes_stable_project_and_session_ids(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "customer-a"
    project_path.mkdir()
    sm = SessionManager(tmp_path)
    session = Session(
        key="websocket:project-chat",
        metadata={
            "title": "Project chat",
            "workspace_scope": {
                "project_path": str(project_path),
                "access_mode": "restricted",
            },
        },
    )
    session.add_message("user", "project question")
    sm.save(session)
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=29939,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29939/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        projects_response = await _http_get(
            "http://127.0.0.1:29939/api/projects",
            headers=auth,
        )
        assert projects_response.status_code == 200
        project = next(
            row
            for row in projects_response.json()["projects"]
            if row["name"] == "customer-a"
        )
        assert project["id"].startswith("prj_")
        assert project["kind"] == "workspace"
        assert project["root_path"] == str(project_path)

        sessions_response = await _http_get(
            f"http://127.0.0.1:29939/api/projects/{project['id']}/sessions",
            headers=auth,
        )
        assert sessions_response.status_code == 200
        [project_session] = sessions_response.json()["sessions"]
        assert project_session["id"].startswith("ses_")
        assert project_session["project_id"] == project["id"]
        assert project_session["session_key"] == "websocket:project-chat"
        assert project_session["title"] == "Project chat"

        exported = await _http_get(
            f"http://127.0.0.1:29939/api/projects/{project['id']}/export",
            headers=auth,
        )
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert {"manifest.json", "state-export.json"} <= set(archive.namelist())
            exported_manifest = json.loads(archive.read("manifest.json"))
        assert exported_manifest["project"]["id"] == project["id"]
        assert exported_manifest["sessions"][0]["id"] == project_session["id"]

        relocated_path = tmp_path / "customer-a-relocated"
        relocated_path.mkdir()
        relocated = await _http_get(
            f"http://127.0.0.1:29939/api/projects/{project['id']}/relocate"
            f"?path={quote(str(relocated_path))}",
            headers=auth,
        )
        assert relocated.status_code == 200
        assert relocated.json()["project"]["id"] == project["id"]
        assert relocated.json()["project"]["root_path"] == str(relocated_path)

        archived = await _http_get(
            f"http://127.0.0.1:29939/api/projects/{project['id']}/archive",
            headers=auth,
        )
        assert archived.status_code == 200
        assert archived.json()["project"]["status"] == "archived"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_project_memory_routes_are_removed(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "memory-project"
    project_path.mkdir()
    channel = _ch(
        bus,
        session_manager=SessionManager(tmp_path),
        workspace_path=tmp_path,
        port=29948,
    )
    project = channel.gateway.state.ensure_project(project_path, kind="workspace")
    channel.gateway.state.upsert_project_memory(
        project.id,
        kind="workflow",
        title="Package manager",
        content="Use pnpm.",
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        url = f"http://127.0.0.1:29948/api/projects/{project.id}/memories"
        listing = await _http_get(url)
        assert listing.status_code == 404
        # Removing the feature does not destructively erase legacy rows.
        assert channel.gateway.state.list_project_memories(project.id)[0]["content"] == "Use pnpm."
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_artifact_routes_list_and_serve_workspace_file(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    sm = _seed_session(tmp_path, key="websocket:artifact-chat")
    report = tmp_path / "reports" / "market.pdf"
    report.parent.mkdir()
    report.write_bytes(b"%PDF-session-artifact")
    unrelated = tmp_path / "reports" / "another-session.pdf"
    unrelated.write_bytes(b"%PDF-unrelated")
    session = sm.get_or_create("websocket:artifact-chat")
    session.add_message(
        "tool",
        json.dumps({"files": [{"path": str(report)}]}),
    )
    sm.save(session)
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=29938,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        unauthenticated = await _http_get(
            "http://127.0.0.1:29938/api/sessions/"
            "websocket%3Aartifact-chat/artifacts"
        )
        assert unauthenticated.status_code == 401

        boot = await _http_get("http://127.0.0.1:29938/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}
        thread = await _http_get(
            "http://127.0.0.1:29938/api/sessions/"
            "websocket%3Aartifact-chat/thread",
            headers=auth,
        )
        assert thread.status_code == 200
        assert any(
            artifact["path"] == "reports/market.pdf"
            for artifact in thread.json()["artifacts"]
        )
        assert all(
            artifact["path"] != "reports/another-session.pdf"
            for artifact in thread.json()["artifacts"]
        )
        channel.gateway.state.register_artifact(
            "websocket:artifact-chat",
            unrelated,
            relation_type="referenced",
        )
        repaired = await _http_get(
            "http://127.0.0.1:29938/api/sessions/"
            "websocket%3Aartifact-chat/thread",
            headers=auth,
        )
        assert repaired.status_code == 200
        assert all(
            artifact["path"] != "reports/another-session.pdf"
            for artifact in repaired.json()["artifacts"]
        )
        listing = await _http_get(
            "http://127.0.0.1:29938/api/sessions/"
            "websocket%3Aartifact-chat/artifacts",
            headers=auth,
        )
        assert listing.status_code == 200
        row = next(
            artifact
            for artifact in listing.json()["artifacts"]
            if artifact["path"] == "reports/market.pdf"
        )
        assert row["name"] == "market.pdf"
        assert row["id"].startswith("art_")
        assert row["project_id"].startswith("prj_")
        assert row["session_id"].startswith("ses_")
        assert row["status"] == "ready"
        assert row["preview_url"].startswith("/api/artifacts/")
        assert "session=websocket%3Aartifact-chat" in row["preview_url"]
        assert "local_path" not in row

        content = await _http_get(
            f"http://127.0.0.1:29938{row['preview_url']}",
            headers=auth,
        )
        assert content.status_code == 200
        assert content.content == b"%PDF-session-artifact"
        assert content.headers["content-type"] == "application/pdf"
        assert content.headers["content-disposition"].startswith("inline;")

        missing_session = await _http_get(
            f"http://127.0.0.1:29938/api/artifacts/{row['id']}/content",
            headers=auth,
        )
        assert missing_session.status_code == 400

        wrong_session = await _http_get(
            f"http://127.0.0.1:29938/api/artifacts/{row['id']}/content"
            "?session=websocket%3Aother-chat",
            headers=auth,
        )
        assert wrong_session.status_code == 404

        diagnostic_denied = await _http_get(
            "http://127.0.0.1:29938/api/diagnostics/logs"
        )
        assert diagnostic_denied.status_code == 401
        diagnostics = await _http_get(
            "http://127.0.0.1:29938/api/diagnostics/logs"
            f"?artifact_id={row['id']}",
            headers=auth,
        )
        assert diagnostics.status_code == 200
        events = {
            log["event_name"] for log in diagnostics.json()["logs"]
        }
        assert "artifact_content_served" in events
        assert "artifact_content_failed" in events

        traversal = await _http_get(
            "http://127.0.0.1:29938/api/sessions/"
            "websocket%3Aartifact-chat/artifacts/content?path=../secret.txt",
            headers=auth,
        )
        assert traversal.status_code in {403, 404}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_thread_resource_pages_messages_by_canonical_event_sequence(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    session_key = "websocket:thread-page"
    sm = _seed_session(tmp_path, key=session_key)
    port = _free_port()
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=port,
    )
    project = channel.gateway.state.ensure_project(tmp_path)
    channel.gateway.state.bind_session(session_key, project.id)
    for index in range(1, 6):
        channel.gateway.journal.commit(
            session_key,
            {
                "event": "user",
                "turn_id": f"turn-{index}",
                "text": f"canonical-{index}",
            },
        )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}
        base = (
            f"http://127.0.0.1:{port}/api/sessions/"
            "websocket%3Athread-page/thread"
        )
        latest = await _http_get(f"{base}?message_limit=2", headers=auth)
        assert latest.status_code == 200
        latest_payload = latest.json()
        assert [message["content"] for message in latest_payload["messages"]] == [
            "canonical-4",
            "canonical-5",
        ]
        assert latest_payload["message_page"] == {
            "before_event_seq": 4,
            "has_more_before": True,
            "loaded_message_count": 2,
        }

        older = await _http_get(
            f"{base}?message_limit=2&before_message_event_seq=4",
            headers=auth,
        )
        assert older.status_code == 200
        older_payload = older.json()
        assert [message["content"] for message in older_payload["messages"]] == [
            "canonical-2",
            "canonical-3",
        ]
        assert older_payload["message_page"]["before_event_seq"] == 2
        assert older_payload["message_page"]["has_more_before"] is True
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_thread_resource_anchors_user_before_long_turn_tail(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    session_key = "websocket:thread-long-turn"
    sm = _seed_session(tmp_path, key=session_key)
    port = _free_port()
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=port,
    )
    project = channel.gateway.state.ensure_project(tmp_path)
    channel.gateway.state.bind_session(session_key, project.id)
    channel.gateway.journal.commit(
        session_key,
        {
            "event": "user",
            "turn_id": "turn-long",
            "text": "帮我分析下 长江电力",
        },
    )
    for index in range(205):
        channel.gateway.journal.commit(
            session_key,
            {
                "event": "message",
                "turn_id": "turn-long",
                "kind": "progress",
                "text": f"progress-{index}",
            },
        )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}
        response = await _http_get(
            f"http://127.0.0.1:{port}/api/sessions/"
            "websocket%3Athread-long-turn/thread?message_limit=200",
            headers=auth,
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "帮我分析下 长江电力"
        assert sum(row["role"] == "user" for row in payload["messages"]) == 1
        assert payload["message_page"]["has_more_before"] is True
        assert payload["message_page"]["before_event_seq"] == 7
        assert payload["message_page"]["loaded_message_count"] == 2
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_thread_resource_tolerates_legacy_malformed_message_projection(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    session_key = "websocket:thread-malformed-json"
    sm = _seed_session(tmp_path, key=session_key)
    port = _free_port()
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=port,
    )
    project = channel.gateway.state.ensure_project(tmp_path)
    session = channel.gateway.state.bind_session(session_key, project.id)
    channel.gateway.journal.commit(
        session_key,
        {
            "event": "user",
            "turn_id": "turn-malformed",
            "text": "帮我分析下 特变电工 A股",
        },
    )
    channel.gateway.journal.commit(
        session_key,
        {
            "event": "message",
            "turn_id": "turn-malformed",
            "kind": "progress",
            "text": "working",
        },
    )
    with sqlite3.connect(channel.gateway.state.path) as connection:
        connection.execute(
            "UPDATE messages SET content_json = ? "
            "WHERE session_id = ? AND sequence_no = 2",
            ('{"truncated":', session.id),
        )
        connection.commit()

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}
        response = await _http_get(
            f"http://127.0.0.1:{port}/api/sessions/"
            "websocket%3Athread-malformed-json/thread?message_limit=200",
            headers=auth,
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "帮我分析下 特变电工 A股"
        assert any(
            message["content"] == "working"
            for message in payload["messages"]
        )
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_automations_route_filters_by_webui_session(
    bus: MagicMock, tmp_path: Path
) -> None:
    cron = CronService(tmp_path / "cron" / "jobs.json")
    hourly = CronSchedule(kind="every", every_ms=3_600_000)
    pending_job_id = ""
    for name, message, to in (
        ("Morning check", "Check the project status", "abc"),
        ("Other session", "Do not show", "other"),
    ):
        job = cron.add_job(
            name=name,
            schedule=hourly,
            message=message,
            session_key=f"websocket:{to}",
            origin_channel="websocket",
            origin_chat_id=to,
        )
        if name == "Morning check":
            pending_job_id = job.id
    cron.add_job(
        name="Legacy same target",
        schedule=hourly,
        message="Legacy job should be migrated",
        deliver=True,
        channel="websocket",
        to="abc",
        session_key="websocket:abc",
    )
    cron.register_system_job(
        CronJob(
            id="heartbeat",
            name="heartbeat",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(kind="system_event"),
        )
    )
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path, key="websocket:abc"),
        cron_service=cron,
        cron_pending_job_ids=lambda key: {pending_job_id} if key == "websocket:abc" else set(),
        port=29914,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get(
            "http://127.0.0.1:29914/api/sessions/websocket:abc/automations"
        )
        assert deny.status_code == 401

        boot = await _http_get("http://127.0.0.1:29914/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            "http://127.0.0.1:29914/api/sessions/websocket%3Aabc/automations",
            headers=auth,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert [job["name"] for job in body["jobs"]] == ["Morning check", "Legacy same target"]
        job = body["jobs"][0]
        assert job["schedule"]["kind"] == "every"
        assert job["schedule"]["every_ms"] == 3_600_000
        assert job["payload"]["message"] == "Check the project status"
        assert job["state"]["pending"] is True
        assert body["jobs"][1]["state"]["pending"] is False
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_automations_route_ignores_unified_owner(
    bus: MagicMock, tmp_path: Path
) -> None:
    cron = CronService(tmp_path / "cron" / "jobs.json")
    hourly = CronSchedule(kind="every", every_ms=3_600_000)
    cron.add_job(
        name="Unified check",
        schedule=hourly,
        message="Check the shared session",
        session_key=UNIFIED_SESSION_KEY,
        origin_channel="websocket",
        origin_chat_id="abc",
    )
    cron.add_job(
        name="Visible chat job",
        schedule=hourly,
        message="Show for this chat",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
    )
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path, key="websocket:abc"),
        cron_service=cron,
        port=29917,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29917/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            "http://127.0.0.1:29917/api/sessions/websocket%3Aabc/automations",
            headers=auth,
        )
        assert resp.status_code == 200
        assert [job["name"] for job in resp.json()["jobs"]] == ["Visible chat job"]

        resp = await _http_get(
            "http://127.0.0.1:29917/api/sessions/websocket%3Aother/automations",
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_webui_skills_route_requires_token_and_hides_paths(
    bus: MagicMock, tmp_path: Path
) -> None:
    workspace_skill = tmp_path / "skills" / "workspace-skill"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\nname: workspace-skill\ndescription: Workspace skill.\n---\n",
        encoding="utf-8",
    )
    unavailable_skill = tmp_path / "skills" / "zz-unavailable-skill"
    unavailable_skill.mkdir(parents=True)
    (unavailable_skill / "SKILL.md").write_text(
        "\n".join([
            "---",
            "name: zz-unavailable-skill",
            "description: Missing CLI skill.",
            "metadata:",
            "  nanobot:",
            "    requires:",
            "      bins:",
            "        - definitely-missing-nanobot-skill-cli",
            "      env:",
            "        - DEFINITELY_MISSING_NANOBOT_SKILL_ENV",
            "---",
            "Use the missing CLI and env var.",
        ]),
        encoding="utf-8",
    )
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path),
        workspace_path=tmp_path,
        port=29920,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get("http://127.0.0.1:29920/api/webui/skills")
        assert deny.status_code == 401
        deny_detail = await _http_get("http://127.0.0.1:29920/api/webui/skills/workspace-skill")
        assert deny_detail.status_code == 401

        boot = await _http_get("http://127.0.0.1:29920/webui/bootstrap")
        token = boot.json()["token"]
        resp = await _http_get(
            "http://127.0.0.1:29920/api/webui/skills",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        names = [skill["name"] for skill in body["skills"]]
        assert names[0] == "workspace-skill"
        assert "cron" in names
        assert all("path" not in skill for skill in body["skills"])
        workspace = body["skills"][0]
        assert workspace == {
            "name": "workspace-skill",
            "description": "Workspace skill.",
            "source": "workspace",
            "available": True,
            "unavailable_reason": "",
        }
        unavailable = next(skill for skill in body["skills"] if skill["name"] == "zz-unavailable-skill")
        assert unavailable["available"] is False
        assert unavailable["unavailable_reason"] == (
            "CLI: definitely-missing-nanobot-skill-cli, "
            "ENV: DEFINITELY_MISSING_NANOBOT_SKILL_ENV"
        )

        detail = await _http_get(
            "http://127.0.0.1:29920/api/webui/skills/zz-unavailable-skill",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200
        detail_body = detail.json()
        assert "path" not in detail_body
        assert detail_body["requirements"] == {
            "bins": ["definitely-missing-nanobot-skill-cli"],
                "env": ["DEFINITELY_MISSING_NANOBOT_SKILL_ENV"],
                "files": [],
                "missing_bins": ["definitely-missing-nanobot-skill-cli"],
                "missing_env": ["DEFINITELY_MISSING_NANOBOT_SKILL_ENV"],
                "missing_files": [],
            }
        assert "Use the missing CLI and env var." in detail_body["raw_markdown"]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cli_apps_routes_require_token_and_return_payload(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def payload(*, installed_only: bool = False) -> dict[str, Any]:
        return {
            "apps": [
                {
                    "name": "gimp",
                    "display_name": "GIMP",
                    "category": "image",
                    "description": "Image editing",
                    "requires": "Python",
                    "source": "harness",
                    "entry_point": "cli-anything-gimp",
                    "install_supported": True,
                    "installed": False,
                    "available": False,
                    "status": "not_installed",
                    "logo_url": None,
                    "brand_color": None,
                    "skill_installed": False,
                }
            ],
            "installed_count": 0,
            "catalog_updated_at": "2026-04-18",
        }

    monkeypatch.setattr(
        "nanobot.webui.settings_routes.cli_apps_payload",
        payload,
    )
    monkeypatch.setattr(
        "nanobot.webui.settings_routes.cli_apps_action",
        lambda action, query: {
            "apps": [],
            "installed_count": 1,
            "catalog_updated_at": "2026-04-18",
            "last_action": {"ok": True, "message": f"{action}:{query['name'][0]}"},
        },
    )
    channel = _ch(bus, session_manager=_seed_session(tmp_path), port=29912)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get("http://127.0.0.1:29912/api/settings/cli-apps")
        assert deny.status_code == 401

        boot = await _http_get("http://127.0.0.1:29912/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        catalog = await _http_get(
            "http://127.0.0.1:29912/api/settings/cli-apps",
            headers=auth,
        )
        assert catalog.status_code == 200
        assert catalog.json()["apps"][0]["name"] == "gimp"

        installed = await _http_get(
            "http://127.0.0.1:29912/api/settings/cli-apps/install?name=gimp",
            headers=auth,
        )
        assert installed.status_code == 200
        assert installed.json()["last_action"]["message"] == "install:gimp"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cli_apps_catalog_does_not_block_other_webui_http_routes(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_payload(*, installed_only: bool = False) -> dict[str, Any]:
        assert installed_only is False
        entered.set()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(release.wait(), 2.0)
        return {"apps": [], "installed_count": 0, "catalog_updated_at": None}

    monkeypatch.setattr("nanobot.webui.settings_routes.cli_apps_payload", slow_payload)
    channel = _ch(bus, session_manager=_seed_session(tmp_path), port=29935)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29935/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        started = time.perf_counter()
        catalog_task = asyncio.create_task(
            _http_get("http://127.0.0.1:29935/api/settings/cli-apps", headers=auth)
        )
        assert await asyncio.wait_for(entered.wait(), 2.0)
        assert time.perf_counter() - started < 1.0

        workspaces_started = time.perf_counter()
        workspaces = await _http_get("http://127.0.0.1:29935/api/workspaces", headers=auth)
        assert time.perf_counter() - workspaces_started < 1.0
        assert workspaces.status_code == 200

        release.set()
        catalog = await catalog_task
        assert catalog.status_code == 200
        assert catalog.json()["apps"] == []
    finally:
        release.set()
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cli_apps_route_supports_installed_only_payload(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def payload(*, installed_only: bool = False) -> dict[str, Any]:
        calls.append(installed_only)
        return {"apps": [], "installed_count": 0, "catalog_updated_at": None}

    monkeypatch.setattr("nanobot.webui.settings_routes.cli_apps_payload", payload)
    channel = _ch(bus, session_manager=_seed_session(tmp_path), port=29936)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29936/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            "http://127.0.0.1:29936/api/settings/cli-apps?installed_only=1",
            headers=auth,
        )

        assert resp.status_code == 200
        assert resp.json()["apps"] == []
        assert calls == [True]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_mcp_presets_routes_require_token_and_return_payload(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nanobot.webui.mcp_presets_api.mcp_presets_payload",
        lambda: {
            "presets": [
                {
                    "name": "browserbase",
                    "display_name": "Browserbase",
                    "category": "browser",
                    "description": "Cloud browser automation",
                    "docs_url": "https://docs.browserbase.com/integrations/mcp/configuration",
                    "transport": "streamableHttp",
                    "requires": "Browserbase API key",
                    "note": "",
                    "install_supported": True,
                    "installed": False,
                    "configured": False,
                    "available": False,
                    "status": "not_installed",
                    "logo_url": None,
                    "brand_color": "#111827",
                    "required_fields": [],
                    "connection_summary": "",
                }
            ],
            "installed_count": 0,
        },
    )
    preset_queries: list[tuple[str, dict[str, list[str]]]] = []
    custom_queries: list[tuple[str, dict[str, list[str]]]] = []

    def _mcp_preset_action(action: str, query: dict[str, list[str]]) -> dict[str, Any]:
        preset_queries.append((action, query))
        return {
            "presets": [],
            "installed_count": 1,
            "requires_restart": action != "test",
            "last_action": {"ok": True, "message": f"{action}:{query['name'][0]}"},
        }

    def _custom_action(action: str, query: dict[str, list[str]]) -> dict[str, Any]:
        custom_queries.append((action, query))
        return {
            "presets": [],
            "installed_count": 1,
            "requires_restart": True,
            "last_action": {
                "ok": True,
                "message": f"{action}:{query.get('name', ['config'])[0]}",
            },
        }

    monkeypatch.setattr(
        "nanobot.webui.mcp_presets_api.mcp_presets_action",
        _mcp_preset_action,
    )
    monkeypatch.setattr(
        "nanobot.webui.mcp_presets_api.custom_mcp_action",
        _custom_action,
    )

    async def _hot_reload(_bus):
        return {"ok": True, "message": "MCP config reloaded.", "requires_restart": False}

    monkeypatch.setattr(
        "nanobot.webui.settings_routes.request_mcp_reload",
        _hot_reload,
    )
    channel = _ch(bus, session_manager=_seed_session(tmp_path), port=29913)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get("http://127.0.0.1:29913/api/settings/mcp-presets")
        assert deny.status_code == 401

        boot = await _http_get("http://127.0.0.1:29913/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        catalog = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets",
            headers=auth,
        )
        assert catalog.status_code == 200
        assert catalog.json()["presets"][0]["name"] == "browserbase"

        enabled = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/enable?name=browserbase",
            headers={
                **auth,
                "X-Nanobot-MCP-Values": json.dumps(
                    {"browserbase_api_key": "bb_live_secret"}
                ),
            },
        )
        assert enabled.status_code == 200
        assert preset_queries[-1][1]["browserbase_api_key"] == ["bb_live_secret"]
        body = enabled.json()
        assert "bb_live_secret" not in enabled.text
        assert body["last_action"]["message"] == "enable:browserbase MCP config reloaded."
        assert body["hot_reload"]["ok"] is True
        assert body["restart_required_sections"] == []

        bad_header = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/enable?name=browserbase",
            headers={**auth, "X-Nanobot-MCP-Values": "[]"},
        )
        assert bad_header.status_code == 400

        custom = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/custom",
            headers={
                **auth,
                "X-Nanobot-MCP-Values": json.dumps(
                    {"name": "docs", "command": "npx"}
                ),
            },
        )
        assert custom.status_code == 200
        assert custom_queries[-1][1]["command"] == ["npx"]
        assert custom.json()["last_action"]["message"] == "custom:docs MCP config reloaded."

        imported = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/import",
            headers={**auth, "X-Nanobot-MCP-Values": json.dumps({"config": "{}"})},
        )
        assert imported.status_code == 200
        assert imported.json()["last_action"]["message"] == "import:config MCP config reloaded."

        tools = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/tools",
            headers={
                **auth,
                "X-Nanobot-MCP-Values": json.dumps(
                    {"name": "docs", "enabled_tools": []}
                ),
            },
        )
        assert tools.status_code == 200
        assert tools.json()["last_action"]["message"] == "tools:docs MCP config reloaded."
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_list_only_returns_websocket_sessions_by_default(
    bus: MagicMock, tmp_path: Path
) -> None:
    # Seed a realistic multi-channel disk state: CLI, Slack, Lark and
    # websocket sessions all live in the same ``sessions/`` directory.
    sm = _seed_many(
        tmp_path,
        [
            "cli:direct",
            "slack:C123",
            "lark:oc_abc",
            "websocket:alpha",
            "websocket:beta",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29906/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get(
            "http://127.0.0.1:29906/api/sessions", headers=auth
        )
        assert listing.status_code == 200
        keys = {s["key"] for s in listing.json()["sessions"]}
        # Only websocket-channel sessions are part of the webui surface; CLI /
        # Slack / Lark rows would be non-resumable from the browser.
        assert keys == {"websocket:alpha", "websocket:beta"}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_webui_sidebar_state_routes_are_config_dir_scoped(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:sidebar")
    channel = _ch(bus, session_manager=sm, port=29911)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29911/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        initial = await _http_get(
            "http://127.0.0.1:29911/api/webui/sidebar-state",
            headers=auth,
        )
        assert initial.status_code == 200
        assert initial.json()["schema_version"] == 1
        assert initial.json()["pinned_keys"] == []

        payload = {
            "pinned_keys": ["websocket:sidebar"],
            "archived_keys": ["websocket:old"],
            "title_overrides": {"websocket:sidebar": "Pinned work"},
            "view": {"density": "compact", "show_archived": True},
        }
        query = urlencode({"state": json.dumps(payload)})
        updated = await _http_get(
            f"http://127.0.0.1:29911/api/webui/sidebar-state/update?{query}",
            headers=auth,
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["pinned_keys"] == ["websocket:sidebar"]
        assert body["title_overrides"] == {"websocket:sidebar": "Pinned work"}
        assert body["view"]["density"] == "compact"

        state_path = tmp_path / "webui" / "sidebar-state.json"
        assert state_path.is_file()
        assert json.loads(state_path.read_text(encoding="utf-8"))["pinned_keys"] == [
            "websocket:sidebar"
        ]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_archives_and_can_restore_without_removing_files(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object("websocket:doomed", {"event": "user", "chat_id": "doomed", "text": "x"})
    channel = _ch(
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        port=29903,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29903/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        assert path.exists()
        webui_path = tmp_path / "webui" / f"{SessionManager.safe_key('websocket:doomed')}.jsonl"
        assert webui_path.is_file()
        resp = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/delete",
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert resp.json()["archived"] is True
        assert path.exists()
        assert webui_path.exists()
        hidden = await _http_get(
            "http://127.0.0.1:29903/api/sessions",
            headers=auth,
        )
        assert all(row["key"] != "websocket:doomed" for row in hidden.json()["sessions"])
        restored = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/restore",
            headers=auth,
        )
        assert restored.status_code == 200
        assert restored.json()["restored"] is True

        archived = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/archive",
            headers=auth,
        )
        assert archived.status_code == 200
        assert archived.json()["archived"] is True
        managed = await _http_get(
            "http://127.0.0.1:29903/api/data-management/archives",
            headers=auth,
        )
        assert managed.status_code == 200
        assert [row["session_key"] for row in managed.json()["archived_sessions"]] == [
            "websocket:doomed"
        ]
        purged = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/purge",
            headers=auth,
        )
        assert purged.status_code == 200
        assert purged.json()["purged"] is True
        assert not path.exists()
        assert not webui_path.exists()
        assert channel.gateway.state.get_session("websocket:doomed") is None
        assert channel.gateway.lifecycle.session_state("websocket:doomed") == "purged"

        # Even a residual/recreated JSONL cannot reappear after a projection
        # rebuild because the durable purge tombstone is authoritative.
        stray = Session(key="websocket:doomed")
        stray.add_message("user", "stray residual")
        sm.save(stray)
        still_hidden = await _http_get(
            "http://127.0.0.1:29903/api/sessions",
            headers=auth,
        )
        assert all(
            row["key"] != "websocket:doomed"
            for row in still_hidden.json()["sessions"]
        )
    finally:
        await channel.stop()
        await server_task


def test_schedule_run_delete_purges_conversation_but_preserves_workspace_files(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    session_key = "cron:task-1:run-1"
    sm = _seed_session(tmp_path, key=session_key)
    from nanobot.webui.transcript import append_transcript_object

    append_transcript_object(
        session_key,
        {"event": "user", "chat_id": session_key, "text": "scheduled prompt"},
    )
    workspace_file = tmp_path / "scheduled-report.md"
    workspace_file.write_text("keep this report", encoding="utf-8")

    cron = CronService(tmp_path / "cron" / "jobs.json")
    created = cron.add_job(
        name="Scheduled report",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="Create a report",
        channel="websocket",
        to="direct",
        session_key="websocket:source",
    )
    cron._running = True
    store = cron._load_store()
    job = next(item for item in store.jobs if item.id == created.id)
    job.state.run_history.append(
        CronRunRecord(
            run_at_ms=123,
            status="ok",
            run_id="run-1",
            session_key=session_key,
        )
    )
    cron._save_store()

    gateway = _make_handler(
        {"enabled": True},
        bus,
        session_manager=sm,
        workspace_path=tmp_path,
        cron_service=cron,
    )
    listed = gateway.http._sessions_list_payload()
    assert any(row["key"] == session_key for row in listed["sessions"])
    session_path = sm._get_session_path(session_key)
    webui_path = tmp_path / "webui" / f"{SessionManager.safe_key(session_key)}.jsonl"
    assert session_path.is_file()
    assert webui_path.is_file()

    request = Request(
        f"/api/schedule/runs/delete?task_id={job.id}&run_id=run-1",
        Headers(),
    )
    response = gateway.http.schedule_routes._delete_run(request)

    assert response.status_code == 200
    assert cron.get_job(job.id).state.run_history == []
    assert gateway.state.get_session(session_key) is None
    assert gateway.lifecycle.session_state(session_key) == "purged"
    assert not session_path.exists()
    assert not webui_path.exists()
    assert workspace_file.read_text(encoding="utf-8") == "keep this report"


@pytest.mark.asyncio
async def test_webui_automations_route_lists_all_jobs_and_allows_user_actions(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cron = CronService(tmp_path / "cron" / "jobs.json")
    user_job = cron.add_job(
        name="Daily repo check",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        message="Check the repo status",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
    )
    incomplete_job = cron.add_job(
        name="english-quiz",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Practice English",
        session_key="unified:default",
    )
    external_job = cron.add_job(
        name="WeChat quiz",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Send a quiz",
        session_key="weixin:wx-chat",
        origin_channel="weixin",
        origin_chat_id="wx-chat",
    )
    past_one_shot_job = cron.add_job(
        name="Past one-shot",
        schedule=CronSchedule(kind="at", at_ms=1),
        message="Old one-shot message",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
        delete_after_run=True,
    )
    cron.register_system_job(
        CronJob(
            id="heartbeat",
            name="heartbeat",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(kind="system_event"),
        )
    )
    session_manager = _seed_session(tmp_path, key="websocket:abc")
    external_session = Session(key="weixin:wx-chat")
    external_session.add_message("user", "Scheduled cron job triggered")
    session_manager.save(external_session)
    channel = _ch(
        bus,
        session_manager=session_manager,
        cron_service=cron,
        cron_pending_job_ids=lambda key: {user_job.id} if key == "websocket:abc" else set(),
        port=port,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get(f"{base_url}/api/webui/automations")
        assert deny.status_code == 401, deny.text

        boot = await _http_get(f"{base_url}/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/automations",
            headers=auth,
        )
        assert resp.status_code == 200
        assert "wx-chat" not in resp.text
        assert "unified:default" not in resp.text
        body = resp.json()
        by_id = {job["id"]: job for job in body["jobs"]}
        assert by_id[user_job.id]["protected"] is False
        assert by_id[user_job.id]["state"]["pending"] is True
        assert by_id[user_job.id]["state"]["run_history"] == []
        assert by_id[user_job.id]["origin"]["session_key"] == "websocket:abc"
        assert by_id[user_job.id]["origin"]["preview"] == "hi"
        assert "session_key" not in by_id[incomplete_job.id]["payload"]
        assert "origin_channel" not in by_id[incomplete_job.id]["payload"]
        assert "origin_chat_id" not in by_id[incomplete_job.id]["payload"]
        assert by_id[incomplete_job.id]["origin"] is None
        assert "session_key" not in by_id[external_job.id]["payload"]
        assert "origin_channel" not in by_id[external_job.id]["payload"]
        assert "origin_chat_id" not in by_id[external_job.id]["payload"]
        assert by_id[external_job.id]["origin"]["channel"] == "weixin"
        assert "session_key" not in by_id[external_job.id]["origin"]
        assert "chat_id" not in by_id[external_job.id]["origin"]
        assert by_id[external_job.id]["origin"]["preview"] == ""
        assert by_id["heartbeat"]["protected"] is True

        updated = await _http_get(
            f"{base_url}/api/webui/automations/update?id={user_job.id}",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": json.dumps(
                    {
                        "name": "Daily quiz",
                        "message": "Ask the daily quiz",
                        "schedule": {
                            "kind": "cron",
                            "expr": "0 9 * * *",
                            "tz": "UTC",
                        },
                    }
                ),
            },
        )
        assert updated.status_code == 200
        by_id = {job["id"]: job for job in updated.json()["jobs"]}
        assert by_id[user_job.id]["name"] == "Daily quiz"
        assert by_id[user_job.id]["payload"]["message"] == "Ask the daily quiz"
        assert by_id[user_job.id]["schedule"]["kind"] == "cron"
        assert by_id[user_job.id]["schedule"]["expr"] == "0 9 * * *"
        assert by_id[user_job.id]["schedule"]["tz"] == "UTC"

        unicode_update = await _http_get(
            f"{base_url}/api/webui/automations/update?id={user_job.id}",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": quote(
                    json.dumps(
                        {
                            "name": "每日测验",
                            "message": "问今日测验",
                        },
                        ensure_ascii=False,
                    ),
                    safe="",
                ),
            },
        )
        assert unicode_update.status_code == 200
        assert cron.get_job(user_job.id).name == "每日测验"
        assert cron.get_job(user_job.id).payload.message == "问今日测验"

        malformed_update = await _http_get(
            f"{base_url}/api/webui/automations/update?id={user_job.id}",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": json.dumps({"message": ["bad"]}),
            },
        )
        assert malformed_update.status_code == 400
        assert cron.get_job(user_job.id).payload.message == "问今日测验"

        invalid_cron_update = await _http_get(
            f"{base_url}/api/webui/automations/update?id={user_job.id}",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": json.dumps(
                    {"schedule": {"kind": "cron", "expr": "not a cron", "tz": "UTC"}}
                ),
            },
        )
        assert invalid_cron_update.status_code == 400
        assert cron.get_job(user_job.id).schedule.expr == "0 9 * * *"

        past_one_shot_update = await _http_get(
            f"{base_url}/api/webui/automations/update?id={past_one_shot_job.id}",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": json.dumps(
                    {
                        "message": "Updated one-shot message",
                        "schedule": {"kind": "at", "at_ms": 1},
                    }
                ),
            },
        )
        assert past_one_shot_update.status_code == 200
        assert cron.get_job(past_one_shot_job.id).payload.message == "Updated one-shot message"
        assert cron.get_job(past_one_shot_job.id).schedule.at_ms == 1

        protected_update = await _http_get(
            f"{base_url}/api/webui/automations/update?id=heartbeat",
            headers={
                **auth,
                "X-Nanobot-Automation-Values": json.dumps({"name": "bad"}),
            },
        )
        assert protected_update.status_code == 403

        disabled = await _http_get(
            f"{base_url}/api/webui/automations/disable?id={user_job.id}",
            headers=auth,
        )
        assert disabled.status_code == 200
        by_id = {job["id"]: job for job in disabled.json()["jobs"]}
        assert by_id[user_job.id]["enabled"] is False

        disabled_run = await _http_get(
            f"{base_url}/api/webui/automations/run?id={user_job.id}",
            headers=auth,
        )
        assert disabled_run.status_code == 409

        unbound_run = await _http_get(
            f"{base_url}/api/webui/automations/run?id={incomplete_job.id}",
            headers=auth,
        )
        assert unbound_run.status_code == 409
        assert "no linked chat" in unbound_run.text

        unbound_enable = await _http_get(
            f"{base_url}/api/webui/automations/enable?id={incomplete_job.id}",
            headers=auth,
        )
        assert unbound_enable.status_code == 409
        assert "no linked chat" in unbound_enable.text

        protected_delete = await _http_get(
            f"{base_url}/api/webui/automations/delete?id=heartbeat",
            headers=auth,
        )
        assert protected_delete.status_code == 403
        protected_disable = await _http_get(
            f"{base_url}/api/webui/automations/disable?id=heartbeat",
            headers=auth,
        )
        assert protected_disable.status_code == 403
        protected_run = await _http_get(
            f"{base_url}/api/webui/automations/run?id=heartbeat",
            headers=auth,
        )
        assert protected_run.status_code == 403

        enabled = await _http_get(
            f"{base_url}/api/webui/automations/enable?id={user_job.id}",
            headers=auth,
        )
        assert enabled.status_code == 200
        by_id = {job["id"]: job for job in enabled.json()["jobs"]}
        assert by_id[user_job.id]["enabled"] is True

        deleted = await _http_get(
            f"{base_url}/api/webui/automations/delete?id={user_job.id}",
            headers=auth,
        )
        assert deleted.status_code == 200
        assert user_job.id not in {job["id"] for job in deleted.json()["jobs"]}
        assert "heartbeat" in {job["id"] for job in deleted.json()["jobs"]}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_blocks_when_bound_automation_exists(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    cron = CronService(tmp_path / "cron" / "jobs.json")
    cron.add_job(
        name="Daily check",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        message="Check the repo",
        session_key="websocket:doomed",
        origin_channel="websocket",
        origin_chat_id="doomed",
    )
    channel = _ch(bus, session_manager=sm, cron_service=cron, port=29915)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29915/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        resp = await _http_get(
            "http://127.0.0.1:29915/api/sessions/websocket:doomed/delete",
            headers=auth,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is False
        assert body["blocked_by_automations"] is True
        assert [job["name"] for job in body["automations"]] == ["Daily check"]
        assert path.exists()
        assert cron.list_bound_cron_jobs_for_session("websocket:doomed")
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_archive_allows_reminder_and_keeps_it_scheduled(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:reminder-chat")
    cron = CronService(tmp_path / "cron" / "jobs.json")
    reminder = cron.add_job(
        name="找wanghongjun",
        schedule=CronSchedule(kind="at", at_ms=4_091_735_700_000),
        message="提醒我找wanghongjun",
        result_type="none",
        session_key="websocket:reminder-chat",
        origin_channel="websocket",
        origin_chat_id="reminder-chat",
    )
    channel = _ch(bus, session_manager=sm, cron_service=cron, port=29919)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29919/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            "http://127.0.0.1:29919/api/sessions/websocket:reminder-chat/archive",
            headers=auth,
        )

        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        assert cron.get_job(reminder.id) is not None
        managed = await _http_get(
            "http://127.0.0.1:29919/api/data-management/archives",
            headers=auth,
        )
        assert managed.status_code == 200
        assert [row["session_key"] for row in managed.json()["archived_sessions"]] == [
            "websocket:reminder-chat"
        ]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_archive_and_purge_ignore_completed_one_time_conversation_job(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:legacy-reminder-chat")
    cron = CronService(tmp_path / "cron" / "jobs.json")
    cron._running = True
    created = cron.add_job(
        name="合并代码提醒",
        schedule=CronSchedule(kind="at", at_ms=4_091_735_700_000),
        message="提醒用户：合并代码",
        session_key="websocket:legacy-reminder-chat",
        origin_channel="websocket",
        origin_chat_id="legacy-reminder-chat",
    )
    completed = cron.get_job(created.id)
    assert completed is not None
    completed.enabled = False
    completed.state.next_run_at_ms = None
    completed.state.last_run_at_ms = 1_700_000_000_000
    completed.state.last_status = "ok"
    completed.state.run_history.append(CronRunRecord(
        run_at_ms=1_700_000_000_000,
        status="ok",
        run_id="completed-run",
    ))
    cron._save_store()
    port = _free_port()
    channel = _ch(bus, session_manager=sm, cron_service=cron, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/sessions/websocket:legacy-reminder-chat/archive",
            headers=auth,
        )

        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        assert cron.get_job(completed.id) is not None

        purged = await _http_get(
            f"http://127.0.0.1:{port}/api/sessions/websocket:legacy-reminder-chat/purge",
            headers=auth,
        )

        assert purged.status_code == 200
        assert purged.json()["purged"] is True
        assert cron.get_job(completed.id) is None
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_can_cascade_bound_automations(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    cron = CronService(tmp_path / "cron" / "jobs.json")
    cron.add_job(
        name="Daily check",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        message="Check the repo",
        session_key="websocket:doomed",
        origin_channel="websocket",
        origin_chat_id="doomed",
    )
    cron.add_job(
        name="Legacy same target",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        message="Legacy job remains",
        channel="websocket",
        to="doomed",
    )
    channel = _ch(bus, session_manager=sm, cron_service=cron, port=29916)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29916/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        resp = await _http_get(
            "http://127.0.0.1:29916/api/sessions/websocket:doomed/delete?delete_automations=true",
            headers=auth,
        )

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert path.exists()
        assert cron.list_bound_cron_jobs_for_session("websocket:doomed") == []
        assert cron.list_jobs(include_disabled=True) == []
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_blocks_origin_automation_when_unified_enabled(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    cron = CronService(tmp_path / "cron" / "jobs.json")
    cron.add_job(
        name="Chat daily check",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        message="Check this chat",
        session_key="websocket:doomed",
        origin_channel="websocket",
        origin_chat_id="doomed",
    )
    channel = _ch(
        bus,
        session_manager=sm,
        cron_service=cron,
        port=29918,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29918/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        resp = await _http_get(
            "http://127.0.0.1:29918/api/sessions/websocket:doomed/delete",
            headers=auth,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is False
        assert body["blocked_by_automations"] is True
        assert [job["name"] for job in body["automations"]] == ["Chat daily check"]
        assert path.exists()
        assert [job.name for job in cron.list_bound_cron_jobs_for_session("websocket:doomed")] == [
            "Chat daily check"
        ]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_accept_percent_encoded_websocket_keys(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path, key="websocket:encoded-key")
    channel = _ch(bus, session_manager=sm, port=29910)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29910/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        msgs = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        assert msgs.json()["key"] == "websocket:encoded-key"

        path = sm._get_session_path("websocket:encoded-key")
        assert path.exists()
        deleted = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/delete",
            headers=auth,
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_webui_thread_resigns_assistant_media_urls(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.webui.transcript import append_transcript_object

    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    media_root = tmp_path / "media"
    websocket_media = media_root / "websocket"
    websocket_media.mkdir(parents=True)
    external = tmp_path / "clip.mp4"
    external.write_bytes(b"video")

    def fake_media_dir(channel: str | None = None) -> Path:
        return websocket_media if channel == "websocket" else media_root

    monkeypatch.setattr("nanobot.channels.websocket.get_media_dir", fake_media_dir)

    append_transcript_object(
        "websocket:video-replay",
        {"event": "user", "chat_id": "video-replay", "text": "make a video"},
    )
    append_transcript_object(
        "websocket:video-replay",
        {
            "event": "message",
            "chat_id": "video-replay",
            "text": "video ready",
            "media": [str(external)],
            "media_urls": [{"url": "/api/media/old-sig/old-payload", "name": "clip.mp4"}],
        },
    )

    channel = _ch(bus, port=29914)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29914/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            "http://127.0.0.1:29914/api/sessions/websocket:video-replay/webui-thread",
            headers=auth,
        )
        assert resp.status_code == 200
        assistant = next(m for m in resp.json()["messages"] if m["role"] == "assistant")
        media = assistant["media"]
        assert media[0]["kind"] == "video"
        assert media[0]["name"] == "clip.mp4"
        assert media[0]["url"].startswith("/api/media/")
        assert media[0]["url"] != "/api/media/old-sig/old-payload"

        fetched = await _http_get(f"http://127.0.0.1:29914{media[0]['url']}")
        assert fetched.status_code == 200
        assert fetched.content == b"video"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_non_websocket_keys(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_many(
        tmp_path,
        [
            "websocket:kept",
            "cli:direct",
            "slack:C123",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29909)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29909/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # The webui list already hides non-websocket sessions; handcrafted URLs
        # should hit the same boundary rather than exposing or deleting them.
        msgs = await _http_get(
            "http://127.0.0.1:29909/api/sessions/cli:direct/messages",
            headers=auth,
        )
        assert msgs.status_code == 404

        doomed = sm._get_session_path("slack:C123")
        assert doomed.exists()
        deny_delete = await _http_get(
            "http://127.0.0.1:29909/api/sessions/slack:C123/delete",
            headers=auth,
        )
        assert deny_delete.status_code == 404
        assert doomed.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_invalid_key(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29904)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29904/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # Invalid characters in the key -> regex match fails -> 404
        # (route doesn't match, falls through to channel 404).
        resp = await _http_get(
            "http://127.0.0.1:29904/api/sessions/bad%20key/messages",
            headers=auth,
        )
        assert resp.status_code in {400, 404}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_serves_index_when_dist_present(
    bus: MagicMock, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>nbweb</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    sm = _seed_session(tmp_path / "ws_state")
    channel = _ch(bus, session_manager=sm, static_dist_path=dist, port=29905)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Bare ``GET /`` is a browser opening the app: it must return the SPA
        # index.html, not the WS-upgrade handler's 401/426.
        root = await _http_get("http://127.0.0.1:29905/")
        assert root.status_code == 200
        assert "nbweb" in root.text
        asset = await _http_get("http://127.0.0.1:29905/favicon.svg")
        assert asset.status_code == 200
        assert "<svg" in asset.text
        # Unknown SPA route falls back to index.html.
        spa = await _http_get("http://127.0.0.1:29905/sessions/abc")
        assert spa.status_code == 200
        assert "nbweb" in spa.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_rejects_path_traversal(
    bus: MagicMock, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    channel = _ch(bus, static_dist_path=dist, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29906/../secret.txt")
        # Normalized by httpx into /secret.txt → falls back to index.html, not 'classified'.
        assert "classified" not in resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_route_returns_404(bus: MagicMock) -> None:
    channel = _ch(bus, port=29907)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29907/api/unknown")
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_api_token_pool_purges_expired(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29908)
    # Don't start a server — directly inject and validate.
    import time as _time
    channel.gateway.tokens.api_tokens["expired"] = _time.monotonic() - 1
    channel.gateway.tokens.api_tokens["live"] = _time.monotonic() + 60

    class _FakeReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer expired"}

    assert channel.gateway.tokens.check_api_token(_FakeReq()) is False

    class _LiveReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer live"}

    assert channel.gateway.tokens.check_api_token(_LiveReq()) is True


class _FakeConn:
    """Minimal connection stub with a configurable remote_address."""

    def __init__(self, remote_address: tuple[str, int]):
        self.remote_address = remote_address

    def respond(self, status: int, body: str) -> Any:
        from websockets.http11 import Response

        return Response(status=status, body=body.encode())


class _FakeReq:
    """Minimal request stub with configurable headers."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


_REMOTE = _FakeConn(("192.168.1.5", 12345))
_LOCAL = _FakeConn(("127.0.0.1", 12345))
_NO_HEADERS = _FakeReq()


def test_wildcard_host_without_auth_raises_on_startup(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="0.0.0.0")


def test_wildcard_host_with_token_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", token="my-token")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_host_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_ipv6_without_auth_raises(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="::")


def test_wildcard_ipv6_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="::", tokenIssueSecret="s3cret")
    resp = channel.gateway.http._handle_bootstrap(
        _REMOTE, _FakeReq({"X-Nanobot-Auth": "s3cret"})
    )
    assert resp.status_code == 200


def test_bootstrap_accepts_static_token_as_secret(bus: MagicMock) -> None:
    """When only token (not token_issue_secret) is set, bootstrap accepts it."""
    channel = _ch(bus, host="0.0.0.0", token="static-tok")
    resp = channel.gateway.http._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer static-tok"})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_bootstrap_ws_url_uses_forwarded_https_host(bus: MagicMock) -> None:
    channel = _ch(bus, host="127.0.0.1", port=29931)
    resp = channel.gateway.http._handle_bootstrap(
        _LOCAL,
        _FakeReq({"Host": "nanobot.example", "X-Forwarded-Proto": "https"}),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ws_url"] == "wss://nanobot.example/"


def test_localhost_without_auth_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="127.0.0.1")
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200


def test_bootstrap_exposes_agent_and_mcp_readiness(bus: MagicMock) -> None:
    channel = _ch(
        bus,
        host="127.0.0.1",
        runtime_ready=lambda: False,
        runtime_mcp_status=lambda: "warming",
    )
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["agent_ready"] is False
    assert body["mcp_status"] == "warming"


def test_bootstrap_prefers_runtime_model_name(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.webui.ws_http._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "  live/model  ")
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "live/model"


def test_bootstrap_falls_back_when_runtime_returns_empty(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.webui.ws_http._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "   ")
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_falls_back_when_runtime_raises(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.webui.ws_http._default_model_name_from_config",
        lambda: "from-disk",
    )

    def boom():
        raise RuntimeError("resolver failed")

    channel = _ch(bus, host="127.0.0.1", runtime_model_name=boom)
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_rejects_wrong_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="correct")
    resp = channel.gateway.http._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer wrong"})
    )
    assert resp.status_code == 401


def test_bootstrap_accepts_remote_with_valid_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel.gateway.http._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer s3cret"})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_bootstrap_accepts_x_nanobot_auth_header(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel.gateway.http._handle_bootstrap(
        _REMOTE, _FakeReq({"X-Nanobot-Auth": "s3cret"})
    )
    assert resp.status_code == 200


def test_bootstrap_secret_also_enforced_on_localhost(bus: MagicMock) -> None:
    """When secret is set, even localhost must provide it (reverse-proxy safety)."""
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel.gateway.http._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 401

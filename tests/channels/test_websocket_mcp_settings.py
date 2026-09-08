"""MCP management uses structured frames on the existing authenticated channel."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from nanobot.agent.tools import mcp as mcp_runtime
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.config import loader
from nanobot.config.schema import Config
from nanobot.webui import mcp_editor
from nanobot.webui.gateway_services import build_gateway_services


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    loader.save_config(Config())
    bus = MessageBus()
    config = WebSocketConfig(allow_from=["desktop"])
    gateway = build_gateway_services(
        config=config, bus=bus, session_manager=None, static_dist_path=None,
        workspace_path=tmp_path, default_restrict_to_workspace=False,
        runtime_model_name=None, runtime_surface="desktop", runtime_capabilities_overrides=None,
    )
    monkeypatch.setattr(mcp_runtime, "request_mcp_reload", AsyncMock(return_value={"ok": True}))
    return WebSocketChannel(config.model_dump(), bus, gateway=gateway)


async def test_real_websocket_large_save_and_correlated_validation(channel):
    async def handler(connection):
        async for raw in connection:
            await channel._dispatch_envelope_inner(connection, "desktop", json.loads(raw))
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            await client.send(json.dumps({"type": "mcp_settings", "request_id": "large", "action": "save", "values": {
                "name": "docs", "display_name": "Docs", "url": "https://example.com/mcp", "headers_patch": {"Authorization": "x" * 16000},
            }}))
            result = json.loads(await asyncio.wait_for(client.recv(), 5))
            assert result["request_id"] == "large"
            assert result["result"]["last_action"]["saved"] is True
            await client.send(json.dumps({"type": "mcp_settings", "request_id": "invalid", "action": [], "values": {}}))
            result = json.loads(await asyncio.wait_for(client.recv(), 5))
            assert result["request_id"] == "invalid"
            assert result["status"] == 400
    await channel.stop()


async def test_permissions_and_size_validation(channel):
    connection = object()
    channel._send_event = AsyncMock()
    await channel._dispatch_envelope_inner(connection, "stranger", {"type": "mcp_settings", "request_id": "denied", "action": "list"})
    assert channel._send_event.call_args.kwargs["status"] == 403
    await channel._dispatch_envelope_inner(connection, "desktop", {"type": "mcp_settings", "request_id": "oversize", "action": "save", "values": {"config": "x" * (1024 * 1024)}})
    await asyncio.gather(*channel._mcp_settings_tasks)
    assert channel._send_event.call_args.kwargs["status"] == 400
    assert loader.load_config().tools.mcp_servers == {}
    await channel.stop()


async def test_probe_cancellation_is_owned_by_connection_and_does_not_block_list(channel, monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def action(kind, values, reload):
        if kind == "probe":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return {"presets": [], "installed_count": 0}
    monkeypatch.setattr(mcp_editor, "mcp_editor_action", action)
    channel._send_event = AsyncMock()
    connection, other = object(), object()
    await channel._dispatch_envelope_inner(connection, "desktop", {"type": "mcp_settings", "request_id": "probe", "action": "probe"})
    await asyncio.wait_for(started.wait(), 1)
    await channel._dispatch_envelope_inner(connection, "desktop", {"type": "mcp_settings", "request_id": "list", "action": "list"})
    await asyncio.sleep(0)
    assert channel._send_event.call_args.kwargs["request_id"] == "list"
    await channel._dispatch_envelope_inner(other, "desktop", {"type": "mcp_settings_cancel", "request_id": "probe"})
    assert not stopped.is_set()
    await channel._dispatch_envelope_inner(connection, "desktop", {"type": "mcp_settings_cancel", "request_id": "probe"})
    await asyncio.wait_for(stopped.wait(), 1)
    await channel.stop()
    assert not channel._mcp_settings_tasks

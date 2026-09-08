"""Desktop MCP configuration regression tests using an isolated config file."""

import json
import sys
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock

import pytest

from nanobot.config import loader
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.runtime import mcp_diagnostics
from nanobot.webui import mcp_editor as editor
from nanobot.webui.mcp_presets_api import McpPresetError


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    monkeypatch.setattr(mcp_diagnostics, "_live", {})
    loader.save_config(Config())


def remote(**values):
    return {"display_name": "Company docs", "name": "docs", "url": "https://example.com/mcp", **values}


@pytest.fixture
def reload_mcp():
    return AsyncMock(return_value={"ok": True, "requires_restart": False})


async def test_large_save_chinese_label_and_duplicate_does_not_overwrite(reload_mcp):
    values = remote(display_name="公司知识库", headers_patch={"Authorization": "x" * 16000})
    result = await editor.mcp_editor_action("save", values, reload_mcp)
    assert result["last_action"]["saved"] is True
    row = next(row for row in result["presets"] if row["name"] == "docs")
    assert row["display_name"] == "公司知识库"
    assert row["connection"]["header_keys"] == ["Authorization"]
    assert "x" * 16000 not in json.dumps(result)
    with pytest.raises(McpPresetError) as error:
        await editor.mcp_editor_action("save", remote(), reload_mcp)
    assert error.value.status == 409
    assert loader.load_config().tools.mcp_servers["docs"].headers["Authorization"] == "x" * 16000


async def test_edit_keeps_secrets_and_unchanged_live_status(reload_mcp):
    values = remote(url="https://example.com/mcp?token=secret", headers_patch={"A": "secret", "B": "remove"})
    await editor.mcp_editor_action("save", values, reload_mcp)
    mcp_diagnostics.record_connection("docs", "connected", "Connected", ["mcp_docs_search"])
    await editor.mcp_editor_action("save", {"original_name": "docs", "display_name": "Company docs"}, reload_mcp)
    assert mcp_diagnostics.connection_snapshot("docs")["status"] == "connected"
    await editor.mcp_editor_action("save", {"original_name": "docs", "display_name": "Renamed", "headers_patch": {"B": None}}, reload_mcp)
    cfg = loader.load_config().tools.mcp_servers["docs"]
    assert cfg.headers == {"A": "secret"}
    assert cfg.url.endswith("token=secret")


async def test_import_preview_and_atomic_conflict_resolution(reload_mcp):
    await editor.mcp_editor_action("save", remote(), reload_mcp)
    imported = {"config": json.dumps({"mcpServers": {
        "new-server": {"command": sys.executable},
        "docs": {"url": "https://example.org/mcp"},
        "oauth": {"url": "https://example.org/mcp", "oauth": {}},
    }})}
    preview = await editor.mcp_editor_action("preview-import", imported, reload_mcp)
    rows = {row["name"]: row for row in preview["import_preview"]}
    assert rows["docs"]["conflict"] is True
    assert "oauth" in rows["oauth"]["errors"][0]
    with pytest.raises(McpPresetError):
        await editor.mcp_editor_action("import", imported, reload_mcp)
    assert set(loader.load_config().tools.mcp_servers) == {"docs"}
    await editor.mcp_editor_action("import", {**imported, "decisions": {
        "docs": {"action": "rename", "name": "docs-2"}, "oauth": {"action": "skip"},
    }}, reload_mcp)
    assert set(loader.load_config().tools.mcp_servers) == {"docs", "docs-2", "new-server"}
    await editor.mcp_editor_action("import", {"config": json.dumps({"docs": {"url": "https://example.org/mcp"}}), "decisions": {"docs": {"action": "replace"}}}, reload_mcp)
    assert loader.load_config().tools.mcp_servers["docs"].url == "https://example.org/mcp"


async def test_import_variables_disabled_timeouts_and_tool_scope(reload_mcp, monkeypatch):
    monkeypatch.delenv("MCP_EDITOR_TEST_SECRET", raising=False)
    values = {"config": json.dumps({"mcpServers": {"local": {
        "command": sys.executable, "args": ["-m", "server with spaces"],
        "env": {"TOKEN": "${env:MCP_EDITOR_TEST_SECRET}"}, "disabled": True,
        "connectTimeout": 22, "toolTimeout": 90, "enabledTools": ["search"],
    }}})}
    await editor.mcp_editor_action("import", values, reload_mcp)
    cfg = loader.load_config().tools.mcp_servers["local"]
    assert not cfg.enabled
    assert (cfg.connect_timeout, cfg.tool_timeout, cfg.enabled_tools) == (22, 90, ["search"])
    assert cfg.env == {"TOKEN": "${MCP_EDITOR_TEST_SECRET}"}
    loader.resolve_config_env_vars(loader.load_config())
    with pytest.raises(McpPresetError, match="environment variables"):
        await editor.mcp_editor_action("toggle", {"name": "local", "enabled": True}, reload_mcp)
    assert not loader.load_config().tools.mcp_servers["local"].enabled
    monkeypatch.setenv("MCP_EDITOR_TEST_SECRET", "available")
    await editor.mcp_editor_action("toggle", {"name": "local", "enabled": True}, reload_mcp)
    assert loader.load_config().tools.mcp_servers["local"].enabled


@pytest.mark.parametrize("raw", [
    '{"x":{"command":"python"},"x":{"command":"node"}}',
    '{"x":{"command":"python","cwd":"${workspaceFolder}"}}',
    '{"x":{"url":"https://example.com","type":{}}}',
])
async def test_invalid_import_never_changes_config(raw, reload_mcp):
    with pytest.raises(McpPresetError):
        await editor.mcp_editor_action("import", {"config": raw}, reload_mcp)
    assert loader.load_config().tools.mcp_servers == {}


async def test_probe_does_not_save_or_replace_live_diagnostics(monkeypatch, reload_mcp):
    closed = []
    async def connect(servers, registry):
        name = next(iter(servers))
        mcp_diagnostics.record_connection(name, "connected", "Connected", ["mcp_docs_search"])
        stack = AsyncExitStack()
        stack.callback(lambda: closed.append(True))
        return {name: stack}
    monkeypatch.setattr(editor, "connect_mcp_servers", connect)
    mcp_diagnostics.record_connection("docs", "failed", "Live connection failed")
    result = await editor.mcp_editor_action("probe", remote(), reload_mcp)
    assert result["probe"]["ok"] is True
    assert closed == [True]
    assert loader.load_config().tools.mcp_servers == {}
    assert mcp_diagnostics.connection_snapshot("docs")["status"] == "failed"
    reload_mcp.assert_not_called()


async def test_real_stdio_probe_and_disabled_server(reload_mcp):
    from nanobot.agent.tools.mcp import connect_mcp_servers
    from nanobot.agent.tools.registry import ToolRegistry

    disabled = MCPServerConfig(command="nonexistent-mcp-editor-test", enabled=False)
    assert await connect_mcp_servers({"disabled": disabled}, ToolRegistry()) == {}
    script = 'from mcp.server.fastmcp import FastMCP; app = FastMCP("editor-test"); app.tool(name="ping")(lambda: "ok"); app.run(transport="stdio")'
    result = await editor.mcp_editor_action("probe", {
        "display_name": "SDK test", "name": "sdk", "command": sys.executable,
        "args": ["-c", script], "connect_timeout": 10,
    }, reload_mcp)
    assert result["probe"]["ok"] is True
    assert result["probe"]["tool_names"]
    assert loader.load_config().tools.mcp_servers == {}


async def test_ssrf_remains_blocked(reload_mcp):
    result = await editor.mcp_editor_action("probe", remote(url="http://169.254.169.254/mcp"), reload_mcp)
    assert result["probe"]["ok"] is False
    assert loader.load_config().tools.mcp_servers == {}


async def test_existing_mixed_case_identifier_survives_edit_toggle_and_remove(reload_mcp):
    config = loader.load_config()
    config.tools.mcp_servers["MyMCP"] = MCPServerConfig(url="https://example.com/mcp")
    loader.save_config(config)
    await editor.mcp_editor_action("save", {"original_name": "MyMCP", "name": "MyMCP", "display_name": "Renamed"}, reload_mcp)
    await editor.mcp_editor_action("toggle", {"name": "MyMCP", "enabled": False}, reload_mcp)
    assert set(loader.load_config().tools.mcp_servers) == {"MyMCP"}
    assert not loader.load_config().tools.mcp_servers["MyMCP"].enabled
    await editor.mcp_editor_action("remove", {"name": "MyMCP"}, reload_mcp)
    assert loader.load_config().tools.mcp_servers == {}


async def test_live_connection_takes_precedence_over_executable_path_hint(reload_mcp):
    await editor.mcp_editor_action("save", {
        "name": "local", "display_name": "Local docs", "command": "runtime-provided-mcp-command",
    }, reload_mcp)
    mcp_diagnostics.record_connection("local", "connected", "Connected", ["mcp_local_search"])
    result = await editor.mcp_editor_action("list", {}, reload_mcp)
    row = next(row for row in result["presets"] if row["name"] == "local")
    assert row["connection_state"] == "connected"
    assert row["available"] is True
    assert not row.get("error")


async def test_real_save_reconnect_disable_and_reenable_lifecycle():
    from nanobot.agent.tools.mcp import reload_servers
    from nanobot.agent.tools.registry import ToolRegistry

    class State:
        def __init__(self):
            self._mcp_servers = {}
            self._mcp_stacks = {}

    state, registry = State(), ToolRegistry()
    async def reload(name):
        return await reload_servers(state, registry, force_server=name)

    script = 'from mcp.server.fastmcp import FastMCP; app = FastMCP("editor-test"); app.tool(name="ping")(lambda: "ok"); app.run(transport="stdio")'
    try:
        result = await editor.mcp_editor_action("save", {
            "name": "sdk", "display_name": "SDK", "command": sys.executable,
            "args": ["-c", script], "connect_timeout": 10,
        }, reload)
        assert result["last_action"]["ok"] is True
        assert registry.has("mcp_sdk_ping")
        original = state._mcp_stacks["sdk"]
        await editor.mcp_editor_action("reconnect", {"name": "sdk"}, reload)
        assert state._mcp_stacks["sdk"] is not original
        await editor.mcp_editor_action("toggle", {"name": "sdk", "enabled": False}, reload)
        assert not state._mcp_stacks
        assert not registry.has("mcp_sdk_ping")
        assert "sdk" in loader.load_config().tools.mcp_servers
        await editor.mcp_editor_action("toggle", {"name": "sdk", "enabled": True}, reload)
        assert registry.has("mcp_sdk_ping")
    finally:
        for stack in state._mcp_stacks.values():
            await stack.aclose()

"""Tests for SubagentManager."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.filesystem import FileToolsConfig
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.storage.state import StateStore


@pytest.mark.asyncio
async def test_subagent_uses_tool_loader():
    """Verify subagent registers tools via ToolLoader, not hard-coded imports."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=Path("/tmp"),
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    tools = sm._build_tools()
    assert tools.has("read_file")
    assert tools.has("write_file")
    assert not tools.has("message")
    assert not tools.has("spawn")


def test_workflow_members_can_read_supplements_but_cannot_overwrite_reports(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    manager = SubagentManager(provider=provider, workspace=tmp_path,
                              bus=MessageBus(), max_tool_result_chars=16000)
    scoped = manager._workflow_member_tools(manager._build_tools(), {})
    assert scoped.has("read_file")
    assert not scoped.has("write_file")
    assert not scoped.has("edit_file")
    assert not scoped.has("exec")


@pytest.mark.asyncio
async def test_subagent_build_tools_isolates_file_read_state(tmp_path):
    """Each spawned subagent needs a fresh file-state cache."""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )

    first_read = sm._build_tools().get("read_file")
    second_read = sm._build_tools().get("read_file")

    assert first_read is not second_read
    assert (await first_read.execute(path="note.txt")).startswith("1| hello")
    second_result = await second_read.execute(path="note.txt")
    assert second_result.startswith("1| hello")
    assert "File unchanged" not in second_result


def test_subagent_respects_file_tool_toggle(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
        tools_config=ToolsConfig(file=FileToolsConfig(enable=False)),
    )

    tools = sm._build_tools()

    file_tools = {
        "apply_patch",
        "edit_file",
        "find_files",
        "grep",
        "list_dir",
        "read_file",
        "write_file",
    }
    assert file_tools.isdisjoint(tools.tool_names)


def test_expert_team_inherits_only_its_configured_mcp_tools(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    parent_tools = ToolRegistry()
    juyuan = MagicMock()
    juyuan.name = "mcp_juyuan_company_financials"
    unrelated = MagicMock()
    unrelated.name = "mcp_browser_snapshot"
    parent_tools.register(juyuan)
    parent_tools.register(unrelated)
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=16_000,
        parent_tools=parent_tools,
    )

    tools = manager._build_tools(expert_team={
        "mcp_presets": [{"name": "juyuan", "configured": True}],
    })

    assert tools.get("mcp_juyuan_company_financials") is juyuan
    assert not tools.has("mcp_browser_snapshot")


def test_asset_team_inherits_configured_three_source_and_fallback_mcp_tools(tmp_path):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    parent_tools = ToolRegistry()
    names = (
        "mcp_hexin-ifind-ds-stock-mcp_quote",
        "mcp_juyuan_company_financials",
        "mcp_caihui_mcp_company_financials",
        "mcp_anysearch_search",
    )
    tools_by_name = {}
    for name in names:
        tool = MagicMock()
        tool.name = name
        parent_tools.register(tool)
        tools_by_name[name] = tool
    unrelated = MagicMock()
    unrelated.name = "mcp_playwright_browser_snapshot"
    parent_tools.register(unrelated)
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=16_000,
        parent_tools=parent_tools,
    )

    tools = manager._build_tools(expert_team={
        "mcp_presets": [
            {"name": "hexin-ifind-ds-stock-mcp", "configured": True},
            {"name": "juyuan", "configured": True},
            {"name": "caihui_mcp", "configured": True},
            {"name": "anysearch", "configured": True},
        ],
    })

    for name, tool in tools_by_name.items():
        assert tools.get(name) is tool
    assert not tools.has("mcp_playwright_browser_snapshot")


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_turn", [False, True])
async def test_expert_team_member_terminal_result_is_registered_as_artifact(tmp_path, newer_turn):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    state = StateStore(
        tmp_path / ".nanobot" / "state.sqlite",
        default_workspace=tmp_path,
    )
    project = state.ensure_project(tmp_path)
    session = state.bind_session("websocket:artifact-chat", project.id)
    state.project_event(session.session_key, {
        "schema_version": 1,
        "event": "user",
        "event_id": "artifact-user",
        "event_seq": 1,
        "recorded_at": 1,
        "project_id": project.id,
        "session_id": session.id,
        "turn_id": "turn-artifact",
        "text": "research",
    })
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=16_000,
    )

    if newer_turn:
        state.project_event(session.session_key, {
            "event": "turn_end", "event_id": "artifact-end", "event_seq": 2,
            "recorded_at": 2, "project_id": project.id, "session_id": session.id,
            "turn_id": "turn-artifact", "finish_reason": "cancelled",
        })
        state.project_event(session.session_key, {
            "event": "user", "event_id": "next-user", "event_seq": 3,
            "recorded_at": 3, "project_id": project.id, "session_id": session.id,
            "turn_id": "next-turn", "text": "new task",
        })

    relative = await manager._persist_expert_team_member_artifact(
        content="# 结论\n\n数据来源：交易所公告",
        label="financial-analyst",
        run_id="run-artifact",
        origin={
            "channel": "websocket",
            "chat_id": "artifact-chat",
            "session_key": session.session_key,
            "turn_id": "turn-artifact",
        },
        workspace_scope=None,
        delivery_status="completed",
    )

    assert relative == (
        "reports/.team-runs/run-artifact/members/financial-analyst.md"
    )
    assert (tmp_path / relative).is_file()
    artifacts = state.list_session_artifacts(session.session_key)
    assert any(item.relative_path == relative for item in artifacts)
    with sqlite3.connect(state.path) as connection:
        assert connection.execute("SELECT turn_id FROM artifact_links").fetchall() == [("turn-artifact",)]


@pytest.mark.asyncio
async def test_role_artifact_write_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    manager = SubagentManager(provider=provider, workspace=tmp_path,
                              bus=MessageBus(), max_tool_result_chars=16_000)
    artifact = tmp_path / "reports/.team-runs/run/members/financial-analyst.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("previous complete result", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    result = await manager._persist_expert_team_member_artifact(
        content="new result", label="financial-analyst", run_id="run",
        origin={"channel": "websocket", "chat_id": "c1"},
        workspace_scope=None, delivery_status="completed",
    )
    assert result is None
    assert artifact.read_text(encoding="utf-8") == "previous complete result"
    assert list(artifact.parent.glob("*.tmp")) == []

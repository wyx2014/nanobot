"""Tests for AgentLoop integration with AgentRunner: streaming, think-filter, error handling, subagent."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import (
    _generated_artifact_paths,
    _SingleRewriteAuditToolRegistry,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _make_loop(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


@pytest.mark.asyncio
async def test_state_run_routes_asset_team_to_runtime_graph_not_general_agent(tmp_path):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.bus.events import InboundMessage

    loop = _make_loop(tmp_path)
    loop._last_usage = {"total_tokens": 7}
    loop._run_asset_research_workflow = AsyncMock(return_value=(
        "graph delivered",
        ["write_file"],
        [{"role": "assistant", "content": "graph delivered"}],
        "completed",
        False,
    ))
    loop._run_agent_loop = AsyncMock()
    runtime_events = MagicMock()
    runtime_events.run_status_changed = AsyncMock()
    loop._runtime_events = MagicMock(return_value=runtime_events)
    session = MagicMock()
    session.metadata = {}
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-graph",
        content="A股 北方华创",
        metadata={
            "expert_team": {"id": "asset-research-team"},
            "expert_team_run_id": "run-graph",
            "_expert_team_turn_route": {
                "action": "run",
                "target": "北方华创",
            },
        },
    )
    ctx = TurnContext(
        msg=msg,
        session_key="websocket:chat-graph",
        state=TurnState.RUN,
        turn_id="turn-graph",
        session=session,
    )

    event = await loop._state_run(ctx)

    assert event == "ok"
    loop._run_asset_research_workflow.assert_awaited_once_with(ctx)
    loop._run_agent_loop.assert_not_awaited()
    assert ctx.final_content == "graph delivered"
    assert ctx.stop_reason == "completed"
    assert ctx.turn_usage == {"total_tokens": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing_target", "model_route_unavailable", "resume_without_prior_run"])
async def test_team_clarification_cannot_fall_through_to_agent_plan_or_search(tmp_path, reason):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.bus.events import InboundMessage

    loop = _make_loop(tmp_path)
    loop._run_agent_loop = AsyncMock(side_effect=AssertionError("general research must not run"))
    loop._run_asset_research_workflow = AsyncMock()
    loop._last_usage = {"total_tokens": 999}
    runtime_events = MagicMock()
    runtime_events.run_status_changed = AsyncMock()
    loop._runtime_events = MagicMock(return_value=runtime_events)
    session = MagicMock()
    session.metadata = {"expert_team": {"id": "asset-research-team"}}
    msg = InboundMessage(
        channel="websocket", sender_id="user", chat_id="anji", content="帮我分析下 安集科技 A股",
        metadata={
            "webui": True, "_expert_team_turn_suppressed": True,
            "_expert_team_turn_route": {"action": "clarify", "reason": reason},
        },
    )
    messages = [
        {"role": "system", "content": "Always make a plan and search financial data."},
        {"role": "user", "content": msg.content},
    ]
    ctx = TurnContext(msg=msg, session_key="websocket:anji", state=TurnState.RUN,
                      turn_id="turn-anji", session=session, initial_messages=messages)
    assert await loop._state_run(ctx) == "ok"
    loop._run_agent_loop.assert_not_awaited()
    loop._run_asset_research_workflow.assert_not_awaited()
    assert ctx.tools_used == []
    assert ctx.turn_usage == {}
    assert ctx.all_messages[-1] == {"role": "assistant", "content": ctx.final_content}
    assert ctx.stop_reason == "completed"
    assert "重试" in ctx.final_content if reason == "model_route_unavailable" else "股票的名称或代码" in ctx.final_content


@pytest.mark.asyncio
async def test_state_run_scopes_brand_new_webui_greeting_to_no_tools(tmp_path):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.bus.events import InboundMessage

    loop = _make_loop(tmp_path)
    loop._run_agent_loop = AsyncMock(return_value=(
        "你好！",
        [],
        [{"role": "assistant", "content": "你好！"}],
        "completed",
        False,
    ))
    runtime_events = MagicMock()
    runtime_events.run_status_changed = AsyncMock()
    loop._runtime_events = MagicMock(return_value=runtime_events)
    session = MagicMock()
    session.metadata = {}
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-greeting",
        content="你好晚上好 今天心情如何",
        metadata={"webui": True},
    )
    ctx = TurnContext(
        msg=msg,
        session_key="websocket:chat-greeting",
        state=TurnState.RUN,
        turn_id="turn-greeting",
        session=session,
        history=[],
    )

    event = await loop._state_run(ctx)

    assert event == "ok"
    passed_tools = loop._run_agent_loop.await_args.kwargs["tools"]
    assert isinstance(passed_tools, ToolRegistry)
    assert passed_tools.tool_names == []


@pytest.mark.asyncio
async def test_asset_workflow_applies_strict_policy_only_to_report_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
    from nanobot.bus.events import InboundMessage
    from nanobot.graph.workflows.asset_research import REPORT_AUDIT
    from nanobot.graph.workflows.asset_research_runtime import (
        AUDIT_MAX_TOOL_ITERATIONS,
        AssetResearchWorkflowOutcome,
    )
    from nanobot.session.manager import Session
    from nanobot.utils.markdown_html import HTML_TEMPLATE_METADATA_KEY

    loop = _make_loop(tmp_path)
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(WriteFileTool(workspace=tmp_path))
    tools.register(EditFileTool(workspace=tmp_path))
    loop.tools = tools
    loop._last_usage = {}
    loop._run_agent_loop = AsyncMock(return_value=(
        "审校完成",
        [],
        [],
        "completed",
        False,
    ))

    class AuditOnlyRuntime:
        def __init__(self, *, run_agent_node, **_kwargs) -> None:
            self._run_agent_node = run_agent_node

        async def run(self, **_kwargs) -> AssetResearchWorkflowOutcome:
            await self._run_agent_node(REPORT_AUDIT, "audit", True)
            return AssetResearchWorkflowOutcome(
                final_content="done",
                stop_reason="completed",
                graph_state={},
                tools_used=[],
                usage={},
                artifacts=[],
            )

    monkeypatch.setattr(
        "nanobot.agent.loop.AssetResearchWorkflowRuntime",
        AuditOnlyRuntime,
    )
    session = Session(key="websocket:audit", metadata={})
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="audit",
        content="分析安集科技",
        metadata={
            "expert_team": {
                "id": "asset-research-team",
                "mcp_presets": [],
            },
            "expert_team_run_id": "run-audit",
            "_expert_team_turn_route": {
                "action": "run",
                "target": "安集科技",
            },
        },
    )
    ctx = TurnContext(
        msg=msg,
        session_key=session.key,
        state=TurnState.RUN,
        turn_id="turn-audit",
        session=session,
        initial_messages=[{"role": "system", "content": "system"}],
        tools=tools,
    )

    await loop._run_asset_research_workflow(ctx)

    kwargs = loop._run_agent_loop.await_args.kwargs
    assert kwargs["max_iterations"] == AUDIT_MAX_TOOL_ITERATIONS
    assert kwargs["metadata"][HTML_TEMPLATE_METADATA_KEY] == "research_report"
    assert "finalize_on_max_iterations" not in kwargs
    assert "write_file" in kwargs["tools"].tool_names
    assert "edit_file" not in kwargs["tools"].tool_names


@pytest.mark.asyncio
async def test_role_revision_adapter_spawns_only_risk_and_disables_synthesis_search(tmp_path, monkeypatch):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.bus.events import InboundMessage
    from nanobot.graph.workflows.asset_research_runtime import AssetResearchWorkflowOutcome
    from nanobot.session.manager import Session

    loop = _make_loop(tmp_path)
    tools = ToolRegistry()
    for name in ("read_file", "write_file", "web_search", "web_fetch"):
        tool = MagicMock()
        tool.name = name
        tools.register(tool)
    loop.tools = tools
    loop.subagents.spawn_for_workflow = AsyncMock(return_value="could not start role")
    loop._run_agent_loop = AsyncMock(return_value=("done", [], [], "completed", False))

    class RevisionRuntime:
        def __init__(self, *, run_agent_node, run_member_wave, **kwargs):
            self.node = run_agent_node
            self.wave = run_member_wave

        async def run(self, **kwargs):
            assert kwargs["rerun_member_ids"] == ["risk-assessor"]
            assert "-v2-" in kwargs["report_path"]
            batch = await self.wave({"risk-assessor": "read supplemental evidence"})
            assert list(batch.members) == ["risk-assessor"]
            for node in ("team-lead", "report-audit"):
                await self.node(node, "update", False)
            return AssetResearchWorkflowOutcome(final_content="done", stop_reason="completed",
                                                 graph_state={}, tools_used=[], usage={}, artifacts=[])

    monkeypatch.setattr("nanobot.agent.loop.AssetResearchWorkflowRuntime", RevisionRuntime)
    session = Session(key="websocket:revision", metadata={})
    msg = InboundMessage(channel="websocket", sender_id="user", chat_id="revision", content="update",
                         metadata={"expert_team": {"id": "asset-research-team"},
                                   "expert_team_run_id": "new-run",
                                   "_expert_team_turn_route": {"action": "run", "target": "比亚迪"},
                                   "expert_team_resume": {"selected_roles": ["risk-assessor"],
                                                          "graph_state": {"run_id": "old-run"}}})
    ctx = TurnContext(msg=msg, session_key=session.key, state=TurnState.RUN,
                      turn_id="turn", session=session, initial_messages=[], tools=tools)
    await loop._run_asset_research_workflow(ctx)
    loop.subagents.spawn_for_workflow.assert_awaited_once()
    assert loop.subagents.spawn_for_workflow.await_args.kwargs["label"] == "risk-assessor"
    for call in loop._run_agent_loop.await_args_list:
        assert set(call.kwargs["tools"].tool_names) == {"read_file", "write_file"}
        system = call.args[0][0]["content"]
        assert "Validated security: 比亚迪" in system
        assert "Current run: new-run" in system
        assert "evidence-index.json" in system
        assert call.kwargs["max_iterations"] in {5, 24}
    assert (tmp_path / "reports/.team-runs/new-run/evidence-index.json").is_file()


@pytest.mark.asyncio
async def test_supply_chain_workflow_fallback_writer_creates_html_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
    from nanobot.bus.events import InboundMessage
    from nanobot.graph.workflows.supply_chain_bottleneck_runtime import (
        SupplyChainBottleneckWorkflowOutcome,
    )
    from nanobot.session.manager import Session

    loop = _make_loop(tmp_path)
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(WriteFileTool(workspace=tmp_path))
    loop.tools = tools

    captured: dict[str, Any] = {}

    class FallbackOnlyRuntime:
        def __init__(self, *, write_report, **_kwargs) -> None:
            captured["write_report"] = write_report

        async def run(self, **_kwargs) -> SupplyChainBottleneckWorkflowOutcome:
            artifacts = await captured["write_report"](
                "reports/bottleneck-map/test.md",
                "# AI硬件供应链瓶颈地图\n\n## 结论\n\n测试正文。",
            )
            captured["artifacts"] = artifacts
            return SupplyChainBottleneckWorkflowOutcome(
                final_content="done",
                stop_reason="completed",
                graph_state={},
                tools_used=["write_file"],
                usage={},
                artifacts=artifacts,
            )

    monkeypatch.setattr(
        "nanobot.agent.loop.SupplyChainBottleneckWorkflowRuntime",
        FallbackOnlyRuntime,
    )
    session = Session(key="websocket:bottleneck", metadata={})
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="bottleneck",
        content="研究 AI 硬件供应链",
        metadata={
            "expert_team": {
                "id": "supply-chain-bottleneck-team",
                "mcp_presets": [],
            },
            "expert_team_run_id": "run-bottleneck",
            "_expert_team_turn_route": {
                "action": "run",
                "target": "AI硬件",
                "team_id": "supply-chain-bottleneck-team",
            },
        },
    )
    ctx = TurnContext(
        msg=msg,
        session_key=session.key,
        state=TurnState.RUN,
        turn_id="turn-bottleneck",
        session=session,
        initial_messages=[{"role": "system", "content": "system"}],
        tools=tools,
    )

    await loop._run_supply_chain_bottleneck_workflow(ctx)

    assert len(captured["artifacts"]) == 1
    html_path = captured["artifacts"][0]
    assert html_path.endswith("test.html")
    assert "Generated from Markdown by TPACowork" in Path(html_path).read_text(
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_asset_workflow_keeps_configured_financial_mcp_tools_for_graph_nodes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.bus.events import InboundMessage
    from nanobot.graph.workflows.asset_research import DATA_PACKAGE, REPORT_AUDIT, TEAM_LEAD
    from nanobot.graph.workflows.asset_research_runtime import AssetResearchWorkflowOutcome
    from nanobot.session.manager import Session

    loop = _make_loop(tmp_path)
    tools = ToolRegistry()
    financial_tools = {
        "mcp_hexin-ifind-ds-stock-mcp_quote",
        "mcp_juyuan_company_financials",
        "mcp_caihui_mcp_company_financials",
        "mcp_anysearch_search",
    }
    for name in {
        "web_search",
        "web_fetch",
        "read_file",
        "write_file",
        *financial_tools,
        "mcp_playwright_browser_snapshot",
    }:
        tool = MagicMock()
        tool.name = name
        tools.register(tool)
    loop.tools = tools
    loop._run_agent_loop = AsyncMock(return_value=(
        "node complete",
        [],
        [],
        "completed",
        False,
    ))

    class CaptureGraphNodesRuntime:
        def __init__(self, *, run_agent_node, **_kwargs) -> None:
            self._run_agent_node = run_agent_node

        async def run(self, **_kwargs) -> AssetResearchWorkflowOutcome:
            await self._run_agent_node(DATA_PACKAGE, "data", False)
            await self._run_agent_node(TEAM_LEAD, "lead", False)
            await self._run_agent_node(REPORT_AUDIT, "audit", True)
            return AssetResearchWorkflowOutcome(
                final_content="done",
                stop_reason="completed",
                graph_state={},
                tools_used=[],
                usage={},
                artifacts=[],
            )

    monkeypatch.setattr(
        "nanobot.agent.loop.AssetResearchWorkflowRuntime",
        CaptureGraphNodesRuntime,
    )
    team = {
        "id": "asset-research-team",
        "mcp_presets": [
            {"name": "hexin-ifind-ds-stock-mcp", "configured": True},
            {"name": "juyuan", "configured": True},
            {"name": "caihui_mcp", "configured": True},
            {"name": "anysearch", "configured": True},
        ],
    }
    session = Session(key="websocket:finance", metadata={})
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="finance",
        content="分析长江电力",
        metadata={
            "webui": True,
            "expert_team": team,
            "expert_team_run_id": "run-finance",
            "_expert_team_turn_route": {
                "action": "run",
                "target": "长江电力",
            },
        },
    )
    ctx = TurnContext(
        msg=msg,
        session_key=session.key,
        state=TurnState.RUN,
        turn_id="turn-finance",
        session=session,
        initial_messages=[{"role": "system", "content": "system"}],
    )

    await loop._run_asset_research_workflow(ctx)

    assert loop._run_agent_loop.await_count == 3
    for call in loop._run_agent_loop.await_args_list:
        node_tools = set(call.kwargs["tools"].tool_names)
        assert financial_tools.issubset(node_tools)
        assert "mcp_playwright_browser_snapshot" not in node_tools


@pytest.mark.asyncio
async def test_run_agent_loop_respects_an_explicit_empty_tool_registry(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="你好！", tool_calls=[])
    )
    fallback_tool = MagicMock()
    fallback_tool.name = "my"
    fallback_tool.to_schema.return_value = {
        "type": "function",
        "function": {"name": "my", "description": "state", "parameters": {}},
    }
    loop.tools.register(fallback_tool)

    final_content, _, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "你好"}],
        channel="websocket",
        session_key="websocket:greeting",
        metadata={"webui": True},
        tools=ToolRegistry(),
    )

    assert final_content == "你好！"
    assert loop.provider.chat_with_retry.await_args.kwargs["tools"] == []


@pytest.mark.asyncio
async def test_loop_persists_provider_ttft_with_turn_scope(tmp_path):
    from nanobot.security.project_context import PROJECT_CONTEXT_METADATA_KEY
    from nanobot.storage.logs import StructuredLogStore

    loop = _make_loop(tmp_path)
    logs = StructuredLogStore(tmp_path / ".nanobot" / "logs.sqlite")
    loop._performance_logs = logs
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[])
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    def observe_provider_timing(payload: dict[str, Any]) -> None:
        if payload.get("event") == "request_started":
            payload["turn_elapsed_ms"] = 123

    with (
        patch("nanobot.agent.runner._SLOW_PROVIDER_TIMING_CALLBACK_MS", 0),
        patch("nanobot.agent.runner.logger.warning") as log_warning,
    ):
        final_content, _, _, _, _ = await loop._run_agent_loop(
            [{"role": "user", "content": "hello"}],
            channel="websocket",
            message_id="request-1",
            session_key="websocket:chat-1",
            metadata={
                "_runtime_turn_id": "turn-1",
                PROJECT_CONTEXT_METADATA_KEY: {
                    "project_id": "project-1",
                    "session_id": "session-1",
                    "session_key": "websocket:chat-1",
                },
            },
            provider_timing_observer=observe_provider_timing,
        )

    assert final_content == "done"
    assert any(
        call.args and str(call.args[0]).startswith("slow provider timing callback")
        for call in log_warning.call_args_list
    )
    records = logs.query(session_id="session-1")
    ttft = next(record for record in records if record.event_name == "provider_ttft")
    assert ttft.project_id == "project-1"
    assert ttft.turn_id == "turn-1"
    assert ttft.request_id == "request-1"
    assert ttft.duration_ms is not None
    assert ttft.details["provider_ttft_ms"] == ttft.duration_ms
    assert ttft.details["model"] == "test-model"
    assert ttft.details["iteration"] == 0
    assert ttft.details["prompt_estimate"] > 0
    request_started = next(
        record for record in records
        if record.event_name == "provider_request_started"
    )
    assert request_started.details["turn_elapsed_ms"] == 123


@pytest.mark.asyncio
async def test_active_turn_correction_is_anchored_and_reviewed_before_final(tmp_path):
    from nanobot.bus.events import InboundMessage
    from nanobot.webui.metadata import ACTIVE_TURN_CORRECTION_METADATA_KEY

    loop = _make_loop(tmp_path)
    loop.context._build_user_content.side_effect = lambda content, _media: content
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {"query": "雪球 可转债"}, None))
    loop.tools.execute = AsyncMock(return_value="雪球页面只有发行数量汇总")
    responses = [
        LLMResponse(
            content="继续查询雪球",
            tool_calls=[
                ToolCallRequest(
                    id="search-1",
                    name="web_search",
                    arguments={"query": "雪球 可转债"},
                )
            ],
        ),
        LLMResponse(content="这里是百度新闻热点摘要。", tool_calls=[]),
        LLMResponse(content="今年发行的可转债名单如下。", tool_calls=[]),
    ]
    seen_messages: list[list[dict[str, Any]]] = []

    async def chat_with_retry(*, messages, **_kwargs):
        seen_messages.append(messages)
        return responses.pop(0)

    loop.provider.chat_with_retry = chat_with_retry
    pending: asyncio.Queue[InboundMessage] = asyncio.Queue()
    await pending.put(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="如果找不到，不要局限在雪球",
        metadata={ACTIVE_TURN_CORRECTION_METADATA_KEY: True},
    ))

    final_content, _, _, _, had_injections = await loop._run_agent_loop(
        [{"role": "user", "content": "帮我找今年发行的可转债，先看雪球"}],
        pending_queue=pending,
    )

    assert had_injections is True
    assert final_content == "今年发行的可转债名单如下。"
    assert len(seen_messages) == 3
    second_prompt = str(seen_messages[1])
    assert "Active objective" in second_prompt
    assert "帮我找今年发行的可转债，先看雪球" in second_prompt
    assert "不要局限在雪球" in second_prompt
    third_prompt = str(seen_messages[2])
    assert "Active-turn correction review" in third_prompt
    assert "older or unrelated topic" in third_prompt


@pytest.mark.asyncio
async def test_loop_max_iterations_message_stays_stable(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={})],
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")
    loop.max_iterations = 2

    final_content, _, _, _, _ = await loop._run_agent_loop([])

    assert final_content == (
        "I reached the maximum number of tool call iterations (2) "
        "without completing the task. You can try breaking the task into smaller steps."
    )


@pytest.mark.asyncio
async def test_loop_node_iteration_limit_uses_one_no_tools_finalization(
    tmp_path,
):
    loop = _make_loop(tmp_path)
    call_index = 0

    async def keep_auditing(*, tools=None, **_kwargs) -> LLMResponse:
        nonlocal call_index
        call_index += 1
        if tools is None:
            return LLMResponse(
                content="核心结论：竞争优势明确，估值仍需保留安全边际。",
                tool_calls=[],
            )
        return LLMResponse(
            content="still auditing",
            tool_calls=[ToolCallRequest(
                id=f"call_{call_index}",
                name="read_file",
                arguments={"path": f"reports/part-{call_index}.md"},
            )],
        )

    loop.provider.chat_with_retry = AsyncMock(side_effect=keep_auditing)
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")

    final_content, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        max_iterations=5,
    )

    assert stop_reason == "max_iterations"
    assert loop.provider.chat_with_retry.await_count == 6
    assert loop.provider.chat_with_retry.await_args_list[-1].kwargs["tools"] is None
    assert final_content == "核心结论：竞争优势明确，估值仍需保留安全边际。"


@pytest.mark.asyncio
async def test_audit_tool_registry_allows_only_one_complete_rewrite() -> None:
    class CountingTool(Tool):
        def __init__(self, name: str) -> None:
            self._name = name
            self.calls = 0

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return self._name

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }

        async def execute(self, **_kwargs: Any) -> str:
            self.calls += 1
            return "written"

    source = ToolRegistry()
    write = CountingTool("write_file")
    source.register(write)
    source.register(CountingTool("edit_file"))
    audit_tools = _SingleRewriteAuditToolRegistry(source)

    assert "edit_file" not in audit_tools.tool_names
    assert await audit_tools.execute("write_file", {"path": "reports/a.md"}) == "written"
    second = await audit_tools.execute("write_file", {"path": "reports/a.md"})

    assert "single allowed complete report rewrite" in second
    assert write.calls == 1


@pytest.mark.asyncio
async def test_loop_goal_turn_uses_standard_iteration_budget(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={})],
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")
    loop.max_iterations = 2

    final_content, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        metadata={"original_command": "/goal"},
    )

    assert stop_reason == "max_iterations"
    assert loop.provider.chat_with_retry.await_count == 3
    assert loop.provider.chat_with_retry.await_args_list[-1].kwargs["tools"] is None
    assert final_content == (
        "I reached the maximum number of tool call iterations (2) "
        "without completing the task. You can try breaking the task into smaller steps."
    )


@pytest.mark.asyncio
async def test_loop_stream_filter_handles_think_only_prefix_without_crashing(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []
    endings: list[bool] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("<think>hidden")
        await on_content_delta("</think>Hello")
        return LLMResponse(content="<think>hidden</think>Hello", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    async def on_stream_end(*, resuming: bool = False) -> None:
        endings.append(resuming)

    final_content, _, _, _, _ = await loop._run_agent_loop(
        [],
        on_stream=on_stream,
        on_stream_end=on_stream_end,
    )

    assert final_content == "Hello"
    assert deltas == ["Hello"]
    assert endings == [False]


@pytest.mark.asyncio
async def test_stream_end_classifies_tool_content_as_narration(tmp_path):
    loop = _make_loop(tmp_path)
    tool_call = ToolCallRequest(
        id="call-1",
        name="web_search",
        arguments={"query": "market"},
    )
    responses = iter([
        LLMResponse(content="I will verify the market data.", tool_calls=[tool_call]),
        LLMResponse(content="Final answer.", tool_calls=[]),
    ])

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        response = next(responses)
        await on_content_delta(response.content)
        return response

    loop.provider.chat_stream_with_retry = chat_stream_with_retry
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {"query": "market"}, None))
    loop.tools.execute = AsyncMock(return_value="ok")
    endings: list[tuple[bool, str]] = []

    async def on_stream(_delta: str) -> None:
        return None

    async def on_stream_end(
        *,
        resuming: bool = False,
        stream_kind: str = "answer",
    ) -> None:
        endings.append((resuming, stream_kind))

    async def on_progress(_content: str, **_kwargs) -> None:
        return None

    final_content, _, _, _, _ = await loop._run_agent_loop(
        [],
        on_stream=on_stream,
        on_stream_end=on_stream_end,
        on_progress=on_progress,
        channel="websocket",
    )

    assert final_content == "Final answer."
    assert endings == [(True, "narration"), (False, "answer")]


@pytest.mark.asyncio
async def test_loop_stream_filter_hides_partial_trailing_think_prefix(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("Hello <thin")
        await on_content_delta("k>hidden</think>World")
        return LLMResponse(content="Hello <think>hidden</think>World", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    final_content, _, _, _, _ = await loop._run_agent_loop([], on_stream=on_stream)

    assert final_content == "Hello World"
    assert deltas == ["Hello", " World"]


@pytest.mark.asyncio
async def test_loop_stream_filter_hides_complete_trailing_think_tag(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("Hello <think>")
        await on_content_delta("hidden</think>World")
        return LLMResponse(content="Hello <think>hidden</think>World", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    final_content, _, _, _, _ = await loop._run_agent_loop([], on_stream=on_stream)

    assert final_content == "Hello World"
    assert deltas == ["Hello", " World"]


@pytest.mark.asyncio
async def test_loop_retries_think_only_final_response(tmp_path):
    loop = _make_loop(tmp_path)
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(content="<think>hidden</think>", tool_calls=[], usage={})
        return LLMResponse(content="Recovered answer", tool_calls=[], usage={})

    loop.provider.chat_with_retry = chat_with_retry

    final_content, _, _, _, _ = await loop._run_agent_loop([])

    assert final_content == "Recovered answer"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_streamed_flag_not_set_on_llm_error(tmp_path):
    """When LLM errors during a streaming-capable channel interaction,
    _streamed must NOT be set so ChannelManager delivers the error."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    error_resp = LLMResponse(
        content="503 service unavailable", finish_reason="error", tool_calls=[], usage={},
    )
    loop.provider.chat_with_retry = AsyncMock(return_value=error_resp)
    loop.provider.chat_stream_with_retry = AsyncMock(return_value=error_resp)
    loop.tools.get_definitions = MagicMock(return_value=[])

    msg = InboundMessage(
        channel="feishu", sender_id="u1", chat_id="c1", content="hi",
    )
    result = await loop._process_message(
        msg,
        on_stream=AsyncMock(),
        on_stream_end=AsyncMock(),
    )

    assert result is not None
    assert "503" in result.content
    assert not result.metadata.get("_streamed"), \
        "_streamed must not be set when stop_reason is error"


@pytest.mark.asyncio
async def test_ssrf_soft_block_can_finalize_after_streamed_tool_call(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    tool_call_resp = LLMResponse(
        content="checking metadata",
        tool_calls=[ToolCallRequest(
            id="call_ssrf",
            name="exec",
            arguments={"command": "curl http://169.254.169.254/latest/meta-data/"},
        )],
        usage={},
    )
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        tool_call_resp,
        LLMResponse(
            content="I cannot access private URLs. Please share the local file.",
            tool_calls=[],
            usage={},
        ),
    ])

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {}, None))
    loop.tools.execute = AsyncMock(return_value=(
        "Error: Command blocked by safety guard (internal/private URL detected)"
    ))

    result = await loop._process_message(
        InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="hi"),
        on_stream=AsyncMock(),
        on_stream_end=AsyncMock(),
    )

    assert result is not None
    assert result.content == "I cannot access private URLs. Please share the local file."
    assert result.metadata.get("_streamed") is True


@pytest.mark.asyncio
async def test_next_turn_after_llm_error_keeps_turn_boundary(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.runner import _PERSISTED_MODEL_ERROR_PLACEHOLDER
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="429 rate limit exceeded", finish_reason="error", tool_calls=[], usage={}),
        LLMResponse(content="Recovered answer", tool_calls=[], usage={}),
    ])

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="first question")
    )
    assert first is not None
    assert first.content == "429 rate limit exceeded"

    session = loop.sessions.get_or_create("cli:test")
    assert [
        {key: value for key, value in message.items() if key in {"role", "content"}}
        for message in session.messages
    ] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": _PERSISTED_MODEL_ERROR_PLACEHOLDER},
    ]

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="second question")
    )
    assert second is not None
    assert second.content == "Recovered answer"

    request_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    non_system = [message for message in request_messages if message.get("role") != "system"]
    assert non_system[0]["role"] == "user"
    assert "first question" in non_system[0]["content"]
    assert non_system[1]["role"] == "assistant"
    assert _PERSISTED_MODEL_ERROR_PLACEHOLDER in non_system[1]["content"]
    assert non_system[2]["role"] == "user"
    assert "second question" in non_system[2]["content"]


@pytest.mark.asyncio
async def test_subagent_max_iterations_announces_existing_fallback(tmp_path, monkeypatch):
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    request_count = 0

    async def keep_working(**kwargs):
        nonlocal request_count
        request_count += 1
        return LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(
                id=f"call_{request_count}",
                name="list_dir",
                arguments={"path": f"./step-{request_count}"},
            )],
        )

    provider.chat_with_retry = AsyncMock(side_effect=keep_working)
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    async def fake_execute(self, **kwargs):
        return "tool result"

    monkeypatch.setattr("nanobot.agent.tools.filesystem.ListDirTool.execute", fake_execute)

    status = SubagentStatus(task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic())
    await mgr._run_subagent("sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status)

    mgr._announce_result.assert_awaited_once()
    args = mgr._announce_result.await_args.args
    assert args[3] == "Task completed but no final response was generated."
    assert args[5] == "ok"


def test_generated_artifact_paths_only_reads_current_structured_tool_results(tmp_path):
    import json

    html = tmp_path / "report.html"
    html.write_text("<html></html>", encoding="utf-8")
    messages = [
        {"role": "assistant", "content": "old"},
        {"role": "tool", "content": json.dumps({"text": "ok", "files": [{"path": str(html)}]})},
    ]

    assert _generated_artifact_paths(messages) == [str(html)]

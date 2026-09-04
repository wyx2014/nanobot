"""Tests for AgentRunner tool execution: batching, concurrency, exclusive tools."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.openai_responses.parsing import parse_response_output
from nanobot.runtime.plan_policy import PlanPolicyState

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _DelayTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        delay: float,
        read_only: bool,
        shared_events: list[str],
        exclusive: bool = False,
    ):
        self._name = name
        self._delay = delay
        self._read_only = read_only
        self._shared_events = shared_events
        self._exclusive = exclusive

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def exclusive(self) -> bool:
        return self._exclusive

    async def execute(self, **kwargs):
        self._shared_events.append(f"start:{self._name}")
        await asyncio.sleep(self._delay)
        self._shared_events.append(f"end:{self._name}")
        return self._name


async def _run_optional_tool_response(response: LLMResponse):
    provider = MagicMock()
    calls = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return response
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = ToolRegistry()
    shared_events: list[str] = []
    tools.register(_DelayTool(
        "optional_tool",
        delay=0,
        read_only=True,
        shared_events=shared_events,
    ))

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "try optional"}],
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))
    return result, shared_events


def _tool_message(result, tool_call_id: str) -> dict:
    return [
        msg for msg in result.messages
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
    ][0]


@pytest.mark.asyncio
async def test_runner_batches_read_only_tools_before_exclusive_work():
    tools = ToolRegistry()
    shared_events: list[str] = []
    read_a = _DelayTool("read_a", delay=0.05, read_only=True, shared_events=shared_events)
    read_b = _DelayTool("read_b", delay=0.05, read_only=True, shared_events=shared_events)
    write_a = _DelayTool("write_a", delay=0.01, read_only=False, shared_events=shared_events)
    tools.register(read_a)
    tools.register(read_b)
    tools.register(write_a)

    runner = AgentRunner(MagicMock())
    await runner._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="ro1", name="read_a", arguments={}),
            ToolCallRequest(id="ro2", name="read_b", arguments={}),
            ToolCallRequest(id="rw1", name="write_a", arguments={}),
        ],
        {},
        {},
    )

    assert shared_events[0:2] == ["start:read_a", "start:read_b"]
    assert "end:read_a" in shared_events and "end:read_b" in shared_events
    assert shared_events.index("end:read_a") < shared_events.index("start:write_a")
    assert shared_events.index("end:read_b") < shared_events.index("start:write_a")
    assert shared_events[-2:] == ["start:write_a", "end:write_a"]


@pytest.mark.asyncio
async def test_runner_plan_barrier_blocks_complex_business_tools() -> None:
    tools = ToolRegistry()
    shared_events: list[str] = []
    tools.register(_DelayTool(
        "write_file",
        delay=0,
        read_only=False,
        shared_events=shared_events,
    ))
    state = PlanPolicyState(forced_reason="explicit_complex_request")

    results, events, fatal, _interactive = await AgentRunner(MagicMock())._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        ),
        [ToolCallRequest(id="write-1", name="write_file", arguments={})],
        {},
        {},
        plan_policy_state=state,
    )

    assert shared_events == []
    assert results[0].startswith("Error [PLAN_REQUIRED]")
    assert events == [{
        "name": "write_file",
        "status": "error",
        "detail": "PLAN_REQUIRED",
    }]
    assert fatal is None
    assert state.correction_count == 1


@pytest.mark.asyncio
async def test_runner_plan_barrier_requires_retry_after_plan_in_same_response() -> None:
    tools = ToolRegistry()
    shared_events: list[str] = []
    tools.register(_DelayTool(
        "update_task_progress",
        delay=0,
        read_only=False,
        shared_events=shared_events,
    ))
    tools.register(_DelayTool(
        "web_search",
        delay=0,
        read_only=True,
        shared_events=shared_events,
    ))
    state = PlanPolicyState(forced_reason="explicit_complex_request")

    results, events, fatal, _interactive = await AgentRunner(MagicMock())._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="plan-1", name="update_task_progress", arguments={}),
            ToolCallRequest(id="search-1", name="web_search", arguments={}),
        ],
        {},
        {},
        plan_policy_state=state,
    )

    assert shared_events == [
        "start:update_task_progress",
        "end:update_task_progress",
    ]
    assert events[0]["status"] == "ok"
    assert events[1]["detail"] == "PLAN_REQUIRED"
    assert results[1].startswith("Error [PLAN_REQUIRED]")
    assert fatal is None
    assert state.plan_created is True
    assert state.correction_count == 0


@pytest.mark.asyncio
async def test_asset_research_runner_enforces_core_then_anysearch_then_web() -> None:
    tools = ToolRegistry()
    shared_events: list[str] = []
    names = (
        "mcp_hexin-ifind-ds-stock-mcp_quote",
        "mcp_juyuan_AShareLiveQuote",
        "mcp_caihui_mcp_company_financials",
        "mcp_anysearch_search",
        "web_search",
    )
    for name in names:
        tools.register(_DelayTool(
            name,
            delay=0,
            read_only=True,
            shared_events=shared_events,
        ))
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        enforce_finance_source_priority=True,
    )
    counts: dict[str, int] = {}
    runner = AgentRunner(MagicMock())

    results, events, fatal, _interactive = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="web-first", name="web_search", arguments={})],
        counts,
        {},
    )
    assert shared_events == []
    assert "source priority blocked" in results[0]
    assert events[0]["detail"] == "asset-research source priority blocked"
    assert fatal is None

    await runner._execute_tools(
        spec,
        [
            ToolCallRequest(id="ifind", name=names[0], arguments={}),
            ToolCallRequest(id="juyuan", name=names[1], arguments={}),
            ToolCallRequest(id="caihui", name=names[2], arguments={}),
        ],
        counts,
        {},
    )
    assert shared_events == [
        f"start:{names[0]}",
        f"end:{names[0]}",
        f"start:{names[1]}",
        f"end:{names[1]}",
        f"start:{names[2]}",
        f"end:{names[2]}",
    ]

    results, events, _, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="web-before-anysearch", name="web_search", arguments={})],
        counts,
        {},
    )
    assert "mcp_anysearch_" in results[0]
    assert events[0]["detail"] == "asset-research source priority blocked"

    await runner._execute_tools(
        spec,
        [ToolCallRequest(id="anysearch", name=names[3], arguments={})],
        counts,
        {},
    )
    results, events, fatal, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="web-last", name="web_search", arguments={})],
        counts,
        {},
    )
    assert results == ["web_search"]
    assert events[0]["status"] == "ok"
    assert fatal is None


@pytest.mark.asyncio
async def test_runner_does_not_batch_exclusive_read_only_tools():
    tools = ToolRegistry()
    shared_events: list[str] = []
    read_a = _DelayTool("read_a", delay=0.03, read_only=True, shared_events=shared_events)
    read_b = _DelayTool("read_b", delay=0.03, read_only=True, shared_events=shared_events)
    ddg_like = _DelayTool(
        "ddg_like",
        delay=0.01,
        read_only=True,
        shared_events=shared_events,
        exclusive=True,
    )
    tools.register(read_a)
    tools.register(ddg_like)
    tools.register(read_b)

    runner = AgentRunner(MagicMock())
    await runner._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="ro1", name="read_a", arguments={}),
            ToolCallRequest(id="ddg1", name="ddg_like", arguments={}),
            ToolCallRequest(id="ro2", name="read_b", arguments={}),
        ],
        {},
        {},
    )

    assert shared_events[0] == "start:read_a"
    assert shared_events.index("end:read_a") < shared_events.index("start:ddg_like")
    assert shared_events.index("end:ddg_like") < shared_events.index("start:read_b")


@pytest.mark.asyncio
async def test_runner_rejects_near_miss_tool_name_without_executing():
    provider = MagicMock()
    call_count = {"n": 0}
    captured_second_call: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="readFile",
                        arguments={"path": "notes.txt"},
                    )
                ],
                finish_reason="tool_calls",
                usage={},
            )
        captured_second_call[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = ToolRegistry()
    shared_events: list[str] = []
    tools.register(_DelayTool(
        "read_file",
        delay=0,
        read_only=True,
        shared_events=shared_events,
    ))

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read notes"}],
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.final_content == "done"
    assert result.tools_used == []
    assert shared_events == []
    assistant_message = [
        msg for msg in result.messages
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ][0]
    assert assistant_message["tool_calls"][0]["function"]["name"] == "readFile"
    tool_message = [
        msg for msg in result.messages
        if msg.get("role") == "tool" and msg.get("tool_call_id") == "call_1"
    ][0]
    assert tool_message["name"] == "readFile"
    assert "Tool 'readFile' not found" in tool_message["content"]
    assert "Did you mean 'read_file'?" in tool_message["content"]
    replayed_assistant = [
        msg for msg in captured_second_call
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ][0]
    assert replayed_assistant["tool_calls"][0]["function"]["name"] == "readFile"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{path:"notes.txt"}', "null"])
async def test_runner_rejects_openai_compat_invalid_arguments_without_executing(arguments):
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        parsed = OpenAICompatProvider()._parse({
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "optional_tool",
                            "arguments": arguments,
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        })

    result, shared_events = await _run_optional_tool_response(parsed)

    assert result.final_content == "done"
    assert parsed.tool_calls[0].arguments == arguments
    assert result.tools_used == []
    assert shared_events == []
    tool_message = _tool_message(result, "call_1")
    assert "parameters must be a JSON object" in tool_message["content"]


@pytest.mark.asyncio
async def test_runner_rejects_openai_responses_malformed_arguments_without_executing():
    parsed = parse_response_output({
        "output": [{
            "type": "function_call",
            "call_id": "call_1",
            "id": "fc_1",
            "name": "optional_tool",
            "arguments": "{bad",
        }],
        "status": "completed",
        "usage": {},
    })

    result, shared_events = await _run_optional_tool_response(parsed)

    assert result.final_content == "done"
    assert parsed.tool_calls[0].arguments == "{bad"
    assert result.tools_used == []
    assert shared_events == []
    tool_message = _tool_message(result, "call_1|fc_1")
    assert "parameters must be a JSON object" in tool_message["content"]


@pytest.mark.asyncio
async def test_runner_rejects_openai_responses_array_arguments_without_executing():
    parsed = parse_response_output({
        "output": [{
            "type": "function_call",
            "call_id": "call_1",
            "id": "fc_1",
            "name": "optional_tool",
            "arguments": [],
        }],
        "status": "completed",
        "usage": {},
    })

    result, shared_events = await _run_optional_tool_response(parsed)

    assert result.final_content == "done"
    assert parsed.tool_calls[0].arguments == []
    assert result.tools_used == []
    assert shared_events == []
    tool_message = _tool_message(result, "call_1|fc_1")
    assert "parameters must be a JSON object" in tool_message["content"]


@pytest.mark.asyncio
async def test_runner_blocks_repeated_external_fetches():
    provider = MagicMock()
    captured_final_call: list[dict] = []
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 3:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id=f"call_{call_count['n']}", name="web_fetch", arguments={"url": "https://example.com"})],
                usage={},
            )
        captured_final_call[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="page content")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research task"}],
        tools=tools,
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.final_content == "done"
    assert tools.execute.await_count == 2
    blocked_tool_message = [
        msg for msg in captured_final_call
        if msg.get("role") == "tool" and msg.get("tool_call_id") == "call_3"
    ][0]
    assert "repeated external lookup blocked" in blocked_tool_message["content"]


@pytest.mark.asyncio
async def test_runner_disables_external_lookups_and_continues_local_artifact_work():
    provider = MagicMock()
    normal_calls = {"n": 0}
    filtered_tool_names: list[str] = []

    async def chat_with_retry(*, messages, tools=None, **kwargs):
        if messages[-1].get("role") == "user" and "has disabled further" in messages[-1]["content"]:
            filtered_tool_names.extend(
                schema["function"]["name"] for schema in (tools or [])
            )
            return LLMResponse(
                content="creating the local artifact",
                tool_calls=[ToolCallRequest(
                    id="create_local_artifact",
                    name="exec",
                    arguments={"command": "create artifact"},
                )],
                usage={},
            )
        if messages[-1].get("role") == "tool" and messages[-1].get("name") == "exec":
            return LLMResponse(
                content="Artifact created using already collected evidence.",
                tool_calls=[],
                usage={},
            )
        normal_calls["n"] += 1
        return LLMResponse(
            content="still searching",
            tool_calls=[ToolCallRequest(
                id=f"repeat_{normal_calls['n']}",
                name="web_search",
                arguments={"query": "same query forever"},
            )],
            usage={},
        )

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = [
        {"type": "function", "function": {"name": "web_search", "parameters": {}}},
        {"type": "function", "function": {"name": "exec", "parameters": {}}},
    ]
    tools.execute = AsyncMock(return_value="search result")

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research task"}],
        tools=tools,
        model="test-model",
        max_iterations=20,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "completed"
    assert result.final_content == "Artifact created using already collected evidence."
    assert normal_calls["n"] == 5
    assert filtered_tool_names == ["exec"]
    assert tools.execute.await_count == 3


@pytest.mark.asyncio
async def test_runner_never_returns_serialized_tool_markup_after_lookup_circuit_breaker():
    provider = MagicMock()
    normal_calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):
        if (
            tools is None
            or (
                messages[-1].get("role") == "user"
                and "has disabled further" in messages[-1]["content"]
            )
        ):
            return LLMResponse(
                content=(
                    "I will create the artifact now.<tool_call>\n"
                    "<function=exec>\n"
                    "<parameter=command>mkdir demo</parameter>\n"
                    "</function>\n"
                    "</tool_call>"
                ),
                tool_calls=[],
                usage={},
            )
        normal_calls["n"] += 1
        return LLMResponse(
            content="still searching",
            tool_calls=[ToolCallRequest(
                id=f"repeat_markup_{normal_calls['n']}",
                name="web_search",
                arguments={"query": "same query forever"},
            )],
            usage={},
        )

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = [
        {"type": "function", "function": {"name": "web_search", "parameters": {}}},
        {"type": "function", "function": {"name": "exec", "parameters": {}}},
    ]
    tools.execute = AsyncMock(return_value="search result")

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research task"}],
        tools=tools,
        model="test-model",
        max_iterations=20,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.final_content is not None
    assert "<tool_call>" not in result.final_content
    assert result.stop_reason == "empty_final_response"


@pytest.mark.asyncio
async def test_runner_finalizes_after_consecutive_identical_local_reads():
    provider = MagicMock()
    normal_calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):
        if tools is None:
            assert "circuit breaker" in messages[-1]["content"]
            return LLMResponse(
                content="Stopped the loop and reported the missing evidence.",
                tool_calls=[],
                usage={},
            )
        normal_calls["n"] += 1
        return LLMResponse(
            content="checking the helper again",
            tool_calls=[ToolCallRequest(
                id=f"repeat_local_{normal_calls['n']}",
                name="read_file",
                arguments={"path": "/workspace/skills/ifind-finance-data/call-node.js"},
            )],
            usage={},
        )

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="[File unchanged since last read]")

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research task"}],
        tools=tools,
        model="test-model",
        max_iterations=20,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "repeated_local_tool_call"
    assert result.final_content == "Stopped the loop and reported the missing evidence."
    assert normal_calls["n"] == 5
    assert tools.execute.await_count == 2
    blocked_results = [
        msg["content"] for msg in result.messages
        if msg.get("role") == "tool"
        and "repeated local tool call blocked" in str(msg.get("content"))
    ]
    assert len(blocked_results) == 3


@pytest.mark.asyncio
async def test_runner_switches_from_failed_ifind_to_juyuan():
    provider = MagicMock()
    calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="query iFinD",
                tool_calls=[ToolCallRequest(
                    id="ifind-1",
                    name="exec",
                    arguments={
                        "command": (
                            "cd /workspace/skills/ifind-finance-data && "
                            "node scripts/call-node.js stock get_stock_info "
                            """'{"query":"新易盛 300502.SZ"}'"""
                        )
                    },
                )],
                usage={},
            )
        if calls["n"] == 2:
            tool_result = next(
                msg["content"]
                for msg in reversed(messages)
                if msg.get("role") == "tool"
            )
            assert "mcp_juyuan_" in tool_result
            return LLMResponse(
                content="switch to Juyuan",
                tool_calls=[ToolCallRequest(
                    id="juyuan-1",
                    name="mcp_juyuan_AShareFinancialReportReview",
                    arguments={"query": "新易盛 300502.SZ 财务"},
                )],
                usage={},
            )
        return LLMResponse(content="report completed", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=[
        '{"ok":true,"status_code":200,"data":{"text":"call failed: status 429"}}',
        '{"code":0,"results":[{"revenue":"248.42亿元"}]}',
    ])

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research 新易盛"}],
        tools=tools,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        fail_on_tool_error=False,
    ))

    assert result.final_content == "report completed"
    assert [call.args[0] for call in tools.execute.await_args_list] == [
        "exec",
        "mcp_juyuan_AShareFinancialReportReview",
    ]

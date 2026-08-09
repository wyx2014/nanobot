from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import build_immediate_prior_turn_evidence
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import Session


def _solar_session() -> Session:
    session = Session(key="websocket:solar")
    session.messages = [
        {"role": "user", "content": "帮我分析下中国的光伏行业"},
        {
            "role": "assistant",
            "content": "开始检索",
            "tool_calls": [
                {
                    "id": "mcp-1",
                    "type": "function",
                    "function": {
                        "name": "mcp_anysearch_batch_search",
                        "arguments": (
                            '{"queries":[{"query":"2026年中国光伏行业现状"},'
                            '{"query":"光伏产业链价格走势"}],"api_key":"secret"}'
                        ),
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "mcp-1",
            "name": "mcp_anysearch_batch_search",
            "content": (
                "### 1. 光伏行业报告\n"
                "- **URL**: https://example.com/solar-report\n"
                "- summary"
            ),
        },
        {"role": "assistant", "content": "中国光伏行业报告"},
    ]
    return session


def test_prior_turn_evidence_is_generic_and_derived_from_replay() -> None:
    evidence = build_immediate_prior_turn_evidence(_solar_session().messages)

    assert "帮我分析下中国的光伏行业" in evidence
    assert "mcp_anysearch_batch_search" in evidence
    assert "2026年中国光伏行业现状" in evidence
    assert "https://example.com/solar-report" in evidence
    assert '"api_key":"[REDACTED]"' in evidence
    assert "secret" not in evidence


def test_prior_turn_evidence_omits_unparseable_tool_arguments() -> None:
    history = [
        {"role": "user", "content": "继续"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "bad-1",
                "type": "function",
                "function": {
                    "name": "custom_tool",
                    "arguments": "api_key=do-not-copy",
                },
            }],
        },
        {"role": "tool", "tool_call_id": "bad-1", "content": "done"},
        {"role": "assistant", "content": "完成"},
    ]

    evidence = build_immediate_prior_turn_evidence(history)

    assert "[unparseable arguments omitted]" in evidence
    assert "do-not-copy" not in evidence


@pytest.mark.asyncio
async def test_history_question_uses_normal_model_with_same_session_evidence(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="上一轮调用了 mcp_anysearch_batch_search。",
        tool_calls=[],
    ))
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    session = loop.sessions.get_or_create("websocket:solar")
    session.messages = _solar_session().messages
    loop.sessions.save(session)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)  # type: ignore[method-assign]

    outbound = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="solar",
        content="你刚才查了什么资料",
    ))

    assert outbound is not None
    assert outbound.content == "上一轮调用了 mcp_anysearch_batch_search。"
    assert "history_audit" not in outbound.metadata
    provider.chat_with_retry.assert_awaited_once()
    request = provider.chat_with_retry.await_args.kwargs["messages"]
    assert "Same-session Conversation Continuity" in request[0]["content"]
    current_user = next(
        message for message in reversed(request) if message["role"] == "user"
    )
    assert "Immediate Prior Turn Evidence" in current_user["content"]
    assert "mcp_anysearch_batch_search" in current_user["content"]
    assert "https://example.com/solar-report" in current_user["content"]

    persisted = loop.sessions.get_or_create("websocket:solar").messages
    assert persisted[-2]["content"] == "你刚才查了什么资料"
    assert "_history_audit" not in persisted[-2]
    assert "_history_audit" not in persisted[-1]

    await loop.close_mcp()


@pytest.mark.asyncio
async def test_two_turn_tool_research_is_available_to_arbitrary_followup(tmp_path) -> None:
    """Continuity does not depend on recognizing a fixed follow-up phrase."""
    from nanobot.agent.tools.base import Tool
    from nanobot.providers.base import ToolCallRequest

    class SolarSearchTool(Tool):
        @property
        def name(self) -> str:
            return "mcp_anysearch_batch_search"

        @property
        def description(self) -> str:
            return "Search solar sources"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"queries": {"type": "array"}},
                "required": ["queries"],
            }

        async def execute(self, **_kwargs):
            return "### 1. 光伏报告\n- **URL**: https://example.com/solar"

    provider = MagicMock()
    provider.get_default_model.return_value = "provider-a"
    provider.generation = SimpleNamespace(max_tokens=4096)
    requests: list[list[dict]] = []
    responses = [
        LLMResponse(
            content="正在查询光伏资料",
            tool_calls=[
                ToolCallRequest(
                    id="mcp-solar-1",
                    name="mcp_anysearch_batch_search",
                    arguments={"queries": [{"query": "中国光伏行业"}]},
                )
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="光伏行业分析完成", tool_calls=[]),
        LLMResponse(content="估值仍受价格周期影响", tool_calls=[]),
    ]

    async def chat_with_retry(*, messages, **_kwargs):
        requests.append(deepcopy(messages))
        return responses.pop(0)

    provider.chat_with_retry = chat_with_retry
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="provider-a",
    )
    loop.tools.register(SolarSearchTool())
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)  # type: ignore[method-assign]

    first = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="solar-followup",
        content="帮我分析下中国的光伏行业",
    ))
    assert first is not None
    assert first.content == "光伏行业分析完成"

    second = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="solar-followup",
        content="那估值呢？",
    ))
    assert second is not None

    second_turn_messages = requests[-1]
    assert [message["role"] for message in second_turn_messages] == [
        "system", "user", "assistant", "tool", "assistant", "user"
    ]
    assert "帮我分析下中国的光伏行业" in second_turn_messages[1]["content"]
    assert second_turn_messages[2]["tool_calls"][0]["function"]["name"] == (
        "mcp_anysearch_batch_search"
    )
    assert "https://example.com/solar" in second_turn_messages[3]["content"]
    assert second_turn_messages[4]["content"] == "光伏行业分析完成"
    assert "Immediate Prior Turn Evidence" in second_turn_messages[-1]["content"]
    assert "mcp_anysearch_batch_search" in second_turn_messages[-1]["content"]

    await loop.close_mcp()


@pytest.mark.asyncio
async def test_summary_created_during_turn_is_injected_before_provider_call(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="我记得上一轮的光伏分析。",
        tool_calls=[],
    ))
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    session = loop.sessions.get_or_create("websocket:compact-now")
    session.messages = _solar_session().messages
    loop.sessions.save(session)

    async def compact_now(active_session, **_kwargs):
        active_session.last_consolidated = len(active_session.messages)
        active_session.metadata["_last_summary"] = {
            "text": (
                "用户要求分析中国光伏行业；调用 mcp_anysearch_batch_search，"
                "来源 https://example.com/solar-report。"
            ),
            "last_active": "2026-08-06T17:25:14",
            "kind": "session",
        }
        loop.sessions.save(active_session)

    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=compact_now)  # type: ignore[method-assign]

    outbound = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="compact-now",
        content="继续",
    ))

    assert outbound is not None
    request = provider.chat_with_retry.await_args.kwargs["messages"]
    assert "[Archived Context Summary]" in request[0]["content"]
    assert "mcp_anysearch_batch_search" in request[0]["content"]
    assert "https://example.com/solar-report" in request[0]["content"]

    await loop.close_mcp()


def test_legacy_project_consolidation_is_restored_without_deleting_messages(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    session = _solar_session()
    session.metadata["project_id"] = "prj_solar"
    session.metadata["_last_summary"] = {
        "text": "- [skip] 光伏检索是一次性研究",
        "last_active": "2026-08-06T17:25:14",
    }
    session.last_consolidated = len(session.messages) - 1
    original_messages = list(session.messages)

    repaired = loop._repair_legacy_project_consolidation(session)

    assert repaired is True
    assert session.last_consolidated == 0
    assert session.messages == original_messages
    assert "_last_summary" not in session.metadata


def test_new_session_continuity_summary_is_not_reset(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    session = _solar_session()
    session.metadata["project_id"] = "prj_solar"
    session.metadata["_last_summary"] = {
        "text": "调用了 mcp_anysearch_batch_search。",
        "last_active": "2026-08-06T17:25:14",
        "kind": "session",
    }
    session.last_consolidated = 3

    repaired = loop._repair_legacy_project_consolidation(session)

    assert repaired is False
    assert session.last_consolidated == 3

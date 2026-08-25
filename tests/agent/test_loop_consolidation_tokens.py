from unittest.mock import AsyncMock, MagicMock

import pytest

import nanobot.agent.memory as memory_module
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import (
    EXPERT_TEAM_HISTORY_SOURCE,
    PROFILE_CANDIDATE_KIND,
)
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY


def _make_loop(tmp_path, *, estimated_tokens: int, context_window_tokens: int) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (estimated_tokens, "test-counter")
    _response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_response)
    provider.chat_stream_with_retry = AsyncMock(return_value=_response)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0
    return loop


def test_project_session_uses_session_only_consolidator(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    project = tmp_path / "project"
    project.mkdir()
    session = loop.sessions.get_or_create("websocket:project")
    session.metadata[WORKSPACE_SCOPE_METADATA_KEY] = {
        "project_path": str(project),
        "access_mode": "restricted",
    }

    assert loop._consolidator_for_session(session) is loop.session_consolidator
    assert loop.session_consolidator is loop.consolidator
    assert loop.session_consolidator.store is None
    assert loop.session_consolidator.profile_store is loop.context.memory


def test_root_project_session_also_uses_session_only_consolidator(tmp_path) -> None:
    """Desktop projects commonly use the runtime workspace as their root."""
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("websocket:root-project")
    session.metadata["project_id"] = "prj_root"
    session.metadata[WORKSPACE_SCOPE_METADATA_KEY] = {
        "project_path": str(tmp_path),
        "access_mode": "full",
    }

    assert loop._consolidator_for_session(session) is loop.session_consolidator
    assert loop.session_consolidator.profile_store is loop.context.memory


@pytest.mark.asyncio
async def test_short_project_turn_records_profile_candidate_without_compaction(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("websocket:root-project")
    session.metadata["project_id"] = "prj_root"
    loop.sessions.save(session)

    await loop.process_direct(
        "Please remember that I prefer concise replies in every chat.",
        session_key=session.key,
    )

    entries = loop.context.memory.read_unprocessed_history(since_cursor=0)
    assert len(entries) == 1
    assert entries[0]["kind"] == PROFILE_CANDIDATE_KIND
    assert "prefer concise replies" in entries[0]["content"]
    # One provider call serves the chat turn; profile capture itself adds no LLM call.
    assert loop.provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_ephemeral_turn_does_not_record_profile_candidate(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    await loop.process_direct(
        "This synthetic prompt is not user-profile evidence.",
        session_key="cli:ephemeral-check",
        ephemeral=True,
    )

    assert loop.context.memory.read_unprocessed_history(since_cursor=0) == []


def test_expert_team_turn_is_source_tagged_at_capture_boundary(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("websocket:expert-chat")
    message = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="expert-chat",
        content="Across all chats, call me Ada.",
        metadata={"expert_team": {"id": "asset-research-team"}},
    )

    assert loop._persist_user_message_early(message, session) is True

    entry = loop.context.memory.read_unprocessed_history(since_cursor=0)[0]
    assert entry["source"] == "expert-team"
    assert entry["content"].startswith(EXPERT_TEAM_HISTORY_SOURCE)


@pytest.mark.asyncio
async def test_failed_session_only_compaction_keeps_live_messages(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("websocket:project")
    session.messages = [
        {"role": role, "content": f"message {index}"}
        for index, role in enumerate(["user", "assistant"] * 6)
    ]
    loop.sessions.save(session)
    loop.session_consolidator.archive = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await loop.session_consolidator.compact_idle_session(session.key)

    assert result is None
    assert len(loop.sessions.get_or_create(session.key).messages) == 12


@pytest.mark.asyncio
async def test_prompt_below_threshold_does_not_consolidate(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.consolidator.archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_above_threshold_triggers_consolidation(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]
    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _message: 500)

    await loop.process_direct("hello", session_key="cli:test")

    assert loop.consolidator.archive.await_count >= 1


@pytest.mark.asyncio
async def test_prompt_above_threshold_archives_until_next_user_boundary(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
    ]
    loop.sessions.save(session)

    token_map = {"u1": 120, "a1": 120, "u2": 120, "a2": 120, "u3": 120}
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda message: token_map[message["content"]])

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    archived_chunk = loop.consolidator.archive.await_args.args[0]
    assert [message["content"] for message in archived_chunk] == ["u1", "a1", "u2", "a2"]
    assert session.last_consolidated == 4


@pytest.mark.asyncio
async def test_consolidation_loops_until_target_met(tmp_path, monkeypatch) -> None:
    """Verify maybe_consolidate_by_tokens keeps looping until under threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]
    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (300, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    assert loop.consolidator.archive.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_continues_below_trigger_until_half_target(tmp_path, monkeypatch) -> None:
    """Once triggered, consolidation should continue until it drops below half threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (150, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    assert loop.consolidator.archive.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_persists_summary_for_next_prepare_session(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value="User discussed project status.")  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 150)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    reloaded = loop.sessions.get_or_create("cli:test")
    meta = reloaded.metadata.get("_last_summary")
    assert meta is not None
    assert meta["text"] == "User discussed project status."

    reloaded, pending = loop.auto_compact.prepare_session(reloaded, "cli:test")
    assert pending is not None
    assert "User discussed project status." in pending
    # _last_summary persists for restart survival.
    assert "_last_summary" in reloaded.metadata


@pytest.mark.asyncio
async def test_preflight_consolidation_receives_pending_summary(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("cli:test")
    loop.auto_compact.prepare_session = MagicMock(
        return_value=(session, "Previous conversation summary: earlier context")
    )  # type: ignore[method-assign]
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)  # type: ignore[method-assign]
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.consolidator.maybe_consolidate_by_tokens.assert_any_await(
        session,
        replay_max_messages=loop._max_messages,
    )


@pytest.mark.asyncio
async def test_preflight_consolidation_before_llm_call(tmp_path, monkeypatch) -> None:
    """Verify preflight consolidation runs before the LLM call in process_direct."""
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)

    archived_session_keys: list[str | None] = []

    async def track_consolidate(messages, *, session_key=None):
        order.append("consolidate")
        archived_session_keys.append(session_key)
        return True
    loop.consolidator.archive = track_consolidate  # type: ignore[method-assign]

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])
    loop.provider.chat_with_retry = track_llm
    loop.provider.chat_stream_with_retry = track_llm
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 500)

    call_count = [0]
    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        return (1000 if call_count[0] <= 1 else 80, "test")
    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate" in order
    assert "llm" in order
    assert order.index("consolidate") < order.index("llm")
    assert archived_session_keys == ["cli:test"]

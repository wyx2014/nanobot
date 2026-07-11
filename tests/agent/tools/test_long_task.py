"""Tests for sustained goal tools (`long_task`, `complete_goal`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.long_task import (
    CompleteGoalTool,
    GetGoalTool,
    LongTaskTool,
    UpdateGoalTool,
)
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventBus
from nanobot.session.goal_state import GOAL_STATE_KEY
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WebuiTurnCoordinator


def _tools(sm: SessionManager) -> tuple[LongTaskTool, CompleteGoalTool]:
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    lt.set_context(rc)
    cg.set_context(rc)
    return lt, cg


def _goal_tools(sm: SessionManager) -> tuple[LongTaskTool, GetGoalTool, UpdateGoalTool]:
    lt = LongTaskTool(sessions=sm)
    gg = GetGoalTool(sessions=sm)
    ug = UpdateGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    lt.set_context(rc)
    gg.set_context(rc)
    ug.set_context(rc)
    return lt, gg, ug


@pytest.mark.asyncio
async def test_long_task_records_goal_metadata(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    out = await lt.execute(goal="Do the thing", ui_summary="thing")
    assert "Goal recorded" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert isinstance(blob, dict)
    assert blob["status"] == "active"
    assert blob["objective"] == "Do the thing"
    assert blob["ui_summary"] == "thing"


@pytest.mark.asyncio
async def test_long_task_rejects_second_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    await lt.execute(goal="First")
    out = await lt.execute(goal="Second")
    assert "already active" in out


@pytest.mark.asyncio
async def test_complete_goal_closes_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(goal="X")
    out = await cg.execute(recap="Done.")
    assert "marked complete" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert blob["status"] == "completed"
    assert blob["recap"] == "Done."


@pytest.mark.asyncio
async def test_get_goal_returns_public_goal_state(tmp_path):
    sm = SessionManager(tmp_path)
    lt, gg, ug = _goal_tools(sm)

    await lt.execute(goal="Ship it", ui_summary="ship")
    await ug.execute(status="blocked", blocker="missing token")

    out = await gg.execute()
    assert out["active"] is True
    assert out["goal"]["objective"] == "Ship it"
    assert "_blocked_audit" not in out["goal"]


@pytest.mark.asyncio
async def test_update_goal_blocks_only_after_three_same_blockers(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _gg, ug = _goal_tools(sm)

    await lt.execute(goal="Wait for external input")
    assert "1/3" in await ug.execute(status="blocked", blocker="api unavailable")
    assert "2/3" in await ug.execute(status="blocked", blocker="api unavailable")
    out = await ug.execute(
        status="blocked",
        blocker="api unavailable",
        recap="Cannot continue until API returns.",
    )
    assert "marked blocked" in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["status"] == "blocked"
    assert blob["blocker"] == "api unavailable"
    assert "_blocked_audit" not in blob


@pytest.mark.asyncio
async def test_update_goal_resets_blocked_audit_for_different_blocker(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _gg, ug = _goal_tools(sm)

    await lt.execute(goal="Wait")
    assert "1/3" in await ug.execute(status="blocked", blocker="one")
    assert "1/3" in await ug.execute(status="blocked", blocker="two")


@pytest.mark.asyncio
async def test_update_goal_requires_blocker_for_blocked_status(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _gg, ug = _goal_tools(sm)

    await lt.execute(goal="Wait")
    out = await ug.execute(status="blocked")
    assert "blocker is required" in out


@pytest.mark.asyncio
async def test_goal_tools_keep_request_context_per_task(tmp_path):
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    ctx_a = RequestContext(channel="websocket", chat_id="a", session_key="websocket:a")
    ctx_b = RequestContext(channel="websocket", chat_id="b", session_key="websocket:b")

    lt.set_context(ctx_a)
    task_a = asyncio.create_task(lt.execute(goal="Goal A"))
    lt.set_context(ctx_b)
    task_b = asyncio.create_task(lt.execute(goal="Goal B"))
    await asyncio.gather(task_a, task_b)

    assert sm.get_or_create("websocket:a").metadata[GOAL_STATE_KEY]["objective"] == "Goal A"
    assert sm.get_or_create("websocket:b").metadata[GOAL_STATE_KEY]["objective"] == "Goal B"

    cg.set_context(ctx_a)
    done_a = asyncio.create_task(cg.execute(recap="Done A"))
    cg.set_context(ctx_b)
    done_b = asyncio.create_task(cg.execute(recap="Done B"))
    await asyncio.gather(done_a, done_b)

    assert sm.get_or_create("websocket:a").metadata[GOAL_STATE_KEY]["recap"] == "Done A"
    assert sm.get_or_create("websocket:b").metadata[GOAL_STATE_KEY]["recap"] == "Done B"


@pytest.mark.asyncio
async def test_goal_tools_context_isolated_across_tool_types(tmp_path):
    """LongTaskTool and CompleteGoalTool must not share routing context."""
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    ctx = RequestContext(channel="websocket", chat_id="a", session_key="websocket:a")

    lt.set_context(ctx)
    assert cg._request_ctx.get() is None

    cg.set_context(ctx)
    assert lt._request_ctx.get() is ctx
    assert cg._request_ctx.get() is ctx


@pytest.mark.asyncio
async def test_long_task_publishes_goal_state_ws_after_save(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    runtime_events = RuntimeEventBus()
    sm = SessionManager(tmp_path)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=sm,
        schedule_background=lambda _coro: None,
    ).subscribe(runtime_events)
    lt = LongTaskTool(sessions=sm, runtime_events=runtime_events)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-99",
        session_key="websocket:chat-99",
        metadata={},
    )
    lt.set_context(rc)

    await lt.execute(goal="Objective alpha", ui_summary="alpha")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.channel == "websocket"
    assert call.chat_id == "chat-99"
    assert call.metadata.get("_goal_state_sync") is True
    assert call.metadata["goal_state"] == {
        "active": True,
        "ui_summary": "alpha",
        "objective": "Objective alpha",
    }


@pytest.mark.asyncio
async def test_complete_goal_publishes_inactive_goal_state_ws(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    runtime_events = RuntimeEventBus()
    sm = SessionManager(tmp_path)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=sm,
        schedule_background=lambda _coro: None,
    ).subscribe(runtime_events)
    lt = LongTaskTool(sessions=sm, runtime_events=runtime_events)
    cg = CompleteGoalTool(sessions=sm, runtime_events=runtime_events)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-z",
        session_key="websocket:chat-z",
        metadata={},
    )
    lt.set_context(rc)
    await lt.execute(goal="X")

    bus.publish_outbound.reset_mock()
    cg.set_context(rc)
    await cg.execute(recap="Done.")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.metadata["goal_state"] == {"active": False}


@pytest.mark.asyncio
async def test_complete_goal_without_active_is_noop_message(tmp_path):
    sm = SessionManager(tmp_path)
    _lt, cg = _tools(sm)

    out = await cg.execute(recap="n/a")
    assert "No active" in out


@pytest.mark.asyncio
async def test_long_task_skips_ws_publish_without_bus(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)
    out = await lt.execute(goal="Solo", ui_summary="s")
    assert "Goal recorded" in out


@pytest.mark.asyncio
async def test_long_task_and_complete_goal_registered(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    lt = loop.tools.get("long_task")
    cg = loop.tools.get("complete_goal")
    gg = loop.tools.get("get_goal")
    ug = loop.tools.get("update_goal")
    assert lt is not None and lt.name == "long_task"
    assert cg is not None and cg.name == "complete_goal"
    assert gg is not None and gg.name == "get_goal"
    assert ug is not None and ug.name == "update_goal"

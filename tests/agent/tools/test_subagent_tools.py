"""Tests for subagent tool registration and wiring."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.config.schema import AgentDefaults

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _valid_team_report() -> str:
    return (
        "# 角色研究报告\n\n## 数据与来源\n\n"
        + "基于同花顺结构化数据和公司公告完成交叉核验。" * 40
        + "\n\n## 结论\n\n核心结论清晰，证据与局限性已经列明。"
    )


@pytest.mark.asyncio
async def test_subagent_exec_tool_receives_allowed_env_keys(tmp_path):
    """allowed_env_keys from ExecToolConfig must be forwarded to the subagent's ExecTool."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.agent.tools.shell import ExecToolConfig
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ToolsConfig

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        tools_config=ToolsConfig(exec=ExecToolConfig(allowed_env_keys=["GOPATH", "JAVA_HOME"])),
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        exec_tool = spec.tools.get("exec")
        assert exec_tool is not None
        assert exec_tool.allowed_env_keys == ["GOPATH", "JAVA_HOME"]
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status = SubagentStatus(
        task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic()
    )
    await mgr._run_subagent(
        "sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_uses_configured_max_iterations(tmp_path):
    """Subagents should honor the configured tool-iteration limit."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        max_iterations=37,
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        assert spec.max_iterations == 37
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status = SubagentStatus(
        task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic()
    )
    await mgr._run_subagent(
        "sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_forwards_temperature_to_run_spec(tmp_path):
    """A temperature passed to spawn() should reach the AgentRunSpec."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    seen = {}

    async def fake_run(spec):
        seen["temperature"] = spec.temperature
        return SimpleNamespace(
            stop_reason="done", final_content=_valid_team_report(), error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    await mgr.spawn(task="do task", temperature=0.9)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert seen["temperature"] == 0.9


@pytest.mark.asyncio
async def test_spawn_tool_rejects_when_at_concurrency_limit(tmp_path):
    """SpawnTool should return an error string when the concurrency limit is reached."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    # Block the first subagent so it stays "running"
    release = asyncio.Event()

    async def fake_run(spec):
        await release.wait()
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    from nanobot.agent.tools.context import RequestContext

    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    # First spawn succeeds
    result = await tool.execute(task="first task")
    assert "started" in result

    # Second spawn should be rejected (default limit is 1)
    result = await tool.execute(task="second task")
    assert "Cannot spawn subagent" in result
    assert "concurrency limit reached" in result

    # Release the first subagent
    release.set()
    # Allow cleanup
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_expert_team_gets_session_scoped_four_agent_limit(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()
    release = asyncio.Event()

    async def fake_run(spec):
        await release.wait()
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="team-chat",
        session_key="websocket:team-chat",
        metadata={
            "expert_team": {
                "id": "asset-research-team",
                "requested_concurrency": 4,
                "members": [],
            },
            "expert_team_run_id": "run-1",
        },
    ))

    for index in range(4):
        result = await tool.execute(task=f"task {index}", label=f"member-{index}")
        assert "started" in result
    rejected = await tool.execute(task="fifth", label="member-5")
    assert "concurrency limit reached" in rejected

    release.set()
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_expert_team_member_uses_recoverable_tool_errors_and_runtime_contract(tmp_path):
    """Imported team prompts must use nanobot tools and recover from bad sources."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    skill_dir = tmp_path / "skills" / "ifind-finance-data"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ifind-finance-data\ndescription: structured finance\n---\n"
        "# 同花顺金融数据查询\n\nUse call-node.js for structured company data.",
        encoding="utf-8",
    )
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        assert spec.fail_on_tool_error is False
        assert spec.max_iterations == 100
        system_prompt = spec.initial_messages[0]["content"]
        assert "Expert Team Member Runtime Contract" in system_prompt
        assert "`web_search` and `web_fetch`" in system_prompt
        assert "Do not wait for another member" in system_prompt
        assert "external lookup more than twice" in system_prompt
        assert "Required Integrated Financial Data Sources" in system_prompt
        assert "同花顺金融数据查询" in system_prompt
        assert "财务报表、现金流和估值" in system_prompt
        return SimpleNamespace(
            stop_reason="done", final_content=_valid_team_report(), error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    status = SubagentStatus(
        task_id="team-1",
        label="financial-analyst",
        task_description="research",
        started_at=time.monotonic(),
    )
    await mgr._run_subagent(
        "team-1",
        "research",
        "financial-analyst",
        {"channel": "websocket", "chat_id": "c1", "session_key": "websocket:c1"},
        status,
        expert_team={
            "id": "asset-research-team",
            "members": [],
            "data_sources": [{
                "id": "ifind-finance-data",
                "name": "同花顺 iFinD 金融数据",
                "skill": "ifind-finance-data",
                "priority": "primary",
                "required": True,
                "assignments": {"financial-analyst": "财务报表、现金流和估值"},
            }],
        },
        expert_team_run_id="run-1",
    )

    mgr.runner.run.assert_awaited_once()


def test_expert_team_data_source_is_added_to_project_skill_scope(tmp_path):
    from nanobot.agent.loop import _project_skill_scope

    metadata = {
        "expert_team": {
            "id": "asset-research-team",
            "data_sources": [{"skill": "ifind-finance-data"}],
        },
    }
    with patch("nanobot.agent.loop.project_skill_grants", return_value=["project-skill"]):
        scope = _project_skill_scope(tmp_path, tmp_path / "project", metadata)

    assert scope == {
        "project_bound_user_skills": ["project-skill", "ifind-finance-data"],
        "explicit_skills": ["ifind-finance-data"],
    }


@pytest.mark.asyncio
async def test_expert_team_member_retries_once_before_degrading(tmp_path):
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()
    results = iter([
        SimpleNamespace(
            stop_reason="tool_error",
            final_content=None,
            error="source failed",
            tool_events=[{"name": "web_fetch", "status": "error", "detail": "blocked"}],
        ),
        SimpleNamespace(
            stop_reason="done",
            final_content=_valid_team_report(),
            error=None,
            tool_events=[],
        ),
    ])
    mgr.runner.run = AsyncMock(side_effect=lambda spec: next(results))
    status = SubagentStatus(
        task_id="team-retry",
        label="risk-assessor",
        task_description="research risk",
        started_at=time.monotonic(),
    )

    await mgr._run_subagent(
        "team-retry",
        "research risk",
        "risk-assessor",
        {"channel": "websocket", "chat_id": "c1", "session_key": "websocket:c1"},
        status,
        expert_team={"id": "asset-research-team", "members": []},
        expert_team_run_id="run-1",
    )

    assert mgr.runner.run.await_count == 2
    mgr._announce_result.assert_awaited_once()
    assert mgr._announce_result.await_args.args[5] == "ok"


@pytest.mark.asyncio
async def test_expert_team_member_timeout_degrades_without_retry(tmp_path):
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    async def never_finishes(_spec):
        await asyncio.sleep(1)

    mgr.runner.run = AsyncMock(side_effect=never_finishes)
    status = SubagentStatus(
        task_id="team-timeout",
        label="financial-analyst",
        task_description="research financials",
        started_at=time.monotonic(),
    )

    with patch("nanobot.agent.subagent._EXPERT_TEAM_MEMBER_TIMEOUT_S", 0.01):
        await mgr._run_subagent(
            "team-timeout",
            "research financials",
            "financial-analyst",
            {
                "channel": "websocket",
                "chat_id": "c1",
                "session_key": "websocket:c1",
            },
            status,
            expert_team={"id": "asset-research-team", "members": []},
            expert_team_run_id="run-1",
        )

    assert mgr.runner.run.await_count == 1
    assert status.phase == "error"
    assert status.stop_reason == "timeout"
    assert mgr._announce_result.await_args.args[5] == "error"
    assert "Juyuan MCP" in mgr._announce_result.await_args.args[3]


def test_expert_team_report_quality_rejects_max_iteration_placeholder():
    from nanobot.agent.subagent import SubagentManager

    issue = SubagentManager._expert_team_report_quality_issue(SimpleNamespace(
        stop_reason="max_iterations",
        final_content="Task completed but no final response was generated.",
    ))

    assert issue == "达到工具轮次上限，未形成正式报告"


@pytest.mark.asyncio
async def test_regular_subagent_keeps_fail_fast_tool_behavior(tmp_path):
    """The expert-team recovery policy must not loosen ordinary subagents."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        assert spec.fail_on_tool_error is True
        assert "Expert Team Member Runtime Contract" not in spec.initial_messages[0]["content"]
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    status = SubagentStatus(
        task_id="regular-1",
        label="helper",
        task_description="work",
        started_at=time.monotonic(),
    )
    await mgr._run_subagent(
        "regular-1", "work", "helper", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()


def test_subagent_default_max_concurrent_matches_agent_defaults(tmp_path):
    """Direct SubagentManager construction should use the agent default concurrency limit."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    assert mgr.max_concurrent_subagents == AgentDefaults().max_concurrent_subagents


def test_subagent_default_max_iterations_matches_agent_defaults(tmp_path):
    """Direct SubagentManager construction should use the agent default limit."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    assert mgr.max_iterations == AgentDefaults().max_tool_iterations


def test_agent_loop_passes_max_iterations_to_subagents(tmp_path):
    """AgentLoop's configured limit should be shared with spawned subagents."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        max_iterations=42,
    )

    assert loop.subagents.max_iterations == 42


@pytest.mark.asyncio
async def test_agent_loop_syncs_updated_max_iterations_before_run(tmp_path):
    """Runtime max_iterations changes should be reflected before tool execution."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        max_iterations=42,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    async def fake_run(spec):
        assert spec.max_iterations == 55
        assert loop.subagents.max_iterations == 55
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_run)
    loop.max_iterations = 55

    await loop._run_agent_loop([])

    loop.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_pending_blocks_while_subagents_running(tmp_path):
    """_drain_pending should block when no messages are available but sub-agents are still running."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    session = Session(key="test:drain-block")
    injection_callback = None

    # Capture the injection_callback that _run_agent_loop creates
    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback

        # Simulate: first call to injection_callback should block because
        # sub-agents are running and no messages are in the queue yet.
        # We'll resolve this from a concurrent task.
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    # Register a running sub-agent in the SubagentManager for this session
    async def _hang_forever():
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())
    loop.subagents._session_tasks.setdefault(session.key, set()).add("sub-drain-1")
    loop.subagents._running_tasks["sub-drain-1"] = hang_task

    # Run _run_agent_loop — this defines the _drain_pending closure
    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=session,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # Now test the callback directly
    # With sub-agents running and an empty queue, it should block
    drain_task = asyncio.create_task(injection_callback())

    # Let the task enter the blocking queue wait.
    await asyncio.sleep(0)

    # Should still be running (blocked on pending_queue.get())
    assert not drain_task.done(), "drain should block while sub-agents are running"

    # Now put a message in the queue (simulating sub-agent completion)
    await pending_queue.put(InboundMessage(
        sender_id="subagent",
        channel="test",
        chat_id="c1",
        content="Sub-agent result",
        media=None,
        metadata={},
    ))

    # Should unblock and return results
    results = await asyncio.wait_for(drain_task, timeout=2.0)
    assert len(results) >= 1
    assert results[0]["role"] == "user"
    assert "Sub-agent result" in str(results[0]["content"])

    # Cleanup
    hang_task.cancel()
    try:
        await hang_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_expert_team_results_are_batched_and_force_team_lead_continuation(tmp_path):
    """The last member delivery must resume synthesis instead of becoming the final answer."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    session = Session(key="websocket:expert-batch")
    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)
    await loop._run_agent_loop(
        [{"role": "user", "content": "analyze stock"}],
        session=session,
        channel="websocket",
        chat_id="expert-batch",
        metadata={"expert_team": {"id": "asset-research-team"}},
        pending_queue=pending_queue,
    )
    assert injection_callback is not None

    async def _hang_forever():
        await asyncio.Event().wait()

    task_ids = ["business", "financial", "industry", "risk"]
    hanging = [asyncio.create_task(_hang_forever()) for _ in task_ids]
    loop.subagents._session_tasks[session.key] = set(task_ids)
    loop.subagents._running_tasks.update(dict(zip(task_ids, hanging)))

    drain_task = asyncio.create_task(injection_callback())
    await asyncio.sleep(0)
    for index, task_id in enumerate(task_ids):
        status = "failed" if task_id == "industry" else "completed successfully"
        await pending_queue.put(InboundMessage(
            sender_id="subagent",
            channel="system",
            chat_id="websocket:expert-batch",
            content=f"[Subagent '{task_id}' {status}]\n\nResult:\nreport {index}",
            metadata={
                "injected_event": "subagent_result",
                "subagent_task_id": task_id,
            },
        ))
        await asyncio.sleep(0)
        if index < len(task_ids) - 1:
            assert not drain_task.done()

    results = await asyncio.wait_for(drain_task, timeout=2.0)
    assert len(results) == 1
    bundle = results[0]["content"]
    assert all(f"report {index}" in bundle for index in range(4))
    assert "all members terminal" in bundle
    assert "mark `team-lead` running" in bundle
    assert "`report-audit` running" in bundle
    assert "A single failed member is a degradable gap" in bundle

    for task in hanging:
        task.cancel()
    await asyncio.gather(*hanging, return_exceptions=True)


@pytest.mark.asyncio
async def test_expert_team_run_enables_injection_overflow_while_members_are_active(tmp_path):
    """The loop should opt expert teams into later-phase result delivery."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    session = Session(
        key="websocket:expert-overflow",
        metadata={"expert_team": {"id": "trading-analysis-team"}},
    )
    captured_spec = None

    async def fake_runner_run(spec):
        nonlocal captured_spec
        captured_spec = spec
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)
    await loop._run_agent_loop(
        [{"role": "user", "content": "analyze stock"}],
        session=session,
        channel="websocket",
        chat_id="expert-overflow",
        metadata={},
        pending_queue=pending_queue,
    )

    assert captured_spec is not None
    assert captured_spec.injection_overflow_predicate is not None
    assert captured_spec.final_response_guard is None
    loop.subagents.get_running_count_by_session = MagicMock(return_value=1)
    assert captured_spec.injection_overflow_predicate() is True
    loop.subagents.get_running_count_by_session.return_value = 0
    assert captured_spec.injection_overflow_predicate() is False


def test_expert_team_completion_guard_requires_final_delivery_tools() -> None:
    from nanobot.agent.loop import _expert_team_completion_guard_message

    team = {
        "completion": {
            "required_tools": ["write_file", "create_docx", "create_pdf"],
            "required_artifacts": ["html", "docx", "pdf"],
        },
    }
    partial = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "write",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{}"},
        }],
    }]

    message = _expert_team_completion_guard_message(team, partial)
    assert message is not None
    assert "`create_docx`" in message
    assert "`create_pdf`" in message
    assert "do not use `write_stdin`" in message

    complete = [*partial, {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "docx",
                "type": "function",
                "function": {"name": "create_docx", "arguments": "{}"},
            },
            {
                "id": "pdf",
                "type": "function",
                "function": {"name": "create_pdf", "arguments": "{}"},
            },
        ],
    }]
    assert _expert_team_completion_guard_message(team, complete) is None


@pytest.mark.asyncio
async def test_expert_team_run_configures_final_delivery_guard(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    session = Session(
        key="websocket:expert-completion",
        metadata={
            "expert_team": {
                "id": "trading-analysis-team",
                "completion": {
                    "required_tools": ["write_file", "create_docx", "create_pdf"],
                    "required_artifacts": ["html", "docx", "pdf"],
                },
            },
        },
    )
    captured_spec = None

    async def fake_runner_run(spec):
        nonlocal captured_spec
        captured_spec = spec
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)
    await loop._run_agent_loop(
        [{"role": "user", "content": "analyze stock"}],
        session=session,
        channel="websocket",
        chat_id="expert-completion",
        pending_queue=asyncio.Queue(),
    )

    assert captured_spec is not None
    assert captured_spec.final_response_guard is not None
    message = captured_spec.final_response_guard([
        {"role": "assistant", "content": "risk manager says SELL"},
    ])
    assert message is not None
    assert "not complete yet" in message


@pytest.mark.asyncio
async def test_drain_pending_no_block_when_no_subagents(tmp_path):
    """_drain_pending should not block when no sub-agents are running."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue = asyncio.Queue()
    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=None,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # With no sub-agents and empty queue, should return immediately
    results = await asyncio.wait_for(injection_callback(), timeout=1.0)
    assert results == []


@pytest.mark.asyncio
async def test_drain_pending_timeout(tmp_path):
    """_drain_pending should return empty after timeout when sub-agents hang."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue = asyncio.Queue()
    session = Session(key="test:drain-timeout")
    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    # Register a "running" sub-agent that will never complete
    async def _hang_forever():
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())
    loop.subagents._session_tasks.setdefault(session.key, set()).add("sub-timeout-1")
    loop.subagents._running_tasks["sub-timeout-1"] = hang_task

    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=session,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # Patch the timeout path without leaking the queue.get() coroutine.
    async def _timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    with patch("nanobot.agent.loop.asyncio.wait_for", side_effect=_timeout):
        results = await injection_callback()
        assert results == []

    # Cleanup
    hang_task.cancel()
    try:
        await hang_task
    except asyncio.CancelledError:
        pass

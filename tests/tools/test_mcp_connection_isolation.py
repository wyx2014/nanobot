import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from nanobot.agent.tools import mcp
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        fail_on_initialize=set(), failures=set(), blocked=set(),
        triggers={}, initialized={}, entered=[], exited=[],
    )

    @asynccontextmanager
    async def transport(params):
        name = params.command
        trigger = state.triggers.setdefault(name, asyncio.Event())
        state.entered.append((name, asyncio.current_task()))

        async def fail_in_background():
            await trigger.wait()
            raise RuntimeError("connector reader failed")

        try:
            async with anyio.create_task_group() as group:
                if name in state.failures:
                    group.start_soon(fail_in_background)
                yield object(), name
                group.cancel_scope.cancel()
        finally:
            state.exited.append((name, asyncio.current_task()))

    @asynccontextmanager
    async def session(_read, name):
        async def initialize():
            state.initialized.setdefault(name, asyncio.Event()).set()
            if name in state.fail_on_initialize:
                state.triggers[name].set()
            if name in state.blocked or name in state.fail_on_initialize:
                await asyncio.Event().wait()

        async def list_tools():
            return SimpleNamespace(tools=[SimpleNamespace(
                name="lookup", description="Lookup", inputSchema={"type": "object"},
            )])

        async with anyio.create_task_group():
            yield SimpleNamespace(initialize=initialize, list_tools=list_tools)

    async def load_runtime():
        return session, SimpleNamespace, None, transport, None

    monkeypatch.setattr(mcp, "_load_mcp_client_runtime_async", load_runtime)
    return state


async def close_stacks(stacks):
    for stack in stacks.values():
        # Reload/shutdown may be called from a different task than connection setup.
        await asyncio.create_task(stack.aclose())


async def test_connector_background_failure_during_handshake_is_isolated(runtime):
    runtime.failures.add("broken")
    runtime.fail_on_initialize.add("broken")
    registry = ToolRegistry()
    async with asyncio.timeout(3):
        stacks = await mcp.connect_mcp_servers({
            "broken": MCPServerConfig(command="broken"),
            "healthy": MCPServerConfig(command="healthy"),
        }, registry)
    try:
        assert set(stacks) == {"healthy"}
        assert registry.tool_names == ["mcp_healthy_lookup"]
        assert asyncio.current_task().cancelling() == 0
        await asyncio.sleep(0)
    finally:
        await close_stacks(stacks)
    assert runtime.entered == runtime.exited
    assert all(task.done() for _, task in runtime.entered)


async def test_connector_failure_after_ready_removes_only_its_tools_and_reconnects(runtime):
    class State:
        _mcp_connecting = False
        _mcp_connected = False

    state = State()
    state._mcp_servers = {
        "broken_child": MCPServerConfig(command="healthy"),
        "broken": MCPServerConfig(command="broken"),
    }
    state._mcp_stacks = {}
    runtime.failures.add("broken")
    registry = ToolRegistry()
    await mcp.connect_missing_servers(state, registry)
    failed_owner = state._mcp_stacks["broken"].mcp_owner_task
    healthy_owner = state._mcp_stacks["broken_child"].mcp_owner_task
    try:
        runtime.triggers["broken"].set()
        async with asyncio.timeout(3):
            await asyncio.gather(failed_owner, return_exceptions=True)
        assert registry.tool_names == ["mcp_broken_child_lookup"]
        assert not healthy_owner.done()
        assert asyncio.current_task().cancelling() == 0

        runtime.failures.clear()
        await mcp.connect_missing_servers(state, registry)
        assert state._mcp_stacks["broken"].mcp_owner_task is not failed_owner
        assert mcp._stack_is_live(state._mcp_stacks["broken"])
        assert set(registry.tool_names) == {"mcp_broken_child_lookup", "mcp_broken_lookup"}
    finally:
        await close_stacks(state._mcp_stacks)
    assert sorted(runtime.entered, key=lambda row: id(row[1])) == sorted(
        runtime.exited, key=lambda row: id(row[1]),
    )


async def test_caller_cancellation_propagates_and_cleans_all_connections(runtime):
    runtime.blocked.add("slow")
    started = runtime.initialized.setdefault("slow", asyncio.Event())
    registry = ToolRegistry()
    task = asyncio.create_task(mcp.connect_mcp_servers({
        "healthy": MCPServerConfig(command="healthy"),
        "slow": MCPServerConfig(command="slow"),
    }, registry))
    try:
        async with asyncio.timeout(3):
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert registry.tool_names == []
    assert all(owner.done() for _, owner in runtime.entered)
    assert sorted(runtime.entered, key=lambda row: row[0]) == sorted(
        runtime.exited, key=lambda row: row[0],
    )


async def test_connector_timeout_leaves_caller_and_other_connections_alive(runtime):
    runtime.blocked.add("slow")
    registry = ToolRegistry()
    async with asyncio.timeout(3):
        stacks = await mcp.connect_mcp_servers({
            "slow": MCPServerConfig(command="slow", connect_timeout=1),
            "healthy": MCPServerConfig(command="healthy"),
        }, registry)
    try:
        assert set(stacks) == {"healthy"}
        assert registry.tool_names == ["mcp_healthy_lookup"]
        assert asyncio.current_task().cancelling() == 0
    finally:
        await close_stacks(stacks)
    assert runtime.entered == runtime.exited

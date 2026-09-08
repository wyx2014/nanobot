import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.graph.workflows.asset_research import MEMBER_NODES
from nanobot.webui.gateway_services import build_gateway_services


@pytest.fixture
def channel(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    config = WebSocketConfig(enabled=True, allow_from=["*"])
    gateway = build_gateway_services(
        config=config, bus=bus, session_manager=None, static_dist_path=None,
        workspace_path=tmp_path, default_restrict_to_workspace=True,
        runtime_model_name=None, runtime_surface="browser", runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    team = {"id": "asset-research-team", "name": "资产投研团队",
            "members": [{"id": member, "name": member} for member in MEMBER_NODES]}
    monkeypatch.setattr("nanobot.channels.websocket.normalize_expert_team_binding", lambda _: team)
    channel._route_expert_team_turn = AsyncMock(side_effect=AssertionError("must bypass model router"))
    channel._team_revisions = MagicMock()
    channel._team_revisions.consume.return_value = {
        "plan_id": "plan", "run_id": "v1", "target": "比亚迪",
        "selected_roles": ["risk-assessor"], "supplement_path": "reports/supplement.json",
        "source": {"graph_state": {
            "run_id": "v1", "target": "比亚迪", "degraded": True,
            "members": {member: {"status": "failed" if member == "risk-assessor" else "completed",
                                  "artifact": f"reports/v1/{member}.md"} for member in MEMBER_NODES},
        }},
    }
    return channel, bus


@pytest.mark.asyncio
async def test_explicit_revision_bypasses_router_and_projects_only_selected_role(channel):
    channel, bus = channel
    connection = AsyncMock()
    await channel._dispatch_envelope(connection, "desktop", {
        "type": "message", "chat_id": "byd", "content": "更新风险角色",
        "expert_team": {"id": "asset-research-team"}, "expert_team_revision_plan_id": "plan",
    })
    bus.publish_inbound.assert_awaited_once()
    metadata = bus.publish_inbound.await_args.args[0].metadata
    assert metadata["expert_team_resume"]["selected_roles"] == ["risk-assessor"]
    assert metadata["_expert_team_turn_route_source"] == "explicit_revision"
    graph = channel._team_runs[("byd", metadata["expert_team_run_id"])]["graph_state"]
    assert graph["active_nodes"] == ["risk-assessor"]
    assert graph["members"]["financial-analyst"]["reused"] is True
    channel._route_expert_team_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_revision_rejection_never_falls_back_to_full_research(channel):
    channel, bus = channel
    channel._team_revisions.consume.side_effect = ValueError("方案已失效")
    connection = AsyncMock()
    await channel._dispatch_envelope(connection, "desktop", {
        "type": "message", "chat_id": "byd", "content": "更新风险角色",
        "expert_team": {"id": "asset-research-team"}, "expert_team_revision_plan_id": "old",
    })
    bus.publish_inbound.assert_not_awaited()
    channel._route_expert_team_turn.assert_not_awaited()
    event = json.loads(connection.send.await_args.args[0])
    assert event["detail"] == "expert_team_revision_rejected"
    assert event["chat_id"] == "byd"


@pytest.mark.asyncio
async def test_prepare_only_returns_correlated_plan_without_agent_work(channel):
    channel, bus = channel
    channel._team_revisions.prepare.return_value = {"plan_id": "new-plan"}
    connection = AsyncMock()
    await channel._dispatch_envelope(connection, "desktop", {
        "type": "expert_team_revision", "action": "prepare", "request_id": "request",
        "chat_id": "byd", "run_id": "v1", "roles": ["risk-assessor"],
    })
    event = json.loads(connection.send.await_args.args[0])
    assert event["request_id"] == "request"
    assert event["result"]["plan_id"] == "new-plan"
    bus.publish_inbound.assert_not_awaited()
    channel._team_revisions.consume.assert_not_called()

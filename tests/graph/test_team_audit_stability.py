from unittest.mock import AsyncMock

import pytest

from nanobot.graph.workflows.asset_research_runtime import (
    AgentNodeOutcome,
    AssetResearchWorkflowRuntime,
    MemberBatchOutcome,
    MemberNodeOutcome,
)
from nanobot.graph.workflows.supply_chain_bottleneck_runtime import (
    SupplyChainBottleneckWorkflowRuntime,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [
    AssetResearchWorkflowRuntime, SupplyChainBottleneckWorkflowRuntime,
])
@pytest.mark.parametrize(("content", "reason", "verified"), [
    ("Audit passed", "completed", True),
    ("", "completed", False),
    ("provider rate limited", "error", False),
    ("read failed", "tool_error", False),
    ("Partial findings", "max_iterations", False),
])
async def test_report_delivery_preserves_audit_failure(runtime, content, reason, verified):
    async def run_node(node_id, _prompt, _stream):
        if node_id == "report-audit":
            return AgentNodeOutcome(content=content, stop_reason=reason)
        if node_id == "team-lead":
            return AgentNodeOutcome(content="Report ready", artifacts=["reports/report.html"])
        return AgentNodeOutcome(content="Verified source package")

    async def run_members(tasks):
        return MemberBatchOutcome(members={
            key: MemberNodeOutcome(member_id=key, status="completed", content="Evidence")
            for key in tasks
        })

    publish = AsyncMock()
    outcome = await runtime(
        run_agent_node=run_node, run_member_wave=run_members, publish_state=publish,
    ).run(run_id="audit-stability", target="Example", request="Research Example",
          team={"members": []}, report_path="reports/report.md")

    assert outcome.artifacts == ["reports/report.html"]
    assert outcome.stop_reason == ("completed" if verified else "completed_with_warnings")
    assert outcome.graph_state["audit"]["verified"] is verified
    assert outcome.graph_state["node_states"]["report-audit"]["status"] == (
        "completed" if verified else "failed"
    )
    assert outcome.graph_state["active_nodes"] == []
    if not verified:
        warning = outcome.graph_state["audit"]["warning"]
        assert warning and warning in outcome.final_content
        assert publish.await_args.args[2] == warning
        if reason in {"error", "tool_error"}:
            assert content not in outcome.final_content

from __future__ import annotations

from typing import Any

import pytest

from nanobot.graph.workflows.asset_research import (
    MEMBER_NODES,
    REPORT_AUDIT,
    TEAM_LEAD,
    advance_asset_research_graph,
    new_asset_research_state,
)
from nanobot.graph.workflows.asset_research_runtime import (
    AgentNodeOutcome,
    AssetResearchWorkflowRuntime,
    MemberBatchOutcome,
    MemberNodeOutcome,
)


def _team() -> dict[str, Any]:
    return {
        "id": "asset-research-team",
        "members": [
            {"id": member_id, "name": member_id}
            for member_id in MEMBER_NODES
        ],
    }


def _member_batch(*, missing: str | None = None) -> MemberBatchOutcome:
    return MemberBatchOutcome(members={
        member_id: MemberNodeOutcome(
            member_id=member_id,
            status="completed",
            content=f"{member_id} current-run report",
            artifact=f"reports/.team-runs/run-1/members/{member_id}.md",
        )
        for member_id in MEMBER_NODES
        if member_id != missing
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("role_status", ["completed", "failed", "cancelled"])
async def test_revision_executes_only_selected_role_and_preserves_prior(role_status) -> None:
    from copy import deepcopy

    prior = new_asset_research_state(run_id="v1", member_ids=MEMBER_NODES)
    prior = advance_asset_research_graph(prior, "data_package_ready", {"artifact": "reports/base.md"})
    for member in MEMBER_NODES:
        prior = advance_asset_research_graph(prior, "member_updated", {
            "id": member, "status": "failed" if member == "risk-assessor" else "completed",
            "artifact": f"reports/v1/{member}.md",
        })
    prior = advance_asset_research_graph(prior, "report_written", {"artifact": "reports/v1.html"})
    prior = advance_asset_research_graph(prior, "audit_completed")
    original = deepcopy(prior)
    calls = []
    checkpoints = []

    async def node(name, prompt, stream):
        calls.append(name)
        assert name != "data-package"
        if name == TEAM_LEAD:
            assert "Revision v2" in prompt
            assert "reports/v1.html" in prompt
            assert "supplement.json" in prompt
        return AgentNodeOutcome(content="revised", artifacts=["reports/v2.html"])

    async def members(tasks):
        assert list(tasks) == ["risk-assessor"]
        assert "reports/base.md" in tasks["risk-assessor"]
        assert "reports/v1/financial-analyst.md" in tasks["risk-assessor"]
        calls.extend(tasks)
        return MemberBatchOutcome(members={"risk-assessor": MemberNodeOutcome(
            member_id="risk-assessor", status=role_status, content="risk result",
            artifact="reports/v2/risk.md",
        )})

    async def publish(state, event, activity):
        checkpoints.append(deepcopy(state))

    result = await AssetResearchWorkflowRuntime(
        run_agent_node=node, run_member_wave=members, publish_state=publish,
    ).run(run_id="v2", target="比亚迪", request="更新风险", team=_team(),
          report_path="reports/v2.md", resume_from=prior,
          rerun_member_ids=["risk-assessor"], supplemental_artifacts=["supplement.json"])
    assert calls == ["risk-assessor", TEAM_LEAD, REPORT_AUDIT]
    assert checkpoints[0]["active_nodes"] == ["risk-assessor"]
    assert result.graph_state["report_version"] == 2
    assert result.graph_state["degraded"] == (role_status != "completed")
    assert prior == original
    for member in MEMBER_NODES[:-1]:
        assert result.graph_state["members"][member]["artifact"] == prior["members"][member]["artifact"]
        assert result.graph_state["members"][member]["reused"] is True


def test_revision_rejects_unknown_member_before_execution() -> None:
    with pytest.raises(ValueError, match="valid member"):
        new_asset_research_state(run_id="v2", member_ids=MEMBER_NODES,
                                 resume_from={}, rerun_member_ids=["unknown"])


@pytest.mark.asyncio
async def test_revision_stops_at_synthesis_budget_and_keeps_completed_roles():
    prior = new_asset_research_state(run_id="old", member_ids=MEMBER_NODES)
    prior["user_supplements"] = ["uploads/prior-supplement.json"]
    prior = advance_asset_research_graph(prior, "data_package_ready", {"artifact": "old/base.md"})
    for member in MEMBER_NODES:
        prior = advance_asset_research_graph(prior, "member_updated", {
            "id": member, "status": "completed", "artifact": f"old/{member}.md",
        })
    calls = []

    async def node(name, prompt, stream):
        calls.append(name)
        assert "uploads/prior-supplement.json" in prompt
        assert "No new external research" in prompt
        return AgentNodeOutcome(stop_reason="max_iterations", content="no report yet")

    async def members(tasks):
        raise AssertionError("completed roles must not rerun during synthesis recovery")

    async def publish(*args):
        pass

    result = await AssetResearchWorkflowRuntime(
        run_agent_node=node, run_member_wave=members, publish_state=publish,
    ).run(run_id="new", target="China Taiping", request="continue synthesis", team=_team(),
          report_path="reports/v2.md", resume_from=prior)
    assert calls == [TEAM_LEAD]
    assert result.stop_reason == "workflow_error"
    assert result.graph_state["user_supplements"] == ["uploads/prior-supplement.json"]
    for member in MEMBER_NODES:
        assert result.graph_state["members"][member]["artifact"] == f"old/{member}.md"


@pytest.mark.asyncio
async def test_runtime_executes_fixed_fanout_join_and_delivery_order() -> None:
    node_calls: list[str] = []
    published: list[tuple[str, list[str]]] = []
    member_prompts: dict[str, str] = {}

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        if node_id == "data-package":
            return AgentNodeOutcome(content="verified data package")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(
                content="draft complete",
                artifacts=["reports/北方华创-run-1-投资研究报告.html"],
            )
        return AgentNodeOutcome(content="审校完成，HTML 报告已交付")

    async def run_members(tasks: dict[str, str]) -> MemberBatchOutcome:
        member_prompts.update(tasks)
        return _member_batch()

    async def publish(state: dict[str, Any], event: str, _activity: str) -> None:
        published.append((event, list(state["active_nodes"])))

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="北方华创",
        request="A股 北方华创",
        team=_team(),
        report_path="reports/北方华创-run-1-投资研究报告.md",
    )

    assert node_calls == ["data-package", TEAM_LEAD, REPORT_AUDIT]
    assert tuple(member_prompts) == MEMBER_NODES
    assert published[1] == ("data_package_ready", list(MEMBER_NODES))
    member_updates = [active for event, active in published if event == "member_updated"]
    assert all(TEAM_LEAD not in active for active in member_updates[:-1])
    assert member_updates[-1] == [TEAM_LEAD]
    assert published[-2] == ("report_written", [REPORT_AUDIT])
    assert published[-1] == ("audit_completed", [])
    assert outcome.graph_state["node"] == "delivered"
    assert outcome.stop_reason == "completed"


@pytest.mark.asyncio
async def test_audit_iteration_limit_delivers_business_summary_without_runtime_copy() -> None:
    audit_prompt = ""

    async def run_node(node_id: str, prompt: str, _stream: bool) -> AgentNodeOutcome:
        nonlocal audit_prompt
        if node_id == "data-package":
            return AgentNodeOutcome(content="verified data package")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(artifacts=["reports/安集科技-run-1-投资研究报告.html"])
        audit_prompt = prompt
        return AgentNodeOutcome(
            content=(
                "核心结论：公司竞争优势明确，但当前估值仍需保留安全边际。"
                "主要风险是下游需求与盈利兑现不及预期。"
                "完整 HTML 报告：`reports/安集科技-run-1-投资研究报告.html`"
            ),
            stop_reason="max_iterations",
        )

    async def run_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        return _member_batch()

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="安集科技",
        request="分析安集科技",
        team=_team(),
        report_path="reports/安集科技-run-1-投资研究报告.md",
    )

    assert "at most 6 model rounds" in audit_prompt
    assert "5 tool-capable rounds" in audit_prompt
    assert "at most one complete rewrite" in audit_prompt
    assert "`edit_file` is intentionally unavailable" in audit_prompt
    assert "do not repeat its path" in audit_prompt
    assert "核心结论" in outcome.final_content
    assert "完整 HTML 报告" not in outcome.final_content
    assert "reports/安集科技-run-1-投资研究报告.html" not in outcome.final_content
    assert "reports/安集科技-run-1-投资研究报告.html" in outcome.artifacts
    assert "轮上限" not in outcome.final_content
    assert "降级" not in outcome.final_content
    assert "fallback" not in outcome.final_content.lower()
    assert "iteration" not in outcome.final_content.lower()
    assert outcome.stop_reason == "completed_with_warnings"
    assert outcome.graph_state["status"] == "completed_with_warnings"
    assert outcome.graph_state["audit"]["verified"] is False
    assert "审校未完成" in outcome.final_content


@pytest.mark.asyncio
async def test_audit_generic_iteration_fallback_is_hidden_from_delivery() -> None:
    async def run_node(
        node_id: str,
        _prompt: str,
        _stream: bool,
    ) -> AgentNodeOutcome:
        if node_id == "data-package":
            return AgentNodeOutcome(content="verified data package")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(artifacts=["reports/安集科技-run-1-投资研究报告.html"])
        return AgentNodeOutcome(
            content=(
                "I reached the maximum number of tool call iterations (5) "
                "without completing the task."
            ),
            stop_reason="max_iterations",
        )

    async def run_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        return _member_batch()

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="安集科技",
        request="分析安集科技",
        team=_team(),
        report_path="reports/安集科技-run-1-投资研究报告.md",
    )

    assert "maximum number" not in outcome.final_content.lower()
    assert "轮上限" not in outcome.final_content
    assert "降级" not in outcome.final_content
    assert "完整 HTML 报告" not in outcome.final_content
    assert "reports/安集科技-run-1-投资研究报告.html" not in outcome.final_content
    assert "reports/安集科技-run-1-投资研究报告.html" in outcome.artifacts
    assert outcome.graph_state["status"] == "completed_with_warnings"
    assert "审校未完成" in outcome.final_content


@pytest.mark.asyncio
async def test_missing_member_result_is_terminal_degradation_not_a_skipped_join() -> None:
    seen_lead_prompt = ""

    async def run_node(node_id: str, prompt: str, _stream: bool) -> AgentNodeOutcome:
        nonlocal seen_lead_prompt
        if node_id == "data-package":
            return AgentNodeOutcome(content="package")
        if node_id == TEAM_LEAD:
            seen_lead_prompt = prompt
            return AgentNodeOutcome(artifacts=["reports/report.html"])
        return AgentNodeOutcome(content="done")

    async def run_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        return _member_batch(missing="financial-analyst")

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="北方华创",
        request="分析北方华创",
        team=_team(),
        report_path="reports/report.md",
    )

    assert outcome.graph_state["members"]["financial-analyst"]["status"] == "failed"
    assert outcome.graph_state["degraded"] is True
    assert "Runtime did not receive a terminal member result" in seen_lead_prompt
    assert outcome.graph_state["status"] == "completed_with_warnings"


@pytest.mark.asyncio
async def test_runtime_never_enters_audit_without_a_real_report_artifact() -> None:
    node_calls: list[str] = []
    states: list[dict[str, Any]] = []

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        return AgentNodeOutcome(
            content="package" if node_id == "data-package" else "wrong file",
            artifacts=(
                []
                if node_id == "data-package"
                else ["reports/unrelated-old-report.html"]
            ),
        )

    async def run_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        return _member_batch()

    async def publish(state: dict[str, Any], _event: str, _activity: str) -> None:
        states.append(state)

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="北方华创",
        request="分析北方华创",
        team=_team(),
        report_path="reports/report.md",
    )

    assert node_calls == ["data-package", TEAM_LEAD, TEAM_LEAD]
    assert REPORT_AUDIT not in node_calls
    assert outcome.stop_reason == "workflow_error"
    assert states[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_resume_starts_at_team_lead_and_does_not_rerun_member_wave() -> None:
    prior = new_asset_research_state(run_id="old", member_ids=MEMBER_NODES)
    prior = advance_asset_research_graph(prior, "data_package_ready", {"content": "old package"})
    for member_id in MEMBER_NODES:
        prior = advance_asset_research_graph(
            prior,
            "member_updated",
            {"id": member_id, "status": "completed", "content": f"{member_id} report"},
        )

    node_calls: list[str] = []

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(artifacts=["reports/resumed.html"])
        return AgentNodeOutcome(content="resume delivered")

    async def fail_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        raise AssertionError("resume must not rerun the member wave")

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    outcome = await AssetResearchWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=fail_members,
        publish_state=publish,
    ).run(
        run_id="new",
        target="北方华创",
        request="补充上次数据",
        team=_team(),
        report_path="reports/resumed.md",
        resume_from=prior,
    )

    assert node_calls == [TEAM_LEAD, REPORT_AUDIT]
    assert outcome.graph_state["resume_from_run_id"] == "old"

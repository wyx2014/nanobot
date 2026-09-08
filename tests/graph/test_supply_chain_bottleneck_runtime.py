from __future__ import annotations

from typing import Any

import pytest

from nanobot.graph.workflows.asset_research_runtime import (
    AgentNodeOutcome,
    MemberBatchOutcome,
    MemberNodeOutcome,
)
from nanobot.graph.workflows.supply_chain_bottleneck import (
    DISCOVERY_MEMBER_NODES,
    MEMBER_NODES,
    REPORT_AUDIT,
    SCOPE_BRIEF,
    TEAM_LEAD,
    VALIDATION_MEMBER_NODES,
    advance_supply_chain_bottleneck_graph,
    new_supply_chain_bottleneck_state,
)
from nanobot.graph.workflows.supply_chain_bottleneck_runtime import (
    SupplyChainBottleneckWorkflowRuntime,
)


def _team() -> dict[str, Any]:
    return {
        "id": "supply-chain-bottleneck-team",
        "members": [
            {
                "id": member_id,
                "name": member_id,
                "description": f"responsibility for {member_id}",
                "instructions": f"playbook for {member_id}",
            }
            for member_id in MEMBER_NODES
        ],
    }


def _batch(tasks: dict[str, str]) -> MemberBatchOutcome:
    return MemberBatchOutcome(members={
        member_id: MemberNodeOutcome(
            member_id=member_id,
            status="completed",
            content=f"{member_id} current-run evidence",
            artifact=f"reports/.team-runs/run-1/members/{member_id}.md",
        )
        for member_id in tasks
    })


def test_graph_enforces_discovery_join_before_validation_wave() -> None:
    state = new_supply_chain_bottleneck_state(
        run_id="run-1",
        member_ids=MEMBER_NODES,
    )
    assert state["active_nodes"] == [SCOPE_BRIEF]

    state = advance_supply_chain_bottleneck_graph(
        state,
        "scope_brief_ready",
        {"content": "AI infrastructure theme"},
    )
    assert state["active_nodes"] == list(DISCOVERY_MEMBER_NODES)

    state = advance_supply_chain_bottleneck_graph(
        state,
        "member_updated",
        {"id": DISCOVERY_MEMBER_NODES[0], "status": "completed", "content": "trend"},
    )
    assert state["active_nodes"] == [DISCOVERY_MEMBER_NODES[1]]

    state = advance_supply_chain_bottleneck_graph(
        state,
        "member_updated",
        {"id": DISCOVERY_MEMBER_NODES[1], "status": "completed", "content": "chain"},
    )
    assert state["active_nodes"] == list(VALIDATION_MEMBER_NODES)

    for member_id in VALIDATION_MEMBER_NODES:
        state = advance_supply_chain_bottleneck_graph(
            state,
            "member_updated",
            {"id": member_id, "status": "completed", "content": member_id},
        )
    assert state["active_nodes"] == [TEAM_LEAD]


@pytest.mark.asyncio
async def test_runtime_executes_two_member_waves_then_audit() -> None:
    node_calls: list[str] = []
    waves: list[tuple[str, ...]] = []
    validation_prompts: dict[str, str] = {}
    published: list[tuple[str, list[str]]] = []

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        if node_id == SCOPE_BRIEF:
            return AgentNodeOutcome(content="verified AI infrastructure theme card")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(
                content="draft complete",
                artifacts=["reports/bottleneck-map/AI-run-1-供应链瓶颈地图.html"],
            )
        return AgentNodeOutcome(content="瓶颈地图已审校，核心瓶颈与估值约束见报告。")

    async def run_members(tasks: dict[str, str]) -> MemberBatchOutcome:
        waves.append(tuple(tasks))
        if tuple(tasks) == VALIDATION_MEMBER_NODES:
            validation_prompts.update(tasks)
        return _batch(tasks)

    async def publish(state: dict[str, Any], event: str, _activity: str) -> None:
        published.append((event, list(state["active_nodes"])))

    outcome = await SupplyChainBottleneckWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
    ).run(
        run_id="run-1",
        target="AI基础设施",
        request="寻找 AI 基础设施供应链瓶颈",
        team=_team(),
        report_path="reports/bottleneck-map/AI-run-1-供应链瓶颈地图.md",
    )

    assert node_calls == [SCOPE_BRIEF, TEAM_LEAD, REPORT_AUDIT]
    assert waves == [DISCOVERY_MEMBER_NODES, VALIDATION_MEMBER_NODES]
    assert "trend-verifier current-run evidence" in validation_prompts["company-screener"]
    company_prompt = validation_prompts["company-screener"]
    normalized_company_prompt = " ".join(company_prompt.split())
    assert "consistent base table" in normalized_company_prompt
    assert "Never query every source for every field" in normalized_company_prompt
    assert published[-2] == ("report_written", [REPORT_AUDIT])
    assert published[-1] == ("audit_completed", [])
    assert outcome.graph_state["node"] == "delivered"
    assert outcome.graph_state["target"] == "AI基础设施"
    assert outcome.stop_reason == "completed"


@pytest.mark.asyncio
async def test_resume_reuses_both_waves_and_starts_at_team_lead() -> None:
    prior = new_supply_chain_bottleneck_state(
        run_id="old-run",
        member_ids=MEMBER_NODES,
    )
    prior = advance_supply_chain_bottleneck_graph(
        prior,
        "scope_brief_ready",
        {"content": "old theme card"},
    )
    for member_id in DISCOVERY_MEMBER_NODES:
        prior = advance_supply_chain_bottleneck_graph(
            prior,
            "member_updated",
            {"id": member_id, "status": "completed", "content": f"{member_id} old"},
        )
    for member_id in VALIDATION_MEMBER_NODES:
        prior = advance_supply_chain_bottleneck_graph(
            prior,
            "member_updated",
            {"id": member_id, "status": "completed", "content": f"{member_id} old"},
        )
    prior["target"] = "电网升级"

    node_calls: list[str] = []

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(
                artifacts=["reports/bottleneck-map/grid-resume-供应链瓶颈地图.html"],
            )
        return AgentNodeOutcome(content="更新后的瓶颈地图已完成审校。")

    async def fail_members(_tasks: dict[str, str]) -> MemberBatchOutcome:
        raise AssertionError("resume must not rerun either member wave")

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    outcome = await SupplyChainBottleneckWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=fail_members,
        publish_state=publish,
    ).run(
        run_id="resume",
        target="电网升级",
        request="基于上次报告补充最新扩产数据",
        team=_team(),
        report_path="reports/bottleneck-map/grid-resume-供应链瓶颈地图.md",
        resume_from=prior,
    )

    assert node_calls == [TEAM_LEAD, REPORT_AUDIT]
    assert outcome.graph_state["resume_from_run_id"] == "old-run"
    assert outcome.graph_state["status"] == "completed"


@pytest.mark.asyncio
async def test_runtime_persists_team_lead_content_without_rerunning_synthesis() -> None:
    node_calls: list[str] = []
    writes: list[tuple[str, str]] = []
    published: list[str] = []

    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        node_calls.append(node_id)
        if node_id == SCOPE_BRIEF:
            return AgentNodeOutcome(content="verified theme card")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(
                content=(
                    "# AI硬件供应链瓶颈地图\n\n"
                    "## 核心结论\n\n"
                    + "基于成员证据形成瓶颈、公司映射和估值约束。" * 40
                ),
            )
        return AgentNodeOutcome(content="降级报告已审校，核心风险与缺口见报告。")

    async def run_members(tasks: dict[str, str]) -> MemberBatchOutcome:
        return _batch(tasks)

    async def publish(_state: dict[str, Any], event: str, _activity: str) -> None:
        published.append(event)

    async def write_report(path: str, content: str) -> list[str]:
        writes.append((path, content))
        return [path.replace(".md", ".html")]

    outcome = await SupplyChainBottleneckWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=run_members,
        publish_state=publish,
        write_report=write_report,
    ).run(
        run_id="run-1",
        target="AI硬件",
        request="研究 AI 硬件供应链",
        team=_team(),
        report_path="reports/bottleneck-map/AI-run-1-供应链瓶颈地图.md",
    )

    assert node_calls == [SCOPE_BRIEF, TEAM_LEAD, REPORT_AUDIT]
    assert len(writes) == 1
    assert writes[0][0].endswith("供应链瓶颈地图.md")
    assert "核心结论" in writes[0][1]
    assert "report_recovered" in published
    assert outcome.stop_reason == "completed_with_warnings"
    assert outcome.graph_state["status"] == "completed_with_warnings"


@pytest.mark.asyncio
async def test_runtime_builds_evidence_report_when_lead_only_returns_placeholder() -> None:
    async def run_node(node_id: str, _prompt: str, _stream: bool) -> AgentNodeOutcome:
        if node_id == SCOPE_BRIEF:
            return AgentNodeOutcome(content="verified theme card")
        if node_id == TEAM_LEAD:
            return AgentNodeOutcome(content="Draft is ready for audit.")
        return AgentNodeOutcome(content="证据保全报告已审校。")

    async def publish(_state: dict[str, Any], _event: str, _activity: str) -> None:
        return None

    saved: list[str] = []

    async def write_report(path: str, content: str) -> list[str]:
        saved.append(content)
        return [path.replace(".md", ".html")]

    outcome = await SupplyChainBottleneckWorkflowRuntime(
        run_agent_node=run_node,
        run_member_wave=lambda tasks: _async_batch(tasks),
        publish_state=publish,
        write_report=write_report,
    ).run(
        run_id="run-1",
        target="AI硬件",
        request="研究 AI 硬件供应链",
        team=_team(),
        report_path="reports/bottleneck-map/AI-run-1-供应链瓶颈地图.md",
    )

    assert "降级交付" in saved[0]
    assert "trend-verifier current-run evidence" in saved[0]
    assert outcome.stop_reason == "completed_with_warnings"


async def _async_batch(tasks: dict[str, str]) -> MemberBatchOutcome:
    return _batch(tasks)

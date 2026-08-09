from __future__ import annotations

from nanobot.graph.workflows.asset_research import (
    AUDIT,
    DELIVERED,
    MEMBERS,
    PREPARATION,
    SYNTHESIS,
    advance_asset_research_graph,
    asset_research_topology,
    new_asset_research_state,
)


MEMBERS_IDS = (
    "business-analyst",
    "financial-analyst",
    "industry-researcher",
    "risk-assessor",
)


def _started_state() -> dict:
    state = new_asset_research_state(run_id="run-1", member_ids=MEMBERS_IDS)
    return advance_asset_research_graph(
        state,
        "data_package_ready",
        {"artifact": "reports/.team-runs/run-1/data-package.md"},
    )


def test_asset_research_graph_declares_parallel_fanout_and_fanin() -> None:
    topology = asset_research_topology()

    assert topology["start"] == PREPARATION
    assert {node["name"]: node["kind"] for node in topology["nodes"]} == {
        PREPARATION: "preparation",
        MEMBERS: "parallel",
        SYNTHESIS: "agent",
        AUDIT: "audit",
        DELIVERED: "terminal",
    }


def test_four_member_artifacts_fan_in_before_synthesis() -> None:
    state = _started_state()
    assert state["node"] == MEMBERS

    for index, member_id in enumerate(MEMBERS_IDS):
        state = advance_asset_research_graph(
            state,
            "member_updated",
            {
                "id": member_id,
                "status": "completed",
                "artifact": f"reports/.team-runs/run-1/members/{member_id}.md",
            },
        )
        if index < len(MEMBERS_IDS) - 1:
            assert state["node"] == MEMBERS

    assert state["node"] == SYNTHESIS
    assert all(member.get("artifact") for member in state["members"].values())
    assert state["degraded"] is False


def test_failed_member_still_reaches_audited_degraded_delivery() -> None:
    state = _started_state()
    for member_id in MEMBERS_IDS:
        status = "failed" if member_id == "financial-analyst" else "completed"
        state = advance_asset_research_graph(
            state,
            "member_updated",
            {
                "id": member_id,
                "status": status,
                "artifact": f"reports/.team-runs/run-1/members/{member_id}.md",
            },
        )

    assert state["node"] == SYNTHESIS
    assert state["degraded"] is True
    state = advance_asset_research_graph(
        state,
        "report_written",
        {"artifact": "reports/company-report.html"},
    )
    assert state["node"] == AUDIT
    state = advance_asset_research_graph(state, "audit_completed")

    assert state["node"] == DELIVERED
    assert state["status"] == "completed_with_warnings"
    assert state["artifacts"]["report"] == "reports/company-report.html"


def test_user_supplement_resumes_at_synthesis_without_rerunning_members() -> None:
    prior = _started_state()
    for member_id in MEMBERS_IDS:
        prior = advance_asset_research_graph(
            prior,
            "member_updated",
            {
                "id": member_id,
                "status": "failed" if member_id == "risk-assessor" else "completed",
                "artifact": f"reports/.team-runs/run-1/members/{member_id}.md",
            },
        )

    resumed = new_asset_research_state(
        run_id="run-2",
        member_ids=MEMBERS_IDS,
        resume_from=prior,
        supplemental_artifacts=["uploads/missing-risk-data.xlsx"],
    )

    assert resumed["node"] == SYNTHESIS
    assert resumed["resume_from_run_id"] == "run-1"
    assert resumed["user_supplements"] == ["uploads/missing-risk-data.xlsx"]
    assert all(member["status"] == "completed" for member in resumed["members"].values())
    assert resumed["members"]["risk-assessor"]["artifact"].endswith(
        "risk-assessor.md"
    )


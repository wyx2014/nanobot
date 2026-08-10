"""Runtime-owned DAG for the four-role asset research workflow.

Topology (and execution authority):

    data-package
      |-- business-analyst --|
      |-- financial-analyst -|
      |-- industry-researcher|--> team-lead --> report-audit
      `-- risk-assessor -----|

The four role nodes are real graph nodes.  They activate in one wave and the
Team Lead cannot activate until every branch is terminal.  Model output never
selects or skips a node.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from nanobot.graph.engine import Graph, GraphNode, NodeResult, advance_graph

DATA_PACKAGE = "data-package"
BUSINESS_ANALYST = "business-analyst"
FINANCIAL_ANALYST = "financial-analyst"
INDUSTRY_RESEARCHER = "industry-researcher"
RISK_ASSESSOR = "risk-assessor"
TEAM_LEAD = "team-lead"
REPORT_AUDIT = "report-audit"

MEMBER_NODES = (
    BUSINESS_ANALYST,
    FINANCIAL_ANALYST,
    INDUSTRY_RESEARCHER,
    RISK_ASSESSOR,
)
TERMINAL_MEMBER_STATES = frozenset({"completed", "failed", "cancelled"})
CORE_STRUCTURED_SOURCES = ("ifind", "juyuan", "caihui")

# Compatibility stage names used by persisted projections and older clients.
PREPARATION = "preparation"
MEMBERS = "members"
SYNTHESIS = "synthesis"
AUDIT = "audit"
DELIVERED = "delivered"


def _reduce_data_package(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> NodeResult | None:
    if event != "data_package_ready":
        return None
    package = {
        "status": "completed",
        **({"content": str(payload["content"])} if payload.get("content") else {}),
        **({"artifact": str(payload["artifact"])} if payload.get("artifact") else {}),
    }
    return NodeResult(
        writes={
            "data_package": package,
            "degraded": bool(state.get("degraded")) or payload.get("degraded") is True,
        },
        settled=True,
    )


def _member_reducer(member_id: str):
    def reduce(
        state: dict[str, Any],
        event: str,
        payload: Mapping[str, Any],
    ) -> NodeResult | None:
        if event != "member_updated" or str(payload.get("id") or "") != member_id:
            return None
        members = deepcopy(state.get("members") or {})
        member = dict(members.get(member_id) or {})
        status = str(payload.get("status") or member.get("status") or "running")
        member.update({
            "status": status,
            **({"activity": str(payload["activity"])} if payload.get("activity") else {}),
            **({"artifact": str(payload["artifact"])} if payload.get("artifact") else {}),
            **({"content": str(payload["content"])} if payload.get("content") else {}),
        })
        members[member_id] = member
        settled = status in TERMINAL_MEMBER_STATES
        return NodeResult(
            writes={
                "members": members,
                "degraded": bool(state.get("degraded")) or status in {"failed", "cancelled"},
            },
            settled=settled,
            status=status if settled else "running",
        )

    return reduce


def _reduce_team_lead(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> NodeResult | None:
    if event == "user_supplement":
        supplements = list(state.get("user_supplements") or [])
        supplements.extend(
            str(item) for item in payload.get("artifacts", []) if str(item).strip()
        )
        text = str(payload.get("content") or "").strip()
        if text:
            supplements.append(text)
        return NodeResult(
            writes={"user_supplements": list(dict.fromkeys(supplements))},
        )
    if event != "report_written":
        return None
    artifact = str(payload.get("artifact") or "").strip()
    if not artifact:
        return None
    artifacts = dict(state.get("artifacts") or {})
    artifacts["report"] = artifact
    return NodeResult(writes={"artifacts": artifacts}, settled=True)


def _reduce_report_audit(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> NodeResult | None:
    if event != "audit_completed":
        return None
    artifacts = dict(state.get("artifacts") or {})
    for key, value in dict(payload.get("artifacts") or {}).items():
        if str(value).strip():
            artifacts[str(key)] = str(value)
    status = "completed_with_warnings" if state.get("degraded") else "completed"
    return NodeResult(
        writes={"artifacts": artifacts, "status": status},
        settled=True,
    )


def build_asset_research_graph() -> Graph:
    graph = Graph(
        name="asset-research",
        start=DATA_PACKAGE,
        terminal_stage=DELIVERED,
    )
    graph.add_node(GraphNode(
        DATA_PACKAGE,
        kind="agent",
        stage=PREPARATION,
        reducer=_reduce_data_package,
    ))
    for member_id in MEMBER_NODES:
        graph.add_node(GraphNode(
            member_id,
            kind="agent",
            stage=MEMBERS,
            reducer=_member_reducer(member_id),
        ))
    graph.add_node(GraphNode(
        TEAM_LEAD,
        kind="agent",
        stage=SYNTHESIS,
        reducer=_reduce_team_lead,
    ))
    graph.add_node(GraphNode(
        REPORT_AUDIT,
        kind="agent",
        stage=AUDIT,
        reducer=_reduce_report_audit,
    ))

    for member_id in MEMBER_NODES:
        graph.add_edge(DATA_PACKAGE, member_id)
        graph.add_edge(member_id, TEAM_LEAD)
    graph.add_edge(TEAM_LEAD, REPORT_AUDIT)
    return graph


ASSET_RESEARCH_GRAPH = build_asset_research_graph()


def new_asset_research_state(
    *,
    run_id: str,
    member_ids: Iterable[str],
    resume_from: Mapping[str, Any] | None = None,
    supplemental_artifacts: Iterable[str] = (),
) -> dict[str, Any]:
    """Create a fresh blackboard, optionally resuming at Team Lead synthesis."""

    requested_members = tuple(dict.fromkeys(str(item) for item in member_ids))
    if set(requested_members) != set(MEMBER_NODES):
        raise ValueError(
            "asset-research workflow requires exactly: " + ", ".join(MEMBER_NODES)
        )

    resumed = isinstance(resume_from, Mapping)
    prior_members = resume_from.get("members") if resumed else None
    members: dict[str, dict[str, Any]] = {}
    for member_id in MEMBER_NODES:
        prior = (
            prior_members.get(member_id)
            if isinstance(prior_members, Mapping)
            and isinstance(prior_members.get(member_id), Mapping)
            else {}
        )
        members[member_id] = {
            "status": "completed" if resumed else "pending",
            **({"artifact": str(prior["artifact"])} if prior.get("artifact") else {}),
            **({"content": str(prior["content"])} if prior.get("content") else {}),
        }

    settled_nodes = [DATA_PACKAGE, *MEMBER_NODES] if resumed else []
    active_nodes = [TEAM_LEAD] if resumed else [DATA_PACKAGE]
    node_states = {
        name: {
            "status": (
                "running" if name in active_nodes
                else "completed" if name in settled_nodes
                else "pending"
            )
        }
        for name in ASSET_RESEARCH_GRAPH.nodes
    }
    state: dict[str, Any] = {
        "schema_version": 2,
        "workflow": ASSET_RESEARCH_GRAPH.name,
        "run_id": run_id,
        "node": SYNTHESIS if resumed else PREPARATION,
        "active_nodes": active_nodes,
        "settled_nodes": settled_nodes,
        "node_states": node_states,
        "path": [*settled_nodes, *active_nodes] if resumed else [DATA_PACKAGE],
        "checkpoint_revision": 0,
        "status": "running",
        "degraded": bool(resume_from.get("degraded")) if resumed else False,
        "data_package": {"status": "completed" if resumed else "running"},
        "members": members,
        "artifacts": dict(resume_from.get("artifacts") or {}) if resumed else {},
        "user_supplements": [
            str(item) for item in supplemental_artifacts if str(item).strip()
        ],
        "source_policy": {
            "cross_validate": list(CORE_STRUCTURED_SOURCES),
            "fallback": ["anysearch", "duckduckgo"],
            "failed": [],
        },
        "events": [],
    }
    if resumed:
        state["resume_from_run_id"] = str(resume_from.get("run_id") or "")
    return state


def advance_asset_research_graph(
    state: Mapping[str, Any],
    event: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return advance_graph(ASSET_RESEARCH_GRAPH, state, event, payload)


def public_asset_research_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a lightweight checkpoint/projection of the workflow state.

    Full node output stays in the in-memory blackboard while the workflow is
    running. Persisted revisions only need status and artifact pointers;
    duplicating every data package and role report in every snapshot makes
    session history grow quadratically and slows reconnect and resume paths.
    """

    public = deepcopy(dict(state))
    data_package = public.get("data_package")
    if isinstance(data_package, dict):
        data_package.pop("content", None)
    members = public.get("members")
    if isinstance(members, dict):
        for member in members.values():
            if isinstance(member, dict):
                member.pop("content", None)
    return public


def asset_research_topology() -> dict[str, Any]:
    return ASSET_RESEARCH_GRAPH.describe()

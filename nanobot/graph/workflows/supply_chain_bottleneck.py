"""Runtime-owned DAG for the supply-chain bottleneck expert team.

Topology (and execution authority)::

                     |-- trend-verifier --|
    scope-brief -----|                    |-- bottleneck-validator --|
                     |-- chain-mapper ----|-- company-screener ------|-- team-lead -- report-audit
                                          `-- counter-case-analyst --|

The discovery wave must join before any validation member activates. The
runtime owns both joins; model output cannot select, skip, or reorder nodes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from nanobot.graph.engine import Graph, GraphNode, NodeResult, advance_graph

SCOPE_BRIEF = "scope-brief"
TREND_VERIFIER = "trend-verifier"
CHAIN_MAPPER = "chain-mapper"
BOTTLENECK_VALIDATOR = "bottleneck-validator"
COMPANY_SCREENER = "company-screener"
COUNTER_CASE_ANALYST = "counter-case-analyst"
TEAM_LEAD = "team-lead"
REPORT_AUDIT = "report-audit"

DISCOVERY_MEMBER_NODES = (TREND_VERIFIER, CHAIN_MAPPER)
VALIDATION_MEMBER_NODES = (
    BOTTLENECK_VALIDATOR,
    COMPANY_SCREENER,
    COUNTER_CASE_ANALYST,
)
MEMBER_NODES = (*DISCOVERY_MEMBER_NODES, *VALIDATION_MEMBER_NODES)
TERMINAL_MEMBER_STATES = frozenset({"completed", "failed", "cancelled"})

SCOPING = "scoping"
DISCOVERY = "discovery"
VALIDATION = "validation"
SYNTHESIS = "synthesis"
AUDIT = "audit"
DELIVERED = "delivered"


def _reduce_scope_brief(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> NodeResult | None:
    if event != "scope_brief_ready":
        return None
    scope_brief = {
        "status": "completed",
        **({"content": str(payload["content"])} if payload.get("content") else {}),
        **({"artifact": str(payload["artifact"])} if payload.get("artifact") else {}),
    }
    return NodeResult(
        writes={
            "scope_brief": scope_brief,
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
    verified = payload.get("verified", True) is True
    degraded = bool(state.get("degraded")) or not verified
    status = "completed_with_warnings" if degraded else "completed"
    return NodeResult(
        writes={
            "artifacts": artifacts, "status": status, "degraded": degraded,
            "audit": {"verified": verified, "warning": str(payload.get("warning") or "")},
        },
        settled=True,
        status="completed" if verified else "failed",
    )


def build_supply_chain_bottleneck_graph() -> Graph:
    graph = Graph(
        name="supply-chain-bottleneck",
        start=SCOPE_BRIEF,
        terminal_stage=DELIVERED,
    )
    graph.add_node(GraphNode(
        SCOPE_BRIEF,
        kind="agent",
        stage=SCOPING,
        reducer=_reduce_scope_brief,
    ))
    for member_id in DISCOVERY_MEMBER_NODES:
        graph.add_node(GraphNode(
            member_id,
            kind="agent",
            stage=DISCOVERY,
            reducer=_member_reducer(member_id),
        ))
    for member_id in VALIDATION_MEMBER_NODES:
        graph.add_node(GraphNode(
            member_id,
            kind="agent",
            stage=VALIDATION,
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

    for member_id in DISCOVERY_MEMBER_NODES:
        graph.add_edge(SCOPE_BRIEF, member_id)
    for discovery_id in DISCOVERY_MEMBER_NODES:
        for validation_id in VALIDATION_MEMBER_NODES:
            graph.add_edge(discovery_id, validation_id)
    for member_id in VALIDATION_MEMBER_NODES:
        graph.add_edge(member_id, TEAM_LEAD)
    graph.add_edge(TEAM_LEAD, REPORT_AUDIT)
    return graph


SUPPLY_CHAIN_BOTTLENECK_GRAPH = build_supply_chain_bottleneck_graph()


def new_supply_chain_bottleneck_state(
    *,
    run_id: str,
    member_ids: Iterable[str],
    resume_from: Mapping[str, Any] | None = None,
    supplemental_artifacts: Iterable[str] = (),
) -> dict[str, Any]:
    """Create a new two-wave blackboard, optionally resuming at synthesis."""

    requested_members = tuple(dict.fromkeys(str(item) for item in member_ids))
    if set(requested_members) != set(MEMBER_NODES):
        raise ValueError(
            "supply-chain-bottleneck workflow requires exactly: "
            + ", ".join(MEMBER_NODES)
        )

    resumed = isinstance(resume_from, Mapping)
    if resumed and str(resume_from.get("workflow") or "") != SUPPLY_CHAIN_BOTTLENECK_GRAPH.name:
        raise ValueError("resume state belongs to a different expert-team workflow")
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

    settled_nodes = [SCOPE_BRIEF, *MEMBER_NODES] if resumed else []
    active_nodes = [TEAM_LEAD] if resumed else [SCOPE_BRIEF]
    node_states = {
        name: {
            "status": (
                "running" if name in active_nodes
                else "completed" if name in settled_nodes
                else "pending"
            )
        }
        for name in SUPPLY_CHAIN_BOTTLENECK_GRAPH.nodes
    }
    prior_scope = resume_from.get("scope_brief") if resumed else None
    state: dict[str, Any] = {
        "schema_version": 2,
        "workflow": SUPPLY_CHAIN_BOTTLENECK_GRAPH.name,
        "run_id": run_id,
        "node": SYNTHESIS if resumed else SCOPING,
        "active_nodes": active_nodes,
        "settled_nodes": settled_nodes,
        "node_states": node_states,
        "path": [*settled_nodes, *active_nodes] if resumed else [SCOPE_BRIEF],
        "checkpoint_revision": 0,
        "status": "running",
        "degraded": bool(resume_from.get("degraded")) if resumed else False,
        "scope_brief": (
            deepcopy(dict(prior_scope))
            if isinstance(prior_scope, Mapping)
            else {"status": "completed" if resumed else "running"}
        ),
        "members": members,
        "artifacts": dict(resume_from.get("artifacts") or {}) if resumed else {},
        "user_supplements": [
            str(item) for item in supplemental_artifacts if str(item).strip()
        ],
        "source_policy": {
            "discovery": ["anysearch", "official-disclosures", "web_search"],
            "company_validation": ["ifind", "juyuan", "caihui"],
            "adjudication": ["company-filings", "exchange-disclosures"],
            "failed": [],
        },
        "events": [],
    }
    if resumed:
        state["resume_from_run_id"] = str(resume_from.get("run_id") or "")
    return state


def advance_supply_chain_bottleneck_graph(
    state: Mapping[str, Any],
    event: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return advance_graph(SUPPLY_CHAIN_BOTTLENECK_GRAPH, state, event, payload)


def public_supply_chain_bottleneck_state(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip large role bodies from durable/UI checkpoints."""

    public = deepcopy(dict(state))
    scope_brief = public.get("scope_brief")
    if isinstance(scope_brief, dict):
        scope_brief.pop("content", None)
    members = public.get("members")
    if isinstance(members, dict):
        for member in members.values():
            if isinstance(member, dict):
                member.pop("content", None)
    return public


def supply_chain_bottleneck_topology() -> dict[str, Any]:
    return SUPPLY_CHAIN_BOTTLENECK_GRAPH.describe()

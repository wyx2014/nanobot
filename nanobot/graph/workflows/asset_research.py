"""Deterministic graph for the four-role asset research team."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from nanobot.graph.engine import Graph, GraphNode, advance_graph

PREPARATION = "preparation"
MEMBERS = "members"
SYNTHESIS = "synthesis"
AUDIT = "audit"
DELIVERED = "delivered"

TERMINAL_MEMBER_STATES = frozenset({"completed", "failed", "cancelled"})
CORE_STRUCTURED_SOURCES = ("ifind", "juyuan", "caihui")


def _members_terminal(state: Mapping[str, Any]) -> bool:
    members = state.get("members")
    return bool(members) and all(
        isinstance(member, Mapping)
        and str(member.get("status") or "") in TERMINAL_MEMBER_STATES
        for member in members.values()
    )


def _reduce_preparation(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if event != "data_package_ready":
        return None
    return {
        "data_package": {
            "status": "completed",
            **(
                {"artifact": str(payload["artifact"])}
                if payload.get("artifact")
                else {}
            ),
        },
    }


def _route_preparation(
    _state: dict[str, Any],
    event: str,
    _payload: Mapping[str, Any],
) -> str | None:
    return MEMBERS if event == "data_package_ready" else None


def _reduce_members(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if event != "member_updated":
        return None
    member_id = str(payload.get("id") or "").strip()
    members = deepcopy(state.get("members") or {})
    if member_id not in members:
        return None
    status = str(payload.get("status") or members[member_id].get("status") or "running")
    members[member_id].update({
        "status": status,
        **(
            {"activity": str(payload["activity"])}
            if payload.get("activity")
            else {}
        ),
        **(
            {"artifact": str(payload["artifact"])}
            if payload.get("artifact")
            else {}
        ),
    })
    degraded = bool(state.get("degraded")) or status in {"failed", "cancelled"}
    return {"members": members, "degraded": degraded}


def _route_members(
    state: dict[str, Any],
    event: str,
    _payload: Mapping[str, Any],
) -> str | None:
    if event == "member_updated" and _members_terminal(state):
        return SYNTHESIS
    return None


def _reduce_synthesis(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if event == "user_supplement":
        supplements = list(state.get("user_supplements") or [])
        supplements.extend(
            str(item) for item in payload.get("artifacts", []) if str(item).strip()
        )
        return {"user_supplements": list(dict.fromkeys(supplements))}
    if event != "report_written":
        return None
    artifacts = dict(state.get("artifacts") or {})
    if payload.get("artifact"):
        artifacts["report"] = str(payload["artifact"])
    return {"artifacts": artifacts}


def _route_synthesis(
    _state: dict[str, Any],
    event: str,
    _payload: Mapping[str, Any],
) -> str | None:
    return AUDIT if event == "report_written" else None


def _reduce_audit(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if event not in {"audit_completed", "turn_completed"}:
        return None
    artifacts = dict(state.get("artifacts") or {})
    for key, value in dict(payload.get("artifacts") or {}).items():
        if str(value).strip():
            artifacts[str(key)] = str(value)
    return {
        "artifacts": artifacts,
        "status": "completed_with_warnings" if state.get("degraded") else "completed",
    }


def _route_audit(
    _state: dict[str, Any],
    event: str,
    _payload: Mapping[str, Any],
) -> str | None:
    return DELIVERED if event in {"audit_completed", "turn_completed"} else None


def _reduce_delivered(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    # A supplement can be associated with an already delivered but degraded
    # run before a new resume run is created.
    if event != "user_supplement":
        return None
    supplements = list(state.get("user_supplements") or [])
    supplements.extend(
        str(item) for item in payload.get("artifacts", []) if str(item).strip()
    )
    return {"user_supplements": list(dict.fromkeys(supplements))}


def build_asset_research_graph() -> Graph:
    graph = Graph(name="asset-research", start=PREPARATION)
    graph.add_node(GraphNode(PREPARATION, "preparation", _reduce_preparation, _route_preparation))
    graph.add_node(GraphNode(MEMBERS, "parallel", _reduce_members, _route_members))
    graph.add_node(GraphNode(SYNTHESIS, "agent", _reduce_synthesis, _route_synthesis))
    graph.add_node(GraphNode(AUDIT, "audit", _reduce_audit, _route_audit))
    graph.add_node(GraphNode(DELIVERED, "terminal", _reduce_delivered, None))
    graph.add_edge(PREPARATION, MEMBERS)
    graph.add_edge(MEMBERS, SYNTHESIS)
    graph.add_edge(SYNTHESIS, AUDIT)
    graph.add_edge(AUDIT, DELIVERED)
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

    resumed = isinstance(resume_from, Mapping)
    members: dict[str, dict[str, Any]] = {}
    prior_members = resume_from.get("members") if resumed else None
    for member_id in member_ids:
        prior = (
            prior_members.get(member_id)
            if isinstance(prior_members, Mapping)
            and isinstance(prior_members.get(member_id), Mapping)
            else {}
        )
        members[member_id] = {
            "status": "completed" if resumed else "pending",
            **(
                {"artifact": str(prior["artifact"])}
                if prior.get("artifact")
                else {}
            ),
        }
    node = SYNTHESIS if resumed else PREPARATION
    state: dict[str, Any] = {
        "schema_version": 1,
        "workflow": ASSET_RESEARCH_GRAPH.name,
        "run_id": run_id,
        "node": node,
        "path": [node],
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


def asset_research_topology() -> dict[str, Any]:
    return ASSET_RESEARCH_GRAPH.describe()

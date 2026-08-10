"""Small event-driven DAG engine for long-running agent workflows.

The normal agent loop remains the unit that talks to a model and executes
tools.  A graph owns *control flow* around those loops.  Runtime events settle
nodes; declared edges activate the next ready wave.  Models never select an
edge or write the active-node set.

This is the asynchronous counterpart of Waku's deliberately small graph
engine.  A workflow state is one checkpointable dictionary, fan-out is an
``active_nodes`` list, and fan-in is derived from declared predecessors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

GraphState = dict[str, Any]
Observer = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class NodeResult:
    """One runtime fact accepted by an active graph node."""

    writes: Mapping[str, Any] = field(default_factory=dict)
    settled: bool = False
    status: str = "completed"


Reducer = Callable[[GraphState, str, Mapping[str, Any]], NodeResult | None]


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One inspectable workflow node.

    A reducer returns ``None`` when an event does not belong to this node.  A
    settled result releases outgoing edges.  Readiness is calculated by the
    engine, never by prompt text or model-authored progress updates.
    """

    name: str
    kind: str = "fn"
    stage: str | None = None
    reducer: Reducer | None = None


@dataclass(slots=True)
class Graph:
    """A fixed DAG topology used both for execution and UI description."""

    name: str
    start: str
    terminal_stage: str = "completed"
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)

    def add_node(self, node: GraphNode) -> None:
        if node.name in self.nodes:
            raise ValueError(f"duplicate graph node: {node.name}")
        self.nodes[node.name] = node

    def add_edge(self, source: str, target: str) -> None:
        if source not in self.nodes or target not in self.nodes:
            raise ValueError(f"unknown graph edge: {source} -> {target}")
        self.edges.add((source, target))

    def predecessors(self, node_name: str) -> set[str]:
        return {source for source, target in self.edges if target == node_name}

    def successors(self, node_name: str) -> set[str]:
        return {target for source, target in self.edges if source == node_name}

    def ordered(self, names: set[str] | list[str]) -> list[str]:
        selected = set(names)
        return [name for name in self.nodes if name in selected]

    def stage_for(self, active_nodes: list[str]) -> str:
        if not active_nodes:
            return self.terminal_stage
        first = self.nodes.get(active_nodes[0])
        return str(first.stage or first.name) if first is not None else active_nodes[0]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start,
            "nodes": [
                {
                    "name": node.name,
                    "kind": node.kind,
                    **({"stage": node.stage} if node.stage else {}),
                }
                for node in self.nodes.values()
            ],
            "edges": [
                {"source": source, "target": target}
                for source, target in sorted(self.edges)
            ],
        }


def _node_state_map(graph: Graph, state: GraphState) -> dict[str, dict[str, Any]]:
    existing = state.get("node_states")
    node_states = deepcopy(existing) if isinstance(existing, Mapping) else {}
    for name in graph.nodes:
        raw = node_states.get(name)
        node_states[name] = dict(raw) if isinstance(raw, Mapping) else {"status": "pending"}
    return node_states


def _ready_successors(
    graph: Graph,
    *,
    settled_nodes: set[str],
    active_nodes: set[str],
) -> list[str]:
    ready: set[str] = set()
    for node_name in graph.nodes:
        if node_name in settled_nodes or node_name in active_nodes:
            continue
        predecessors = graph.predecessors(node_name)
        if predecessors and predecessors <= settled_nodes:
            ready.add(node_name)
    return graph.ordered(ready)


def advance_graph(
    graph: Graph,
    state: Mapping[str, Any],
    event: str,
    payload: Mapping[str, Any] | None = None,
    *,
    observer: Observer | None = None,
) -> GraphState:
    """Apply one runtime fact and return a new checkpointable DAG state.

    Only reducers belonging to currently active nodes may accept an event.  A
    settled node releases its declared outgoing edges; a successor activates
    only after *all* predecessors have settled.  Unknown or stale events are
    retained for diagnostics but cannot invent a transition.
    """

    data = dict(payload or {})
    next_state = deepcopy(dict(state))
    notify = observer or (lambda _kind, _event: None)

    raw_active = next_state.get("active_nodes")
    active_nodes = graph.ordered(
        [str(item) for item in raw_active]
        if isinstance(raw_active, list)
        else [graph.start]
    )
    unknown_active = [name for name in active_nodes if name not in graph.nodes]
    if unknown_active:
        raise ValueError(f"unknown active graph node(s): {', '.join(unknown_active)}")

    raw_settled = next_state.get("settled_nodes")
    settled_nodes = {
        str(item) for item in raw_settled
    } if isinstance(raw_settled, list) else set()
    node_states = _node_state_map(graph, next_state)

    handled: list[tuple[str, NodeResult]] = []
    for node_name in active_nodes:
        node = graph.nodes[node_name]
        if node.reducer is None:
            continue
        result = node.reducer(next_state, event, data)
        if result is not None:
            handled.append((node_name, result))

    if len(handled) > 1:
        names = ", ".join(name for name, _result in handled)
        raise ValueError(f"event {event!r} matched multiple active nodes: {names}")

    activated: list[str] = []
    if handled:
        node_name, result = handled[0]
        notify("node_start", {"workflow": graph.name, "node": node_name, "event": event})
        next_state.update(deepcopy(dict(result.writes)))
        node_states = _node_state_map(graph, next_state)
        node_state = dict(node_states[node_name])
        node_state["status"] = result.status if result.settled else "running"
        if data.get("activity"):
            node_state["activity"] = str(data["activity"])
        if data.get("artifact"):
            node_state["artifact"] = str(data["artifact"])
        node_states[node_name] = node_state

        if result.settled:
            settled_nodes.add(node_name)
            active_nodes = [name for name in active_nodes if name != node_name]
            activated = _ready_successors(
                graph,
                settled_nodes=settled_nodes,
                active_nodes=set(active_nodes),
            )
            for target in activated:
                target_state = dict(node_states[target])
                target_state["status"] = "running"
                node_states[target] = target_state
                notify(
                    "route",
                    {
                        "workflow": graph.name,
                        "source": node_name,
                        "target": target,
                        "event": event,
                    },
                )
            active_nodes = graph.ordered([*active_nodes, *activated])

        notify(
            "node_end",
            {
                "workflow": graph.name,
                "node": node_name,
                "event": event,
                "settled": result.settled,
                "active_nodes": list(active_nodes),
            },
        )

    next_state["active_nodes"] = list(active_nodes)
    next_state["settled_nodes"] = graph.ordered(settled_nodes)
    next_state["node_states"] = node_states
    next_state["node"] = graph.stage_for(active_nodes)
    path = next_state.setdefault("path", [graph.start])
    for target in activated:
        if target not in path:
            path.append(target)

    events = next_state.setdefault("events", [])
    events.append({
        "event": event,
        "handled_by": handled[0][0] if handled else None,
        "active_nodes": list(active_nodes),
    })
    if len(events) > 100:
        del events[:-100]
    next_state["checkpoint_revision"] = int(
        next_state.get("checkpoint_revision") or 0
    ) + 1
    return next_state

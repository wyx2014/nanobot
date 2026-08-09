"""Event-driven graph state around long-running agent work.

The normal agent loop remains the unit that talks to a model and executes tools.
This module gives multi-stage workflows an explicit, deterministic shape around
that loop.  It follows the same deliberately-small pattern as Waku's graph
engine: one dictionary is the blackboard, reducers write state, and Python
routers choose the next node.  Long-running/background nodes advance when a
durable runtime event arrives instead of blocking a worker until they finish.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

GraphState = dict[str, Any]
Reducer = Callable[[GraphState, str, Mapping[str, Any]], dict[str, Any] | None]
Router = Callable[[GraphState, str, Mapping[str, Any]], str | None]
Observer = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One inspectable workflow node.

    ``reducer`` applies facts from an event to the blackboard. ``router`` then
    chooses a target node, or returns ``None`` to remain on the current node.
    """

    name: str
    kind: str = "fn"
    reducer: Reducer | None = None
    router: Router | None = None


@dataclass(slots=True)
class Graph:
    """Topology plus the node that owns each event-driven transition."""

    name: str
    start: str
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

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start,
            "nodes": [
                {"name": node.name, "kind": node.kind}
                for node in self.nodes.values()
            ],
            "edges": [
                {"source": source, "target": target}
                for source, target in sorted(self.edges)
            ],
        }


def advance_graph(
    graph: Graph,
    state: Mapping[str, Any],
    event: str,
    payload: Mapping[str, Any] | None = None,
    *,
    observer: Observer | None = None,
) -> GraphState:
    """Apply one runtime fact and return a new checkpointable graph state.

    The input is never mutated. Unknown events are retained in ``events`` but
    don't invent a transition. A router may only follow a declared edge; this
    keeps control flow testable and prevents prompt text from changing stages.
    """

    data = dict(payload or {})
    next_state = deepcopy(dict(state))
    current = str(next_state.get("node") or graph.start)
    node = graph.nodes.get(current)
    if node is None:
        raise ValueError(f"unknown current graph node: {current}")

    notify = observer or (lambda _kind, _event: None)
    notify("node_start", {"workflow": graph.name, "node": current, "event": event})
    writes = node.reducer(next_state, event, data) if node.reducer is not None else None
    if writes:
        next_state.update(writes)

    target = node.router(next_state, event, data) if node.router is not None else None
    if target is not None:
        if target not in graph.nodes:
            raise ValueError(f"router selected unknown node: {target}")
        if target != current and (current, target) not in graph.edges:
            raise ValueError(f"router selected undeclared edge: {current} -> {target}")
        if target != current:
            next_state["node"] = target
            path = next_state.setdefault("path", [current])
            if not path or path[-1] != target:
                path.append(target)
            notify(
                "route",
                {
                    "workflow": graph.name,
                    "source": current,
                    "target": target,
                    "event": event,
                },
            )

    events = next_state.setdefault("events", [])
    events.append({"event": event, "node": str(next_state.get("node") or current)})
    # Keep checkpoints compact even when a model/tool emits many activity lines.
    if len(events) > 100:
        del events[:-100]
    next_state["checkpoint_revision"] = int(
        next_state.get("checkpoint_revision") or 0
    ) + 1
    notify(
        "node_end",
        {
            "workflow": graph.name,
            "node": current,
            "event": event,
            "target": str(next_state.get("node") or current),
        },
    )
    return next_state

"""Structured task progress tool for rich WebUI clients."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema, tool_parameters_schema
from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage

_STATUSES = ("pending", "running", "completed", "error")


@tool_parameters(
    tool_parameters_schema(
        description=(
            "Publish the complete user-facing plan for a tool-using task. Make this the first "
            "tool call before business tools, normally with 2-4 outcome-oriented steps. "
            "Expert-team workflows are runtime-owned and ignore this tool. Re-send the full "
            "ordered list on every update, preserving every id and title. Every non-terminal snapshot "
            "must have exactly one running step; only a final all-terminal snapshot may have none. "
            "Omit current_step_id for that final all-terminal snapshot."
        ),
        steps=ArraySchema(
            ObjectSchema(
                id=StringSchema("Stable step id; never change it within the task"),
                title=StringSchema(
                    "Short user goal or deliverable; never a tool name or implementation action"
                ),
                status=StringSchema("Step status", enum=_STATUSES),
                required=["id", "title", "status"],
            ),
            description=(
                "The complete ordered plan, not a delta and not a tool-call log. Use "
                "2-4 steps. Use exactly one "
                "running step until all steps are completed or error."
            ),
            min_items=2,
            max_items=4,
        ),
        note=StringSchema(
            "Optional short public progress note for the user. Do not include private reasoning."
        ),
        current_step_id=StringSchema("Stable id of the step currently being worked on"),
        required=["steps"],
    )
)
class TaskProgressTool(Tool, ContextAware):
    """Publish a structured task progress update."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._send_callback = send_callback
        self._channel = "websocket"
        self._chat_id = ""
        self._metadata: dict[str, Any] = {}
        self._plans: dict[str, tuple[tuple[tuple[str, str], ...], int]] = {}

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(send_callback=ctx.bus.publish_outbound if ctx.bus else None)

    def set_context(self, ctx: RequestContext) -> None:
        self._channel = ctx.channel
        self._chat_id = ctx.chat_id
        self._metadata = dict(ctx.metadata or {})

    @property
    def name(self) -> str:
        return "update_task_progress"

    @property
    def description(self) -> str:
        return (
            "Publish the visible task plan for the current conversation. For every multi-step or "
            "complex task expected to call tools, call this first in the first tool batch, before "
            "any business tool. A single low-risk read may remain planless. "
            "Provide the complete ordered plan on every call using 2-4 stable steps. Expert-team "
            "workflow plans are runtime-owned and must not be replaced here. Steps must describe user goals "
            "or deliverables, never implementation actions such as searching, reading, calling a "
            "tool, or running a command. Keep ids and titles unchanged across updates, and use "
            "exactly one running step in every non-terminal snapshot. Zero running steps is valid "
            "only when every step is terminal (completed or error), immediately before the final "
            "answer; omit current_step_id in that snapshot. Use note only for concise public "
            "narration, never private reasoning."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(
        self,
        steps: list[dict[str, Any]],
        note: str = "",
        current_step_id: str = "",
        **_: Any,
    ) -> str:
        expert_team = self._metadata.get("expert_team")
        if isinstance(expert_team, dict) and expert_team:
            # Expert-team plans are created and advanced by the runtime
            # coordinator.  Treat model-authored updates as an acknowledged
            # no-op so an older prompt cannot create a competing plan.
            return (
                "Workflow plan is runtime-owned; the proposed model plan was "
                "ignored and the expert-team workflow remains active"
            )
        max_steps = 4
        if not isinstance(steps, list) or not 2 <= len(steps) <= max_steps:
            return (
                f"Error: steps must contain the complete 2-{max_steps} item task plan"
            )

        normalized: list[dict[str, str]] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                return f"Error: step {index} must be an object"
            title = str(step.get("title") or "").strip()
            if not title:
                return f"Error: step {index} must have a title"
            status = str(step.get("status") or "pending").strip()
            if status not in _STATUSES:
                return f"Error: step {index} has an invalid status"
            step_id = str(step.get("id") or f"step-{index}").strip() or f"step-{index}"
            normalized.append({"id": step_id, "title": title, "status": status})

        step_ids = [step["id"] for step in normalized]
        if len(set(step_ids)) != len(step_ids):
            return "Error: every task-plan step id must be unique"
        running_steps = [step for step in normalized if step["status"] == "running"]
        has_non_terminal_step = any(
            step["status"] in {"pending", "running"} for step in normalized
        )
        if has_non_terminal_step and len(running_steps) != 1:
            return "Error: a non-terminal task plan must have exactly one running step"
        if not self._send_callback or not self._chat_id:
            return "Error: task progress is unavailable in this runtime"

        public_note = " ".join(str(note or "").split()).strip()[:240]
        current_id = str(current_step_id or "").strip()
        valid_step_ids = {step["id"] for step in normalized}
        if current_id and current_id not in valid_step_ids:
            return "Error: current_step_id must match a task-plan step id"
        # ``steps`` is the complete authoritative snapshot. Models sometimes
        # advance the running step correctly but leave ``current_step_id`` on
        # the just-completed step, or keep it on the final completed step. A
        # valid-but-stale pointer must not reject an otherwise unambiguous
        # snapshot: derive it from the sole running step, or clear it when the
        # plan is fully terminal. Unknown ids remain an error because they can
        # indicate a different/partial plan rather than a stale pointer.
        current_id = running_steps[0]["id"] if running_steps else ""

        metadata = dict(self._metadata)
        metadata["_progress"] = True
        turn_id = str(
            metadata.get("_runtime_turn_id")
            or metadata.get("webui_turn_id")
            or f"{self._channel}:{self._chat_id}"
        ).strip()
        signature = tuple((step["id"], step["title"]) for step in normalized)
        previous = self._plans.get(turn_id)
        if previous is not None and previous[0] != signature:
            return "Error: task-plan ids, order, and titles are immutable within a turn"
        revision = (previous[1] if previous is not None else 0) + 1
        self._plans[turn_id] = (signature, revision)
        agent_ui: dict[str, Any] = {
            "kind": "task_progress",
            "plan_id": f"plan:{turn_id}",
            "turn_id": turn_id,
            "plan_kind": "dynamic",
            "owner": "agent",
            "policy": "required",
            "execution": "serial",
            "status": (
                "failed"
                if any(step["status"] == "error" for step in normalized)
                else "completed"
                if all(step["status"] == "completed" for step in normalized)
                else "running"
            ),
            "revision": revision,
            "active_step_ids": [step["id"] for step in running_steps],
            "steps": normalized,
        }
        if public_note:
            agent_ui["note"] = public_note
        if current_id:
            agent_ui["current_step_id"] = current_id
        metadata[OUTBOUND_META_AGENT_UI] = agent_ui
        await self._send_callback(
            OutboundMessage(
                channel=self._channel,
                chat_id=self._chat_id,
                content="",
                metadata=metadata,
            )
        )
        return "Task progress updated"

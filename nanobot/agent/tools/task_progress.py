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
            "Report user-facing task progress for multi-step work. Use this when a task has "
            "clear stages such as research, writing, verification, formatting, code changes, "
            "or report generation. Keep labels short and non-technical. When the stage changes, "
            "include a brief public note explaining what is happening next."
        ),
        steps=ArraySchema(
            ObjectSchema(
                id=StringSchema("Stable step id, e.g. research or draft"),
                title=StringSchema("Short user-facing stage title"),
                status=StringSchema("Step status", enum=_STATUSES),
                required=["id", "title", "status"],
            ),
            description="Ordered task stages.",
            min_items=1,
            max_items=8,
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
            "Update the visible task progress panel for the current conversation. "
            "Call early for multi-step tasks, then update statuses as work advances. "
            "Use short stage names that users understand; do not expose tool names. "
            "Use note for a concise public transition update, never private reasoning."
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
        normalized: list[dict[str, str]] = []
        for index, step in enumerate(steps[:8], start=1):
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip()
            if not title:
                continue
            status = str(step.get("status") or "pending").strip()
            if status not in _STATUSES:
                status = "pending"
            step_id = str(step.get("id") or f"step-{index}").strip() or f"step-{index}"
            normalized.append({"id": step_id, "title": title, "status": status})

        if not normalized:
            return "Error: steps must contain at least one valid item"
        if not self._send_callback or not self._chat_id:
            return "Error: task progress is unavailable in this runtime"

        public_note = " ".join(str(note or "").split()).strip()[:240]
        current_id = str(current_step_id or "").strip()
        valid_step_ids = {step["id"] for step in normalized}

        metadata = dict(self._metadata)
        metadata["_progress"] = True
        agent_ui: dict[str, Any] = {
            "kind": "task_progress",
            "steps": normalized,
        }
        if public_note:
            agent_ui["note"] = public_note
        if current_id in valid_step_ids:
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

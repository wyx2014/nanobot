"""Spawn tool for creating background subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema
from nanobot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative). "
                "Defaults to the provider's configured temperature."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["task"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id",
            default=None,
        )
        self._expert_team: ContextVar[dict[str, Any] | None] = ContextVar(
            "spawn_expert_team",
            default=None,
        )
        self._expert_team_run_id: ContextVar[str | None] = ContextVar(
            "spawn_expert_team_run_id",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)
        team = ctx.metadata.get("expert_team")
        self._expert_team.set(dict(team) if isinstance(team, dict) else None)
        run_id = ctx.metadata.get("expert_team_run_id")
        self._expert_team_run_id.set(run_id if isinstance(run_id, str) else None)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )

    async def execute(
        self,
        task: str,
        label: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        team = self._expert_team.get()
        team_limit = team.get("requested_concurrency") if isinstance(team, dict) else None
        limit = (
            max(1, min(4, int(team_limit)))
            if isinstance(team_limit, int)
            else self._manager.max_concurrent_subagents
        )
        session_key = self._session_key.get()
        global_running = self._manager.get_running_count()
        get_session_count = getattr(self._manager, "get_running_count_by_session", None)
        session_running = (
            get_session_count(session_key)
            if callable(get_session_count)
            else global_running
        )
        global_limit = max(self._manager.max_concurrent_subagents, limit)
        if session_running >= limit or global_running >= global_limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({session_running}/{limit} in session, {global_running}/{global_limit} total). "
                f"Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        team_kwargs: dict[str, Any] = {}
        if team is not None:
            team_kwargs = {
                "expert_team": team,
                "expert_team_run_id": self._expert_team_run_id.get(),
            }
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=session_key,
            origin_message_id=self._origin_message_id.get(),
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
            **team_kwargs,
        )

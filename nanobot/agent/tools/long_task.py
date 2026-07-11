"""Sustained goal tools on the main agent (Codex-style).

Follow the built-in **long-goal** skill for lifecycle rules and how to phrase
objectives (especially **idempotent**, compaction-safe goals). Load that skill
from the skills listing (path shown there) before composing ``long_task.goal`` text.

``long_task`` registers an objective on the session (JSON-serializable metadata).
Active objectives are mirrored each turn into the Runtime Context block (see
``nanobot.session.goal_state.goal_state_runtime_lines``) so compaction cannot hide them.
Work proceeds in ordinary agent turns (same runner, compaction as configured).
Call ``update_goal`` when the sustained objective is complete or genuinely blocked.
``complete_goal`` remains as a compatibility alias for successful completion.

There is **no** sub-agent orchestrator and **no** special WebSocket ``agent_ui`` stream.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.bus.runtime_events import GoalStateChanged, RuntimeEventBus, RuntimeEventContext
from nanobot.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_state_public_blob,
    goal_state_raw,
    parse_goal_state,
)

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


def _iso_now() -> str:
    return datetime.now().isoformat()


_BLOCKED_THRESHOLD = 3


class _GoalToolsMixin(ContextAware):
    """Shared routing context + Session lookup."""

    def __init__(
        self,
        sessions: SessionManager,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._runtime_events = runtime_events
        # Each subclass gets its own ContextVar so concurrent tasks across
        # different tool types (LongTaskTool vs CompleteGoalTool) do not
        # interfere with each other.
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            f"{self.__class__.__name__}_request_ctx",
            default=None,
        )

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    def _session(self):
        request_ctx = self._request_ctx.get()
        if request_ctx is None:
            return None
        key = request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def _publish_goal_state_changed(self, metadata: dict[str, Any]) -> None:
        """Publish authoritative goal metadata as a runtime event."""
        runtime_events = self._runtime_events
        rc = self._request_ctx.get()
        if runtime_events is None or rc is None:
            return
        cid = (rc.chat_id or "").strip()
        if not cid:
            return
        await runtime_events.publish(
            GoalStateChanged(
                context=RuntimeEventContext(
                    channel=rc.channel,
                    chat_id=cid,
                    session_key=rc.session_key or f"{rc.channel}:{cid}",
                    metadata=dict(rc.metadata or {}),
                ),
                session_metadata=dict(metadata),
            )
        )


@tool_parameters(
    tool_parameters_schema(
        goal=StringSchema(
            "Sustained objective for this chat thread. First read the built-in **long-goal** skill, "
            "especially its Start fast section, then call this promptly once the user's intent is clear. "
            "The goal must still be idempotent, self-contained, bounded, and explicit about done-ness; "
            "do not delay this tool call to over-plan, research, or decide execution details.",
            max_length=12_000,
        ),
        ui_summary=StringSchema(
            "Optional one-line label for session lists / logs (≤120 chars).",
            max_length=120,
            nullable=True,
        ),
        required=["goal"],
    )
)
class LongTaskTool(Tool, _GoalToolsMixin):
    """Begin or replace focus on a long-running objective stored on the session."""

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None  # guarded by enabled()
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "long_task"

    @property
    def description(self) -> str:
        return (
            "Mark this thread as a sustained long-running task. "
            "First read the built-in **long-goal** skill, especially its Start fast section; then call this "
            "as soon as the user's intent is clear. Write a good idempotent goal, but do not delay the tool "
            "call with long planning, research, or execution-detail thinking. "
            "The active goal is mirrored in Runtime Context each turn. Use normal tools until done, then call "
            "update_goal with status='complete' when the objective is satisfied and verified. "
            "If a goal is already active, finish it before registering another."
        )

    async def execute(self, goal: str, ui_summary: str | None = None, **kwargs: Any) -> str:
        sess = self._session()
        if sess is None:
            return (
                "Error: long_task requires an active chat session (missing routing context)."
            )
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        if isinstance(prior, dict) and prior.get("status") == "active":
            return (
                "Error: a sustained goal is already active. "
                "Use update_goal when finished, or ask the user before replacing it."
            )

        summary = (ui_summary or "").strip()[:120]
        blob = {
            "status": "active",
            "objective": goal.strip(),
            "ui_summary": summary,
            "started_at": _iso_now(),
        }
        sess.metadata[GOAL_STATE_KEY] = blob
        discard_legacy_goal_state_key(sess.metadata)
        self._sessions.save(sess)
        await self._publish_goal_state_changed(sess.metadata)
        extra = f"\nSummary line: {summary}" if summary else ""
        return (
            "Goal recorded. Keep working toward the objective using ordinary tools. "
            "When fully done (verified against what was asked), call update_goal with "
            f"status='complete' and a short recap.{extra}"
        )


@tool_parameters(tool_parameters_schema(required=[]))
class GetGoalTool(Tool, _GoalToolsMixin):
    """Return the current sustained goal state for this session."""

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "get_goal"

    @property
    def description(self) -> str:
        return (
            "Get the current sustained goal for this thread, including status and objective. "
            "Use before deciding whether a long-running objective is active, complete, or blocked."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        sess = self._session()
        if sess is None:
            return {"active": False, "goal": None, "error": "missing active chat session"}
        return goal_state_public_blob(sess.metadata)


@tool_parameters(
    tool_parameters_schema(
        status=StringSchema(
            "Set to 'complete' only when the full objective is achieved and verified. "
            "Set to 'blocked' only when the same blocker has repeated for at least three "
            "consecutive goal turns and no meaningful progress is possible without user input "
            "or an external state change.",
            enum=["complete", "blocked"],
        ),
        recap=StringSchema(
            "Short recap for the user. For complete, summarize what was delivered. "
            "For blocked, summarize the repeated blocker.",
            max_length=8000,
            nullable=True,
        ),
        blocker=StringSchema(
            "Required when status='blocked': stable description of the blocking condition.",
            max_length=1000,
            nullable=True,
        ),
        required=["status"],
    )
)
class UpdateGoalTool(Tool, _GoalToolsMixin):
    """Mark the active sustained goal complete or genuinely blocked."""

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "update_goal"

    @property
    def description(self) -> str:
        return (
            "Update the existing sustained goal. Use only to mark it achieved or genuinely blocked. "
            "Do not use 'complete' until the requested end state is actually verified. "
            "Do not use 'blocked' for uncertainty or first-time trouble; the same blocker must repeat "
            "for three consecutive goal turns."
        )

    async def execute(
        self,
        status: str,
        recap: str | None = None,
        blocker: str | None = None,
        **kwargs: Any,
    ) -> str:
        sess = self._session()
        if sess is None:
            return "Error: update_goal requires an active chat session."
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        if not isinstance(prior, dict) or prior.get("status") != "active":
            return "No active goal to update."

        status = status.strip().lower()
        if status == "complete":
            return await self._complete(sess, prior, recap)
        if status == "blocked":
            return await self._blocked(sess, prior, blocker, recap)
        return "Error: status must be 'complete' or 'blocked'."

    async def _complete(self, sess: Any, prior: dict[str, Any], recap: str | None) -> str:
        ended = _iso_now()
        sess.metadata[GOAL_STATE_KEY] = {
            **prior,
            "status": "completed",
            "completed_at": ended,
            "recap": (recap or "").strip(),
        }
        sess.metadata[GOAL_STATE_KEY].pop("_blocked_audit", None)
        discard_legacy_goal_state_key(sess.metadata)
        self._sessions.save(sess)
        await self._publish_goal_state_changed(sess.metadata)
        tail = (recap or "").strip()
        if tail:
            return f"Goal marked complete ({ended}). Recap:\n{tail}"
        return f"Goal marked complete ({ended})."

    async def _blocked(
        self,
        sess: Any,
        prior: dict[str, Any],
        blocker: str | None,
        recap: str | None,
    ) -> str:
        blocker = (blocker or "").strip()
        if not blocker:
            return "Error: blocker is required when status is 'blocked'."

        audit = prior.get("_blocked_audit")
        if not isinstance(audit, dict) or audit.get("blocker") != blocker:
            audit = {"blocker": blocker, "count": 0}
        audit["count"] = int(audit.get("count") or 0) + 1

        if audit["count"] < _BLOCKED_THRESHOLD:
            sess.metadata[GOAL_STATE_KEY] = {**prior, "_blocked_audit": audit}
            discard_legacy_goal_state_key(sess.metadata)
            self._sessions.save(sess)
            return (
                f"Blocked audit recorded ({audit['count']}/{_BLOCKED_THRESHOLD}). "
                "Keep the goal active and continue if any meaningful progress is possible."
            )

        ended = _iso_now()
        sess.metadata[GOAL_STATE_KEY] = {
            **prior,
            "status": "blocked",
            "blocked_at": ended,
            "blocker": blocker,
            "recap": (recap or "").strip(),
        }
        sess.metadata[GOAL_STATE_KEY].pop("_blocked_audit", None)
        discard_legacy_goal_state_key(sess.metadata)
        self._sessions.save(sess)
        await self._publish_goal_state_changed(sess.metadata)
        tail = (recap or "").strip()
        if tail:
            return f"Goal marked blocked ({ended}). Recap:\n{tail}"
        return f"Goal marked blocked ({ended})."


@tool_parameters(
    tool_parameters_schema(
        recap=StringSchema(
            "Brief recap for the user (plain text). When the goal succeeded, confirm outcomes; "
            "if the user cancelled, pivoted, or replaced the objective, say so honestly.",
            max_length=8000,
            nullable=True,
        ),
        required=[],
    )
)
class CompleteGoalTool(Tool, _GoalToolsMixin):
    """Mark the active sustained goal finished after all required work is verified."""

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "complete_goal"

    @property
    def description(self) -> str:
        return (
            "End bookkeeping for the active sustained goal. "
            "Use when the objective is fully achieved and verified—recap what was delivered. "
            "Also call when the user cancels, redirects, or replaces the goal: recap must reflect "
            "what actually happened (not necessarily success). "
            "If no goal is active, the tool reports that and leaves metadata unchanged."
        )

    async def execute(self, recap: str | None = None, **kwargs: Any) -> str:
        updater = UpdateGoalTool(self._sessions, self._runtime_events)
        rc = self._request_ctx.get()
        if rc is not None:
            updater.set_context(rc)
        return await updater.execute(status="complete", recap=recap)

"""Runtime policy for requiring a user-facing task plan before business tools.

The model is allowed to propose the plan, but it is not the authority deciding
whether a complex turn may proceed without one.  This module deliberately
operates on tool names only; it never derives plan steps from tool activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

PLAN_TOOL_NAME = "update_task_progress"

_CONTROL_TOOLS = {
    PLAN_TOOL_NAME,
    "request_user_input",
    "create_goal",
    "update_goal",
}
_TRIVIAL_READ_TOOLS = {
    "read_file",
    "list_dir",
    "web_search",
    "web_fetch",
    "weather",
    "get_weather",
}
_TRIVIAL_READ_PREFIXES = (
    "mcp_",
    "search_",
    "fetch_",
    "get_",
    "list_",
    "read_",
)
_COMPLEX_TOOL_MARKERS = (
    "write",
    "edit",
    "exec",
    "shell",
    "spawn",
    "agent",
    "create",
    "generate",
    "convert",
    "render",
    "pdf",
    "docx",
    "xlsx",
    "image",
    "delete",
    "move",
    "copy",
    "schedule",
    "cron",
)
_COMPLEX_REQUEST_MARKERS = (
    "分析",
    "研究",
    "比较",
    "调查",
    "排查",
    "诊断",
    "实现",
    "修改",
    "修复",
    "生成",
    "转换",
    "报告",
    "审计",
    "完整方案",
    "analyze",
    "analyse",
    "research",
    "compare",
    "investigate",
    "diagnose",
    "implement",
    "fix",
    "generate",
    "convert",
    "report",
    "audit",
)


class PlanPolicyKind(StrEnum):
    OPTIONAL = "optional"
    REQUIRED = "required"
    WORKFLOW = "workflow"


@dataclass
class PlanPolicyState:
    """Mutable per-turn policy facts owned by :class:`AgentRunner`."""

    plan_created: bool = False
    business_tool_calls: int = 0
    correction_count: int = 0
    forced_reason: str | None = None


@dataclass(frozen=True)
class PlanPolicyDecision:
    kind: PlanPolicyKind
    reason: str

    @property
    def requires_plan(self) -> bool:
        return self.kind is PlanPolicyKind.REQUIRED


def _is_trivial_read(name: str) -> bool:
    normalized = name.strip().lower()
    if normalized in _TRIVIAL_READ_TOOLS:
        return True
    if any(marker in normalized for marker in _COMPLEX_TOOL_MARKERS):
        return False
    return normalized.startswith(_TRIVIAL_READ_PREFIXES)


def complex_request_reason(content: str) -> str | None:
    """Return a stable policy reason for explicit complex user intent."""

    normalized = " ".join(str(content or "").lower().split())
    if not normalized:
        return None
    if any(marker in normalized for marker in _COMPLEX_REQUEST_MARKERS):
        return "explicit_complex_request"
    return None


def decide_plan_policy(
    tool_names: Iterable[str],
    state: PlanPolicyState,
    *,
    expert_team: bool = False,
) -> PlanPolicyDecision:
    """Classify the next tool batch without synthesizing a plan.

    One low-risk read is allowed without ceremony.  A second read, a batch of
    multiple business calls, or any state-changing/long-running tool promotes
    the turn to a required dynamic plan.
    """

    if expert_team:
        return PlanPolicyDecision(PlanPolicyKind.WORKFLOW, "expert_team_workflow")
    names = [
        str(name).strip()
        for name in tool_names
        if str(name).strip() and str(name).strip() not in _CONTROL_TOOLS
    ]
    if not names or state.plan_created:
        return PlanPolicyDecision(PlanPolicyKind.OPTIONAL, "no_unplanned_business_tools")
    if state.forced_reason:
        return PlanPolicyDecision(PlanPolicyKind.REQUIRED, state.forced_reason)
    if len(names) > 1:
        return PlanPolicyDecision(PlanPolicyKind.REQUIRED, "multiple_business_tools")
    if state.business_tool_calls > 0:
        return PlanPolicyDecision(PlanPolicyKind.REQUIRED, "runtime_promoted_after_first_read")
    if not _is_trivial_read(names[0]):
        return PlanPolicyDecision(PlanPolicyKind.REQUIRED, "complex_business_tool")
    return PlanPolicyDecision(PlanPolicyKind.OPTIONAL, "single_trivial_read")


def plan_required_result(reason: str) -> str:
    return (
        "Error [PLAN_REQUIRED]: this turn has become a multi-step task "
        f"({reason}). Call update_task_progress first with a stable 2-4 step "
        "user-goal plan, then retry the blocked business tool."
    )

"""Executable coordinator for the asset-research DAG.

This module contains the product workflow, not another agent loop.  Each graph
node is executed through an injected callback, while this coordinator alone
owns node activation and fan-in.  Keeping the callbacks injectable makes the
control flow deterministic in tests and lets AgentLoop provide the existing
provider, tools, tracing, and WebSocket progress surfaces.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanobot.graph.workflows.asset_research import (
    DATA_PACKAGE,
    MEMBER_NODES,
    REPORT_AUDIT,
    TEAM_LEAD,
    advance_asset_research_graph,
    new_asset_research_state,
)


@dataclass(slots=True)
class AgentNodeOutcome:
    """Result returned by one tool-capable model node."""

    content: str = ""
    stop_reason: str = "completed"
    tools_used: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemberNodeOutcome:
    """Terminal result for one of the four parallel member nodes."""

    member_id: str
    status: str
    content: str
    artifact: str | None = None
    activity: str = ""


@dataclass(slots=True)
class MemberBatchOutcome:
    members: dict[str, MemberNodeOutcome]
    supplements: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AssetResearchWorkflowOutcome:
    final_content: str
    stop_reason: str
    graph_state: dict[str, Any]
    tools_used: list[str]
    usage: dict[str, int]
    artifacts: list[str]


RunAgentNode = Callable[[str, str, bool], Awaitable[AgentNodeOutcome]]
RunMemberWave = Callable[
    [dict[str, str]],
    Awaitable[MemberBatchOutcome],
]
PublishState = Callable[[dict[str, Any], str, str], Awaitable[None]]

AUDIT_MAX_TOOL_ITERATIONS = 5
AUDIT_MAX_MODEL_ROUNDS = 6


def _member_config(team: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = team.get("members")
    if not isinstance(raw, list):
        return {}
    return {
        str(item.get("id")): item
        for item in raw
        if isinstance(item, Mapping) and str(item.get("id") or "") in MEMBER_NODES
    }


def _data_package_prompt(*, target: str, request: str) -> str:
    return f"""# Runtime graph node: {DATA_PACKAGE}

You are executing only the fixed asset-research graph's data-package node.
The runtime, not you, owns all later nodes and edges. Do not create or update a
plan, do not spawn agents, do not inspect old reports/history, and do not begin
Team Lead synthesis.

Validated current security: {target}
Current user request: {request}

Build one bounded, self-contained base data package for this exact security.
Include identity/code/market, company summary and business mix, recent annual
and quarterly financial indicators with dates and units, valuation inputs,
material announcements/news, and industry/sector classification. Query every
configured core source among iFinD, Juyuan, and Caihui by field; retain source
conflicts. Only after the core sources miss a field may you use AnySearch and
then explicit DuckDuckGo. Mark gaps and confidence instead of looping.

Return the full package in your final response. Do not write the final research
report in this node.
"""


def _member_prompts(
    *,
    target: str,
    request: str,
    team: Mapping[str, Any],
    data_package: str,
) -> dict[str, str]:
    configs = _member_config(team)
    prompts: dict[str, str] = {}
    for member_id in MEMBER_NODES:
        member = configs.get(member_id, {})
        name = str(member.get("name") or member_id)
        framework = str(member.get("framework") or "").strip()
        description = str(member.get("description") or "").strip()
        instructions = str(member.get("instructions") or "").strip()
        responsibility = (
            description
            or "Complete this role's assigned investment-research dimension."
        )
        prompts[member_id] = f"""# Runtime graph node: {member_id}

You are the {name}{f' ({framework})' if framework else ''} branch in a fixed
asset-research DAG. Research only the validated current security `{target}`.
Work independently and return a complete role report; the runtime joins all
four terminal branches automatically. Do not coordinate, wait, or synthesize
the other roles.

Current user request:
{request}

Shared verified base data package:
---
{data_package}
---

Role responsibility:
{responsibility}

Role playbook:
{instructions or 'Use the team framework, cite evidence, label dates/units/conflicts/gaps, and give a clear conclusion.'}

Reuse the shared package first. Query only evidence missing for your role.
Observe the runtime source order: configured iFinD/Juyuan/Caihui core sources,
then AnySearch, then explicit DuckDuckGo. Return the fullest supported report
even when a source fails.
"""
    return prompts


def _member_evidence(state: Mapping[str, Any]) -> str:
    sections: list[str] = []
    members = state.get("members")
    if not isinstance(members, Mapping):
        return "(no member results recorded)"
    for member_id in MEMBER_NODES:
        member = members.get(member_id)
        if not isinstance(member, Mapping):
            continue
        content = str(member.get("content") or "").strip()
        artifact = str(member.get("artifact") or "").strip()
        status = str(member.get("status") or "unknown")
        body = content or (f"Role artifact: {artifact}" if artifact else "No usable role body.")
        sections.append(
            f"## {member_id} [{status}]\n\n"
            + body
        )
    return "\n\n---\n\n".join(sections)


def _team_lead_prompt(
    *,
    target: str,
    request: str,
    state: Mapping[str, Any],
    report_path: str,
    retry_note: str = "",
) -> str:
    data_package = state.get("data_package")
    package_content = (
        str(data_package.get("content") or "")
        if isinstance(data_package, Mapping)
        else ""
    )
    supplements = "\n".join(
        f"- {item}" for item in state.get("user_supplements", []) if str(item).strip()
    ) or "- none"
    return f"""# Runtime graph node: {TEAM_LEAD}

The runtime has completed the data-package node and joined all four role
branches. You are now executing only Team Lead cross-examination and synthesis.
Do not spawn agents, do not change graph state, and do not search old runs or
unrelated reports. Use only the current-run evidence below.

Validated current security: {target}
Current user request: {request}

Base data package:
---
{package_content or '(degraded package; rely on the role evidence and label gaps)'}
---

Four current-run role results:
{_member_evidence(state)}

Active-turn supplements:
{supplements}

Cross-examine disagreements, align dates/units/accounting scope, distinguish
facts from estimates, personally fill degraded dimensions from already bound
structured sources when necessary, and produce the complete investment report.
The report must include a data cutoff, source matrix, four-dimensional score,
key metric/trend tables, bull/base/bear reasoning, risk matrix, information
richness rating, confidence/gaps, and an AI/non-investment-advice disclaimer.

You MUST write the full Markdown report with `write_file` to exactly:
`{report_path}`
Writing that Markdown creates the HTML companion used for delivery. Do not hand
write a separate HTML document. Your final response should briefly state that
the draft is ready for the fixed report-audit node.
{retry_note}
"""


def _audit_prompt(
    *,
    target: str,
    request: str,
    state: Mapping[str, Any],
    report_path: str,
) -> str:
    return f"""# Runtime graph node: {REPORT_AUDIT}

You are executing the final fixed graph node for `{target}`. Read only the
current report `{report_path}` and the current-run evidence supplied in this
prompt. Do not spawn agents, change plans, or inspect old report directories.

Current user request: {request}

Audit the Markdown report for: correct security identity; material numeric
claims carrying date/unit/source; contradictions against the base package and
four role results; source conflicts; unsupported certainty; missing risk and
limitation disclosures; and presence of the required report sections. Use
structured sources only for a small number of material spot checks. The audit
has at most {AUDIT_MAX_MODEL_ROUNDS} model rounds: no more than
{AUDIT_MAX_TOOL_ITERATIONS} tool-capable rounds followed, only when necessary,
by one final no-tools delivery-summary round. If material corrections are
needed, perform at most one complete rewrite of the same Markdown path with
`write_file`. `edit_file` is intentionally unavailable. After that single
rewrite, do not attempt another file modification; finish the audit using the
current report. The HTML companion created by `write_file` remains the final
delivery artifact.

Current-run graph evidence:
{_member_evidence(state)}

When the audit is complete, give the user a concise Chinese executive summary
of the report's core conclusion, supporting reasons, valuation/risk view, and
explicitly identify the HTML report path. This is business-facing delivery
copy: never mention internal iteration limits, fallbacks, degradation, tool
budgets, or runtime control state. Do not merely describe what you would audit.
"""


def _preferred_report_artifact(paths: list[str], report_path: str) -> str | None:
    expected = Path(report_path).with_suffix(".html")
    for path in paths:
        candidate = Path(path)
        if candidate == expected:
            return path
        if (
            not expected.is_absolute()
            and len(candidate.parts) >= len(expected.parts)
            and candidate.parts[-len(expected.parts):] == expected.parts
        ):
            return path
    return None


def _merge_usage(target: dict[str, int], addition: Mapping[str, Any]) -> None:
    for key, value in addition.items():
        if isinstance(value, int | float):
            target[str(key)] = target.get(str(key), 0) + int(value)


def fail_asset_research_state(
    state: Mapping[str, Any],
    *,
    node: str,
    error: str,
) -> dict[str, Any]:
    failed = deepcopy(dict(state))
    node_states = deepcopy(dict(failed.get("node_states") or {}))
    node_state = dict(node_states.get(node) or {})
    node_state.update({"status": "failed", "error": error})
    node_states[node] = node_state
    failed.update({
        "node_states": node_states,
        "active_nodes": [],
        "node": node,
        "status": "failed",
        "error": error,
        "checkpoint_revision": int(failed.get("checkpoint_revision") or 0) + 1,
    })
    return failed


class AssetResearchWorkflowRuntime:
    """Run the fixed asset-research DAG using injected node executors."""

    def __init__(
        self,
        *,
        run_agent_node: RunAgentNode,
        run_member_wave: RunMemberWave,
        publish_state: PublishState,
    ) -> None:
        self._run_agent_node = run_agent_node
        self._run_member_wave = run_member_wave
        self._publish_state = publish_state

    async def run(
        self,
        *,
        run_id: str,
        target: str,
        request: str,
        team: Mapping[str, Any],
        report_path: str,
        resume_from: Mapping[str, Any] | None = None,
        supplemental_artifacts: list[str] | None = None,
    ) -> AssetResearchWorkflowOutcome:
        state = new_asset_research_state(
            run_id=run_id,
            member_ids=MEMBER_NODES,
            resume_from=resume_from,
            supplemental_artifacts=supplemental_artifacts or [],
        )
        await self._publish_state(
            state,
            "resume_started" if resume_from is not None else "run_started",
            "主笔正在读取上次角色产物并重新交叉质证"
            if resume_from is not None
            else "正在建立当前标的的基础数据包",
        )

        usage: dict[str, int] = {}
        tools_used: list[str] = []
        artifacts: list[str] = []

        if resume_from is None:
            data_outcome = await self._run_agent_node(
                DATA_PACKAGE,
                _data_package_prompt(target=target, request=request),
                False,
            )
            _merge_usage(usage, data_outcome.usage)
            tools_used.extend(data_outcome.tools_used)
            package = data_outcome.content.strip()
            degraded_package = (
                data_outcome.stop_reason in {"error", "tool_error", "max_iterations"}
                or not package
            )
            if not package:
                package = (
                    "基础数据节点未返回完整正文。四个角色必须使用当前配置的结构化"
                    "数据源独立补齐，并在结果中明确标注本数据包缺失和低置信度。"
                )
            state = advance_asset_research_graph(
                state,
                "data_package_ready",
                {"content": package, "degraded": degraded_package},
            )
            await self._publish_state(
                state,
                "data_package_ready",
                "基础数据包已建立，四位专家开始并行研究",
            )

            member_batch = await self._run_member_wave(_member_prompts(
                target=target,
                request=request,
                team=team,
                data_package=package,
            ))
            if member_batch.supplements:
                # Supplements are retained now and consumed after the join.
                state["user_supplements"] = list(dict.fromkeys([
                    *state.get("user_supplements", []),
                    *member_batch.supplements,
                ]))
            for member_id in MEMBER_NODES:
                outcome = member_batch.members.get(member_id)
                if outcome is None:
                    outcome = MemberNodeOutcome(
                        member_id=member_id,
                        status="failed",
                        content="Runtime did not receive a terminal member result.",
                        activity="未收到该角色终态，主笔将按降级流程补齐",
                    )
                state = advance_asset_research_graph(
                    state,
                    "member_updated",
                    {
                        "id": member_id,
                        "status": outcome.status,
                        "content": outcome.content,
                        "activity": outcome.activity,
                        **({"artifact": outcome.artifact} if outcome.artifact else {}),
                    },
                )
                await self._publish_state(
                    state,
                    "member_updated",
                    outcome.activity or f"{member_id} 已进入终态",
                )

        if state.get("active_nodes") != [TEAM_LEAD]:
            state = fail_asset_research_state(
                state,
                node=TEAM_LEAD,
                error="four-role fan-in did not activate Team Lead",
            )
            await self._publish_state(state, "workflow_failed", "四角色汇合失败，流程已停止")
            return AssetResearchWorkflowOutcome(
                final_content="资产投研工作流未能完成四角色汇合，请稍后重试。",
                stop_reason="workflow_error",
                graph_state=state,
                tools_used=tools_used,
                usage=usage,
                artifacts=artifacts,
            )

        lead_outcome: AgentNodeOutcome | None = None
        report_artifact: str | None = None
        for attempt in range(2):
            retry_note = (
                "\nRuntime verification: the previous attempt did not create the required "
                "Markdown/HTML artifact. Write the exact path now before finalizing."
                if attempt
                else ""
            )
            lead_outcome = await self._run_agent_node(
                TEAM_LEAD,
                _team_lead_prompt(
                    target=target,
                    request=request,
                    state=state,
                    report_path=report_path,
                    retry_note=retry_note,
                ),
                False,
            )
            _merge_usage(usage, lead_outcome.usage)
            tools_used.extend(lead_outcome.tools_used)
            artifacts.extend(lead_outcome.artifacts)
            report_artifact = _preferred_report_artifact(artifacts, report_path)
            if report_artifact is not None:
                break

        if lead_outcome is None or report_artifact is None:
            state = fail_asset_research_state(
                state,
                node=TEAM_LEAD,
                error="Team Lead did not create the required HTML report artifact",
            )
            await self._publish_state(
                state,
                "workflow_failed",
                "主笔未生成必需报告文件，流程已停止",
            )
            return AssetResearchWorkflowOutcome(
                final_content="主笔汇总完成失败：未生成可交付的 HTML 研报。",
                stop_reason="workflow_error",
                graph_state=state,
                tools_used=list(dict.fromkeys(tools_used)),
                usage=usage,
                artifacts=list(dict.fromkeys(artifacts)),
            )

        state = advance_asset_research_graph(
            state,
            "report_written",
            {"artifact": report_artifact},
        )
        await self._publish_state(
            state,
            "report_written",
            "主笔交叉质证完成，进入报告审校与交付",
        )

        audit_outcome = await self._run_agent_node(
            REPORT_AUDIT,
            _audit_prompt(
                target=target,
                request=request,
                state=state,
                report_path=report_path,
            ),
            True,
        )
        _merge_usage(usage, audit_outcome.usage)
        tools_used.extend(audit_outcome.tools_used)
        artifacts.extend(audit_outcome.artifacts)
        artifacts.append(report_artifact)
        final_artifacts = list(dict.fromkeys(artifacts))
        audit_degraded = audit_outcome.stop_reason in {"error", "tool_error"}
        if audit_degraded:
            state["degraded"] = True
        state = advance_asset_research_graph(
            state,
            "audit_completed",
            {"artifacts": {"report": report_artifact}},
        )
        await self._publish_state(
            state,
            "audit_completed",
            "报告已完成审校并交付",
        )

        final_content = audit_outcome.content.strip() or (
            "资产投研报告已完成。"
            f"完整 HTML 报告：`{report_artifact}`"
        )
        internal_runtime_markers = (
            "maximum number of tool call iterations",
            "tool-call budget",
            "max-iteration",
            "轮上限",
            "迭代上限",
            "降级交付",
        )
        if (
            audit_outcome.stop_reason == "max_iterations"
            and any(
                marker in final_content.lower()
                for marker in internal_runtime_markers
            )
        ):
            final_content = (
                "资产投研报告已完成复核，请查看报告中的核心结论、估值情景与"
                f"风险分析。完整 HTML 报告：`{report_artifact}`"
            )
        elif report_artifact not in final_content:
            final_content = (
                f"{final_content}\n\n完整 HTML 报告：`{report_artifact}`"
            )
        return AssetResearchWorkflowOutcome(
            final_content=final_content,
            stop_reason=(
                audit_outcome.stop_reason
                if audit_outcome.stop_reason not in {"error", "tool_error"}
                else "completed_with_warnings"
            ),
            graph_state=state,
            tools_used=list(dict.fromkeys(tools_used)),
            usage=usage,
            artifacts=final_artifacts,
        )

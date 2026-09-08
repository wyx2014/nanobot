"""Executable coordinator for the two-wave supply-chain bottleneck DAG."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from nanobot.graph.workflows.asset_research_runtime import (
    AUDIT_MAX_MODEL_ROUNDS,
    AUDIT_MAX_TOOL_ITERATIONS,
    AgentNodeOutcome,
    MemberBatchOutcome,
    MemberNodeOutcome,
    _audit_warning,
    _merge_usage,
    _preferred_report_artifact,
    _strip_duplicate_report_reference,
)
from nanobot.graph.workflows.supply_chain_bottleneck import (
    DISCOVERY_MEMBER_NODES,
    MEMBER_NODES,
    REPORT_AUDIT,
    SCOPE_BRIEF,
    TEAM_LEAD,
    VALIDATION_MEMBER_NODES,
    advance_supply_chain_bottleneck_graph,
    new_supply_chain_bottleneck_state,
)

RunAgentNode = Callable[[str, str, bool], Awaitable[AgentNodeOutcome]]
RunMemberWave = Callable[[dict[str, str]], Awaitable[MemberBatchOutcome]]
PublishState = Callable[[dict[str, Any], str, str], Awaitable[None]]
WriteReport = Callable[[str, str], Awaitable[list[str]]]

SCOPE_MAX_TOOL_ITERATIONS = 8
TEAM_LEAD_MAX_TOOL_ITERATIONS = 6
_MEMBER_EVIDENCE_MAX_CHARS = 8_000


@dataclass(slots=True)
class SupplyChainBottleneckWorkflowOutcome:
    final_content: str
    stop_reason: str
    graph_state: dict[str, Any]
    tools_used: list[str]
    usage: dict[str, int]
    artifacts: list[str]


def _member_config(team: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = team.get("members")
    if not isinstance(raw, list):
        return {}
    return {
        str(item.get("id")): item
        for item in raw
        if isinstance(item, Mapping) and str(item.get("id") or "") in MEMBER_NODES
    }


def _scope_brief_prompt(*, target: str, request: str) -> str:
    return f"""# Runtime graph node: {SCOPE_BRIEF}

You are executing only the fixed supply-chain-bottleneck graph's scope-brief
node. The runtime owns all later nodes. Do not create/update a plan, spawn
agents, inspect old reports, rank stocks, or begin final synthesis.

Validated current research theme: {target}
Current user request: {request}

Create one bounded research-theme card for this exact theme. Normalize the
theme, geography, 3-5 year horizon, physical end demand, known capex programs,
important terminology in Chinese and English, and the evidence questions that
the two discovery members must answer. Record an initial data cutoff and source
plan. This is intake and shared context, not the final bottleneck conclusion.

Return the complete theme card in your final response. Mark ambiguity and
evidence gaps rather than inheriting an older conversation topic.
"""


def _scope_content(state: Mapping[str, Any]) -> str:
    raw = state.get("scope_brief")
    return str(raw.get("content") or "") if isinstance(raw, Mapping) else ""


def _member_evidence(
    state: Mapping[str, Any],
    member_ids: tuple[str, ...] = MEMBER_NODES,
    *,
    max_chars_per_member: int = _MEMBER_EVIDENCE_MAX_CHARS,
) -> str:
    sections: list[str] = []
    members = state.get("members")
    if not isinstance(members, Mapping):
        return "(no member results recorded)"
    for member_id in member_ids:
        member = members.get(member_id)
        if not isinstance(member, Mapping):
            continue
        content = str(member.get("content") or "").strip()
        artifact = str(member.get("artifact") or "").strip()
        status = str(member.get("status") or "unknown")
        if content and len(content) > max_chars_per_member:
            content = (
                content[:max_chars_per_member].rstrip()
                + "\n\n[角色正文已按运行时上下文预算截断；需要更多细节时读取角色产物："
                + (artifact or "未记录")
                + "]"
            )
        body = content or (f"Role artifact: {artifact}" if artifact else "No usable role body.")
        sections.append(f"## {member_id} [{status}]\n\n{body}")
    return "\n\n---\n\n".join(sections) or "(no usable member evidence)"


def _member_prompts(
    *,
    member_ids: tuple[str, ...],
    phase_name: str,
    target: str,
    request: str,
    team: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, str]:
    configs = _member_config(team)
    discovery_evidence = (
        _member_evidence(state, DISCOVERY_MEMBER_NODES)
        if phase_name == "validation"
        else "(first wave; no prior member evidence)"
    )
    supplements = "\n".join(
        f"- {item}" for item in state.get("user_supplements", []) if str(item).strip()
    ) or "- none"
    prompts: dict[str, str] = {}
    for member_id in member_ids:
        member = configs.get(member_id, {})
        name = str(member.get("name") or member_id)
        framework = str(member.get("framework") or "").strip()
        description = str(member.get("description") or "").strip()
        instructions = str(member.get("instructions") or "").strip()
        prompts[member_id] = f"""# Runtime graph node: {member_id}

You are the {name}{f' ({framework})' if framework else ''} branch in phase
`{phase_name}` of a fixed two-wave supply-chain bottleneck DAG. Research only
the validated current theme `{target}`. Work independently and return a
complete role report. Do not coordinate, wait, spawn, modify plans, or perform
Team Lead synthesis.

Current user request:
{request}

Shared research-theme card:
---
{_scope_content(state) or '(scope intake was degraded; explicitly reconstruct only what your role needs)'}
---

First-wave discovery evidence available to validation members:
{discovery_evidence}

Active-turn user supplements:
{supplements}

Role responsibility:
{description or 'Complete this role’s assigned supply-chain research dimension.'}

Role playbook:
{instructions or 'Cite evidence, label dates/units/conflicts/gaps, and give a bounded conclusion.'}

For global chain and bottleneck evidence, prefer configured AnySearch, official
industry/company/customer disclosures, and public web sources. For candidate
company financial, market, and valuation fields, use one configured iFinD,
Juyuan, or Caihui source as the consistent base table. Cross-check only material
claims or conflicts for the bounded finalists with a second source; use a third
source only to adjudicate a conclusion-changing conflict. Never query every
source for every field. A failed source is a gap, not permission to invent data
or repeat the same query indefinitely.
Keep the role report concise: no more than 2,500 Chinese characters plus compact
tables and source references. Stop researching once your playbook's bounded
coverage target is met; explicitly mark remaining gaps instead of expanding the
theme or exhaustively enumerating entities.
"""
    return prompts


def _team_lead_prompt(
    *,
    target: str,
    request: str,
    state: Mapping[str, Any],
    report_path: str,
) -> str:
    supplements = "\n".join(
        f"- {item}" for item in state.get("user_supplements", []) if str(item).strip()
    ) or "- none"
    return f"""# Runtime graph node: {TEAM_LEAD}

The runtime has joined both discovery members and all three validation members.
Execute only Team Lead cross-examination and synthesis. Do not spawn agents,
change graph state, inspect old report directories, or substitute a previous
theme. Use the current-run evidence below.

Validated current research theme: {target}
Current user request: {request}

Research-theme card:
---
{_scope_content(state) or '(degraded scope; preserve this gap in the report)'}
---

Five current-run role results:
{_member_evidence(state)}

Active-turn supplements:
{supplements}

Cross-examine disagreements and keep three decisions separate: whether the
trend is real, whether a physical node is a durable bottleneck, and whether a
listed company at its current valuation is an investable mapping. Align dates,
units, currencies, accounting periods, and geographic scope. Show contrary
evidence, confidence, and the earliest conditions that can release each
bottleneck. Treat all numeric thresholds from the source method as disclosed
screening heuristics, not facts that can replace evidence.

The report must include a data cutoff; trend validation; Mermaid physical-chain
map; S/A/B bottleneck score table with all six dimensions; duration/release
conditions; candidate-company exposure and financial/valuation tables;
positive-vs-counter evidence matrix; action tiers; source matrix; information
richness; gaps/confidence; and an AI/non-investment-advice disclaimer.

You MUST write the full Chinese Markdown report with `write_file` to exactly:
`{report_path}`
Writing that Markdown creates the HTML companion used for delivery. Do not hand
write a separate HTML document. Keep the report within 6,000 Chinese characters
excluding compact tables and sources. Call `write_file` before returning any
final response; do not stream the report body as chat text. Your final response
should only state that the draft is ready for the fixed report-audit node.
"""


def _fallback_report_markdown(
    *,
    target: str,
    request: str,
    state: Mapping[str, Any],
    lead_content: str,
) -> str:
    """Create a useful, auditable draft when Team Lead forgot file delivery."""

    cleaned = lead_content.strip()
    usable_lead = len(cleaned) >= 600 and any(
        marker in cleaned for marker in ("##", "结论", "瓶颈", "供应链")
    )
    if usable_lead:
        body = cleaned
        if not body.lstrip().startswith("#"):
            body = f"# {target}供应链瓶颈地图\n\n{body}"
        return body + (
            "\n\n---\n\n## 交付说明\n\n"
            "主笔已完成正文，但未主动执行文件写入；运行时已保全本轮正文并生成交付文件。"
        )

    degraded_members = [
        member_id
        for member_id, member in dict(state.get("members") or {}).items()
        if isinstance(member, Mapping)
        and str(member.get("status") or "") in {"failed", "cancelled"}
    ]
    degraded_text = "、".join(degraded_members) or "无"
    return f"""# {target}供应链瓶颈地图（降级交付）

## 研究范围

- 当前请求：{request}
- 研究主题：{target}
- 数据截止日：以各角色证据中标注日期为准；未注明日期的数据视为待复核。
- 降级角色：{degraded_text}

## 研究主题卡

{_scope_content(state) or '主题卡未形成完整正文，以下结论仅基于现有角色证据。'}

## 角色证据汇总

{_member_evidence(state, max_chars_per_member=6_000)}

## 综合判断与行动边界

本轮主笔未形成可直接交付的完整综合正文。以上为运行时保全的当前轮证据，
可用于判断趋势、物理瓶颈、候选公司与估值约束，但不能把缺失字段视为零或
把单一来源推断视为确定事实。报告审校节点应补齐核心冲突、失效条件与置信度；
仍缺数据的候选公司不得进入高置信度行动层级。

## 风险声明

本报告由 AI 基于当前可用资料生成，仅用于研究辅助，不构成投资建议。
"""


def _recoverable_lead_content(outcome: AgentNodeOutcome) -> str:
    """Join visible Team Lead segments produced during output-length recovery."""

    parts: list[str] = []
    for message in outcome.messages:
        if message.get("role") != "assistant" or message.get("tool_calls"):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    if outcome.content.strip():
        parts.append(outcome.content.strip())
    return "\n\n".join(dict.fromkeys(parts))


def _audit_prompt(
    *,
    target: str,
    request: str,
    state: Mapping[str, Any],
    report_path: str,
) -> str:
    return f"""# Runtime graph node: {REPORT_AUDIT}

You are executing the final fixed graph node for supply-chain theme `{target}`.
Read only the current report `{report_path}` and current-run evidence in this
prompt. Do not spawn agents, change plans, or inspect old report directories.

Current user request: {request}

Audit for: correct theme identity and scope; physical rather than narrative
chain decomposition; every S/A/B score retaining evidence; numeric claims with
date/unit/currency/source; separation of trend, bottleneck, company, and
valuation conclusions; conflicting evidence; substitution and bottleneck
release conditions; unsupported certainty; missing valuation/risk/limitation
disclosures; and all required report sections. Use tools only for a few material
spot checks. The audit has at most {AUDIT_MAX_MODEL_ROUNDS} model rounds: no
more than {AUDIT_MAX_TOOL_ITERATIONS} tool-capable rounds and, only when needed,
one final no-tools delivery-summary round. If corrections are material, perform
at most one complete rewrite of the same Markdown with `write_file`.
`edit_file` is intentionally unavailable. Do not modify the file again after
that rewrite.

Current-run evidence:
{_member_evidence(state)}

When complete, give the user a concise Chinese executive summary: validated
bottlenecks, strongest mapped opportunities, valuation constraints, invalidation
signals, and key risks. The GUI presents the HTML in an attachment card, so do
not repeat its path, filename, Markdown link, or “完整 HTML 报告”. Never mention
internal iteration limits, fallback mechanics, degradation, or runtime state.
"""


def fail_supply_chain_bottleneck_state(
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


class SupplyChainBottleneckWorkflowRuntime:
    """Run the fixed two-wave workflow using injected agent executors."""

    def __init__(
        self,
        *,
        run_agent_node: RunAgentNode,
        run_member_wave: RunMemberWave,
        publish_state: PublishState,
        write_report: WriteReport | None = None,
    ) -> None:
        self._run_agent_node = run_agent_node
        self._run_member_wave = run_member_wave
        self._publish_state = publish_state
        self._write_report = write_report

    async def _apply_member_wave(
        self,
        *,
        state: dict[str, Any],
        member_ids: tuple[str, ...],
        prompts: dict[str, str],
        completion_activity: str,
    ) -> tuple[dict[str, Any], list[str]]:
        batch = await self._run_member_wave(prompts)
        supplements = list(batch.supplements)
        if supplements:
            state["user_supplements"] = list(dict.fromkeys([
                *state.get("user_supplements", []),
                *supplements,
            ]))
        for member_id in member_ids:
            outcome = batch.members.get(member_id)
            if outcome is None:
                outcome = MemberNodeOutcome(
                    member_id=member_id,
                    status="failed",
                    content="Runtime did not receive a terminal member result.",
                    activity="未收到该角色终态，主笔将按降级流程补齐",
                )
            state = advance_supply_chain_bottleneck_graph(
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
        await self._publish_state(state, "wave_joined", completion_activity)
        return state, supplements

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
    ) -> SupplyChainBottleneckWorkflowOutcome:
        state = new_supply_chain_bottleneck_state(
            run_id=run_id,
            member_ids=MEMBER_NODES,
            resume_from=resume_from,
            supplemental_artifacts=supplemental_artifacts or [],
        )
        state["target"] = target
        await self._publish_state(
            state,
            "resume_started" if resume_from is not None else "run_started",
            "主笔正在读取上次角色产物并重新交叉质证"
            if resume_from is not None
            else "正在建立本轮研究主题卡",
        )

        usage: dict[str, int] = {}
        tools_used: list[str] = []
        artifacts: list[str] = []

        if resume_from is None:
            scope_outcome = await self._run_agent_node(
                SCOPE_BRIEF,
                _scope_brief_prompt(target=target, request=request),
                False,
            )
            _merge_usage(usage, scope_outcome.usage)
            tools_used.extend(scope_outcome.tools_used)
            scope = scope_outcome.content.strip()
            degraded_scope = (
                scope_outcome.stop_reason in {"error", "tool_error", "max_iterations"}
                or not scope
            )
            if not scope:
                scope = (
                    f"研究主题：{target}\n当前请求：{request}\n"
                    "主题卡节点未形成完整证据边界；成员必须自行核验地域、时间窗口和"
                    "物理需求，并明确标注低置信度。"
                )
            state = advance_supply_chain_bottleneck_graph(
                state,
                "scope_brief_ready",
                {"content": scope, "degraded": degraded_scope},
            )
            await self._publish_state(
                state,
                "scope_brief_ready",
                "研究主题卡已建立，两位发现专家开始并行研究",
            )

            state, _ = await self._apply_member_wave(
                state=state,
                member_ids=DISCOVERY_MEMBER_NODES,
                prompts=_member_prompts(
                    member_ids=DISCOVERY_MEMBER_NODES,
                    phase_name="discovery",
                    target=target,
                    request=request,
                    team=team,
                    state=state,
                ),
                completion_activity="趋势与产业链发现已汇合，三位验证专家开始并行研究",
            )
            if state.get("active_nodes") != list(VALIDATION_MEMBER_NODES):
                return await self._workflow_failure(
                    state=state,
                    node=VALIDATION_MEMBER_NODES[0],
                    error="discovery fan-in did not activate the validation wave",
                    activity="第一阶段汇合失败，流程已停止",
                    tools_used=tools_used,
                    usage=usage,
                    artifacts=artifacts,
                )

            state, _ = await self._apply_member_wave(
                state=state,
                member_ids=VALIDATION_MEMBER_NODES,
                prompts=_member_prompts(
                    member_ids=VALIDATION_MEMBER_NODES,
                    phase_name="validation",
                    target=target,
                    request=request,
                    team=team,
                    state=state,
                ),
                completion_activity="瓶颈、标的估值与反方验证已汇合，主笔开始交叉质证",
            )

        if state.get("active_nodes") != [TEAM_LEAD]:
            return await self._workflow_failure(
                state=state,
                node=TEAM_LEAD,
                error="validation fan-in did not activate Team Lead",
                activity="第二阶段汇合失败，流程已停止",
                tools_used=tools_used,
                usage=usage,
                artifacts=artifacts,
            )

        lead_outcome = await self._run_agent_node(
            TEAM_LEAD,
            _team_lead_prompt(
                target=target,
                request=request,
                state=state,
                report_path=report_path,
            ),
            False,
        )
        _merge_usage(usage, lead_outcome.usage)
        tools_used.extend(lead_outcome.tools_used)
        artifacts.extend(lead_outcome.artifacts)
        report_artifact = _preferred_report_artifact(artifacts, report_path)

        if report_artifact is None and self._write_report is not None:
            fallback_markdown = _fallback_report_markdown(
                target=target,
                request=request,
                state=state,
                lead_content=_recoverable_lead_content(lead_outcome),
            )
            try:
                fallback_artifacts = await self._write_report(
                    report_path,
                    fallback_markdown,
                )
            except Exception:
                fallback_artifacts = []
            artifacts.extend(fallback_artifacts)
            report_artifact = _preferred_report_artifact(artifacts, report_path)
            if report_artifact is not None:
                state["degraded"] = True
                tools_used.append("write_file")
                await self._publish_state(
                    state,
                    "report_recovered",
                    "主笔正文未主动落盘，运行时已保全内容并继续审校",
                )

        if report_artifact is None:
            return await self._workflow_failure(
                state=state,
                node=TEAM_LEAD,
                error="Team Lead did not create the required HTML report artifact",
                activity="主笔未生成必需报告文件，流程已停止",
                tools_used=tools_used,
                usage=usage,
                artifacts=artifacts,
            )

        state = advance_supply_chain_bottleneck_graph(
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
        audit_warning = _audit_warning(audit_outcome)
        state = advance_supply_chain_bottleneck_graph(
            state,
            "audit_completed",
            {"artifacts": {"report": report_artifact},
             "verified": not audit_warning, "warning": audit_warning},
        )
        await self._publish_state(
            state, "audit_completed", audit_warning or "瓶颈地图已完成审校并交付",
        )

        final_content = audit_outcome.content.strip() or (
            "供应链瓶颈地图已完成，请通过下方报告卡片查看完整内容。"
        )
        internal_runtime_markers = (
            "maximum number of tool call iterations",
            "tool-call budget",
            "max-iteration",
            "轮上限",
            "迭代上限",
            "降级交付",
        )
        if audit_warning:
            if (audit_outcome.stop_reason in {"error", "tool_error", "empty_final_response"}
                    or not audit_outcome.content.strip()
                    or any(marker in final_content.lower() for marker in internal_runtime_markers)):
                final_content = audit_warning
            else:
                final_content = f"{audit_warning}\n\n{final_content}"
        final_content = _strip_duplicate_report_reference(
            final_content,
            report_artifact,
        ) or "供应链瓶颈地图已完成，请通过下方报告卡片查看完整内容。"
        return SupplyChainBottleneckWorkflowOutcome(
            final_content=final_content,
            stop_reason="completed_with_warnings" if state.get("degraded") else "completed",
            graph_state=state,
            tools_used=list(dict.fromkeys(tools_used)),
            usage=usage,
            artifacts=final_artifacts,
        )

    async def _workflow_failure(
        self,
        *,
        state: Mapping[str, Any],
        node: str,
        error: str,
        activity: str,
        tools_used: list[str],
        usage: dict[str, int],
        artifacts: list[str],
    ) -> SupplyChainBottleneckWorkflowOutcome:
        failed = fail_supply_chain_bottleneck_state(state, node=node, error=error)
        await self._publish_state(failed, "workflow_failed", activity)
        return SupplyChainBottleneckWorkflowOutcome(
            final_content="供应链瓶颈研究工作流未能生成可交付报告，请稍后重试。",
            stop_reason="workflow_error",
            graph_state=failed,
            tools_used=list(dict.fromkeys(tools_used)),
            usage=usage,
            artifacts=list(dict.fromkeys(artifacts)),
        )

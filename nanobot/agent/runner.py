"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.request_user_input import InteractivePromptRequested
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.plan_policy import (
    PLAN_TOOL_NAME,
    PlanPolicyState,
    complex_request_reason,
    decide_plan_policy,
    plan_required_result,
)
from nanobot.runtime.trace_context import (
    TraceContext,
    current_trace_context,
    reset_trace_context,
    set_trace_context,
)
from nanobot.security.project_context import current_project_context
from nanobot.utils.file_edit_events import (
    StreamingFileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from nanobot.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from nanobot.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    find_legal_message_start,
    maybe_persist_tool_result,
    strip_reasoning_tags,
    strip_think,
    truncate_text,
)
from nanobot.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    available_structured_finance_sources,
    build_budget_exhausted_finalization_message,
    build_finalization_retry_message,
    build_goal_continue_message,
    build_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    is_internal_tool_call_markup,
    mark_structured_finance_source_attempted,
    mark_structured_finance_source_failed,
    normalize_tool_message_content,
    repeated_external_lookup_error,
    repeated_local_lookup_error,
    repeated_workspace_violation_error,
    structured_finance_fallback_instruction,
    structured_finance_result_failed,
    structured_finance_source,
    structured_finance_source_priority_error,
)

GoalContinueMessage = str | Callable[[], str | None]

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_MAX_CONSECUTIVE_REPEAT_LOOKUP_BLOCKS = 3
_EXTERNAL_LOOKUP_TOOL_NAMES = frozenset({
    "web_search",
    "search_web",
    "web_fetch",
    "navigate",
    "browser_navigate",
    "mcp_playwright_browser_navigate",
})
_EXTERNAL_LOOKUP_TOOL_PREFIXES = (
    "mcp_anysearch_",
    "mcp_juyuan_",
    "mcp_caihui_",
    "mcp_caihui_mcp_",
    "mcp_hexin-ifind-ds-",
    "mcp_ifind_",
)
_SLOW_PROVIDER_TIMING_CALLBACK_MS = 500
_SNIP_SAFETY_BUFFER = 1024
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir", "list_exec_sessions",
})
# read_file is the recovery path for persisted results; exempting it prevents persist->read->persist loops.
_TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS = frozenset({"read_file"})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"

# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    injection_overflow_predicate: Callable[[], bool] | None = None
    final_response_guard: Callable[[list[dict[str, Any]]], str | None] | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: GoalContinueMessage | None = None
    finalize_on_max_iterations: bool = True
    provider_timing_callback: Callable[[dict[str, Any]], Any] | None = None
    trace_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    parent_span_id: str | None = None
    agent_kind: str = "main"
    agent_label: str | None = None
    enforce_finance_source_priority: bool = False
    security_service: Any | None = None
    security_approval_callback: Callable[[dict[str, Any]], Any] | None = None
    security_interactive: bool = False
    security_chat_id: str | None = None
    security_turn_id: str | None = None
    security_turn_grants: set[str] = field(default_factory=set)


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    interactive_prompt_requested: bool = False


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider, trace_collector: Any | None = None):
        self.provider = provider
        self.trace_collector = trace_collector

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
        allow_final_response_guard: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found before the normal cycle cap, or an explicit
        overflow predicate says trusted work is still pending, append them to
        *messages* (and emit a checkpoint if *assistant_message* and *iteration*
        are both provided) and return (True, cycles+1) so the caller continues
        the iteration loop. Otherwise return (False, cycles).
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        guard_injection = False
        allow_overflow = False
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            predicate = spec.injection_overflow_predicate
            if predicate is not None:
                try:
                    allow_overflow = bool(predicate())
                except Exception:
                    logger.exception("injection_overflow_predicate callback failed")
        if injection_cycles < _MAX_INJECTION_CYCLES or allow_overflow:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [self._build_goal_continue_message(spec)]
        if (
            not injections
            and allow_final_response_guard
            and assistant_message is not None
            and spec.final_response_guard is not None
        ):
            try:
                guard_message = spec.final_response_guard([*messages, assistant_message])
            except Exception:
                logger.exception("final_response_guard callback failed")
                guard_message = None
            if guard_message and guard_message.strip():
                injections = [{"role": "user", "content": guard_message.strip()}]
                guard_injection = True
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            if injection_cycles > _MAX_INJECTION_CYCLES:
                logger.info(
                    "Injected {} expert-team follow-up message(s) {} "
                    "({}; normal cycle cap {})",
                    len(injections),
                    phase,
                    injection_cycles,
                    _MAX_INJECTION_CYCLES,
                )
            else:
                logger.info(
                    "Injected {} follow-up message(s) {} ({}/{})",
                    len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
                )
        elif guard_injection:
            logger.info("Injected final-response completion guard {}", phase)
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    def _build_goal_continue_message(self, spec: AgentRunSpec) -> dict[str, str]:
        custom = spec.goal_continue_message
        if callable(custom):
            try:
                custom = custom()
            except Exception:
                logger.exception("goal_continue_message callback failed")
                custom = None
        return build_goal_continue_message(custom)

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                if self._has_injection_content(item.get("content")):
                    injected_messages.append(item)
                continue
            if isinstance(item, dict):
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))
        inherited_trace = current_trace_context()
        trace_id = spec.trace_id or (
            inherited_trace.trace_id if inherited_trace is not None else None
        )
        trace_run_id: str | None = None
        trace_token = None
        trace_status = "completed"
        trace_stop_reason: str | None = None
        trace_error: dict[str, Any] | None = None

        if self.trace_collector is not None and trace_id is not None:
            project_context = current_project_context()
            request_context = current_request_context()
            request_metadata = (
                request_context.metadata
                if request_context is not None
                and isinstance(request_context.metadata, dict)
                else {}
            )
            trace_run_id = await self.trace_collector.begin_run(
                trace_id=trace_id,
                run_id=spec.run_id,
                parent_run_id=(
                    spec.parent_run_id
                    or (inherited_trace.run_id if inherited_trace is not None else None)
                ),
                parent_span_id=(
                    spec.parent_span_id
                    or (inherited_trace.span_id if inherited_trace is not None else None)
                ),
                agent_kind=spec.agent_kind,
                agent_label=spec.agent_label,
                project_id=(project_context.project_id if project_context else None),
                session_id=(project_context.session_id if project_context else None),
                turn_id=(
                    str(request_metadata.get("_runtime_turn_id") or "").strip()
                    or None
                ),
                provider=type(self.provider).__name__,
                model=spec.model,
            )
            trace_token = set_trace_context(
                TraceContext(trace_id=trace_id, run_id=trace_run_id)
            )
            try:
                tool_definitions = spec.tools.get_definitions()
            except Exception:
                tool_definitions = None
            context_span_id = await self.trace_collector.begin_span(
                kind="context",
                name="context.build",
                attributes={
                    "message_count": len(messages),
                    "tool_count": len(tool_definitions or []),
                },
            )
            try:
                await self.trace_collector.record_prompt_manifest(
                    trace_id=trace_id,
                    run_id=trace_run_id,
                    messages=messages,
                    tool_definitions=tool_definitions,
                )
            finally:
                await self.trace_collector.end_span(
                    context_span_id,
                    status="completed",
                )

        try:
            await hook.before_run(context)
            result = await self._run_core(spec, hook, messages)
        except asyncio.CancelledError as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            trace_status = "cancelled"
            trace_stop_reason = "cancelled"
            raise
        except Exception as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            trace_status = "failed"
            trace_stop_reason = "error"
            trace_error = {"type": type(exc).__name__, "message": str(exc)}
            await hook.on_error(context)
            raise
        else:
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            trace_stop_reason = result.stop_reason
            if result.stop_reason in {"error", "tool_error"} or result.error:
                trace_status = "failed"
                trace_error = {"message": result.error or result.stop_reason}
            elif result.stop_reason in {"cancelled", "interrupted", "stopped"}:
                trace_status = "cancelled"
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            context.messages = deepcopy(messages)
            try:
                if context.exception is None:
                    await hook.on_finally(context)
                else:
                    try:
                        await hook.on_finally(context)
                    except Exception:
                        logger.exception(
                            "AgentHook.on_finally error after {}",
                            context.stop_reason or "run exception",
                        )
            finally:
                # Trace cleanup is deliberately nested under hook cleanup.  A
                # user hook may fail, but it must never leave the Agent Run in
                # ``running`` state or leak its ContextVar into a later turn.
                try:
                    if self.trace_collector is not None and trace_run_id is not None:
                        await self.trace_collector.end_run(
                            run_id=trace_run_id,
                            status=trace_status,
                            stop_reason=trace_stop_reason or context.stop_reason,
                            error=trace_error,
                        )
                finally:
                    if trace_token is not None:
                        reset_trace_context(trace_token)

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        local_lookup_state: dict[str, Any] = {}
        consecutive_repeated_lookup_blocks = 0
        repeated_block_kind: str | None = None
        external_lookup_circuit_open = False
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        length_recovery_parts: list[str] = []
        had_injections = False
        injection_cycles = 0
        request_context = current_request_context()
        request_metadata = (
            request_context.metadata
            if request_context is not None and isinstance(request_context.metadata, dict)
            else {}
        )
        expert_team_plan = isinstance(request_metadata.get("expert_team"), dict)
        plan_policy_enabled = bool(
            request_context is not None
            and request_context.channel == "websocket"
            and request_metadata.get("webui") is True
        )
        latest_user_content = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ),
            "",
        )
        plan_policy_state = PlanPolicyState(
            plan_created=expert_team_plan or not plan_policy_enabled,
            forced_reason=(
                complex_request_reason(latest_user_content)
                if plan_policy_enabled and not expert_team_plan
                else None
            ),
        )

        for iteration in range(spec.max_iterations):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                messages_for_model = self._drop_orphan_tool_results(messages)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                messages_for_model = self._microcompact(messages_for_model)
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                messages_for_model = self._snip_history(spec, messages_for_model)
                # Snipping may have created new orphans; clean them up.
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
            except Exception:
                logger.exception(
                    "Context governance failed on turn {} for {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                )
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(context)
            try:
                usage_tools = spec.tools.get_definitions()
            except Exception:
                usage_tools = None
            if external_lookup_circuit_open:
                usage_tools = self._without_external_lookup_tools(usage_tools)
            prompt_estimate, _ = estimate_prompt_tokens_chain(
                self.provider,
                spec.model,
                messages_for_model,
                usage_tools,
            )
            live_usage = {
                **usage,
                "prompt_tokens": max(0, int(usage.get("prompt_tokens", 0)))
                + max(0, prompt_estimate),
                "completion_tokens": max(0, int(usage.get("completion_tokens", 0))),
            }
            live_usage["total_tokens"] = (
                live_usage["prompt_tokens"] + live_usage["completion_tokens"]
            )
            # The current prompt's cache hit ratio isn't known until the
            # provider responds.  Keep a confirmed lower bound so live UIs
            # don't count the whole prompt as new and then jump backwards
            # when cached_tokens arrives.
            confirmed_total = max(0, int(usage.get("total_tokens", 0)))
            if confirmed_total == 0:
                confirmed_total = (
                    max(0, int(usage.get("prompt_tokens", 0)))
                    + max(0, int(usage.get("completion_tokens", 0)))
                )
            confirmed_cached = min(
                confirmed_total,
                max(
                    0,
                    int(
                        usage.get(
                            "cached_tokens",
                            usage.get("cache_read_input_tokens", 0),
                        )
                    ),
                ),
            )
            live_usage["confirmed_new_tokens"] = confirmed_total - confirmed_cached
            live_usage["new_tokens"] = live_usage["confirmed_new_tokens"]
            await hook.on_usage(context, live_usage, estimated=True)
            response = await self._request_model(
                spec,
                messages_for_model,
                hook,
                context,
                prompt_estimate=prompt_estimate,
                tool_definitions=usage_tools,
            )
            context.response = response
            context.tool_calls = list(response.tool_calls)

            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            if (response.finish_reason == "length" or length_recovery_parts) and (
                response.content and response.content.strip() == cleaned_content
            ):
                # Preserve split words/table rows and Markdown newlines when
                # reasoning cleanup made no change beyond trimming whitespace.
                cleaned_content = response.content
            response.content = cleaned_content
            raw_usage = self._usage_or_estimate(spec, messages_for_model, response)
            context.usage = dict(raw_usage)
            self._accumulate_usage(usage, raw_usage)
            await hook.on_usage(
                context,
                dict(usage),
                estimated=bool(usage.get("estimated_tokens", 0)),
            )
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                source_priority_errors: dict[str, str] = {}
                if spec.enforce_finance_source_priority:
                    available_sources = available_structured_finance_sources(
                        spec.tools.tool_names
                    )
                    for tool_call in response.tool_calls:
                        priority_error = structured_finance_source_priority_error(
                            tool_call.name,
                            tool_call.arguments,
                            external_lookup_counts,
                            available_sources,
                        )
                        if priority_error is not None:
                            source_priority_errors[tool_call.id] = priority_error
                    context.hidden_tool_call_ids.update(source_priority_errors)

                plan_decision = decide_plan_policy(
                    (tool_call.name for tool_call in response.tool_calls),
                    plan_policy_state,
                    expert_team=expert_team_plan,
                )
                if (
                    plan_decision.requires_plan
                    and not plan_policy_state.plan_created
                ):
                    context.hidden_tool_call_ids.update({
                        tool_call.id
                        for tool_call in response.tool_calls
                        if tool_call.name != PLAN_TOOL_NAME
                    })
                await hook.before_execute_tools(context)

                results, new_events, fatal_error, interactive_prompt_requested = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                    local_lookup_state,
                    plan_policy_state,
                    expert_team=expert_team_plan,
                    source_priority_errors=source_priority_errors,
                )
                tool_events.extend(new_events)
                blocked_repeated_external_lookup = any(
                    event.get("status") == "error"
                    and event.get("detail") == "repeated external lookup blocked"
                    for event in new_events
                )
                blocked_repeated_local_lookup = any(
                    event.get("status") == "error"
                    and event.get("detail") == "repeated local tool call blocked"
                    for event in new_events
                )
                blocked_repeated_lookup = (
                    blocked_repeated_external_lookup or blocked_repeated_local_lookup
                )
                if blocked_repeated_local_lookup:
                    repeated_block_kind = "local"
                elif blocked_repeated_external_lookup:
                    repeated_block_kind = "external"
                elif not blocked_repeated_lookup:
                    repeated_block_kind = None
                consecutive_repeated_lookup_blocks = (
                    consecutive_repeated_lookup_blocks + 1
                    if blocked_repeated_lookup
                    else 0
                )
                tools_used.extend(
                    tool_call.name
                    for tool_call, event in zip(response.tool_calls, new_events)
                    if event.get("status") == "ok"
                )
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                if interactive_prompt_requested:
                    final_content = None
                    stop_reason = "interactive_prompt"
                    context.final_content = final_content
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    break
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                length_recovery_parts.clear()
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                if (
                    not _drained
                    and consecutive_repeated_lookup_blocks
                    >= _MAX_CONSECUTIVE_REPEAT_LOOKUP_BLOCKS
                ):
                    logger.warning(
                        "Repeated {} lookup circuit breaker triggered for {} "
                        "after {} consecutive blocked iteration(s)",
                        repeated_block_kind or "tool",
                        spec.session_key or "default",
                        consecutive_repeated_lookup_blocks,
                    )
                    if repeated_block_kind == "external":
                        external_lookup_circuit_open = True
                        consecutive_repeated_lookup_blocks = 0
                        repeated_block_kind = None
                        messages.append({
                            "role": "user",
                            "content": (
                                "The external lookup circuit breaker has disabled further web, "
                                "browser, and remote research tools for this turn. Do not retry, "
                                "rename, or serialize those tool calls. Continue the original task "
                                "now with evidence already collected and the non-network tools that "
                                "remain available. If the task creates an artifact, finish it with "
                                "local file and artifact tools; replace unavailable images with "
                                "editable native exhibits and disclose evidence gaps. Never claim "
                                "an artifact exists until its creation tool returns successfully."
                            ),
                        })
                        await hook.after_iteration(context)
                        continue
                    final_content = await self._try_finalize_after_repeated_lookup_loop(
                        spec,
                        hook,
                        messages,
                        usage,
                        iteration=iteration,
                    )
                    if final_content is None:
                        final_content = self._max_iterations_fallback(spec)
                    stop_reason = (
                        "repeated_local_tool_call"
                        if repeated_block_kind == "local"
                        else "repeated_external_lookup"
                    )
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    break
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = self._finalize_model_content(hook, context, response.content)
            if response.finish_reason != "error" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                retry_messages = self._finalization_retry_messages(messages_for_model)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_or_estimate(spec, retry_messages, response)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = self._finalize_model_content(hook, context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    length_recovery_parts.append(clean)
                    await hook.after_iteration(context)
                    continue
                stop_reason = "max_iterations"

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=True,
                allow_final_response_guard=True,
            )
            if should_continue:
                had_injections = True
                length_recovery_parts.clear()
                length_recovery_count = 0
                if stop_reason == "max_iterations":
                    stop_reason = "completed"

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            # Length recovery is one answer split across responses. Retain the
            # prefix even if history compaction removed its message meanwhile.
            final_content = "".join([*length_recovery_parts, clean])
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
                length_recovery_parts.clear()
            final_content = None
            if spec.finalize_on_max_iterations and not length_recovery_parts:
                final_content = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    messages,
                    usage,
                )
            if final_content is None:
                final_content = "".join(length_recovery_parts) or self._max_iterations_fallback(spec)
            self._append_final_message(messages, final_content)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            interactive_prompt_requested=stop_reason == "interactive_prompt",
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        prompt_estimate: int | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
    ):
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=tool_definitions,
        )
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )
        streaming = wants_streaming or wants_progress_streaming
        provider_name = type(self.provider).__name__
        timing_base = {
            "iteration": context.iteration,
            "model": spec.model,
            "provider": provider_name,
            "prompt_estimate": max(0, int(prompt_estimate or 0)),
            "prompt_estimate_unit": "tokens",
            "streaming": streaming,
        }

        async def _emit_provider_timing(payload: dict[str, Any]) -> None:
            callback = spec.provider_timing_callback
            if callback is None:
                return
            callback_started = time.perf_counter()
            try:
                outcome = callback(payload)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                # Diagnostics must never be able to fail a model request.
                logger.exception("Provider timing callback failed")
            finally:
                callback_ms = round(
                    (time.perf_counter() - callback_started) * 1000
                )
                if callback_ms >= _SLOW_PROVIDER_TIMING_CALLBACK_MS:
                    logger.warning(
                        "slow provider timing callback event={} duration_ms={} "
                        "provider={} model={} iteration={}",
                        payload.get("event"),
                        callback_ms,
                        provider_name,
                        spec.model,
                        context.iteration,
                    )

        provider_started_at = 0.0
        first_event_recorded = False
        provider_ttft_ms_value: int | None = None

        async def _mark_first_event(
            first_event: str,
            *,
            observed: bool = True,
        ) -> None:
            nonlocal first_event_recorded, provider_ttft_ms_value
            if first_event_recorded:
                return
            first_event_recorded = True
            provider_ttft_ms = max(
                0,
                int(round((time.perf_counter() - provider_started_at) * 1000)),
            )
            provider_ttft_ms_value = provider_ttft_ms
            await _emit_provider_timing({
                **timing_base,
                "event": "first_event",
                "provider_ttft_ms": provider_ttft_ms,
                "first_event": first_event,
                "first_event_observed": observed,
                "measurement": (
                    "first_stream_event"
                    if observed
                    else "response_latency_fallback"
                ),
            })

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        if (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        ):
            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(spec.progress_callback, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if delta:
                await _mark_first_event("tool_call_delta")
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:
            thinking_buf = ""

            async def _stream(delta: str) -> None:
                if delta:
                    await _mark_first_event("content_delta")
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                nonlocal thinking_buf
                if not delta:
                    return
                await _mark_first_event("reasoning_delta")
                prev_clean = strip_reasoning_tags(thinking_buf)
                thinking_buf += delta
                new_clean = strip_reasoning_tags(thinking_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    context.streamed_reasoning = True
                    await hook.emit_reasoning(incremental)

            async def _stream_recover() -> None:
                await hook.on_stream_end(context, resuming=True)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=(
                    _tool_call_delta
                    if live_file_edits is not None or spec.provider_timing_callback is not None
                    else None
                ),
                on_stream_recover=_stream_recover,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                await _mark_first_event("content_delta")
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=(
                    _tool_call_delta
                    if live_file_edits is not None or spec.provider_timing_callback is not None
                    else None
                ),
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded NANOBOT_LLM_TIMEOUT_S.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        llm_span_id = (
            await self.trace_collector.begin_span(
                kind="llm",
                name="llm.call",
                attributes=timing_base,
            )
            if self.trace_collector is not None
            else None
        )
        await _emit_provider_timing({
            **timing_base,
            "event": "request_started",
        })
        provider_started_at = time.perf_counter()
        model_audit = spec.security_service.begin_model_audit(
            provider=self.provider, model=spec.model,
            session_key=spec.session_key, turn_id=spec.security_turn_id,
            agent_label=spec.agent_label,
        ) if spec.security_service is not None else None
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if not first_event_recorded:
                await _mark_first_event("response_completed", observed=False)
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            if spec.security_service is not None:
                spec.security_service.complete_audit(model_audit, result="timed_out")
            await _mark_first_event("timeout", observed=False)
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    llm_span_id,
                    status="failed",
                    ttft_ms=provider_ttft_ms_value,
                    error_code="LLM_TIMEOUT",
                    attributes={"timeout_s": outer_timeout_s},
                    error={"type": "TimeoutError"},
                )
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        except asyncio.CancelledError:
            if spec.security_service is not None:
                spec.security_service.complete_audit(model_audit, result="cancelled")
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    llm_span_id,
                    status="cancelled",
                    ttft_ms=provider_ttft_ms_value,
                    error_code="CANCELLED",
                )
            raise
        except BaseException as exc:
            if spec.security_service is not None:
                spec.security_service.complete_audit(model_audit, result="failed", details={"error_type": type(exc).__name__})
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    llm_span_id,
                    status="failed",
                    ttft_ms=provider_ttft_ms_value,
                    error_code=type(exc).__name__,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            raise
        if spec.security_service is not None:
            spec.security_service.complete_audit(
                model_audit, result="failed" if response.finish_reason == "error" else "succeeded",
                details={"status_code": response.error_status_code} if response.finish_reason == "error" else {},
            )
        if self.trace_collector is not None:
            llm_status = "failed" if response.finish_reason == "error" else "completed"
            await self.trace_collector.end_span(
                llm_span_id,
                status=llm_status,
                usage=self._trace_usage(response.usage),
                ttft_ms=provider_ttft_ms_value,
                error_code=(response.error_kind if llm_status == "failed" else None),
                attributes={
                    "finish_reason": response.finish_reason,
                    "tool_call_count": len(response.tool_calls),
                },
                error=(
                    {
                        "status_code": response.error_status_code,
                        "kind": response.error_kind,
                    }
                    if llm_status == "failed"
                    else None
                ),
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    @staticmethod
    def _without_external_lookup_tools(
        definitions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if definitions is None:
            return None

        def tool_name(schema: dict[str, Any]) -> str:
            function = schema.get("function")
            raw_name = (
                function.get("name")
                if isinstance(function, dict)
                else schema.get("name")
            )
            return str(raw_name or "").lower()

        return [
            schema
            for schema in definitions
            if (
                tool_name(schema) not in _EXTERNAL_LOOKUP_TOOL_NAMES
                and not tool_name(schema).startswith(_EXTERNAL_LOOKUP_TOOL_PREFIXES)
            )
        ]

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = self._finalization_retry_messages(messages)
        return await self._request_no_tools(spec, retry_messages)

    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> str | None:
        retry_messages = self._budget_exhausted_finalization_messages(messages)
        try:
            response = await self._request_no_tools(spec, retry_messages)
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None

        raw_usage = self._usage_or_estimate(spec, retry_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
        )
        clean = self._finalize_model_content(hook, context, response.content)
        if is_blank_text(clean):
            return None
        return clean

    async def _try_finalize_after_repeated_lookup_loop(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        *,
        iteration: int,
    ) -> str | None:
        finalization_messages = list(messages)
        finalization_messages.append({
            "role": "user",
            "content": (
                "The repeated tool-call circuit breaker has stopped an identical lookup loop. "
                "Do not call any tools. Complete the requested deliverable now using evidence "
                "already present in the conversation. Clearly label any unresolved evidence gaps "
                "instead of retrying, guessing, or claiming a blocked lookup succeeded. Return "
                "plain user-facing prose only; never output <tool_call>, <function=...>, or "
                "<parameter=...> protocol markup."
            ),
        })
        try:
            response = await self._request_no_tools(spec, finalization_messages)
        except Exception:
            logger.exception(
                "Repeated-lookup finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None
        raw_usage = self._usage_or_estimate(spec, finalization_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            return None
        final_context = AgentHookContext(
            iteration=iteration,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
        )
        clean = self._finalize_model_content(hook, final_context, response.content)
        return None if is_blank_text(clean) else clean

    @staticmethod
    def _finalize_model_content(
        hook: AgentHook,
        context: AgentHookContext,
        content: str | None,
    ) -> str | None:
        clean = hook.finalize_content(context, content)
        if is_internal_tool_call_markup(clean):
            logger.warning(
                "Suppressing serialized internal tool call returned as final content for {}",
                context.session_key or "default",
            )
            return None
        return clean

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        kwargs = self._build_request_kwargs(spec, messages, tools=None)
        provider_name = type(self.provider).__name__
        span_id = (
            await self.trace_collector.begin_span(
                kind="llm",
                name="llm.call",
                attributes={
                    "model": spec.model,
                    "provider": provider_name,
                    "streaming": False,
                    "tools_enabled": False,
                    "request_kind": "finalization",
                },
            )
            if self.trace_collector is not None
            else None
        )
        started_at = time.perf_counter()
        model_audit = spec.security_service.begin_model_audit(
            provider=self.provider, model=spec.model,
            session_key=spec.session_key, turn_id=spec.security_turn_id,
            agent_label=spec.agent_label,
        ) if spec.security_service is not None else None
        try:
            response = await self.provider.chat_with_retry(**kwargs)
        except asyncio.CancelledError:
            if spec.security_service is not None:
                spec.security_service.complete_audit(model_audit, result="cancelled")
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    span_id,
                    status="cancelled",
                    ttft_ms=max(0, int((time.perf_counter() - started_at) * 1_000)),
                    error_code="CANCELLED",
                )
            raise
        except BaseException as exc:
            if spec.security_service is not None:
                spec.security_service.complete_audit(model_audit, result="failed", details={"error_type": type(exc).__name__})
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    span_id,
                    status="failed",
                    ttft_ms=max(0, int((time.perf_counter() - started_at) * 1_000)),
                    error_code=type(exc).__name__,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            raise
        if spec.security_service is not None:
            spec.security_service.complete_audit(
                model_audit, result="failed" if response.finish_reason == "error" else "succeeded",
            )
        if self.trace_collector is not None:
            status = "failed" if response.finish_reason == "error" else "completed"
            await self.trace_collector.end_span(
                span_id,
                status=status,
                usage=self._trace_usage(response.usage),
                # Non-streaming providers expose no first-byte callback.  The
                # completed response latency is the explicit fallback metric.
                ttft_ms=max(0, int((time.perf_counter() - started_at) * 1_000)),
                error_code=(response.error_kind if status == "failed" else None),
                attributes={
                    "finish_reason": response.finish_reason,
                    "tool_call_count": len(response.tool_calls),
                    "ttft_measurement": "response_latency_fallback",
                },
            )
        return response

    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
            return usage
        if response.finish_reason == "error":
            return {}
        return self._estimate_response_usage(spec, messages, response)

    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        try:
            tools = spec.tools.get_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(self.provider, spec.model, messages, tools)
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @classmethod
    def _trace_usage(cls, usage: dict[str, Any] | None) -> dict[str, int]:
        """Normalize provider-confirmed usage for Trace aggregation.

        Estimated UI counters are deliberately excluded: an observability
        trace must never present an estimate as a provider-confirmed charge.
        """
        raw = cls._usage_dict(usage)
        input_tokens = max(
            0,
            raw.get("input_tokens", raw.get("prompt_tokens", 0)),
        )
        output_tokens = max(
            0,
            raw.get("output_tokens", raw.get("completion_tokens", 0)),
        )
        cached_input_tokens = max(
            0,
            raw.get(
                "cached_input_tokens",
                raw.get("cached_tokens", raw.get("cache_read_input_tokens", 0)),
            ),
        )
        total_tokens = max(
            0,
            raw.get("total_tokens", input_tokens + output_tokens),
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": min(cached_input_tokens, input_tokens),
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        local_lookup_state: dict[str, Any] | None = None,
        plan_policy_state: PlanPolicyState | None = None,
        *,
        expert_team: bool = False,
        source_priority_errors: dict[str, str] | None = None,
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None, bool]:
        local_lookup_state = local_lookup_state if local_lookup_state is not None else {}
        # Direct callers of this internal helper predate PlanPolicy and are
        # treated as already authorized. AgentRunner._run_core always supplies
        # the real per-turn state for WebUI turns.
        policy_state = plan_policy_state or PlanPolicyState(plan_created=True)
        decision = decide_plan_policy(
            (tool_call.name for tool_call in tool_calls),
            policy_state,
            expert_team=expert_team,
        )
        barrier_active = (
            decision.requires_plan
            and not policy_state.plan_created
        )
        publishes_plan = any(
            tool_call.name == PLAN_TOOL_NAME
            for tool_call in tool_calls
        )
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        barrier_counted = False
        for batch in batches:
            blocked = [
                tool_call
                for tool_call in batch
                if (
                    tool_call.name != PLAN_TOOL_NAME
                    and barrier_active
                )
            ]
            if blocked:
                if not barrier_counted and not publishes_plan:
                    policy_state.correction_count += 1
                    barrier_counted = True
                blocked_error = plan_required_result(decision.reason)
                fatal = (
                    RuntimeError(
                        "PLAN_REQUIRED_NOT_CREATED: model repeated complex business "
                        "tools without publishing a plan"
                    )
                    if policy_state.correction_count >= 2
                    else None
                )
                for tool_call in batch:
                    if tool_call.name == PLAN_TOOL_NAME:
                        result = await self._run_tool(
                            spec,
                            tool_call,
                            external_lookup_counts,
                            workspace_violation_counts,
                            local_lookup_state,
                            source_priority_error=(
                                source_priority_errors or {}
                            ).get(tool_call.id),
                        )
                        tool_results.append(result)
                        if (
                            result[2] is None
                            and not str(result[0]).lstrip().lower().startswith("error")
                        ):
                            policy_state.plan_created = True
                        continue
                    tool_results.append((
                        blocked_error,
                        {
                            "name": tool_call.name,
                            "status": "error",
                            "detail": "PLAN_REQUIRED",
                        },
                        fatal,
                    ))
                continue
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    self._run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                        local_lookup_state,
                        source_priority_error=(
                            source_priority_errors or {}
                        ).get(tool_call.id),
                    )
                    for tool_call in batch
                ))
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                        local_lookup_state,
                        source_priority_error=(
                            source_priority_errors or {}
                        ).get(tool_call.id),
                    )
                    tool_results.append(result)
                    batch_results.append(result)
            for tool_call, (result, _event, tool_error) in zip(batch, batch_results):
                if tool_call.name == PLAN_TOOL_NAME:
                    if (
                        tool_error is None
                        and not str(result).lstrip().lower().startswith("error")
                    ):
                        policy_state.plan_created = True
                    continue
                policy_state.business_tool_calls += 1

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        interactive_prompt_requested = False
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if isinstance(result, InteractivePromptRequested):
                interactive_prompt_requested = True
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error, interactive_prompt_requested

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        local_lookup_state: dict[str, Any],
        *,
        source_priority_error: str | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        span_id = (
            await self.trace_collector.begin_span(
                kind="tool",
                name="tool.call",
                attributes={
                    "tool_name": tool_call.name,
                    "call_id": tool_call.id,
                    **self.trace_collector.summarize_tool_arguments(
                        tool_call.arguments
                    ),
                },
            )
            if self.trace_collector is not None
            else None
        )
        inherited = current_trace_context()
        span_token = None
        if inherited is not None and span_id is not None:
            span_token = set_trace_context(
                TraceContext(
                    trace_id=inherited.trace_id,
                    run_id=inherited.run_id,
                    span_id=span_id,
                )
            )
        try:
            result, event, error = await self._run_tool_impl(
                spec,
                tool_call,
                external_lookup_counts,
                workspace_violation_counts,
                local_lookup_state,
                source_priority_error=source_priority_error,
            )
        except asyncio.CancelledError:
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    span_id,
                    status="cancelled",
                    error_code="CANCELLED",
                )
            raise
        except BaseException as exc:
            if self.trace_collector is not None:
                await self.trace_collector.end_span(
                    span_id,
                    status="failed",
                    error_code=type(exc).__name__,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            raise
        finally:
            if span_token is not None:
                reset_trace_context(span_token)

        if self.trace_collector is not None:
            result_text = "" if result is None else str(result)
            await self.trace_collector.end_span(
                span_id,
                status="failed" if event.get("status") == "error" else "completed",
                error_code=(type(error).__name__ if error is not None else None),
                attributes={
                    "result_chars": len(result_text),
                    "result_truncated": len(result_text) > spec.max_tool_result_chars,
                    "event_status": event.get("status"),
                    "event_detail": str(event.get("detail") or "")[:300],
                },
                error=(
                    {"type": type(error).__name__, "message": str(error)}
                    if error is not None
                    else None
                ),
            )
        return result, event, error

    async def _run_tool_impl(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        local_lookup_state: dict[str, Any],
        *,
        source_priority_error: str | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        finance_source = structured_finance_source(tool_call.name, tool_call.arguments)
        if source_priority_error is None and spec.enforce_finance_source_priority:
            source_priority_error = structured_finance_source_priority_error(
                tool_call.name,
                tool_call.arguments,
                external_lookup_counts,
                available_structured_finance_sources(spec.tools.tool_names),
            )
        if source_priority_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "asset-research source priority blocked",
            }
            if spec.fail_on_tool_error:
                return source_priority_error + hint, event, RuntimeError(source_priority_error)
            return source_priority_error + hint, event, None
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        local_lookup_error = repeated_local_lookup_error(
            tool_call.name,
            tool_call.arguments,
            local_lookup_state,
        )
        if local_lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated local tool call blocked",
            }
            if spec.fail_on_tool_error:
                return local_lookup_error + hint, event, RuntimeError(local_lookup_error)
            return local_lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        emit_file_edit_events = (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        )
        progress_callback = spec.progress_callback if emit_file_edit_events else None
        file_edit_trackers = (
            prepare_file_edit_trackers(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                tool=tool,
                workspace=spec.workspace,
                params=params if isinstance(params, dict) else None,
            )
            if progress_callback is not None
            else None
        )
        if prep_error:
            # A streaming provider may already have emitted a speculative
            # file-edit start while the tool arguments were arriving. Close
            # that activity even when schema validation prevents execution;
            # otherwise its staging artifact survives until timeout.
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, prep_error)
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )

        security_assessment = None
        security_audit = None
        security = spec.security_service
        if security is not None and isinstance(params, dict):
            from nanobot.security.protection import AuditUnavailable

            security_assessment = security.assess(
                tool_name=tool_call.name,
                params=params,
                tool=tool,
                workspace=spec.workspace,
            )
            try:
                security_audit = security.begin_audit(
                    security_assessment,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    session_key=spec.session_key,
                    turn_id=spec.security_turn_id,
                    agent_label=spec.agent_label,
                )
            except AuditUnavailable as exc:
                if security_assessment.mutating:
                    payload = "Error: Security audit is unavailable; modifying operations are blocked."
                    event = {
                        "name": tool_call.name,
                        "status": "error",
                        "detail": "security audit unavailable",
                    }
                    return payload, event, RuntimeError(payload) if spec.fail_on_tool_error else None
                logger.error("Security audit unavailable for read-only tool: {}", exc)

            try:
                allowed, security_result = await security.authorize(
                    security_assessment,
                    callback=spec.security_approval_callback,
                    chat_id=spec.security_chat_id,
                    turn_grants=spec.security_turn_grants,
                    interactive=spec.security_interactive,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    audit=security_audit,
                )
            except asyncio.CancelledError:
                security.complete_audit(security_audit, result="cancelled")
                raise
            except Exception as exc:
                security.complete_audit(security_audit, result="failed", details={"error_type": type(exc).__name__})
                raise
            if not allowed:
                security.complete_audit(
                    security_audit,
                    result=security_result,
                    decision=security_assessment.decision,
                    details={"authorization": security_result, "execution_started": False},
                )
                payload = f"Error: Operation blocked by security protection: {security_assessment.summary}"
                event = {
                    "name": tool_call.name,
                    "status": "error",
                    "detail": security_result,
                }
                return payload, event, RuntimeError(payload) if spec.fail_on_tool_error else None
            if security_result.startswith("approved"):
                recorded = security.complete_audit(
                    security_audit,
                    result="executing",
                    decision=security_result,
                    details={
                        "authorization": security_result,
                        "approval_scope": "turn",
                        "authorization_actor": "user" if security_result == "approved" else "turn_grant",
                        "authorized_at": time.time_ns() // 1_000_000,
                    },
                )
                if not recorded and security_assessment.mutating:
                    payload = "Error: Security audit is unavailable; modifying operations are blocked."
                    event = {"name": tool_call.name, "status": "error", "detail": "security audit unavailable"}
                    return payload, event, RuntimeError(payload) if spec.fail_on_tool_error else None
        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_start_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )
        try:
            if security is not None:
                with security.network_context(security_audit):
                    if tool is not None:
                        result = await tool.execute(**params)
                    else:
                        result = await spec.tools.execute(tool_call.name, params)
            elif tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            if security is not None:
                security.complete_audit(security_audit, result="cancelled")
            raise
        except InteractivePromptRequested as exc:
            if security is not None:
                security.complete_audit(security_audit, result="waiting_for_input")
            event = {
                "name": tool_call.name,
                "status": "ok",
                "detail": "waiting for user input",
            }
            return exc, event, None
        except BaseException as exc:
            if security is not None:
                security.complete_audit(
                    security_audit,
                    result="failed",
                    details={"error_type": type(exc).__name__},
                )
            if finance_source is not None:
                mark_structured_finance_source_attempted(
                    external_lookup_counts,
                    finance_source,
                )
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(exc))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if finance_source is not None:
                mark_structured_finance_source_failed(
                    external_lookup_counts,
                    finance_source,
                )
                payload = (
                    f"{payload}\n\n"
                    f"{structured_finance_fallback_instruction(finance_source, external_lookup_counts)}"
                )
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if finance_source is not None:
            mark_structured_finance_source_attempted(
                external_lookup_counts,
                finance_source,
            )

        if isinstance(result, str) and result.startswith("Error"):
            if security is not None:
                from nanobot.security.audit import tool_audit_outcome

                audit_outcome, audit_details = tool_audit_outcome(tool_call.name, result)
                security.complete_audit(
                    security_audit,
                    result=audit_outcome,
                    decision="block" if audit_outcome == "blocked" else None,
                    details=audit_details,
                )
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, result)
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if finance_source is not None:
                mark_structured_finance_source_failed(
                    external_lookup_counts,
                    finance_source,
                )
                result = (
                    f"{result}\n\n"
                    f"{structured_finance_fallback_instruction(finance_source, external_lookup_counts)}"
                )
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        if (
            finance_source is not None
            and structured_finance_result_failed(finance_source, result)
        ):
            if security is not None:
                security.complete_audit(security_audit, result="failed")
            mark_structured_finance_source_failed(
                external_lookup_counts,
                finance_source,
            )
            instruction = structured_finance_fallback_instruction(
                finance_source,
                external_lookup_counts,
            )
            payload = f"{result}\n\nError: {instruction}"
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": f"{finance_source} structured data source failed; switching source",
            }
            if spec.fail_on_tool_error:
                return payload + hint, event, RuntimeError(instruction)
            return payload + hint, event, None

        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_end_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        if security is not None:
            from nanobot.security.audit import tool_audit_outcome

            outcome, audit_details = tool_audit_outcome(tool_call.name, result)
            security.complete_audit(
                security_audit,
                result=outcome,
                details=audit_details,
            )
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        if tool_name in _TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS:
            # Exempt tools bound their own output; skip generic offload and truncation.
            return normalize_tool_message_content(tool_name, result)
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return normalize_tool_message_content(tool_name, content)

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace old compactable tool results with one-line summaries."""
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        fixed_tokens, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            system_messages,
            spec.tools.get_definitions(),
        )
        remaining_budget = max(0, budget - max(system_tokens, fixed_tokens))
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches

"""Agent hook that adapts runner events into channel progress UI."""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.utils.helpers import IncrementalThinkExtractor, strip_think
from nanobot.utils.progress_events import (
    build_automatic_task_progress_event,
    build_tool_event_display,
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from nanobot.utils.tool_hints import format_tool_hints


class AgentProgressHook(AgentHook):
    """Translate runner lifecycle events into user-visible progress signals."""

    def __init__(
        self,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        set_tool_context: Callable[..., None] | None = None,
        on_iteration: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._metadata = metadata or {}
        self._session_key = session_key
        self._tool_hint_max_length = tool_hint_max_length
        self._set_tool_context = set_tool_context
        self._on_iteration = on_iteration
        self._stream_buf = ""
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False
        self._observed_action_calls = 0
        self._plan_reported = False
        self._auto_plan_call_id: str | None = None
        self._auto_plan_iteration: int | None = None
        self._auto_plan_batch_id: str | None = None
        self._auto_plan_sequence: int | None = None
        self._auto_plan_steps: list[dict[str, str]] = []
        self._auto_plan_step_calls: dict[str, set[str]] = {}

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    def _activity_batch_id(self, iteration: int) -> str:
        turn_key = self._message_id or self._chat_id or "turn"
        return f"{turn_key}:{iteration}"

    @staticmethod
    def _activity_sequence_base(iteration: int) -> int:
        return max(0, iteration) * 1000 + 1

    @staticmethod
    def _action_tool_calls(tool_calls: list[Any]) -> list[Any]:
        return [call for call in tool_calls if call.name != "update_task_progress"]

    def _should_emit_automatic_plan(self, tool_calls: list[Any]) -> bool:
        if self._plan_reported or self._auto_plan_call_id or not tool_calls:
            return False
        total_actions = self._observed_action_calls + len(tool_calls)
        long_running = any(
            call.name in {"long_task", "delegate", "spawn_subagent"}
            for call in tool_calls
        )
        return total_actions >= 2 or long_running

    @staticmethod
    def _automatic_step_title(category: str, subject: str | None, count: int) -> str:
        titles = {
            "skill": "加载专项能力",
            "command": "处理任务数据",
            "search": "查询公开资料",
            "browser": "读取网页资料",
            "mcp": "获取外部数据",
            "write": "更新文件内容",
            "read": "读取所需资料",
            "media": "生成内容",
            "tool": "执行任务步骤",
        }
        title = titles.get(category, "执行任务步骤")
        if count > 1:
            return f"{title}（{count} 项）"
        if subject:
            return f"{title}：{subject[:28]}"
        return title

    def _prepare_automatic_plan(
        self,
        tool_calls: list[Any],
        context: AgentHookContext,
    ) -> dict[str, Any]:
        grouped: dict[str, list[tuple[Any, dict[str, str]]]] = {}
        for call in tool_calls:
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            display = build_tool_event_display(call.name, arguments)
            category = display.get("category", "tool")
            grouped.setdefault(category, []).append((call, display))

        steps: list[dict[str, str]] = []
        step_calls: dict[str, set[str]] = {}
        for index, (category, calls) in enumerate(grouped.items(), start=1):
            if index > 8:
                break
            step_id = f"auto-{category}-{index}"
            subject = calls[0][1].get("subject") if len(calls) == 1 else None
            steps.append({
                "id": step_id,
                "title": self._automatic_step_title(category, subject, len(calls)),
                "status": "running",
            })
            step_calls[step_id] = {
                str(getattr(call, "id", "") or "")
                for call, _display in calls
            }

        batch_id = self._activity_batch_id(context.iteration)
        sequence = self._activity_sequence_base(context.iteration) - 1
        call_id = f"auto-task-progress:{batch_id}"
        note = (
            "任务包含多个步骤，开始并行处理"
            if len(tool_calls) > 1
            else "任务进入多步骤处理，继续执行下一阶段"
        )
        self._auto_plan_call_id = call_id
        self._auto_plan_iteration = context.iteration
        self._auto_plan_batch_id = batch_id
        self._auto_plan_sequence = sequence
        self._auto_plan_steps = steps
        self._auto_plan_step_calls = step_calls
        return build_automatic_task_progress_event(
            call_id=call_id,
            steps=steps,
            note=note,
            current_step_id=steps[0]["id"] if steps else None,
            sequence=sequence,
            batch_id=batch_id,
        )

    def _complete_automatic_plan(self, context: AgentHookContext) -> dict[str, Any] | None:
        if (
            self._auto_plan_iteration != context.iteration
            or not self._auto_plan_call_id
            or not self._auto_plan_batch_id
            or self._auto_plan_sequence is None
        ):
            return None
        status_by_call: dict[str, str] = {}
        for call, event in zip(context.tool_calls, context.tool_events, strict=False):
            call_id = str(getattr(call, "id", "") or "")
            status_by_call[call_id] = "completed" if event.get("status") == "ok" else "error"

        completed_steps: list[dict[str, str]] = []
        for step in self._auto_plan_steps:
            call_statuses = [
                status_by_call.get(call_id, "completed")
                for call_id in self._auto_plan_step_calls.get(step["id"], set())
            ]
            status = "error" if "error" in call_statuses else "completed"
            completed_steps.append({**step, "status": status})
        has_error = any(step["status"] == "error" for step in completed_steps)
        event = build_automatic_task_progress_event(
            call_id=self._auto_plan_call_id,
            steps=completed_steps,
            note=(
                "部分步骤未完成，正在整理可用结果"
                if has_error
                else "工具步骤已完成，正在整理结果"
            ),
            current_step_id=None,
            sequence=self._auto_plan_sequence,
            batch_id=self._auto_plan_batch_id,
        )
        self._auto_plan_iteration = None
        return event

    @staticmethod
    def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
        try:
            sig = inspect.signature(cb)
        except (TypeError, ValueError):
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return True
        return name in sig.parameters

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # Answer text has started; close the reasoning segment so the UI can
            # lock the bubble before the answer renders below it.
            await self.emit_reasoning_end()
            if self._on_stream:
                await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration {} for session {}",
            context.iteration,
            self._session_key,
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if any(call.name == "update_task_progress" for call in context.tool_calls):
            self._plan_reported = True
        action_calls = self._action_tool_calls(context.tool_calls)
        if self._on_progress:
            if not self._on_stream and not context.streamed_content:
                thought = self._strip_think(context.response.content if context.response else None)
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            sequence_base = self._activity_sequence_base(context.iteration)
            batch_id = self._activity_batch_id(context.iteration)
            tool_events: list[dict[str, Any]] = []
            if (
                on_progress_accepts_tool_events(self._on_progress)
                and self._should_emit_automatic_plan(action_calls)
            ):
                tool_events.append(self._prepare_automatic_plan(action_calls, context))
            tool_events.extend(
                build_tool_event_start_payload(
                    tc,
                    sequence=sequence_base + index,
                    batch_id=batch_id,
                )
                for index, tc in enumerate(context.tool_calls)
            )
            await invoke_on_progress(
                self._on_progress,
                tool_hint,
                tool_hint=True,
                tool_events=tool_events,
            )
        self._observed_action_calls += len(action_calls)
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        if self._set_tool_context:
            self._set_tool_context(
                self._channel,
                self._chat_id,
                self._message_id,
                self._metadata,
                session_key=self._session_key,
            )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """Publish a reasoning chunk; channel plugins decide whether to render."""
        if (
            self._on_progress
            and reasoning_content
            and self._on_progress_accepts(self._on_progress, "reasoning")
        ):
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """Close the current reasoning stream segment, if any was open."""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        tool_events: list[dict[str, Any]] = []
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events.extend(build_tool_event_finish_payloads(
                context,
                sequence_base=self._activity_sequence_base(context.iteration),
                batch_id=self._activity_batch_id(context.iteration),
            ))
        automatic_plan = self._complete_automatic_plan(context)
        if automatic_plan is not None:
            tool_events.append(automatic_plan)
        if self._on_progress and tool_events:
            await invoke_on_progress(
                self._on_progress,
                "",
                tool_hint=False,
                tool_events=tool_events,
            )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._strip_think(content)

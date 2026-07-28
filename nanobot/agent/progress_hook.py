"""Agent hook that adapts runner events into channel progress UI."""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.utils.helpers import IncrementalThinkExtractor, strip_think
from nanobot.utils.progress_events import (
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
        self._pending_stream_end = False

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

    async def _emit_stream_end(self, *, resuming: bool, stream_kind: str) -> None:
        if not self._on_stream_end:
            return
        if self._on_progress_accepts(self._on_stream_end, "stream_kind"):
            await self._on_stream_end(
                resuming=resuming,
                stream_kind=stream_kind,
            )
        else:
            await self._on_stream_end(resuming=resuming)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        if resuming and context.response is None:
            # Provider-level recovery happens before the final response (and
            # therefore before tool classification) and must keep its existing
            # segment boundary immediately.
            self._pending_stream_end = False
            await self._emit_stream_end(resuming=True, stream_kind="answer")
        elif resuming:
            # Tool-vs-answer classification is only known after the provider
            # returns. Defer the wire commit until before_execute_tools or
            # after_iteration can classify this provisional segment.
            self._pending_stream_end = True
        else:
            self._pending_stream_end = False
            await self._emit_stream_end(resuming=False, stream_kind="answer")
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
        if self._pending_stream_end:
            self._pending_stream_end = False
            await self._emit_stream_end(resuming=True, stream_kind="narration")
        if self._on_progress:
            narration = self._strip_think(
                context.response.content if context.response else None
            )
            supports_narration = (
                self._channel in {"websocket", "webui"}
                and self._on_progress_accepts(self._on_progress, "narration")
                and self._on_progress_accepts(self._on_progress, "narration_end")
            )
            if narration and supports_narration:
                await self._on_progress(narration, narration=True)
                await self._on_progress("", narration_end=True)
            elif narration and not self._on_stream and not context.streamed_content:
                # Compatibility for text-only channels that render public
                # pre-tool narration through their generic progress surface.
                await self._on_progress(narration)
            visible_tool_calls = [
                tool_call
                for tool_call in context.tool_calls
                if tool_call.id not in context.hidden_tool_call_ids
            ]
            tool_hint = self._strip_think(self._tool_hint(visible_tool_calls))
            sequence_base = self._activity_sequence_base(context.iteration)
            batch_id = self._activity_batch_id(context.iteration)
            tool_events = [
                build_tool_event_start_payload(
                    tc,
                    sequence=sequence_base + index,
                    batch_id=batch_id,
                )
                for index, tc in enumerate(visible_tool_calls)
            ]
            if tool_events or tool_hint:
                await invoke_on_progress(
                    self._on_progress,
                    tool_hint,
                    tool_hint=True,
                    tool_events=tool_events,
                )
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
        if self._pending_stream_end:
            self._pending_stream_end = False
            await self._emit_stream_end(resuming=True, stream_kind="answer")
        tool_events: list[dict[str, Any]] = []
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events.extend(
                event
                for event in build_tool_event_finish_payloads(
                    context,
                    sequence_base=self._activity_sequence_base(context.iteration),
                    batch_id=self._activity_batch_id(context.iteration),
                )
                if event.get("call_id") not in context.hidden_tool_call_ids
            )
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

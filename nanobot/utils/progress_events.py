"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.agent.hook import AgentHookContext


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def _tool_event_arguments(tool_call: Any) -> dict[str, Any]:
    arguments = getattr(tool_call, "arguments", {}) or {}
    return arguments if isinstance(arguments, dict) else {}


def build_tool_event_start_payload(
    tool_call: Any,
    *,
    sequence: int | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    name = str(getattr(tool_call, "name", "") or "")
    arguments = _tool_event_arguments(tool_call)
    payload = {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": name,
        "arguments": arguments,
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
        "occurred_at": _unix_millis(),
        "display": build_tool_event_display(name, arguments),
    }
    if sequence is not None:
        payload["sequence"] = sequence
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(
    context: AgentHookContext,
    *,
    sequence_base: int | None = None,
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        name = str(getattr(tool_call, "name", "") or "")
        arguments = _tool_event_arguments(tool_call)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": name,
            "arguments": arguments,
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
            "occurred_at": _unix_millis(),
            "display": build_tool_event_display(name, arguments),
        }
        if sequence_base is not None:
            payload["sequence"] = sequence_base + idx
        if batch_id:
            payload["batch_id"] = batch_id
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads


def build_tool_event_display(name: str, arguments: dict[str, Any]) -> dict[str, str]:
    """Return stable presentation hints without coupling clients to tool names."""
    compact = name.lower()
    if compact == "spawn":
        category, importance = "expert", "primary"
    elif compact == "update_task_progress" or "plan" in compact:
        category, importance = "plan", "primary"
    elif "skill" in compact:
        category, importance = "skill", "primary"
    elif compact in {
        "exec",
        "write_stdin",
        "list_exec_sessions",
        "run_shell_command",
        "run_cli_app",
    } or "command" in compact:
        category, importance = "command", "primary"
    elif "search" in compact:
        category, importance = "search", "primary"
    elif "browser" in compact or "fetch" in compact:
        category, importance = "browser", "primary"
    elif compact.startswith("mcp_") or compact == "mcp":
        category, importance = "mcp", "primary"
    elif any(token in compact for token in ("write", "edit", "patch")):
        category, importance = "write", "primary"
    elif any(token in compact for token in ("read", "list_dir", "grep", "find")):
        category, importance = "read", "secondary"
    elif any(token in compact for token in ("image", "video", "media")):
        category, importance = "media", "primary"
    else:
        category, importance = "tool", "secondary"

    display = {"category": category, "importance": importance}
    if compact == "spawn":
        role = str(arguments.get("label") or "").strip()
        role_display = {
            "business-analyst": (
                "商业模式分析",
                "研究主营业务、生意属性、护城河与关键公告",
            ),
            "financial-analyst": (
                "财务质量与估值",
                "核验财务报表、现金流、盈利质量与估值基础",
            ),
            "industry-researcher": (
                "行业格局研究",
                "分析行业板块、可比公司、竞争格局与长期变化",
            ),
            "risk-assessor": (
                "风险与治理评估",
                "核查治理公告、诉讼处罚、减持质押与下行风险",
            ),
        }.get(role)
        if role_display is not None:
            display["title"], display["subject"] = role_display
            return display
    subject = _display_subject(arguments)
    if subject:
        display["subject"] = subject
    return display


def _display_subject(arguments: dict[str, Any]) -> str | None:
    for key in (
        "query", "q", "keyword", "topic", "ticker", "symbol", "code",
        "path", "file_path", "url", "name",
    ):
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        compact = " ".join(value.split()).strip()
        if compact:
            return compact[:96]
    return None


def _unix_millis() -> int:
    return int(time.time() * 1000)

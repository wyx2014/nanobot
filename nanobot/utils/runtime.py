"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from nanobot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2
_MAX_IFIND_LOOKUPS_PER_TURN = 8
_SOURCE_DISABLED_PREFIX = "__structured_finance_source_disabled__:"
_SOURCE_TOTAL_PREFIX = "__structured_finance_source_total__:"
_SOURCE_ATTEMPTED_PREFIX = "__structured_finance_source_attempted__:"
_CORE_STRUCTURED_FINANCE_SOURCES = ("ifind", "juyuan", "caihui")

# Third consecutive identical local lookup is almost always an agent loop.
_MAX_REPEAT_LOCAL_LOOKUPS = 2
_LOCAL_LOOKUP_TOOLS = frozenset({"read_file", "list_dir", "find_files", "grep"})

# Third same-target workspace violation in a turn escalates to "stop retrying".
_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

BUDGET_EXHAUSTED_FINALIZATION_PROMPT = (
    "The tool-call budget for this turn is exhausted. Based only on the "
    "conversation and tool results above, provide a concise final response to "
    "the user. Do not call or request tools. Do not claim the task is complete "
    "unless the evidence above clearly shows it is complete. State what was "
    "done, what remains, and the best next step if anything is incomplete."
)

_INTERNAL_TOOL_CALL_OPEN = "<tool_call>"

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)

SUSTAINED_GOAL_CONTINUE_PROMPT = (
    "You have an active sustained goal. Please continue working toward the "
    "objective using your tools, or call complete_goal if the work is truly finished."
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def normalize_tool_message_content(tool_name: str, content: Any) -> str | list[dict[str, Any]]:
    """Return provider-safe content for a persisted ``role=tool`` message.

    Tools may return structured Python values so runtime hooks can expose rich
    artifacts to channels.  Chat APIs, however, only accept strings or typed
    content blocks in tool messages.  Persisting a bare dict (or an untyped
    list) poisons the conversation because every later request replays it.
    """
    content = ensure_nonempty_tool_result(tool_name, content)
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and content
        and all(
            isinstance(block, dict)
            and isinstance(block.get("type"), str)
            and bool(block["type"].strip())
            for block in content
        )
    ):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(content)
        return rendered if rendered.strip() else empty_tool_result_message(tool_name)


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def is_internal_tool_call_markup(content: str | None) -> bool:
    """Detect a serialized internal tool call misreported as assistant prose.

    Some OpenAI-compatible models occasionally emit their tool template as XML
    text even when the request has tools disabled. Treat leading markup and
    markup appended after public narration as leaks when the internal marker is
    followed by a function tag. A plain-text mention of ``<tool_call>`` remains
    untouched.
    """
    if not content:
        return False
    normalized = content.lower()
    marker_index = normalized.find(_INTERNAL_TOOL_CALL_OPEN)
    if marker_index < 0:
        return False
    return normalized.find("<function=", marker_index + len(_INTERNAL_TOOL_CALL_OPEN)) >= 0


def is_internal_tool_call_stream_prefix(content: str | None) -> bool:
    """Return True once a stream contains or may be entering tool-call markup."""
    if content is None:
        return False
    normalized = content.lower()
    if not normalized:
        return True
    if _INTERNAL_TOOL_CALL_OPEN in normalized:
        return True
    max_prefix = min(len(normalized), len(_INTERNAL_TOOL_CALL_OPEN) - 1)
    return any(
        normalized.endswith(_INTERNAL_TOOL_CALL_OPEN[:prefix_length])
        for prefix_length in range(1, max_prefix + 1)
    )


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_budget_exhausted_finalization_message() -> dict[str, str]:
    """Prompt the model for a no-tools final response after budget exhaustion."""
    return {"role": "user", "content": BUDGET_EXHAUSTED_FINALIZATION_PROMPT}


def build_length_recovery_message() -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def build_goal_continue_message(custom: str | None = None) -> dict[str, str]:
    """Prompt the model to continue when a sustained goal is still active."""
    return {"role": "user", "content": custom or SUSTAINED_GOAL_CONTINUE_PROMPT}


def external_lookup_signature(tool_name: str, arguments: Any) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if not isinstance(arguments, dict):
        return None
    normalized_tool_name = tool_name.lower()
    if normalized_tool_name in {
        "navigate",
        "browser_navigate",
        "mcp_playwright_browser_navigate",
    }:
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"browser_navigate:{url.lower()}"
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    source = structured_finance_source(tool_name, arguments)
    if source is not None:
        try:
            rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = str(arguments)
        return f"structured_finance:{source}:{tool_name}:{rendered.lower()}"
    return None


def structured_finance_source(tool_name: str, arguments: Any) -> str | None:
    """Identify team-bound structured-finance calls hidden behind generic tools."""
    compact_name = tool_name.lower()
    if compact_name.startswith("mcp_juyuan_"):
        return "juyuan"
    if compact_name.startswith(("mcp_caihui_mcp_", "mcp_caihui_")):
        return "caihui"
    if compact_name.startswith(("mcp_hexin-ifind-ds-", "mcp_ifind_")):
        return "ifind"
    if compact_name.startswith("mcp_anysearch_"):
        return "anysearch"
    if compact_name != "exec" or not isinstance(arguments, dict):
        return None
    command = str(arguments.get("command") or arguments.get("cmd") or "").lower()
    if "ifind-finance-data" in command or (
        "call-node.js" in command and ("51ifind" in command or "ifind" in command)
    ):
        return "ifind"
    return None


def available_structured_finance_sources(tool_names: Iterable[str]) -> tuple[str, ...]:
    """Return the finance-source layers actually exposed to an agent run."""
    available: set[str] = set()
    for tool_name in tool_names:
        source = structured_finance_source(str(tool_name), {})
        if source is not None:
            available.add(source)
    return tuple(
        source
        for source in (*_CORE_STRUCTURED_FINANCE_SOURCES, "anysearch")
        if source in available
    )


def _source_attempted_key(source: str) -> str:
    return f"{_SOURCE_ATTEMPTED_PREFIX}{source}"


def mark_structured_finance_source_attempted(
    seen_counts: dict[str, int],
    source: str,
) -> None:
    """Record that a source call reached a terminal result for this agent run."""
    if source in {*_CORE_STRUCTURED_FINANCE_SOURCES, "anysearch"}:
        seen_counts[_source_attempted_key(source)] = 1


def structured_finance_source_priority_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
    available_sources: Iterable[str],
) -> str | None:
    """Block public fallbacks until the configured higher-priority layers ran.

    This is deliberately a tool-boundary policy rather than a prompt hint.  A
    successful prior call counts as an attempt because only the model can tell
    whether a valid result still omitted the field needed for its current
    analysis; the runtime's job is to prevent skipping an entire source layer.
    """
    source = structured_finance_source(tool_name, arguments)
    normalized_name = tool_name.strip().lower()
    is_public_web_search = normalized_name in {"web_search", "search_web"}
    if source != "anysearch" and not is_public_web_search:
        return None

    available = set(available_sources)
    missing_core = [
        item
        for item in _CORE_STRUCTURED_FINANCE_SOURCES
        if item in available and not seen_counts.get(_source_attempted_key(item), 0)
    ]
    if missing_core:
        labels = {
            "ifind": "iFinD",
            "juyuan": "Juyuan",
            "caihui": "Caihui",
        }
        missing_text = ", ".join(labels[item] for item in missing_core)
        return (
            "Error: asset-research source priority blocked this public fallback. "
            "First query every configured core source that has not yet been tried: "
            f"{missing_text}. "
            "Use the available `mcp_hexin-ifind-ds-...`, `mcp_juyuan_...`, and "
            "`mcp_caihui_mcp_...` tools as applicable. Only retry the fallback after those "
            "source calls have returned, and only for evidence they did not cover."
        )

    if (
        is_public_web_search
        and "anysearch" in available
        and not seen_counts.get(_source_attempted_key("anysearch"), 0)
    ):
        return (
            "Error: asset-research source priority blocked DuckDuckGo/web_search. "
            "Query an available `mcp_anysearch_...` tool first. Use DuckDuckGo only after "
            "AnySearch has returned and still does not cover the required evidence."
        )
    return None


def _source_disabled_key(source: str) -> str:
    return f"{_SOURCE_DISABLED_PREFIX}{source}"


def _source_total_key(source: str) -> str:
    return f"{_SOURCE_TOTAL_PREFIX}{source}"


def structured_finance_fallback_instruction(
    source: str,
    seen_counts: dict[str, int] | None = None,
) -> str:
    """Return a deterministic source-switch instruction for recoverable failures."""
    state = seen_counts or {}
    failed = {
        item
        for item in (*_CORE_STRUCTURED_FINANCE_SOURCES, "anysearch")
        if state.get(_source_disabled_key(item), 0)
    }
    remaining = [
        item for item in _CORE_STRUCTURED_FINANCE_SOURCES if item not in failed
    ]
    labels = {
        "ifind": "iFinD",
        "juyuan": "Juyuan",
        "caihui": "Caihui",
        "anysearch": "AnySearch",
    }
    source_label = labels.get(source, source)
    if remaining:
        prefixes = {
            "ifind": "iFinD Skill or an available `mcp_hexin-ifind-ds-...` tool",
            "juyuan": "an available `mcp_juyuan_...` tool",
            "caihui": "an available `mcp_caihui_mcp_...` tool",
        }
        remaining_text = ", ".join(prefixes[item] for item in remaining)
        lead = (
            "Stop calling iFinD immediately. "
            if source == "ifind"
            else f"Stop calling {source_label} for this field. "
        )
        return (
            f"{source_label} is unavailable or repeating for this run. {lead}"
            f"Continue field-level cross-validation with the remaining configured core sources: "
            f"{remaining_text}. Do not scan Home, discover credentials, or retry the failed source "
            "with a cosmetically different query."
        )
    if "anysearch" not in failed and source != "anysearch":
        return (
            "iFinD, Juyuan, and Caihui are unavailable for this field. Stop retrying the core "
            "sources and use an available `mcp_anysearch_...` tool once. Preserve the missing "
            "field and source failures in the evidence matrix."
        )
    return (
        "The configured structured sources and AnySearch did not provide this field. Use "
        "`web_search` once with `provider=duckduckgo`, prefer exchange/company/regulatory "
        "disclosures, then explicitly label any remaining data gap and finish the role/report."
    )


def mark_structured_finance_source_failed(
    seen_counts: dict[str, int],
    source: str,
) -> None:
    """Disable a hard-failed source for the rest of the current agent run."""
    if source in {*_CORE_STRUCTURED_FINANCE_SOURCES, "anysearch"}:
        seen_counts[_source_disabled_key(source)] = 1


def structured_finance_result_failed(source: str, result: Any) -> bool:
    """Detect hard failures hidden inside a nominally successful tool payload."""
    if source not in {*_CORE_STRUCTURED_FINANCE_SOURCES, "anysearch"}:
        return False
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    compact = text.lower().replace(" ", "")
    hard_markers = (
        '"ok":false',
        '"status_code":401',
        '"status_code":403',
        '"status_code":429',
        '"status":401',
        '"status":403',
        '"status":429',
        "callfailed",
        "toolnotallowed",
        "notconfigured",
        "missingauth",
        "unauthorized",
        "forbidden",
        "ratelimit",
        "toomanyrequests",
        "exitcode:1",
    )
    return any(marker in compact for marker in hard_markers)


def repeated_external_lookup_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    source = structured_finance_source(tool_name, arguments)
    if source is not None and seen_counts.get(_source_disabled_key(source), 0):
        return f"Error: {structured_finance_fallback_instruction(source, seen_counts)}"
    if source == "ifind":
        total_key = _source_total_key(source)
        total = seen_counts.get(total_key, 0) + 1
        seen_counts[total_key] = total
        if total > _MAX_IFIND_LOOKUPS_PER_TURN:
            mark_structured_finance_source_failed(seen_counts, source)
            logger.warning("Disabling iFinD after {} calls in one agent run", total)
            return f"Error: {structured_finance_fallback_instruction(source, seen_counts)}"
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    if source is not None:
        mark_structured_finance_source_failed(seen_counts, source)
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    if source is not None:
        return (
            f"Error: repeated {source} lookup blocked. "
            f"{structured_finance_fallback_instruction(source, seen_counts)}"
        )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


def local_lookup_signature(tool_name: str, arguments: Any) -> str | None:
    """Stable signature for consecutive read-only local calls worth throttling."""
    if tool_name not in _LOCAL_LOOKUP_TOOLS or not isinstance(arguments, dict):
        return None
    if tool_name == "read_file" and arguments.get("force") is True:
        return None
    try:
        rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return f"{tool_name}:{rendered}"


def repeated_local_lookup_error(
    tool_name: str,
    arguments: Any,
    state: dict[str, Any],
) -> str | None:
    """Block the third consecutive identical local lookup within one agent turn."""
    signature = local_lookup_signature(tool_name, arguments)
    if signature is None:
        state.clear()
        return None

    if state.get("signature") != signature:
        state.clear()
        state.update({"signature": signature, "count": 1})
        return None

    count = int(state.get("count") or 0) + 1
    state["count"] = count
    if count <= _MAX_REPEAT_LOCAL_LOOKUPS:
        return None

    logger.warning(
        "Blocking repeated local lookup {} on consecutive attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated local tool call blocked. "
        "The identical read-only call already ran twice. Use the result already present "
        "in the conversation, choose a different operation, or finish with an explicit "
        "evidence gap. Do not issue the same call again."
    )


# Workspace-boundary violations are soft errors, with per-target throttling.

_OUTSIDE_PATH_PATTERN = re.compile(r"(?:^|[\s|>'\"])((?:/[^\s\"'>;|<]+)|(?:~[^\s\"'>;|<]+))")


def workspace_violation_signature(
    tool_name: str,
    arguments: Any,
) -> str | None:
    """Return a stable cross-tool signature for the outside-workspace target."""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "target", "source", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_violation_target(val.strip())

    if tool_name in {"exec", "shell"}:
        cmd = str(arguments.get("command") or "").strip()
        if cmd:
            match = _OUTSIDE_PATH_PATTERN.search(cmd)
            if match:
                return _normalize_violation_target(match.group(1))
        cwd = str(arguments.get("working_dir") or "").strip()
        if cwd:
            return _normalize_violation_target(cwd)

    return None


def _normalize_violation_target(raw: str) -> str:
    """Normalize *raw* path so that equivalent spellings collide on the same key."""
    try:
        normalized = Path(raw).expanduser().resolve().as_posix()
    except Exception:
        normalized = raw.replace("\\", "/")
    return f"violation:{normalized}".lower()


def repeated_workspace_violation_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Return an escalated error after repeated bypass attempts."""
    signature = workspace_violation_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_WORKSPACE_VIOLATIONS:
        return None
    logger.warning(
        "Escalating repeated workspace bypass attempt {} (attempt {})",
        signature[:160],
        count,
    )
    target = signature.split("violation:", 1)[1] if "violation:" in signature else signature
    return (
        "Error: refusing repeated workspace-bypass attempts.\n"
        f"You have tried to access '{target}' (or an equivalent path) "
        f"{count} times in this turn. This is a hard policy boundary -- "
        "switching tools, shell tricks, working_dir overrides, symlinks, "
        "or base64 piping will NOT change the answer. Stop retrying. "
        "If the user genuinely needs this resource, tell them you cannot "
        "access it and ask how they want to proceed (e.g. copy the file "
        "into the workspace, or disable restrict_to_workspace for this run)."
    )

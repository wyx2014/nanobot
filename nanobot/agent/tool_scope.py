"""Capability scoping for interactive WebUI turns.

The desktop runtime owns one process-wide registry because MCP transports are
long lived.  That does not mean every model request should receive every MCP
schema.  This module builds per-turn registry views without mutating the
process-wide registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from nanobot.agent.tools.registry import ToolRegistry

_MCP_TOOL_PREFIX = "mcp_"

_SOCIAL_ANCHORS = frozenset({
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "哈啰",
    "早上好",
    "上午好",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "你好吗",
    "你怎么样",
    "最近怎么样",
    "今天心情如何",
    "心情如何",
    "在吗",
    "你在吗",
    "谢谢",
    "多谢",
    "感谢",
    "再见",
    "拜拜",
    "好的",
    "收到",
    "hello",
    "hi",
    "hey",
    "goodmorning",
    "goodafternoon",
    "goodevening",
    "goodnight",
    "howareyou",
    "thanks",
    "thankyou",
    "bye",
    "ok",
    "okay",
})
_SOCIAL_FILLERS = frozenset({
    "啊",
    "呀",
    "哦",
    "哈",
    "啦",
    "呢",
    "吧",
    "我的朋友",
    "朋友",
    "今天",
    "最近",
    "there",
})
_SOCIAL_FRAGMENTS = tuple(
    sorted(_SOCIAL_ANCHORS | _SOCIAL_FILLERS, key=len, reverse=True)
)


def _compact_social_text(content: str) -> str:
    """Discard spacing, punctuation and emoji while retaining words/numbers."""

    return "".join(char.lower() for char in content.strip() if char.isalnum())


def is_plain_social_turn(content: str) -> bool:
    """Return true only for a short, self-contained greeting/social acknowledgement.

    This deliberately does not classify business intent.  Text such as
    ``你好，分析长江电力`` leaves a non-social remainder and therefore stays on
    the normal model-routed execution path.
    """

    compact = _compact_social_text(content)
    if not compact or len(compact) > 64:
        return False
    if not any(anchor in compact for anchor in _SOCIAL_ANCHORS):
        return False

    remainder = compact
    for fragment in _SOCIAL_FRAGMENTS:
        remainder = remainder.replace(fragment, "")
    return not remainder


def _has_explicit_capability_attachment(
    metadata: Mapping[str, Any],
    media: Sequence[str],
) -> bool:
    if any(str(item).strip() for item in media):
        return True
    for key in ("mcp_presets", "cli_apps"):
        value = metadata.get(key)
        if isinstance(value, list) and value:
            return True
    skill_scope = metadata.get("skill_scope")
    if isinstance(skill_scope, Mapping) and any(
        isinstance(value, list) and value for value in skill_scope.values()
    ):
        return True
    image_generation = metadata.get("image_generation")
    if isinstance(image_generation, Mapping) and image_generation.get("enabled") is True:
        return True
    return any(
        metadata.get(key)
        for key in (
            "expert_team_run_id",
            "expert_team_resume",
            "_active_turn_correction",
            "interactive_prompt_answer",
        )
    )


def _attached_mcp_prefixes(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("mcp_presets")
    if not isinstance(raw, list):
        return ()
    prefixes: list[str] = []
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name:
            prefixes.append(f"{_MCP_TOOL_PREFIX}{name}_")
    return tuple(dict.fromkeys(prefixes))


def build_webui_turn_tools(
    source: ToolRegistry,
    *,
    content: str,
    media: Sequence[str] | None,
    metadata: Mapping[str, Any] | None,
    has_history: bool,
) -> ToolRegistry:
    """Build a non-mutating tool view for one ordinary desktop turn.

    A brand-new plain greeting gets no tool contracts.  Other ordinary WebUI
    turns retain native tools but receive MCP schemas only when the user
    explicitly attached that MCP preset.  Runtime-owned expert workflows do
    not call this function and keep their own configured financial MCP scope.
    """

    turn_metadata = metadata if isinstance(metadata, Mapping) else {}
    turn_media = media or ()
    if (
        not has_history
        and is_plain_social_turn(content)
        and not _has_explicit_capability_attachment(turn_metadata, turn_media)
    ):
        return ToolRegistry()

    attached_prefixes = _attached_mcp_prefixes(turn_metadata)
    scoped = ToolRegistry()
    for name in source.tool_names:
        if name.startswith(_MCP_TOOL_PREFIX) and not name.startswith(attached_prefixes):
            continue
        tool = source.get(name)
        if tool is not None:
            scoped.register(tool)
    return scoped

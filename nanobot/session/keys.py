"""Shared session key constants and helpers."""

from __future__ import annotations

UNIFIED_SESSION_KEY = "unified:default"


def session_key_for_channel(channel: str, chat_id: str, *, unified_session: bool = False) -> str:
    """Return the session key for a channel/chat pair."""
    if unified_session:
        return UNIFIED_SESSION_KEY
    return f"{channel}:{chat_id}"


def webui_session_key_for_chat_id(chat_id: str) -> str:
    """Resolve a WebUI chat id, including canonical per-run cron ids."""
    return chat_id if chat_id.startswith("cron:") else f"websocket:{chat_id}"

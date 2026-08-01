"""Session-aware live browser mirror for the desktop gateway.

The browser itself remains owned by Playwright MCP.  This module observes
Playwright tool calls inside nanobot, captures a lightweight viewport image
after visible mutations, and publishes ephemeral events to the WebSocket
channel.  Frames are never written to chat history.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

BrowserEventSink = Callable[[dict[str, Any]], Awaitable[None] | None]

_PLAYWRIGHT_SERVER_NAMES = {"playwright"}
_BROWSER_MUTATIONS = {
    "browser_check",
    "browser_click",
    "browser_close",
    "browser_drag",
    "browser_drop",
    "browser_evaluate",
    "browser_file_upload",
    "browser_fill_form",
    "browser_handle_dialog",
    "browser_highlight",
    "browser_hover",
    "browser_keydown",
    "browser_keyup",
    "browser_mouse_click_xy",
    "browser_mouse_down",
    "browser_mouse_drag_xy",
    "browser_mouse_move_xy",
    "browser_mouse_up",
    "browser_mouse_wheel",
    "browser_navigate",
    "browser_navigate_back",
    "browser_navigate_forward",
    "browser_press_key",
    "browser_press_sequentially",
    "browser_reload",
    "browser_resize",
    "browser_select_option",
    "browser_snapshot",
    "browser_tabs",
    "browser_type",
    "browser_uncheck",
    "browser_wait_for",
}
_FRAME_LIMIT_BYTES = 4 * 1024 * 1024


def _image_from_result(result: Any) -> tuple[str, str] | None:
    for block in getattr(result, "content", ()) or ():
        block_type = getattr(block, "type", None)
        data = getattr(block, "data", None)
        mime_type = getattr(block, "mimeType", None) or getattr(block, "mime_type", None)
        if block_type != "image" or not isinstance(data, str) or not data:
            continue
        if mime_type not in {"image/jpeg", "image/png"}:
            continue
        # Base64 expands bytes by roughly 4/3.  Keep a hard ceiling so a single
        # pathological page cannot monopolize the chat WebSocket.
        if len(data) > (_FRAME_LIMIT_BYTES * 4 // 3) + 8:
            logger.warning("browser mirror frame dropped because it exceeded the size limit")
            return None
        return data, mime_type
    return None


def _action_label(tool_name: str, arguments: dict[str, Any]) -> str:
    labels = {
        "browser_click": "点击页面元素",
        "browser_close": "关闭浏览器",
        "browser_drag": "拖动页面元素",
        "browser_evaluate": "执行页面脚本",
        "browser_file_upload": "上传文件",
        "browser_fill_form": "填写表单",
        "browser_go_back": "返回上一页",
        "browser_go_forward": "前进",
        "browser_hover": "悬停页面元素",
        "browser_navigate": "打开网页",
        "browser_navigate_back": "返回上一页",
        "browser_press_key": "按下键盘按键",
        "browser_resize": "调整浏览器窗口",
        "browser_select_option": "选择页面选项",
        "browser_tabs": "切换浏览器标签页",
        "browser_take_screenshot": "截取浏览器画面",
        "browser_type": "输入文字",
        "browser_wait_for": "等待页面更新",
    }
    label = labels.get(tool_name, "操作浏览器")
    if tool_name == "browser_navigate":
        url = arguments.get("url")
        if isinstance(url, str) and url:
            return f"{label} · {url[:180]}"
    return label


class BrowserMirrorService:
    """Own live mirror state while Playwright MCP owns the browser process."""

    def __init__(self) -> None:
        self._event_sink: BrowserEventSink | None = None
        self._session: Any | None = None
        self._server_cwd: str | None = None
        self._owner_chat_id: str | None = None
        self._owner_message_id: str | None = None
        self._paused_chat_id: str | None = None
        self._stopped_message_by_chat: dict[str, str | None] = {}
        self._last_url_by_chat: dict[str, str] = {}
        self._last_frame_by_chat: dict[str, dict[str, Any]] = {}
        self._last_status_by_chat: dict[str, dict[str, Any]] = {}
        self._capture_lock = asyncio.Lock()
        self._capture_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing_task: asyncio.Task[None] | None = None

    def set_event_sink(self, sink: BrowserEventSink | None) -> None:
        self._event_sink = sink

    @staticmethod
    def observes(server_name: str, tool_name: str) -> bool:
        return server_name.lower() in _PLAYWRIGHT_SERVER_NAMES and tool_name.startswith(
            "browser_"
        )

    async def before_tool(
        self,
        *,
        chat_id: str,
        session: Any,
        server_cwd: str | None,
        message_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Register the active MCP session and enforce user takeover."""

        stopped_message = self._stopped_message_by_chat.get(chat_id)
        if chat_id in self._stopped_message_by_chat:
            if stopped_message == message_id:
                return (
                    "(Browser automation was stopped by the user for this turn. "
                    "Do not restart it unless the user sends a new request.)"
                )
            self._stopped_message_by_chat.pop(chat_id, None)

        # A user stop closes Playwright in the background so the control
        # command can acknowledge immediately. Before a later turn starts a
        # new browser operation, ensure that old close has actually finished.
        closing_task = self._closing_task
        if closing_task is not None and not closing_task.done():
            await asyncio.shield(closing_task)

        if tool_name in _BROWSER_MUTATIONS and self._paused_chat_id is not None:
            if self._paused_chat_id == chat_id:
                return (
                    "(Browser automation is paused while the user is controlling it. "
                    "Wait until the user returns control.)"
                )
            return (
                "(Browser automation is temporarily unavailable because another "
                "conversation is under user control.)"
            )

        previous_owner = self._owner_chat_id
        self._session = session
        self._server_cwd = server_cwd
        self._owner_chat_id = chat_id
        self._owner_message_id = message_id
        if previous_owner and previous_owner != chat_id:
            await self._emit_status(
                previous_owner,
                "stopped",
                "浏览器控制已切换到另一个会话",
            )
        if tool_name == "browser_navigate":
            url = arguments.get("url")
            if isinstance(url, str) and url:
                self._last_url_by_chat[chat_id] = url
        await self._emit_status(chat_id, "running", "如意正在操作浏览器")
        return None

    async def after_tool(
        self,
        *,
        chat_id: str,
        session: Any,
        server_cwd: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> None:
        if tool_name == "browser_close":
            await self._emit_status(chat_id, "stopped", "浏览器已关闭")
            return

        action_id = str(uuid.uuid4())
        if tool_name in _BROWSER_MUTATIONS or tool_name == "browser_take_screenshot":
            await self._emit({
                "event": "browser_action",
                "chat_id": chat_id,
                "browser_session_id": chat_id,
                "action_id": action_id,
                "label": _action_label(tool_name, arguments),
                "tool_name": tool_name,
                "status": "completed",
                "timestamp": int(time.time() * 1000),
            })

        image = _image_from_result(result)
        if image is None and tool_name in _BROWSER_MUTATIONS:
            image = await self._capture_from_session(session, server_cwd)
        # Stop may arrive while the tool or its follow-up screenshot is still
        # in flight. Never revive a stopped panel with that stale frame.
        if (
            image is not None
            and self._owner_chat_id == chat_id
            and chat_id not in self._stopped_message_by_chat
        ):
            await self._emit_frame(chat_id, image, action_id=action_id)

    async def control(self, chat_id: str, action: str) -> None:
        """Apply a desktop BrowserPanel control command."""

        if action == "pause":
            self._paused_chat_id = chat_id
            await self._emit_status(
                chat_id,
                "user_control",
                "自动操作已暂停，请在浏览器窗口中完成操作",
            )
            return
        if action == "resume":
            self._stopped_message_by_chat.pop(chat_id, None)
            if self._paused_chat_id == chat_id:
                self._paused_chat_id = None
            await self._emit_status(chat_id, "running", "控制权已交还如意")
            if self._session is not None and self._owner_chat_id == chat_id:
                self._schedule_capture(chat_id, self._session, self._server_cwd)
            return
        if action == "capture":
            if self._session is not None and self._owner_chat_id == chat_id:
                self._schedule_capture(chat_id, self._session, self._server_cwd)
            return
        if action == "stop":
            session = self._session if self._owner_chat_id == chat_id else None
            if self._paused_chat_id == chat_id:
                self._paused_chat_id = None
            self._stopped_message_by_chat[chat_id] = self._owner_message_id
            if self._owner_chat_id == chat_id:
                self._owner_chat_id = None
                self._owner_message_id = None
                self._session = None
                self._server_cwd = None
            capture_task = self._capture_tasks.pop(chat_id, None)
            if capture_task is not None and not capture_task.done():
                capture_task.cancel()
            # Tell the GUI first. Closing Playwright can take several seconds
            # and must not block the WebSocket command queue.
            await self._emit_status(chat_id, "stopped", "浏览器已停止")
            if session is not None:
                self._schedule_close(session)
            return
        raise ValueError(f"unsupported browser control action: {action}")

    def _schedule_capture(
        self,
        chat_id: str,
        session: Any,
        server_cwd: str | None,
    ) -> None:
        """Coalesce mirror refreshes and keep them off the WebSocket reader."""

        current = self._capture_tasks.get(chat_id)
        if current is not None and not current.done():
            return

        async def capture() -> None:
            image = await self._capture_from_session(session, server_cwd)
            if (
                image is not None
                and self._owner_chat_id == chat_id
                and chat_id not in self._stopped_message_by_chat
            ):
                await self._emit_frame(chat_id, image)

        task = asyncio.create_task(capture())
        self._capture_tasks[chat_id] = task

        def capture_done(done: asyncio.Task[None]) -> None:
            if self._capture_tasks.get(chat_id) is done:
                self._capture_tasks.pop(chat_id, None)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.debug("browser mirror background capture failed: {}", error)

        task.add_done_callback(capture_done)

    def _schedule_close(self, session: Any) -> None:
        """Close Playwright asynchronously after stop has been acknowledged."""

        previous = self._closing_task
        if previous is not None and not previous.done():
            return

        async def close() -> None:
            try:
                await asyncio.wait_for(
                    session.call_tool("browser_close", arguments={}),
                    timeout=10,
                )
            except Exception as exc:
                logger.debug("browser mirror close failed: {}", exc)

        task = asyncio.create_task(close())
        self._closing_task = task

        def close_done(done: asyncio.Task[None]) -> None:
            if self._closing_task is done:
                self._closing_task = None
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.debug("browser mirror background close failed: {}", error)

        task.add_done_callback(close_done)

    async def replay(self, chat_id: str) -> None:
        status = self._last_status_by_chat.get(chat_id)
        frame = self._last_frame_by_chat.get(chat_id)
        if status is not None:
            await self._emit(dict(status))
        if frame is not None:
            await self._emit(dict(frame))

    async def _capture_from_session(
        self,
        session: Any,
        server_cwd: str | None,
    ) -> tuple[str, str] | None:
        async with self._capture_lock:
            from nanobot.agent.tools.mcp import _artifact_snapshot

            before = _artifact_snapshot(server_cwd)
            try:
                result = await asyncio.wait_for(
                    session.call_tool(
                        "browser_take_screenshot",
                        arguments={"type": "jpeg", "scale": "css"},
                    ),
                    timeout=15,
                )
            except Exception as exc:
                logger.debug("browser mirror screenshot failed: {}", exc)
                return None
            finally:
                # Playwright MCP writes screenshots to its output directory
                # before returning the image block. Remove only newly-created
                # default frame files so mirror traffic never becomes a
                # session artifact or accumulates on disk.
                after = _artifact_snapshot(server_cwd)
                for path in set(after) - set(before):
                    if path.suffix.lower() not in {".jpeg", ".jpg", ".png"}:
                        continue
                    if not path.stem.startswith("page-"):
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug("browser mirror screenshot cleanup failed: {}", exc)
            return _image_from_result(result)

    async def _emit_frame(
        self,
        chat_id: str,
        image: tuple[str, str],
        *,
        action_id: str | None = None,
    ) -> None:
        data, mime_type = image
        payload: dict[str, Any] = {
            "event": "browser_frame",
            "chat_id": chat_id,
            "browser_session_id": chat_id,
            "backend": "playwright_mcp",
            "url": self._last_url_by_chat.get(chat_id),
            "image_base64": data,
            "mime_type": mime_type,
            "captured_at": int(time.time() * 1000),
        }
        if action_id:
            payload["action_id"] = action_id
        self._last_frame_by_chat[chat_id] = payload
        await self._emit(payload)

    async def _emit_status(self, chat_id: str, status: str, message: str) -> None:
        payload = {
            "event": "browser_status",
            "chat_id": chat_id,
            "browser_session_id": chat_id,
            "backend": "playwright_mcp",
            "status": status,
            "message": message,
            "timestamp": int(time.time() * 1000),
        }
        previous = self._last_status_by_chat.get(chat_id)
        self._last_status_by_chat[chat_id] = payload
        if (
            previous is not None
            and previous.get("status") == status
            and previous.get("message") == message
        ):
            return
        await self._emit(payload)

    async def _emit(self, payload: dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            result = sink(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("browser mirror event sink failed")


browser_mirror = BrowserMirrorService()

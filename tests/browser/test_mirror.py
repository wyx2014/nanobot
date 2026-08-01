from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nanobot.browser.mirror import BrowserMirrorService


@dataclass
class ImageBlock:
    data: str = "aGVsbG8="
    mimeType: str = "image/jpeg"
    type: str = "image"


@dataclass
class Result:
    content: list[object]


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return Result([ImageBlock()])


class BlockingSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.capture_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_capture = asyncio.Event()
        self.release_close = asyncio.Event()

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "browser_take_screenshot":
            self.capture_started.set()
            await self.release_capture.wait()
        if name == "browser_close":
            self.close_started.set()
            await self.release_close.wait()
        return Result([ImageBlock()])


@pytest.mark.asyncio
async def test_mutating_playwright_action_emits_action_and_frame() -> None:
    service = BrowserMirrorService()
    session = FakeSession()
    events: list[dict] = []
    service.set_event_sink(events.append)

    blocked = await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
    )
    assert blocked is None

    await service.after_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
        result=Result([]),
    )

    assert session.calls == [
        (
            "browser_take_screenshot",
            {"type": "jpeg", "scale": "css"},
        )
    ]
    assert [event["event"] for event in events] == [
        "browser_status",
        "browser_action",
        "browser_frame",
    ]
    assert events[-1]["chat_id"] == "chat-1"
    assert events[-1]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_user_takeover_blocks_automation_until_resume() -> None:
    service = BrowserMirrorService()
    session = FakeSession()
    events: list[dict] = []
    service.set_event_sink(events.append)

    await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
    )
    await service.control("chat-1", "pause")

    blocked = await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_click",
        arguments={"ref": "e1"},
    )
    assert blocked is not None
    assert "paused" in blocked

    await service.control("chat-1", "resume")
    await asyncio.sleep(0)
    assert await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_click",
        arguments={"ref": "e1"},
    ) is None
    assert [event["status"] for event in events if event["event"] == "browser_status"] == [
        "running",
        "user_control",
        "running",
        "running",
    ]


@pytest.mark.asyncio
async def test_stop_closes_only_the_owning_chat_browser() -> None:
    service = BrowserMirrorService()
    session = FakeSession()
    events: list[dict] = []
    service.set_event_sink(events.append)

    await service.before_tool(
        chat_id="chat-owner",
        session=session,
        server_cwd=None,
        message_id="turn-owner",
        tool_name="browser_navigate",
        arguments={},
    )
    await service.control("chat-owner", "stop")
    await asyncio.sleep(0)

    assert ("browser_close", {}) in session.calls
    assert events[-1] == {
        "event": "browser_status",
        "chat_id": "chat-owner",
        "browser_session_id": "chat-owner",
        "backend": "playwright_mcp",
        "status": "stopped",
        "message": "浏览器已停止",
        "timestamp": events[-1]["timestamp"],
    }
    assert await service.before_tool(
        chat_id="chat-owner",
        session=session,
        server_cwd=None,
        message_id="turn-owner",
        tool_name="browser_click",
        arguments={},
    ) is not None
    assert await service.before_tool(
        chat_id="chat-owner",
        session=session,
        server_cwd=None,
        message_id="turn-next",
        tool_name="browser_navigate",
        arguments={},
    ) is None


@pytest.mark.asyncio
async def test_capture_commands_are_non_blocking_and_coalesced() -> None:
    service = BrowserMirrorService()
    session = BlockingSession()
    events: list[dict] = []
    service.set_event_sink(events.append)

    await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_navigate",
        arguments={},
    )

    await asyncio.wait_for(service.control("chat-1", "capture"), timeout=0.1)
    await session.capture_started.wait()
    await asyncio.wait_for(service.control("chat-1", "capture"), timeout=0.1)
    assert [name for name, _ in session.calls].count("browser_take_screenshot") == 1

    session.release_capture.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(event["event"] == "browser_frame" for event in events)


@pytest.mark.asyncio
async def test_stop_acknowledges_before_slow_browser_close() -> None:
    service = BrowserMirrorService()
    session = BlockingSession()
    events: list[dict] = []
    service.set_event_sink(events.append)

    await service.before_tool(
        chat_id="chat-1",
        session=session,
        server_cwd=None,
        message_id="turn-1",
        tool_name="browser_navigate",
        arguments={},
    )

    await asyncio.wait_for(service.control("chat-1", "stop"), timeout=0.1)
    assert events[-1]["event"] == "browser_status"
    assert events[-1]["status"] == "stopped"

    await session.close_started.wait()
    session.release_close.set()
    await asyncio.sleep(0)

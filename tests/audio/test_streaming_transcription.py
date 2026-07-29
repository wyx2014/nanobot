from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve

from nanobot.audio.streaming_transcription import (
    RealtimeTranscriptionSession,
    resolve_streaming_transcription_profile,
)
from nanobot.audio.transcription import EffectiveTranscriptionConfig


def config(*, provider: str, model: str, api_base: str = "") -> EffectiveTranscriptionConfig:
    return EffectiveTranscriptionConfig(
        enabled=True,
        provider=provider,
        model=model,
        language="zh",
        api_key="secret",
        api_base=api_base,
        max_duration_sec=120,
        max_upload_mb=25,
    )


def test_resolves_only_explicit_realtime_models() -> None:
    qwen = resolve_streaming_transcription_profile(
        config(provider="dashscope", model="qwen3-asr-flash-realtime")
    )
    assert qwen is not None
    assert qwen.name == "qwen-asr-server-vad"
    assert "model=qwen3-asr-flash-realtime" in qwen.websocket_url

    stepfun = resolve_streaming_transcription_profile(
        config(
            provider="stepfun",
            model="stepaudio-2.5-asr",
            api_base="https://api.stepfun.com/step_plan/v1/audio/asr/sse",
        )
    )
    assert stepfun is not None
    assert stepfun.name == "stepfun-asr-server-vad"
    assert stepfun.model == "stepaudio-2.5-asr-stream"
    assert stepfun.batch_fallback is True
    assert (
        stepfun.websocket_url
        == "wss://api.stepfun.com/v1/realtime/asr/stream"
    )

    assert (
        resolve_streaming_transcription_profile(
            config(provider="stepfun", model="stepaudio-2.5-realtime")
        )
        is None
    )


@pytest.mark.asyncio
async def test_qwen_text_is_replaceable_until_completed() -> None:
    effective = config(provider="dashscope", model="qwen3-asr-flash-realtime")
    profile = resolve_streaming_transcription_profile(effective)
    assert profile is not None
    callback = AsyncMock()
    session = RealtimeTranscriptionSession(
        stream_id="voice-test",
        config=effective,
        profile=profile,
        on_transcript=callback,
    )

    await session._handle_event(  # noqa: SLF001 - protocol unit test
        {
            "type": "conversation.item.input_audio_transcription.text",
            "item_id": "item-1",
            "text": "青岛",
            "stash": "皮",
        }
    )
    await session._handle_event(  # noqa: SLF001 - protocol unit test
        {
            "type": "conversation.item.input_audio_transcription.text",
            "item_id": "item-1",
            "text": "青岛啤",
            "stash": "酒",
        }
    )
    await session._handle_event(  # noqa: SLF001 - protocol unit test
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "青岛啤酒",
        }
    )

    assert callback.await_args_list[0].args == ("partial", "青岛皮")
    assert callback.await_args_list[1].args == ("partial", "青岛啤酒")
    assert callback.await_args_list[2].args == ("stable", "青岛啤酒")


@pytest.mark.asyncio
async def test_qwen_session_streams_audio_and_finishes_against_websocket() -> None:
    received_types: list[str] = []

    async def handler(socket) -> None:
        async for raw in socket:
            payload = json.loads(raw)
            received_types.append(payload["type"])
            if payload["type"] == "session.update":
                await socket.send(json.dumps({"type": "session.updated"}))
            elif payload["type"] == "session.finish":
                await socket.send(json.dumps({
                    "type": "conversation.item.input_audio_transcription.text",
                    "item_id": "item-1",
                    "text": "实时",
                    "stash": "语音",
                }))
                await socket.send(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "实时语音",
                }))
                await socket.send(json.dumps({"type": "session.finished"}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        effective = config(
            provider="dashscope",
            model="qwen3-asr-flash-realtime",
            api_base=f"ws://127.0.0.1:{port}/realtime",
        )
        profile = resolve_streaming_transcription_profile(effective)
        assert profile is not None
        callback = AsyncMock()
        session = RealtimeTranscriptionSession(
            stream_id="voice-e2e",
            config=effective,
            profile=profile,
            on_transcript=callback,
        )

        await session.start()
        await session.append_audio(b"\x00\x00" * 640, duration_ms=40)
        result = await session.finish()

    assert result == "实时语音"
    assert received_types == [
        "session.update",
        "input_audio_buffer.append",
        "session.finish",
    ]


@pytest.mark.asyncio
async def test_stepfun_asr_stream_emits_partial_and_final_text() -> None:
    received: list[dict[str, object]] = []
    audio_appends = 0

    async def handler(socket) -> None:
        nonlocal audio_appends
        async for raw in socket:
            payload = json.loads(raw)
            received.append(payload)
            if payload["type"] == "session.update":
                await socket.send(json.dumps({"type": "session.updated"}))
            elif payload["type"] == "input_audio_buffer.append":
                audio_appends += 1
                if audio_appends < 4:
                    await socket.send(json.dumps({
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "item-step",
                        "text": "实时",
                        "stash": "听写",
                    }))
                else:
                    await socket.send(json.dumps({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "item-step",
                        "transcript": "实时听写",
                    }))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        effective = config(
            provider="stepfun",
            model="stepaudio-2.5-asr",
            api_base=f"ws://127.0.0.1:{port}",
        )
        profile = resolve_streaming_transcription_profile(effective)
        assert profile is not None
        callback = AsyncMock()
        session = RealtimeTranscriptionSession(
            stream_id="voice-stepfun",
            config=effective,
            profile=profile,
            on_transcript=callback,
        )

        await session.start()
        await session.append_audio(b"\x00\x00" * 640, duration_ms=40)
        await session.append_audio(b"\x00\x00" * 640, duration_ms=40)
        await session.append_audio(b"\x00\x00" * 640, duration_ms=40)
        result = await session.finish()

    assert result == "实时听写"
    assert callback.await_args_list[0].args == ("partial", "实时听写")
    assert callback.await_args_list[-1].args == ("stable", "实时听写")
    assert [payload["type"] for payload in received] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.append",
        "input_audio_buffer.append",
        "input_audio_buffer.append",
    ]
    session_update = received[0]["session"]
    assert isinstance(session_update, dict)
    transcription = session_update["audio"]["input"]["transcription"]
    assert transcription["model"] == "stepaudio-2.5-asr-stream"

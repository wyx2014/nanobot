from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.audio.transcription import EffectiveTranscriptionConfig
from nanobot.webui import voice_stream_ws
from nanobot.webui.voice_stream_ws import WebuiVoiceStreamManager


def batch_config() -> EffectiveTranscriptionConfig:
    return EffectiveTranscriptionConfig(
        enabled=True,
        provider="stepfun",
        model="stepaudio-2-asr-pro",
        language="zh",
        api_key="secret",
        api_base="https://api.stepfun.com/step_plan/v1",
        max_duration_sec=120,
        max_upload_mb=25,
    )


def stepfun_realtime_config() -> EffectiveTranscriptionConfig:
    return EffectiveTranscriptionConfig(
        enabled=True,
        provider="stepfun",
        model="stepaudio-2.5-asr",
        language="zh",
        api_key="secret",
        api_base="https://api.stepfun.com/v1",
        max_duration_sec=120,
        max_upload_mb=25,
    )


@pytest.mark.asyncio
async def test_start_reports_batch_for_non_realtime_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_stream_ws, "load_config", lambda: object())
    monkeypatch.setattr(
        voice_stream_ws,
        "resolve_transcription_config",
        lambda _config: batch_config(),
    )
    send = AsyncMock()
    manager = WebuiVoiceStreamManager(send)
    connection = object()

    await manager.start(
        connection,
        {
            "type": "voice_stream_start",
            "stream_id": "voice-test",
            "sample_rate": 16_000,
        },
    )

    send.assert_awaited_once_with(
        connection,
        "voice_stream_state",
        stream_id="voice-test",
        state="listening",
        mode="batch",
        provider="stepfun",
        model="stepaudio-2-asr-pro",
    )


@pytest.mark.asyncio
async def test_stepfun_realtime_connection_failure_falls_back_to_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSession:
        def __init__(self, **_kwargs) -> None:
            pass

        async def start(self) -> None:
            raise OSError("endpoint unavailable")

    monkeypatch.setattr(voice_stream_ws, "load_config", lambda: object())
    monkeypatch.setattr(
        voice_stream_ws,
        "resolve_transcription_config",
        lambda _config: stepfun_realtime_config(),
    )
    monkeypatch.setattr(
        voice_stream_ws,
        "RealtimeTranscriptionSession",
        FailingSession,
    )
    send = AsyncMock()
    manager = WebuiVoiceStreamManager(send)
    connection = object()

    await manager.start(
        connection,
        {
            "type": "voice_stream_start",
            "stream_id": "voice-stepfun",
            "sample_rate": 16_000,
        },
    )
    await manager.stop(
        connection,
        {
            "type": "voice_stream_stop",
            "stream_id": "voice-stepfun",
        },
    )

    assert send.await_args_list[0].args == (
        connection,
        "voice_stream_state",
    )
    assert send.await_args_list[0].kwargs == {
        "stream_id": "voice-stepfun",
        "state": "listening",
        "mode": "batch",
        "provider": "stepfun",
        "model": "stepaudio-2.5-asr",
        "fallback_reason": "realtime_connection_failed",
    }
    assert send.await_args_list[1].kwargs == {
        "stream_id": "voice-stepfun",
        "state": "done",
        "mode": "batch",
    }


@pytest.mark.asyncio
async def test_start_rejects_unsupported_capture_rate() -> None:
    send = AsyncMock()
    manager = WebuiVoiceStreamManager(send)
    connection = object()

    await manager.start(
        connection,
        {
            "type": "voice_stream_start",
            "stream_id": "voice-test",
            "sample_rate": 48_000,
        },
    )

    send.assert_awaited_once_with(
        connection,
        "voice_stream_error",
        detail="unsupported_sample_rate",
        stream_id="voice-test",
    )

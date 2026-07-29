"""WebSocket lifecycle adapter for realtime WebUI voice input."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from nanobot.audio.streaming_transcription import (
    RealtimeTranscriptionSession,
    resolve_streaming_transcription_profile,
)
from nanobot.audio.transcription import resolve_transcription_config
from nanobot.config.loader import load_config

_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_PCM_CHUNK_BYTES = 16_384


class VoiceEventSender(Protocol):
    async def __call__(self, connection: Any, event: str, **fields: Any) -> None: ...


@dataclass
class ActiveVoiceStream:
    stream_id: str
    mode: str
    max_duration_ms: int
    provider: str
    model: str
    session: RealtimeTranscriptionSession | None = None
    last_sequence: int = -1
    received_duration_ms: float = 0.0


class WebuiVoiceStreamManager:
    def __init__(self, send_event: VoiceEventSender):
        self._send_event = send_event
        self._active: dict[Any, ActiveVoiceStream] = {}

    async def start(self, connection: Any, envelope: dict[str, Any]) -> None:
        stream_id = envelope.get("stream_id")
        sample_rate = envelope.get("sample_rate")
        if not isinstance(stream_id, str) or _STREAM_ID_RE.fullmatch(stream_id) is None:
            await self._error(connection, stream_id, "invalid_stream")
            return
        if sample_rate != 16_000:
            await self._error(connection, stream_id, "unsupported_sample_rate")
            return
        await self.cleanup(connection)

        config = resolve_transcription_config(load_config())
        if not config.enabled:
            await self._error(connection, stream_id, "disabled")
            return
        if not config.configured:
            await self._error(
                connection,
                stream_id,
                "not_configured",
                provider=config.provider,
            )
            return
        profile = resolve_streaming_transcription_profile(config)
        active = ActiveVoiceStream(
            stream_id=stream_id,
            mode="realtime" if profile else "batch",
            max_duration_ms=config.max_duration_sec * 1000,
            provider=config.provider,
            model=config.model,
        )
        self._active[connection] = active
        if profile is None:
            await self._send_event(
                connection,
                "voice_stream_state",
                stream_id=stream_id,
                state="listening",
                mode="batch",
                provider=config.provider,
                model=config.model,
            )
            return

        async def on_transcript(kind: str, text: str) -> None:
            event = (
                "voice_transcript_stable"
                if kind == "stable"
                else "voice_transcript_partial"
            )
            await self._send_event(connection, event, stream_id=stream_id, text=text)

        session = RealtimeTranscriptionSession(
            stream_id=stream_id,
            config=config,
            profile=profile,
            on_transcript=on_transcript,
        )
        active.session = session
        try:
            await session.start()
        except Exception as exc:
            logger.warning(
                "realtime voice start failed stream_id={} provider={} model={}: {}",
                stream_id,
                config.provider,
                config.model,
                exc,
            )
            if profile.batch_fallback:
                active.mode = "batch"
                active.session = None
                await self._send_event(
                    connection,
                    "voice_stream_state",
                    stream_id=stream_id,
                    state="listening",
                    mode="batch",
                    provider=config.provider,
                    model=config.model,
                    fallback_reason="realtime_connection_failed",
                )
                return
            self._active.pop(connection, None)
            await self._error(
                connection,
                stream_id,
                "connection_failed",
                provider=config.provider,
            )
            return
        await self._send_event(
            connection,
            "voice_stream_state",
            stream_id=stream_id,
            state="listening",
            mode="realtime",
            provider=config.provider,
            model=config.model,
        )

    async def append(self, connection: Any, envelope: dict[str, Any]) -> None:
        active = self._matching(connection, envelope)
        if active is None:
            return
        if active.mode != "realtime" or active.session is None:
            return
        sequence = envelope.get("sequence")
        duration_ms = envelope.get("duration_ms")
        encoded = envelope.get("audio")
        if not isinstance(sequence, int) or sequence < 0:
            await self._error(connection, active.stream_id, "invalid_sequence")
            return
        if sequence <= active.last_sequence:
            return
        if sequence != active.last_sequence + 1:
            logger.debug(
                "voice chunk gap stream_id={} expected={} got={}",
                active.stream_id,
                active.last_sequence + 1,
                sequence,
            )
        if not isinstance(duration_ms, (int, float)) or not 0 < duration_ms <= 1000:
            await self._error(connection, active.stream_id, "invalid_duration")
            return
        if not isinstance(encoded, str):
            await self._error(connection, active.stream_id, "missing_audio")
            return
        try:
            pcm16 = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            await self._error(connection, active.stream_id, "invalid_audio")
            return
        if not pcm16 or len(pcm16) > _MAX_PCM_CHUNK_BYTES or len(pcm16) % 2:
            await self._error(connection, active.stream_id, "invalid_audio")
            return
        next_duration = active.received_duration_ms + float(duration_ms)
        if next_duration > active.max_duration_ms + 1000:
            await self._error(connection, active.stream_id, "duration")
            await self.cleanup(connection)
            return
        active.last_sequence = sequence
        active.received_duration_ms = next_duration
        try:
            await active.session.append_audio(pcm16, duration_ms=float(duration_ms))
        except Exception as exc:
            logger.warning(
                "realtime voice append failed stream_id={} sequence={}: {}",
                active.stream_id,
                sequence,
                exc,
            )
            await self._error(
                connection,
                active.stream_id,
                "connection_interrupted",
                recoverable=bool(active.session.aggregate_transcript()),
            )
            await self.cleanup(connection)

    async def stop(self, connection: Any, envelope: dict[str, Any]) -> None:
        active = self._matching(connection, envelope)
        if active is None:
            return
        if active.mode == "batch" or active.session is None:
            self._active.pop(connection, None)
            await self._send_event(
                connection,
                "voice_stream_state",
                stream_id=active.stream_id,
                state="done",
                mode="batch",
            )
            return
        await self._send_event(
            connection,
            "voice_stream_state",
            stream_id=active.stream_id,
            state="finalizing",
            mode="realtime",
        )
        try:
            text = await active.session.finish()
        except Exception as exc:
            logger.warning(
                "realtime voice finish failed stream_id={}: {}",
                active.stream_id,
                exc,
            )
            text = active.session.aggregate_transcript()
            if not text:
                await self._error(
                    connection,
                    active.stream_id,
                    "connection_interrupted",
                )
                self._active.pop(connection, None)
                return
        self._active.pop(connection, None)
        if not text:
            await self._error(connection, active.stream_id, "empty")
            return
        await self._send_event(
            connection,
            "voice_transcript_final",
            stream_id=active.stream_id,
            text=text,
        )

    async def cancel(self, connection: Any, envelope: dict[str, Any]) -> None:
        active = self._matching(connection, envelope)
        if active is None:
            return
        await self.cleanup(connection)
        await self._send_event(
            connection,
            "voice_stream_state",
            stream_id=active.stream_id,
            state="done",
            mode=active.mode,
            outcome="cancelled",
        )

    async def cleanup(self, connection: Any) -> None:
        active = self._active.pop(connection, None)
        if active is not None and active.session is not None:
            await active.session.cancel()

    def _matching(
        self,
        connection: Any,
        envelope: dict[str, Any],
    ) -> ActiveVoiceStream | None:
        active = self._active.get(connection)
        stream_id = envelope.get("stream_id")
        if active is None or stream_id != active.stream_id:
            return None
        return active

    async def _error(
        self,
        connection: Any,
        stream_id: Any,
        detail: str,
        **extra: Any,
    ) -> None:
        fields = {"detail": detail, **extra}
        if isinstance(stream_id, str):
            fields["stream_id"] = stream_id
        await self._send_event(connection, "voice_stream_error", **fields)

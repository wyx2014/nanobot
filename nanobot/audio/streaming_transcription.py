"""Realtime microphone transcription sessions for WebUI voice input.

This module is deliberately outside the agent loop.  It owns only the
provider WebSocket, transcript aggregation, and the small lifecycle contract
used by the WebSocket channel.
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
from loguru import logger
from websockets.asyncio.client import ClientConnection, connect

from nanobot.audio.transcription import EffectiveTranscriptionConfig

StreamingProfileName = Literal[
    "qwen-asr-server-vad",
    "stepfun-asr-server-vad",
    "openai-transcription-manual",
]
TranscriptCallback = Callable[[str, str], Awaitable[None]]

_QWEN_DEFAULT_URL = (
    "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    "?model=qwen3-asr-flash-realtime"
)
_STEPFUN_ASR_DEFAULT_URL = "wss://api.stepfun.com/v1/realtime/asr/stream"
_STEPFUN_ASR_MODEL = "stepaudio-2.5-asr-stream"
_OPENAI_DEFAULT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_CONNECT_TIMEOUT_S = 8.0
_FINISH_TIMEOUT_S = 5.0
_STEPFUN_FINAL_SILENCE_MS = 1_000


@dataclass(frozen=True)
class StreamingTranscriptionProfile:
    name: StreamingProfileName
    websocket_url: str
    model: str
    sample_rate: int = 16_000
    batch_fallback: bool = False


def resolve_streaming_transcription_profile(
    config: EffectiveTranscriptionConfig,
) -> StreamingTranscriptionProfile | None:
    """Return an explicit realtime profile or ``None`` for batch ASR.

    Model-name detection is intentionally conservative.  An HTTP/SSE model
    must never be presented to the GUI as realtime merely because its response
    body happens to stream.
    """
    model = config.model.strip()
    normalized = model.lower()
    if config.provider == "dashscope" and normalized == "qwen3-asr-flash-realtime":
        return StreamingTranscriptionProfile(
            name="qwen-asr-server-vad",
            websocket_url=_qwen_realtime_url(config.api_base, model),
            model=model,
        )
    if config.provider == "stepfun" and normalized in {
        "stepaudio-2.5-asr",
        "stepaudio-2.5-asr-stream",
        "step-asr-1.1-stream",
    }:
        upstream_model = (
            "step-asr-1.1-stream"
            if normalized == "step-asr-1.1-stream"
            else _STEPFUN_ASR_MODEL
        )
        return StreamingTranscriptionProfile(
            name="stepfun-asr-server-vad",
            websocket_url=_stepfun_asr_realtime_url(config.api_base),
            model=upstream_model,
            batch_fallback=normalized == "stepaudio-2.5-asr",
        )
    if config.provider in {"openai", "custom"} and normalized in {
        "gpt-realtime-whisper",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
    }:
        return StreamingTranscriptionProfile(
            name="openai-transcription-manual",
            websocket_url=_openai_realtime_url(config.api_base),
            model=model,
            sample_rate=24_000,
        )
    return None


def _as_websocket_url(value: str) -> str:
    if value.startswith("wss://") or value.startswith("ws://"):
        return value
    if value.startswith("https://"):
        return f"wss://{value[len('https://') :]}"
    if value.startswith("http://"):
        return f"ws://{value[len('http://') :]}"
    return value


def _with_query(url: str, **values: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(values)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _qwen_realtime_url(api_base: str, model: str) -> str:
    base = api_base.strip()
    if not base:
        return _with_query(_QWEN_DEFAULT_URL, model=model)
    websocket = _as_websocket_url(base.rstrip("/"))
    parts = urlsplit(websocket)
    if parts.netloc.endswith("dashscope.aliyuncs.com"):
        websocket = urlunsplit(
            (parts.scheme, parts.netloc, "/api-ws/v1/realtime", parts.query, parts.fragment)
        )
    elif not parts.path.endswith("/realtime"):
        websocket = f"{websocket}/api-ws/v1/realtime"
    return _with_query(websocket, model=model)


def _openai_realtime_url(api_base: str) -> str:
    base = api_base.strip()
    if not base:
        return _OPENAI_DEFAULT_URL
    websocket = _as_websocket_url(base.rstrip("/"))
    if not urlsplit(websocket).path.endswith("/realtime"):
        websocket = f"{websocket}/realtime"
    return _with_query(websocket, intent="transcription")


def _stepfun_asr_realtime_url(api_base: str) -> str:
    """Resolve a StepFun provider base or ASR endpoint to its WebSocket API."""
    base = api_base.strip()
    if not base:
        return _STEPFUN_ASR_DEFAULT_URL
    websocket = _as_websocket_url(base.rstrip("/"))
    parts = urlsplit(websocket)
    if parts.netloc in {"api.stepfun.com", "api.stepfun.ai"}:
        # Step Plan uses /step_plan/v1 for HTTP APIs, but the dedicated
        # bidirectional ASR socket is exposed on the standard /v1 path.  A
        # Step Plan key is accepted there as well.
        return _STEPFUN_ASR_DEFAULT_URL
    path = parts.path.rstrip("/")
    for suffix in (
        "/realtime/asr/stream",
        "/audio/asr/sse",
        "/audio/transcriptions",
    ):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path.endswith("/realtime/asr/stream"):
        path = f"{path}/realtime/asr/stream"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class RealtimeTranscriptionSession:
    """One upstream realtime ASR WebSocket."""

    def __init__(
        self,
        *,
        stream_id: str,
        config: EffectiveTranscriptionConfig,
        profile: StreamingTranscriptionProfile,
        on_transcript: TranscriptCallback,
    ):
        self.stream_id = stream_id
        self.config = config
        self.profile = profile
        self.on_transcript = on_transcript
        self.socket: ClientConnection | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.failure: Exception | None = None
        self.stopping = False
        self.closed = False
        self.item_order: list[str] = []
        self.partials: dict[str, str] = {}
        self.finals: dict[str, str] = {}
        self.last_emitted = ""
        self.last_emitted_kind = ""
        self.sent_audio_ms = 0.0

    async def start(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        ssl_context = (
            ssl.create_default_context(cafile=certifi.where())
            if self.profile.websocket_url.startswith("wss://")
            else None
        )
        self.socket = await asyncio.wait_for(
            connect(
                self.profile.websocket_url,
                additional_headers=headers,
                ssl=ssl_context,
                max_size=2 * 1024 * 1024,
                ping_interval=25,
                ping_timeout=8,
            ),
            timeout=_CONNECT_TIMEOUT_S,
        )
        self.reader_task = asyncio.create_task(
            self._read_messages(),
            name=f"voice-asr-{self.stream_id}",
        )
        await self._send(self._session_update())
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=_CONNECT_TIMEOUT_S)
        except BaseException:
            await self.cancel()
            raise
        if self.failure:
            raise self.failure

    async def append_audio(
        self,
        pcm16: bytes,
        *,
        duration_ms: float,
    ) -> None:
        if self.closed or self.stopping or not pcm16:
            return
        self.sent_audio_ms += max(0.0, duration_ms)
        upstream_pcm = _resample_pcm16(pcm16, 16_000, self.profile.sample_rate)
        payload: dict[str, Any] = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(upstream_pcm).decode("ascii"),
        }
        if self.profile.name in {
            "qwen-asr-server-vad",
            "stepfun-asr-server-vad",
        }:
            payload["event_id"] = f"append_{self.stream_id}_{int(self.sent_audio_ms)}"
        await self._send(payload)

    async def finish(self) -> str:
        if self.closed:
            return self.aggregate_transcript()
        self.stopping = True
        if self.profile.name == "qwen-asr-server-vad":
            await self._send(
                {
                    "event_id": f"finish_{self.stream_id}",
                    "type": "session.finish",
                }
            )
        elif self.profile.name == "stepfun-asr-server-vad":
            if self.finals and not self.partials:
                # Server VAD already finalized the last utterance while the
                # microphone was open. There is no pending buffer to commit.
                self.finished.set()
            elif self.sent_audio_ms >= 100:
                # StepFun's server-VAD ASR doesn't finalize a partial merely
                # because the client sends commit. Feed one second of PCM
                # silence so it emits speech_stopped + completed immediately.
                silence_samples = (
                    self.profile.sample_rate * _STEPFUN_FINAL_SILENCE_MS // 1000
                )
                await self._send(
                    {
                        "event_id": f"silence_{self.stream_id}",
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(
                            b"\x00\x00" * silence_samples
                        ).decode("ascii"),
                    }
                )
            else:
                self.finished.set()
        elif self.sent_audio_ms >= 100:
            await self._send({"type": "input_audio_buffer.commit"})
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=_FINISH_TIMEOUT_S)
        except TimeoutError:
            logger.debug(
                "voice realtime finish timeout stream_id={} provider={} text_chars={}",
                self.stream_id,
                self.config.provider,
                len(self.aggregate_transcript()),
            )
        text = self.aggregate_transcript()
        await self.cancel()
        if self.failure and not text:
            raise self.failure
        return text

    async def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        socket = self.socket
        self.socket = None
        if socket is not None:
            await socket.close()
        task = self.reader_task
        self.reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.socket is None:
            raise RuntimeError("realtime ASR socket is not connected")
        await self.socket.send(json.dumps(payload, ensure_ascii=False))

    def _session_update(self) -> dict[str, Any]:
        language = self.config.language
        if self.profile.name == "qwen-asr-server-vad":
            return {
                "event_id": f"session_update_{self.stream_id}",
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "input_audio_format": "pcm",
                    "sample_rate": self.profile.sample_rate,
                    "input_audio_transcription": (
                        {"language": language} if language else {}
                    ),
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.0,
                        "silence_duration_ms": 400,
                    },
                },
            }
        if self.profile.name == "stepfun-asr-server-vad":
            transcription: dict[str, Any] = {
                "model": self.profile.model,
                "prompt": "请准确记录用户所说的内容。",
                "full_rerun_on_commit": True,
                "enable_itn": True,
            }
            if language:
                transcription["language"] = language
            return {
                "event_id": f"session_update_{self.stream_id}",
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": {
                            "format": {
                                "type": "pcm",
                                "codec": "pcm_s16le",
                                "rate": self.profile.sample_rate,
                                "bits": 16,
                                "channel": 1,
                            },
                            "transcription": transcription,
                            "turn_detection": {
                                "type": "server_vad",
                                "silence_duration_ms": 800,
                                "threshold": 0.5,
                            },
                        }
                    }
                },
            }
        transcription: dict[str, Any] = {"model": self.profile.model}
        if language:
            transcription["language"] = language
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": self.profile.sample_rate,
                        },
                        "transcription": transcription,
                        "turn_detection": None,
                    }
                },
            },
        }

    async def _read_messages(self) -> None:
        socket = self.socket
        if socket is None:
            return
        try:
            async for raw in socket:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            self.ready.set()
            self.finished.set()
        finally:
            if not self.closed and not self.stopping and self.failure is None:
                self.failure = RuntimeError("realtime ASR connection interrupted")
            self.ready.set()
            self.finished.set()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "session.updated":
            self.ready.set()
            return
        if event_type == "conversation.item.input_audio_transcription.delta":
            item_id = event.get("item_id")
            delta = event.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                self._register_item(item_id)
                self.partials[item_id] = f"{self.partials.get(item_id, '')}{delta}"
                await self._emit("partial")
            elif (
                self.profile.name == "stepfun-asr-server-vad"
                and isinstance(item_id, str)
                and isinstance(event.get("text"), str)
            ):
                text = event["text"]
                stash = event.get("stash")
                self._register_item(item_id)
                if self.profile.model == "step-asr-1.1-stream":
                    self.partials[item_id] = f"{self.partials.get(item_id, '')}{text}"
                else:
                    self.partials[item_id] = (
                        f"{text}{stash if isinstance(stash, str) else ''}"
                    )
                await self._emit("partial")
            return
        if event_type == "conversation.item.input_audio_transcription.text":
            item_id = event.get("item_id")
            text = event.get("text")
            if isinstance(item_id, str) and isinstance(text, str):
                stash = event.get("stash")
                self._register_item(item_id)
                self.partials[item_id] = f"{text}{stash if isinstance(stash, str) else ''}"
                await self._emit("partial")
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            item_id = event.get("item_id")
            transcript = event.get("transcript")
            if not isinstance(transcript, str):
                transcript = event.get("text")
            if isinstance(item_id, str) and isinstance(transcript, str):
                self._register_item(item_id)
                self.finals[item_id] = transcript
                self.partials.pop(item_id, None)
                await self._emit("stable")
                if self.stopping and self.profile.name in {
                    "openai-transcription-manual",
                    "stepfun-asr-server-vad",
                }:
                    self.finished.set()
            return
        if event_type == "session.finished":
            self.finished.set()
            return
        if event_type in {
            "error",
            "conversation.item.input_audio_transcription.failed",
        }:
            message = _upstream_error_message(event)
            self.failure = RuntimeError(message)
            self.ready.set()
            self.finished.set()

    def _register_item(self, item_id: str) -> None:
        if item_id not in self.item_order:
            self.item_order.append(item_id)

    def aggregate_transcript(self) -> str:
        return "".join(
            self.finals.get(item_id, self.partials.get(item_id, ""))
            for item_id in self.item_order
        ).strip()

    async def _emit(self, kind: str) -> None:
        text = self.aggregate_transcript()
        if not text or (text == self.last_emitted and kind == self.last_emitted_kind):
            return
        self.last_emitted = text
        self.last_emitted_kind = kind
        await self.on_transcript(kind, text)


def _upstream_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(event.get("message"), str):
        return event["message"]
    return "realtime ASR failed"


def _resample_pcm16(data: bytes, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or len(data) < 4:
        return data
    sample_count = len(data) // 2
    samples = struct.unpack(f"<{sample_count}h", data[: sample_count * 2])
    output_count = max(1, round(sample_count * target_rate / source_rate))
    output: list[int] = []
    ratio = source_rate / target_rate
    for index in range(output_count):
        source = index * ratio
        left = min(int(source), sample_count - 1)
        right = min(left + 1, sample_count - 1)
        fraction = source - left
        value = round(samples[left] + (samples[right] - samples[left]) * fraction)
        output.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{len(output)}h", *output)

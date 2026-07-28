"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import ssl
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Self

from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.security.project_context import PROJECT_CONTEXT_METADATA_KEY
from nanobot.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeError,
)
from nanobot.session.goal_state import goal_state_ws_blob
from nanobot.session.webui_turns import websocket_turn_wall_started_at
from nanobot.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanobot.webui.cli_apps_api import normalize_cli_app_mentions
from nanobot.webui.expert_teams import (
    EXPERT_TEAM_SESSION_KEY,
    ExpertTeamError,
    expert_team_mcp_attachments,
    normalize_expert_team_binding,
    public_expert_team_binding,
)
from nanobot.webui.forking import handle_webui_fork_chat
from nanobot.webui.gateway_services import GatewayServices
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.interactive_prompt import (
    INBOUND_META_INTERACTIVE_PROMPT_ANSWER,
    OUTBOUND_META_INTERACTIVE_PROMPT,
    normalize_interactive_prompt,
    normalize_interactive_prompt_answer,
)
from nanobot.webui.mcp_presets_api import normalize_mcp_preset_mentions
from nanobot.webui.session_artifacts import (
    explicit_artifact_row,
    registered_artifact_row,
)
from nanobot.webui.transcription_ws import webui_transcription_event
from nanobot.webui.websocket_logging import websockets_server_logger


def normalize_skill_scope(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key in ("project_bound_user_skills", "explicit_skills"):
        value = raw.get(key)
        if not isinstance(value, list):
            continue
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        normalized[key] = names
    return normalized


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      nanobot and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Nanobot-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("unix_socket_path")
    @classmethod
    def unix_socket_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("unix_socket_path must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("unix_socket_path must be an absolute path")
        return str(path)

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


def _interactive_prompt_answer_content(answer: dict[str, Any]) -> str:
    """Build a fallback user-visible answer text from a structured prompt answer."""
    answer_type = answer.get("answerType")
    if answer_type == "group":
        rows: list[str] = []
        for item in answer.get("answers", []):
            if not isinstance(item, dict):
                continue
            question_id = item.get("questionId")
            text = item.get("text")
            if isinstance(question_id, str) and isinstance(text, str) and text.strip():
                rows.append(f"Q: {question_id} A: {text.strip()}")
        return "\n\n".join(rows)
    if answer_type == "option":
        option_id = answer.get("optionId")
        if isinstance(option_id, str) and option_id.strip():
            return f"Selected option: {option_id.strip()}"
    if answer_type == "skip":
        return "Skipped interactive prompt."
    return ""


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        gateway: GatewayServices,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None

        self.gateway = gateway
        self._http_router = gateway.http
        self._tokens = gateway.tokens
        self._media = gateway.media
        self._transcripts = gateway.transcripts
        self._workspaces = gateway.workspaces

        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}
        self._resumable_streams: dict[str, tuple[str, str]] = {}
        # Expert-team member activity used to exist only as transient socket
        # frames. Keep a compact run projection so status transitions can also
        # be appended to the WebUI journal and survive chat switches/reconnects.
        self._team_runs: dict[tuple[str, str], dict[str, Any]] = {}

    # -- Subscription bookkeeping -------------------------------------------

    def _workspace_controls_available(self, connection: Any) -> bool:
        return self._http_router.workspace_controls_available(connection)

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        if self.gateway.session_manager is None:
            return
        row = self.gateway.session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    def _session_expert_team(self, chat_id: str) -> dict[str, Any] | None:
        if self.gateway.session_manager is None:
            return None
        row = self.gateway.session_manager.read_session_file(f"websocket:{chat_id}")
        metadata = row.get("metadata") if isinstance(row, dict) else None
        if not isinstance(metadata, dict):
            return None
        return public_expert_team_binding(metadata.get(EXPERT_TEAM_SESSION_KEY))

    def _bind_expert_team(
        self,
        chat_id: str,
        raw: Any,
    ) -> dict[str, Any] | None:
        """Validate and persist a team binding without trusting client runtime fields."""
        requested = normalize_expert_team_binding(raw) if raw is not None else None
        if self.gateway.session_manager is None:
            return requested
        session = self.gateway.session_manager.get_or_create(f"websocket:{chat_id}")
        existing = session.metadata.get(EXPERT_TEAM_SESSION_KEY)
        existing_id = existing.get("id") if isinstance(existing, dict) else None
        if requested is not None and existing_id not in (None, requested["id"]):
            raise ExpertTeamError("expert team cannot be changed for an existing session", status=409)
        binding = requested
        if binding is None and isinstance(existing_id, str):
            binding = normalize_expert_team_binding({"id": existing_id})
        if binding is not None and existing != binding:
            session.metadata[EXPERT_TEAM_SESSION_KEY] = binding
            self.gateway.session_manager.save(session)
        return binding

    def _set_expert_team(
        self,
        chat_id: str,
        raw: Any,
    ) -> dict[str, Any] | None:
        """Explicitly replace or clear a persisted expert-team binding."""
        binding = normalize_expert_team_binding(raw) if raw is not None else None
        if self.gateway.session_manager is None:
            return binding
        session = self.gateway.session_manager.get_or_create(f"websocket:{chat_id}")
        existing = session.metadata.get(EXPERT_TEAM_SESSION_KEY)
        if binding is None:
            if EXPERT_TEAM_SESSION_KEY in session.metadata:
                session.metadata.pop(EXPERT_TEAM_SESSION_KEY, None)
                self.gateway.session_manager.save(session)
            return None
        if existing != binding:
            session.metadata[EXPERT_TEAM_SESSION_KEY] = binding
            self.gateway.session_manager.save(session)
        return binding

    def _start_team_run_projection(
        self,
        chat_id: str,
        *,
        run_id: str,
        team_id: str,
        team_name: str,
        members: list[dict[str, Any]],
    ) -> None:
        staged_members = [member for member in members if member.get("phase")]
        first_phase = staged_members[0].get("phase") if staged_members else None
        first_phase_count = (
            sum(1 for member in staged_members if member.get("phase") == first_phase)
            if first_phase
            else len(members)
        )
        normalized_members: list[dict[str, Any]] = []
        for member in members:
            member_id = str(member.get("id") or "").strip()
            if not member_id:
                continue
            running = first_phase is None or member.get("phase") == first_phase
            normalized_members.append({
                "id": member_id,
                "name": str(member.get("name") or member_id),
                "framework": str(member.get("framework") or "").strip(),
                "description": str(member.get("description") or "").strip(),
                "phase": str(member.get("phase") or "").strip(),
                "status": "running" if running else "pending",
                "member_status": "running" if running else "pending",
                "activity": (
                    str(member.get("description") or "").strip()
                    or "团队已启动，正在分配研究任务"
                ),
            })
        self._team_runs[(chat_id, run_id)] = {
            "team_id": team_id,
            "team_name": team_name,
            "members": normalized_members,
            "revision": 0,
            "turn_id": self.gateway.state.active_turn_id(f"websocket:{chat_id}"),
            "stage": "members",
            "status": "running",
            "note": (
                f"{len(normalized_members)} 位专家将分阶段协作，"
                f"首阶段 {first_phase_count} 位并行研究"
                if first_phase and first_phase_count < len(normalized_members)
                else f"{len(normalized_members)} 位专家正在并行研究"
            ),
            "completed": False,
        }

    def _persist_team_run_projection(
        self,
        chat_id: str,
        run_id: str,
        *,
        activity: str,
    ) -> None:
        run = self._team_runs.get((chat_id, run_id))
        if run is None:
            return
        run["revision"] = int(run.get("revision") or 0) + 1
        stage = str(run.get("stage") or "members")
        run_status = str(run.get("status") or "running")
        completed = stage == "delivered" and run_status == "completed"
        steps: list[dict[str, Any]] = []
        for member in run.get("members", []):
            if not isinstance(member, dict):
                continue
            title = str(member.get("name") or member.get("id") or "")
            framework = str(member.get("framework") or "").strip()
            if framework:
                title = f"{title} · {framework}"
            member_status = str(member.get("member_status") or "")
            steps.append({
                "id": str(member.get("id") or ""),
                "title": title,
                "detail": str(member.get("activity") or member.get("description") or ""),
                "status": str(member.get("status") or "pending"),
                "kind": "member",
                "stage_key": str(member.get("phase") or "members"),
                **(
                    {"warning": str(member.get("activity") or "成员结果已降级")}
                    if member_status in {"failed", "cancelled"}
                    else {}
                ),
            })
        terminal_step_status = (
            "error" if run_status == "failed"
            else "interrupted" if run_status in {"cancelled", "interrupted"}
            else None
        )
        lead_status = (
            "completed" if stage in {"audit", "delivered"}
            else "running" if stage == "synthesis"
            else terminal_step_status if terminal_step_status is not None
            else "pending"
        )
        audit_status = (
            "completed" if stage == "delivered"
            else "running" if stage == "audit"
            else terminal_step_status if terminal_step_status is not None
            else "pending"
        )
        steps.extend([
            {
                "id": "team-lead",
                "title": "主笔交叉质证与汇总",
                "detail": (
                    "已完成成员结论的交叉质证与汇总"
                    if stage in {"audit", "delivered"}
                    else "正在交叉质证并汇总各成员结论"
                    if stage == "synthesis"
                    else "等待各位专家交付后进行交叉质证"
                ),
                "status": lead_status,
                "kind": "synthesis",
                "stage_key": "synthesis",
            },
            {
                "id": "report-audit",
                "title": "报告审校与交付",
                "detail": (
                    "最终报告已完成审校并交付"
                    if completed
                    else "正在核验关键结论并检查报告产物"
                    if stage == "audit"
                    else "等待交叉质证完成后核验关键结论并生成报告"
                ),
                "status": audit_status,
                "kind": "audit",
                "stage_key": "audit",
            },
        ])
        active_step_ids = [
            str(step["id"]) for step in steps if step.get("status") == "running"
        ]
        current_step_id = active_step_ids[0] if len(active_step_ids) == 1 else None
        turn_id = str(
            run.get("turn_id")
            or self.gateway.state.active_turn_id(f"websocket:{chat_id}")
            or ""
        ).strip()
        if turn_id:
            run["turn_id"] = turn_id
        payload = {
            "event": "message",
            "chat_id": chat_id,
            "kind": "progress",
            "text": activity,
            "agent_ui": {
                "kind": "task_progress",
                "plan_id": f"plan:{turn_id or run_id}",
                "turn_id": turn_id,
                "plan_kind": "workflow",
                "owner": f"expert_team:{run.get('team_id') or ''}",
                "policy": "required",
                "execution": "staged",
                "status": run_status,
                "revision": int(run["revision"]),
                "active_step_ids": active_step_ids,
                "steps": steps,
                "note": str(run.get("note") or ""),
                "current_step_id": current_step_id,
                "stage_key": stage,
                "team_name": str(run.get("team_name") or "专家团队"),
                "team_id": str(run.get("team_id") or ""),
                "team_run_id": run_id,
            },
        }
        if turn_id:
            payload["turn_id"] = turn_id
        try:
            self._transcripts.append(chat_id, payload)
        except Exception:
            self.logger.exception(
                "failed to persist expert-team progress chat_id={} run_id={}",
                chat_id,
                run_id,
            )

    async def _send_turn_plan_snapshot(
        self,
        chat_id: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Broadcast the canonical SQLite plan projection for one turn."""
        resolved_turn_id = (
            str(turn_id or "").strip()
            or str(
                self.gateway.state.active_turn_id(f"websocket:{chat_id}") or ""
            ).strip()
        )
        if not resolved_turn_id:
            return
        plan = self.gateway.state.turn_plan_snapshot(
            session_key=f"websocket:{chat_id}",
            turn_id=resolved_turn_id,
        )
        if plan is None:
            return
        status = str(plan.get("status") or "")
        event = (
            "turn_plan_terminalized"
            if status in {"completed", "failed", "interrupted"}
            else "turn_plan_created"
            if int(plan.get("revision") or 0) == 1
            else "turn_plan_updated"
        )
        body = {
            "schema_version": 3,
            "event": event,
            "event_id": (
                f"plan:{plan.get('id')}:revision:{plan.get('revision')}"
            ),
            "chat_id": chat_id,
            "project_id": plan.get("project_id"),
            "session_id": plan.get("session_id"),
            "turn_id": resolved_turn_id,
            "plan": plan,
        }
        raw = json.dumps(body, ensure_ascii=False)
        for connection in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(connection, raw, label=" turn_plan ")

    async def _advance_team_run_from_tool_events(
        self,
        chat_id: str,
        metadata: dict[str, Any],
        tool_events: Any,
    ) -> None:
        run_id = str(metadata.get("expert_team_run_id") or "").strip()
        run = self._team_runs.get((chat_id, run_id))
        if run is None or not isinstance(tool_events, list):
            return
        successful_names = {
            str(event.get("name") or "").strip().lower()
            for event in tool_events
            if isinstance(event, dict)
            and str(event.get("status") or "") in {"ok", "completed", "succeeded"}
        }
        if not successful_names:
            return
        stage = str(run.get("stage") or "members")
        delivery_activity = any(
            (
                name == "write_file"
                or name.startswith("create_")
                or name.startswith("render_")
                or "audit" in name
            )
            for name in successful_names
        )
        if stage == "synthesis" and delivery_activity:
            run["stage"] = "audit"
            run["note"] = "主笔汇总已完成，正在进行报告审校与交付"
            self._persist_team_run_projection(
                chat_id,
                run_id,
                activity="主笔交叉质证完成，进入报告审校与交付",
            )
            await self._send_turn_plan_snapshot(
                chat_id,
                turn_id=str(run.get("turn_id") or "") or None,
            )

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to the HTTP handler or WS upgrade."""
        got, query = _parse_request_path(request.path)

        # WebSocket upgrade — channel handles this itself
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # Everything else goes to the HTTP handler
        return await self._http_router.dispatch(connection, request)

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._tokens.take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._tokens.take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._tokens.take_issued_token_if_valid(supplied)
        return None

    # -- Server lifecycle and connection ingress ---------------------------

    async def start(self) -> None:
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")
        ws_logger = websockets_server_logger()

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}",
                (
                    f"unix:{self.config.unix_socket_path}{_normalize_config_path(self.config.token_issue_path)}"
                    if self.config.unix_socket_path
                    else (
                        f"{scheme}://{self.config.host}:{self.config.port}"
                        f"{_normalize_config_path(self.config.token_issue_path)}"
                    )
                ),
            )

        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    logger=ws_logger,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                    logger=ws_logger,
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                        "agent_ready": self._http_router.agent_ready(),
                        "mcp_status": self._http_router.mcp_status(),
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket already authenticates at handshake time (token),
                # so pairing is not applicable. Treat as non-DM to avoid
                # sending pairing codes to an already-authenticated client.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    # -- Inbound WebSocket envelopes ---------------------------------------

    def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound WebUI envelope."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            try:
                expert_team = self._bind_expert_team(new_id, envelope.get("expert_team"))
            except ExpertTeamError as exc:
                await self._send_event(connection, "error", detail="expert_team_rejected", reason=exc.message)
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_new_chat(
                    envelope,
                    controls_available=self._workspace_controls_available(connection),
                ),
            )
            if scope is None:
                return
            binding = await self._persist_workspace_scope_or_error(
                connection,
                new_id,
                scope,
            )
            if binding is None:
                return
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=new_id,
                scope="metadata",
                workspace_scope=scope.payload(),
                expert_team=public_expert_team_binding(expert_team),
                **binding,
            )
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "fork_chat":
            await handle_webui_fork_chat(self, connection, envelope)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "set_expert_team":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if (
                websocket_turn_wall_started_at(cid) is not None
                and envelope.get("expert_team") is not None
            ):
                await self._send_event(
                    connection,
                    "error",
                    chat_id=cid,
                    detail="expert_team_rejected",
                    reason="chat_running",
                )
                return
            try:
                expert_team = self._set_expert_team(cid, envelope.get("expert_team"))
            except ExpertTeamError as exc:
                await self._send_event(
                    connection,
                    "error",
                    chat_id=cid,
                    detail="expert_team_rejected",
                    reason=exc.message,
                )
                return
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                expert_team=public_expert_team_binding(expert_team),
            )
            return
        if t == "set_workspace_scope":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_set_request(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return
            binding = await self._persist_workspace_scope_or_error(connection, cid, scope)
            if binding is None:
                return
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                workspace_scope=scope.payload(),
                **binding,
            )
            return
        if t == "transcribe_audio":
            event, payload = await webui_transcription_event(envelope)
            await self._send_event(connection, event, **payload)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            interactive_prompt_answer = normalize_interactive_prompt_answer(
                envelope.get(INBOUND_META_INTERACTIVE_PROMPT_ANSWER)
            )
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str) and interactive_prompt_answer is None:
                await self._send_event(connection, "error", detail="missing content")
                return
            if not isinstance(content, str):
                content = ""
            try:
                expert_team = self._bind_expert_team(cid, envelope.get("expert_team"))
            except ExpertTeamError as exc:
                await self._send_event(
                    connection,
                    "error",
                    chat_id=cid if _is_valid_chat_id(cid) else None,
                    detail="expert_team_rejected",
                    reason=exc.message,
                )
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and interactive_prompt_answer is not None:
                content = _interactive_prompt_answer_content(interactive_prompt_answer)
            if not content.strip() and not media_paths and interactive_prompt_answer is None:
                await self._send_event(connection, "error", detail="missing content")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_message(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
                metadata.update(self._transcripts.client_turn_metadata(envelope.get("turn_id")))
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            requested_mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            team_mcp_presets = (
                expert_team_mcp_attachments(expert_team)
                if expert_team is not None and not content.strip().startswith("/")
                else []
            )
            mcp_presets = normalize_mcp_preset_mentions([
                *requested_mcp_presets,
                *team_mcp_presets,
            ])
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            skill_scope = normalize_skill_scope(envelope.get("skill_scope"))
            if skill_scope:
                metadata["skill_scope"] = skill_scope
            is_team_run = expert_team is not None and not content.strip().startswith("/")
            if expert_team is not None:
                metadata[EXPERT_TEAM_SESSION_KEY] = expert_team
            if is_team_run:
                metadata["expert_team_run_id"] = uuid.uuid4().hex[:12]
            if interactive_prompt_answer:
                metadata[INBOUND_META_INTERACTIVE_PROMPT_ANSWER] = interactive_prompt_answer
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            binding = await self._persist_workspace_scope_or_error(connection, cid, scope)
            if binding is None:
                return
            metadata[PROJECT_CONTEXT_METADATA_KEY] = {
                **binding,
                "session_key": f"websocket:{cid}",
            }
            image_generation = envelope.get("image_generation")
            if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
                aspect_ratio = image_generation.get("aspect_ratio")
                metadata["image_generation"] = {
                    "enabled": True,
                    "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
                }
            if metadata.get("webui") is True and self.is_allowed(client_id):
                self._transcripts.append_user_message(
                    cid,
                    content,
                    metadata=metadata,
                    media_paths=media_paths or None,
                    cli_apps=cli_apps or None,
                    mcp_presets=mcp_presets or None,
                )
            if is_team_run:
                run_id = metadata["expert_team_run_id"]
                team_id = expert_team["id"]
                team_name = expert_team["name"]
                members = expert_team.get("members", [])
                self._start_team_run_projection(
                    cid,
                    run_id=run_id,
                    team_id=team_id,
                    team_name=team_name,
                    members=members,
                )
                await self._send_event(
                    connection,
                    "team_run_started",
                    chat_id=cid,
                    run_id=run_id,
                    team_id=team_id,
                    team_name=team_name,
                    members=members,
                )
                self._persist_team_run_projection(
                    cid,
                    run_id,
                    activity=f"{team_name}已启动",
                )
                await self._send_turn_plan_snapshot(
                    cid,
                    turn_id=str(
                        self._team_runs.get((cid, run_id), {}).get("turn_id") or ""
                    ) or None,
                )
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _workspace_scope_or_error(
        self,
        connection: Any,
        resolver: Callable[[], Any],
        *,
        chat_id: str | None = None,
    ) -> Any | None:
        try:
            return resolver()
        except WorkspaceScopeError as exc:
            await self._send_event(
                connection,
                "error",
                detail="workspace_scope_rejected",
                reason=exc.message,
                **({"chat_id": chat_id} if chat_id else {}),
            )
            return None

    async def _persist_workspace_scope_or_error(
        self,
        connection: Any,
        chat_id: str,
        scope: Any,
    ) -> dict[str, str] | None:
        try:
            return self._workspaces.persist_scope(chat_id, scope)
        except WorkspaceScopeError as exc:
            state_session = self.gateway.state.get_session(f"websocket:{chat_id}")
            await asyncio.to_thread(
                self.gateway.logs.write,
                level="warning",
                component="projects",
                event_name="project_scope_rejected",
                message="session project scope change was rejected",
                project_id=state_session.project_id if state_session else None,
                session_id=state_session.id if state_session else None,
                error_code="SESSION_PROJECT_MISMATCH",
                details={
                    "chat_id": chat_id,
                    "reason": exc.message,
                },
            )
            await self._send_event(
                connection,
                "error",
                chat_id=chat_id,
                detail="workspace_scope_rejected",
                reason=exc.message,
            )
            return None

    # -- Outbound WebSocket events -----------------------------------------

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
                self.logger.debug("server task was already cancelled during shutdown")
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._tokens.clear()
        self._stream_text_buffers.clear()
        self._resumable_streams.clear()
        self._team_runs.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_runtime_status_updated"):
            await self.send_runtime_status_updated(
                agent_ready=msg.metadata.get("agent_ready"),
                mcp_status=msg.metadata.get("mcp_status"),
            )
            return
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return
        if msg.metadata.get("_turn_lifecycle_started"):
            turn = msg.metadata.get("turn")
            if isinstance(turn, dict):
                await self.send_turn_lifecycle_started(
                    msg.chat_id,
                    turn=turn,
                    snapshot_revision=int(msg.metadata.get("snapshot_revision") or 0),
                    metadata=msg.metadata,
                )
            return
        if msg.metadata.get("_turn_lifecycle_completed"):
            turn = msg.metadata.get("turn")
            if isinstance(turn, dict):
                await self.send_turn_lifecycle_completed(
                    msg.chat_id,
                    turn=turn,
                    snapshot_revision=int(msg.metadata.get("snapshot_revision") or 0),
                    metadata=msg.metadata,
                )
            return
        if msg.metadata.get("_thread_runtime_status_changed"):
            snapshot = msg.metadata.get("runtime_snapshot")
            if isinstance(snapshot, dict):
                await self.send_thread_runtime_status_changed(msg.chat_id, snapshot)
            return
        if msg.metadata.get("_team_member_updated"):
            member = msg.metadata.get("team_member")
            if isinstance(member, dict):
                await self.send_team_member_updated(msg.chat_id, member)
            return
        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            if (
                msg.metadata.get("_progress")
                or msg.metadata.get("_file_edit_events")
                or msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
                or msg.metadata.get("_goal_state_sync")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
        if msg.metadata.get("_goal_state_sync"):
            if conns:
                blob = msg.metadata.get("goal_state")
                await self.send_goal_state(msg.chat_id, blob if isinstance(blob, dict) else {"active": False})
            return
        if msg.metadata.get("_goal_status"):
            if conns:
                status = msg.metadata.get("goal_status")
                if status in ("running", "idle"):
                    started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                    await self.send_goal_status(
                        msg.chat_id,
                        status,
                        started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                    )
            return
        # Signal that the agent has fully finished processing the current turn.
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            team = msg.metadata.get(EXPERT_TEAM_SESSION_KEY)
            run_id = msg.metadata.get("expert_team_run_id")
            if isinstance(team, dict) and isinstance(run_id, str):
                await self.send_team_run_completed(
                    msg.chat_id,
                    run_id=run_id,
                    team_id=str(team.get("id") or ""),
                )
            await self.send_turn_end(
                msg.chat_id,
                latency_ms=lat_i,
                goal_state=gs_blob,
                metadata=msg.metadata,
            )
            await self.send_session_updated(msg.chat_id, scope="thread")
            return
        if msg.metadata.get("_session_updated"):
            if conns:
                scope = msg.metadata.get("_session_update_scope")
                await self.send_session_updated(
                    msg.chat_id,
                    scope=scope if isinstance(scope, str) else None,
                )
            return
        if msg.metadata.get("_file_edit_events"):
            edits = msg.metadata.get("_file_edit_events")
            await self.send_file_edit_events(
                msg.chat_id,
                edits if isinstance(edits, list) else [],
                msg.metadata,
            )
            return
        if msg.metadata.get("_narration_end"):
            await self.send_narration_end(msg.chat_id, msg.metadata)
            return
        if msg.metadata.get("_narration_delta"):
            await self.send_narration_delta(msg.chat_id, msg.content, msg.metadata)
            return
        await self._advance_team_run_from_tool_events(
            msg.chat_id,
            msg.metadata,
            msg.metadata.get("_tool_events"),
        )
        interactive_prompt = normalize_interactive_prompt(msg.metadata.get(OUTBOUND_META_INTERACTIVE_PROMPT))
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        has_structured_payload = (
            bool(msg.media)
            or bool(msg.metadata.get("_tool_events"))
            or bool(msg.metadata.get("_tool_hint"))
            or bool(msg.metadata.get("_progress"))
            or interactive_prompt is not None
            or agent_ui is not None
        )
        text = msg.content if isinstance(msg.content, str) else ""
        if not text.strip() and not has_structured_payload:
            self.logger.info(
                "suppressing empty websocket outbound chat_id={} metadata_keys={}",
                msg.chat_id,
                sorted(str(key) for key in msg.metadata.keys()),
            )
            return

        wire_text = self._media.rewrite_local_markdown_images(text)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": wire_text,
        }
        if msg.metadata.get("_streamed"):
            # The answer text already arrived through delta/stream_end frames.
            # This authoritative frame exists to add generated artifacts and
            # final metadata, so WebUI clients must replace the streamed
            # bubble instead of appending a duplicate assistant reply.
            payload["replace_stream"] = True
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, Any]] = []
            for entry in msg.media:
                signed = self._media.sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        if interactive_prompt is not None:
            payload["interactive_prompt"] = interactive_prompt
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        phase = "activity" if payload.get("kind") in ("tool_hint", "progress") else "answer"
        self._transcripts.prepare_and_append(
            msg.chat_id,
            payload,
            metadata=msg.metadata,
            phase=phase,
            include_source=True,
            transcript_overrides={"text": text},
        )
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")
        if (
            isinstance(agent_ui, dict)
            and agent_ui.get("kind") == "task_progress"
        ):
            await self._send_turn_plan_snapshot(
                msg.chat_id,
                turn_id=str(agent_ui.get("turn_id") or "") or None,
            )
        await self._register_output_artifacts(
            msg.chat_id,
            tool_events=msg.metadata.get("_tool_events"),
            media_paths=msg.media,
            metadata=msg.metadata,
            connections=conns,
        )

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_narration_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Commit public pre-tool text to the workbench activity surface.

        Content deltas are provisional until the provider reveals whether tool
        calls follow. ``replaces_stream_id`` lets rich clients move that exact
        provisional bubble into Steps without duplicating it in the answer.
        """
        if not delta:
            return
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        replacement = self._resumable_streams.get(chat_id)
        stream_id = replacement[0] if replacement is not None else meta.get("_stream_id")
        text = self._media.rewrite_local_markdown_images(delta)
        body: dict[str, Any] = {
            "event": "narration_delta",
            "chat_id": chat_id,
            "text": text,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
            body["replaces_stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="activity",
            transcript_overrides={"text": delta},
        )
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" narration ")

    async def send_narration_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close a public narration segment and release its provisional stream."""
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        replacement = self._resumable_streams.pop(chat_id, None)
        stream_id = replacement[0] if replacement is not None else meta.get("_stream_id")
        body: dict[str, Any] = {
            "event": "narration_end",
            "chat_id": chat_id,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
            body["replaces_stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="activity",
        )
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" narration_end ")

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        payload: dict[str, Any] = {
            "event": "file_edit",
            "chat_id": chat_id,
            "edits": edits,
        }
        self._transcripts.prepare_and_append(
            chat_id,
            payload,
            metadata=metadata,
            phase="activity",
        )
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" file_edit ")

        try:
            session_key = f"websocket:{chat_id}"
            scope = self._workspaces.scope_for_session_key(session_key)
            seen_states: set[tuple[str, str]] = set()
            turn_id = (
                str(metadata.get("webui_turn_id"))
                if isinstance(metadata, dict)
                and metadata.get("webui_turn_id")
                else None
            )
            for edit in edits:
                if not isinstance(edit, dict) or edit.get("operation") == "delete":
                    continue
                raw_path = edit.get("absolute_path") or edit.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                relation_type = (
                    "modified"
                    if edit.get("operation") not in {"create", "write"}
                    else "generated"
                )
                tool_call_id = (
                    str(edit.get("call_id"))
                    if edit.get("call_id")
                    else None
                )
                phase = str(edit.get("phase") or "")
                status = str(edit.get("status") or "")
                if phase == "start":
                    record = await asyncio.to_thread(
                        self.gateway.state.stage_artifact,
                        session_key,
                        raw_path,
                        relation_type=relation_type,
                        turn_id=turn_id,
                        tool_call_id=tool_call_id,
                    )
                elif phase == "end" and status == "done":
                    artifact = explicit_artifact_row(
                        raw_path,
                        scope=scope,
                        session_key=session_key,
                    )
                    if artifact is None:
                        continue
                    record = await asyncio.to_thread(
                        self.gateway.state.register_artifact,
                        session_key,
                        scope.project_path / artifact["path"],
                        relation_type=relation_type,
                        artifact_kind=str(artifact.get("kind") or "file"),
                        mime_type=str(
                            artifact.get("mime_type") or "application/octet-stream"
                        ),
                        turn_id=turn_id,
                        tool_call_id=tool_call_id,
                    )
                elif phase in {"end", "error", "cancelled"}:
                    staged = await asyncio.to_thread(
                        self.gateway.state.stage_artifact,
                        session_key,
                        raw_path,
                        relation_type=relation_type,
                        turn_id=turn_id,
                        tool_call_id=tool_call_id,
                    )
                    record = await asyncio.to_thread(
                        self.gateway.state.fail_artifact,
                        staged.id,
                        session_key=session_key,
                        error_code="FILE_EDIT_FAILED",
                        error_message=str(
                            edit.get("error")
                            or edit.get("detail")
                            or "file edit did not complete"
                        ),
                    )
                else:
                    continue
                artifact = registered_artifact_row(record)
                state_key = (record.id, record.status)
                if state_key in seen_states:
                    continue
                await asyncio.to_thread(
                    self.gateway.logs.write,
                    level="info",
                    component="artifacts",
                    event_name="artifact_registered",
                    message="file edit registered as a session artifact",
                    project_id=record.project_id,
                    session_id=record.session_id,
                    artifact_id=record.id,
                    details={
                        "chat_id": chat_id,
                        "relative_path": record.relative_path,
                        "relation": artifact.get("relation"),
                        "status": record.status,
                    },
                )
                seen_states.add(state_key)
                artifact_payload = {
                    "event": "artifact_created",
                    "chat_id": chat_id,
                    "artifact": artifact,
                }
                self._transcripts.prepare_and_append(
                    chat_id,
                    artifact_payload,
                    metadata=metadata,
                    phase="activity",
                )
                artifact_raw = json.dumps(artifact_payload, ensure_ascii=False)
                for connection in conns:
                    await self._safe_send_to(
                        connection,
                        artifact_raw,
                        label=" artifact_created ",
                    )
        except Exception as exc:
            # Artifact discovery is an optional workbench enhancement. A stale
            # workspace or filesystem race must not retry/duplicate the already
            # delivered file_edit frame.
            self.logger.debug(
                "artifact_created discovery failed for chat_id={}",
                chat_id,
                exc_info=True,
            )
            await asyncio.to_thread(
                self.gateway.logs.write,
                level="warning",
                component="artifacts",
                event_name="artifact_registration_failed",
                message="file edit could not be registered as an artifact",
                error_code="ARTIFACT_REGISTRATION_FAILED",
                details={
                    "chat_id": chat_id,
                    "exception_type": type(exc).__name__,
                },
            )

    async def _register_output_artifacts(
        self,
        chat_id: str,
        *,
        tool_events: Any,
        media_paths: list[str] | None,
        metadata: dict[str, Any] | None,
        connections: list[Any],
    ) -> None:
        """Register files explicitly returned by tools or assistant media.

        File-edit events cover source files written through filesystem tools,
        while generators such as ``create_pdf`` return their output in the
        structured tool event's ``files`` collection. Both are durable session
        artifacts and must feed the same SQLite-backed artifact index.
        """

        candidates: dict[str, tuple[str | None, str | None]] = {}

        def add_entries(entries: Any, tool_call_id: str | None) -> None:
            if not isinstance(entries, list):
                return
            for entry in entries:
                if isinstance(entry, str):
                    raw_path = entry
                    mime_type = None
                elif isinstance(entry, dict):
                    raw_path = entry.get("path")
                    mime_type = entry.get("mime_type")
                else:
                    continue
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                normalized_mime = (
                    mime_type.strip()
                    if isinstance(mime_type, str) and mime_type.strip()
                    else None
                )
                candidates.setdefault(
                    raw_path.strip(),
                    (normalized_mime, tool_call_id),
                )

        if isinstance(tool_events, list):
            for event in tool_events:
                if (
                    not isinstance(event, dict)
                    or event.get("phase") != "end"
                    or event.get("error")
                ):
                    continue
                call_id = (
                    str(event.get("call_id"))
                    if event.get("call_id")
                    else None
                )
                for key in ("files", "artifacts"):
                    add_entries(event.get(key), call_id)
                result = event.get("result")
                if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                    try:
                        result = json.loads(result)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        result = None
                if isinstance(result, dict):
                    for key in ("files", "artifacts"):
                        add_entries(result.get(key), call_id)

        add_entries(media_paths, None)
        if not candidates:
            return

        session_key = f"websocket:{chat_id}"
        if self.gateway.state.get_session(session_key) is None:
            return
        scope = self._workspaces.scope_for_session_key(session_key)
        turn_id = (
            str(metadata.get("webui_turn_id"))
            if isinstance(metadata, dict) and metadata.get("webui_turn_id")
            else None
        )
        existing_ready_ids = {
            record.id
            for record in await asyncio.to_thread(
                self.gateway.state.list_session_artifacts,
                session_key,
            )
            if record.status == "ready"
        }

        for raw_path, (mime_type, tool_call_id) in candidates.items():
            try:
                artifact = explicit_artifact_row(
                    raw_path,
                    scope=scope,
                    session_key=session_key,
                )
                if artifact is None:
                    continue
                record = await asyncio.to_thread(
                    self.gateway.state.register_artifact,
                    session_key,
                    scope.project_path / artifact["path"],
                    relation_type="generated",
                    artifact_kind=str(artifact.get("kind") or "file"),
                    mime_type=mime_type or str(
                        artifact.get("mime_type") or "application/octet-stream"
                    ),
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                )
                if record.id in existing_ready_ids:
                    continue
                existing_ready_ids.add(record.id)
                registered = registered_artifact_row(record)
                await asyncio.to_thread(
                    self.gateway.logs.write,
                    level="info",
                    component="artifacts",
                    event_name="artifact_registered",
                    message="tool output registered as a session artifact",
                    project_id=record.project_id,
                    session_id=record.session_id,
                    artifact_id=record.id,
                    details={
                        "chat_id": chat_id,
                        "relative_path": record.relative_path,
                        "relation": registered.get("relation"),
                        "status": record.status,
                        "tool_call_id": tool_call_id,
                    },
                )
                artifact_payload = {
                    "event": "artifact_created",
                    "chat_id": chat_id,
                    "artifact": registered,
                }
                self._transcripts.prepare_and_append(
                    chat_id,
                    artifact_payload,
                    metadata=metadata,
                    phase="activity",
                )
                artifact_raw = json.dumps(artifact_payload, ensure_ascii=False)
                for connection in connections:
                    await self._safe_send_to(
                        connection,
                        artifact_raw,
                        label=" artifact_created ",
                    )
            except Exception as exc:
                self.logger.debug(
                    "tool output artifact registration failed chat_id={} file={}",
                    chat_id,
                    Path(raw_path).name,
                    exc_info=True,
                )
                await asyncio.to_thread(
                    self.gateway.logs.write,
                    level="warning",
                    component="artifacts",
                    event_name="artifact_registration_failed",
                    message="tool output could not be registered as an artifact",
                    error_code="ARTIFACT_REGISTRATION_FAILED",
                    details={
                        "chat_id": chat_id,
                        "file_name": Path(raw_path).name,
                        "exception_type": type(exc).__name__,
                    },
                )

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        stream_key = (chat_id, str(meta.get("_stream_id") or ""))
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = self._stream_text_buffers.pop(stream_key, [])
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            resuming = meta.get("_resuming") is True
            stream_kind = meta.get("_stream_kind")
            if stream_kind not in {"answer", "narration"}:
                stream_kind = "answer"
            body["resuming"] = resuming
            body["stream_kind"] = stream_kind
            if resuming and stream_kind == "narration" and full_text:
                self._resumable_streams[chat_id] = (stream_key[1], full_text)
            elif not resuming:
                self._resumable_streams.pop(chat_id, None)
            rewritten = self._media.rewrite_local_markdown_images(full_text)
            if delta or rewritten != full_text:
                body["text"] = rewritten
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="answer",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn."""
        conns = list(self._subs.get(chat_id, ()))
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        stop_reason = str((metadata or {}).get("_stop_reason") or "")
        body["finish_reason"] = (
            "cancelled" if stop_reason == "cancelled"
            else "error" if stop_reason in {"error", "tool_error"}
            else "completed"
        )
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        usage = (metadata or {}).get("usage")
        if isinstance(usage, dict):
            body["usage"] = {
                str(key): int(value)
                for key, value in usage.items()
                if isinstance(value, int | float)
            }
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=metadata,
            phase="complete",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_turn_lifecycle_started(
        self,
        chat_id: str,
        *,
        turn: dict[str, Any],
        snapshot_revision: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        body = {
            "event": "turn_started",
            "chat_id": chat_id,
            "snapshot_revision": snapshot_revision,
            "turn": turn,
        }
        if not (metadata or {}).get("_turn_lifecycle_persisted"):
            self._transcripts.prepare_and_append(
                chat_id,
                body,
                metadata=metadata,
                phase="activity",
            )
        raw = json.dumps(body, ensure_ascii=False)
        for connection in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(connection, raw, label=" turn_started ")

    async def send_turn_lifecycle_completed(
        self,
        chat_id: str,
        *,
        turn: dict[str, Any],
        snapshot_revision: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        team = (metadata or {}).get(EXPERT_TEAM_SESSION_KEY)
        run_id = (metadata or {}).get("expert_team_run_id")
        if isinstance(team, dict) and isinstance(run_id, str):
            terminal_status = {
                "completed": "completed",
                "interrupted": "cancelled",
                "failed": "failed",
            }.get(str(turn.get("status") or ""), "failed")
            await self.send_team_run_completed(
                chat_id,
                run_id=run_id,
                team_id=str(team.get("id") or ""),
                status=terminal_status,
            )
        enriched_turn = dict(turn)
        plan = self.gateway.state.turn_plan_snapshot(
            session_key=f"websocket:{chat_id}",
            turn_id=str(turn.get("id") or ""),
        )
        if plan is not None:
            enriched_turn["plan"] = plan
            await self._send_turn_plan_snapshot(
                chat_id,
                turn_id=str(turn.get("id") or "") or None,
            )
        body = {
            "event": "turn_completed",
            "chat_id": chat_id,
            "snapshot_revision": snapshot_revision,
            "turn": enriched_turn,
        }
        turn_id = str(turn.get("id") or "").strip()
        runtime_epoch = str(turn.get("runtime_epoch") or "").strip()
        transcript_overrides = None
        if turn_id and runtime_epoch:
            transcript_overrides = {
                "event_id": f"terminal_{runtime_epoch}_{turn_id}",
            }
        if not (metadata or {}).get("_turn_lifecycle_persisted"):
            self._transcripts.prepare_and_append(
                chat_id,
                body,
                metadata=metadata,
                phase="complete",
                transcript_overrides=transcript_overrides,
            )
        raw = json.dumps(body, ensure_ascii=False)
        for connection in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(connection, raw, label=" turn_completed ")

    async def send_thread_runtime_status_changed(
        self,
        chat_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        active_turn = snapshot.get("active_turn")
        latest_turn = snapshot.get("latest_turn")
        for turn in (active_turn, latest_turn):
            if not isinstance(turn, dict) or not turn.get("id"):
                continue
            plan = self.gateway.state.turn_plan_snapshot(
                session_key=f"websocket:{chat_id}",
                turn_id=str(turn["id"]),
            )
            if plan is not None:
                turn["plan"] = plan
        body = {
            "event": "thread_status_changed",
            "chat_id": chat_id,
            "snapshot_revision": int(snapshot.get("snapshot_revision") or 0),
            "runtime_epoch": snapshot.get("runtime_epoch"),
            "thread_status": snapshot.get("thread_status") or {"type": "notLoaded"},
            "active_turn": active_turn,
            "latest_turn": latest_turn,
        }
        raw = json.dumps(body, ensure_ascii=False)
        for connection in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(connection, raw, label=" thread_status_changed ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """Notify WebUI clients that a session row should refresh."""
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        state_session = self.gateway.state.get_session(f"websocket:{chat_id}")
        if state_session is not None:
            body["session_id"] = state_session.id
            body["project_id"] = state_session.project_id
        expert_team = self._session_expert_team(chat_id)
        if expert_team is not None:
            body["expert_team"] = expert_team
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_team_run_completed(
        self,
        chat_id: str,
        *,
        run_id: str,
        team_id: str,
        status: str = "completed",
    ) -> None:
        run = self._team_runs.get((chat_id, run_id))
        if run is None:
            return
        public_status = (
            status
            if status in {"completed", "completed_with_warnings", "failed", "cancelled"}
            else "failed"
        )
        run["completed"] = public_status in {"completed", "completed_with_warnings"}
        run["status"] = (
            "completed"
            if run["completed"]
            else "cancelled"
            if public_status == "cancelled"
            else "failed"
        )
        run["stage"] = "delivered" if run["completed"] else str(
            run.get("stage") or "members"
        )
        run["note"] = (
            "研究与报告已完成"
            if public_status == "completed"
            else "研究与报告已完成，部分维度采用降级结果"
            if public_status == "completed_with_warnings"
            else "专家团队已由用户停止"
            if public_status == "cancelled"
            else "专家团队执行失败"
        )
        for member in run.get("members", []):
            if not isinstance(member, dict):
                continue
            if run["completed"]:
                member["status"] = "completed"
                if member.get("member_status") not in {"failed", "cancelled"}:
                    member["member_status"] = "completed"
            elif member.get("status") in {"running", "pending"}:
                member["status"] = (
                    "interrupted" if public_status == "cancelled" else "error"
                )
            if not str(member.get("activity") or "").strip():
                member["activity"] = (
                    "研究任务已完成"
                    if run["completed"]
                    else "团队运行已停止"
                )
        turn_id = str(run.get("turn_id") or "").strip()
        if turn_id:
            persisted_plan = self.gateway.state.turn_plan_snapshot(
                session_key=f"websocket:{chat_id}",
                turn_id=turn_id,
            )
            if persisted_plan is not None:
                run["revision"] = max(
                    int(run.get("revision") or 0),
                    int(persisted_plan.get("revision") or 0),
                )
        self._persist_team_run_projection(
            chat_id,
            run_id,
            activity=(
                f"{run.get('team_name') or '专家团队'}已完成"
                if run["completed"]
                else f"{run.get('team_name') or '专家团队'}已停止"
            ),
        )
        await self._send_turn_plan_snapshot(
            chat_id,
            turn_id=str(run.get("turn_id") or "") or None,
        )
        conns = list(self._subs.get(chat_id, ()))
        if conns:
            raw = json.dumps({
                "event": "team_run_completed",
                "chat_id": chat_id,
                "run_id": run_id,
                "team_id": team_id,
                "status": public_status,
            }, ensure_ascii=False)
            for connection in conns:
                await self._safe_send_to(connection, raw, label=" team_run_completed ")
        self._team_runs.pop((chat_id, run_id), None)

    async def send_team_member_updated(self, chat_id: str, member: dict[str, Any]) -> None:
        run_id = str(member.get("run_id") or "")
        run = self._team_runs.get((chat_id, run_id))
        persist_transition = False
        if run is not None:
            member_id = str(member.get("id") or "")
            projected_member = next(
                (
                    item
                    for item in run.get("members", [])
                    if isinstance(item, dict) and item.get("id") == member_id
                ),
                None,
            )
            if projected_member is not None:
                incoming_status = str(member.get("status") or "running")
                previous_status = str(projected_member.get("member_status") or "")
                projected_member["member_status"] = incoming_status
                projected_member["status"] = (
                    "running" if incoming_status == "running" else "completed"
                )
                activity = str(member.get("activity") or "").strip()
                if activity:
                    projected_member["activity"] = activity
                persist_transition = incoming_status != previous_status
            all_members_terminal = bool(run.get("members")) and all(
                isinstance(item, dict)
                and str(item.get("member_status") or "")
                in {"completed", "failed", "cancelled"}
                for item in run.get("members", [])
            )
            if (
                all_members_terminal
                and str(run.get("stage") or "members") == "members"
            ):
                run["stage"] = "synthesis"
                run["note"] = "专家研究已全部交付，主笔正在交叉质证与汇总"
                persist_transition = True
        if persist_transition:
            self._persist_team_run_projection(
                chat_id,
                run_id,
                activity=(
                    str(member.get("activity") or "").strip()
                    or f"{member.get('name') or member.get('id') or '专家'}状态已更新"
                ),
            )
            if run is not None:
                await self._send_turn_plan_snapshot(
                    chat_id,
                    turn_id=str(run.get("turn_id") or "") or None,
                )
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps({
            "event": "team_member_updated",
            "chat_id": chat_id,
            "run_id": run_id,
            "team_id": member.get("team_id"),
            "member": {
                "id": member.get("id"),
                "name": member.get("name"),
                "status": member.get("status"),
                "task_id": member.get("task_id"),
                "activity": member.get("activity"),
            },
        }, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" team_member_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")

    async def send_runtime_status_updated(
        self,
        *,
        agent_ready: Any,
        mcp_status: Any,
    ) -> None:
        """Broadcast startup/runtime readiness without blocking chat traffic."""
        conns = list(self._conn_chats)
        if not conns:
            return
        body = {
            "event": "runtime_status",
            "agent_ready": agent_ready is True,
            "mcp_status": (
                mcp_status
                if mcp_status in {"disabled", "pending", "warming", "ready", "unavailable"}
                else "unknown"
            ),
        }
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_status ")

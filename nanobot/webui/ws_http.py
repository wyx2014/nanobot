"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import re
import sqlite3
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.command.builtin import builtin_command_palette
from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import CronJob, CronSchedule
from nanobot.observability.trace_store import TraceStore
from nanobot.session.manager import _metadata_title
from nanobot.storage.lifecycle import LifecycleRegistry
from nanobot.storage.logs import StructuredLogRecord, StructuredLogStore
from nanobot.storage.session_events import SessionEventFileStore, SessionEventService
from nanobot.storage.state import (
    SessionProjectMismatch,
    StateStore,
    StateStoreError,
)
from nanobot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanobot.webui.expert_teams import EXPERT_TEAM_SESSION_KEY, public_expert_team_binding
from nanobot.webui.file_preview import WebUIFilePreviewError, file_preview_payload
from nanobot.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from nanobot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from nanobot.webui.http_utils import (
    host_for_url as _host_for_url,
)
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)
from nanobot.webui.http_utils import (
    is_localhost as _is_localhost,
)
from nanobot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.session_artifacts import (
    SessionArtifactError,
    artifact_content_type,
    discover_session_artifacts,
    read_session_artifact,
    registered_artifact_row,
    resolve_session_artifact,
)
from nanobot.webui.session_automations import (
    all_automations_payload,
    is_terminal_one_time_automation,
    serialize_automation_jobs,
    session_archive_blocking_jobs,
    session_automation_jobs,
    session_automations_payload,
)
from nanobot.webui.session_list_index import (
    is_webui_sidebar_session_data,
    list_webui_sessions,
)
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.skills_api import webui_skill_detail_payload, webui_skills_payload
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import build_webui_thread_response
from nanobot.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_SLOW_WEBUI_STAGE_LOG_MS = 500
_AUTOMATION_VALUES_HEADER = "X-Nanobot-Automation-Values"

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager


def _decode_api_key(raw_key: str) -> str | None:
    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


def _state_title_for_session_list(title: str, metadata: Any) -> str:
    """Apply generated-title validation to the SQLite projection as well as JSONL."""
    if isinstance(metadata, dict) and metadata.get("title_user_edited") is True:
        return _metadata_title(metadata)
    return _metadata_title({"title": title})


def _default_model_name_from_config() -> str | None:
    try:
        from nanobot.config.loader import load_config
        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config() or ""


def _resolve_runtime_ready(runtime_ready: Callable[[], bool] | None) -> bool:
    if runtime_ready is None:
        return True
    try:
        return runtime_ready() is True
    except Exception as e:
        logger.debug("bootstrap runtime readiness resolver failed: {}", e)
        return False


def _resolve_runtime_mcp_status(runtime_mcp_status: Callable[[], str] | None) -> str:
    if runtime_mcp_status is None:
        return "unknown"
    try:
        status = runtime_mcp_status()
    except Exception as e:
        logger.debug("bootstrap MCP status resolver failed: {}", e)
        return "unknown"
    return status if status in {"disabled", "pending", "warming", "ready", "unavailable"} else "unknown"


# ---------------------------------------------------------------------------
# GatewayHTTPHandler
# ---------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: Any,  # WebSocketConfig
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_ready: Callable[[], bool] | None,
        runtime_mcp_status: Callable[[], str] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUIMediaGateway,
        workspaces: WebUIWorkspaceController,
        state_store: StateStore,
        logs_store: StructuredLogStore,
        trace_store: TraceStore,
        journal_store: SessionEventService,
        lifecycle_registry: LifecycleRegistry,
        session_event_files: SessionEventFileStore,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        cron_service: CronService | None = None,
        cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        thread_runtime_registry: Any | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.runtime_ready = runtime_ready
        self.runtime_mcp_status = runtime_mcp_status
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.workspaces = workspaces
        self.state = state_store
        self.logs = logs_store
        self.traces = trace_store
        self.journal = journal_store
        self.lifecycle = lifecycle_registry
        self.session_event_files = session_event_files
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills = disabled_skills or set()
        self.cron_service = cron_service
        self.cron_pending_job_ids = cron_pending_job_ids
        self.thread_runtime_registry = thread_runtime_registry
        self._log = log
        self._runtime_surface = runtime_surface

        from nanobot.webui.schedule_routes import WebUIScheduleRouter
        from nanobot.webui.settings_api import runtime_capabilities as _rc
        from nanobot.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
        )
        self.schedule_routes = WebUIScheduleRouter(
            cron_service=cron_service,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            logger=self._log,
            state_store=state_store,
            purge_session=self._purge_schedule_run_session,
        )

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    def agent_ready(self) -> bool:
        return _resolve_runtime_ready(self.runtime_ready)

    def mcp_status(self) -> str:
        return _resolve_runtime_mcp_status(self.runtime_mcp_status)

    # -- Token management ---------------------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None

        try:
            response = await self._dispatch_resolved(connection, request, got)
            return response
        finally:
            self._log_slow_http(got, response, started)

    async def _dispatch_resolved(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
    ) -> Any | None:
        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(request, got)
        if response is not None:
            return response

        response = await self.schedule_routes.dispatch(request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Automation routes
        response = await self._dispatch_automation_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = await self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _log_slow_http(self, path: str, response: Any | None, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms < _SLOW_WEBUI_HTTP_LOG_MS:
            return
        if not (path.startswith("/api/") or path == "/webui/bootstrap"):
            return
        status = getattr(response, "status_code", None)
        self._log.warning(
            "slow webui http route path={} status={} duration_ms={}",
            path,
            status if status is not None else "none",
            elapsed_ms,
        )

    def _log_slow_stage(self, route: str, stage: str, started: float) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000)
        if duration_ms < _SLOW_WEBUI_STAGE_LOG_MS:
            return
        self._log.warning(
            "slow webui stage route={} stage={} duration_ms={} execution=event_loop",
            route,
            stage,
            duration_ms,
        )

    async def _run_blocking_stage(
        self,
        route: str,
        stage: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run blocking work while separating queue, work, and loop-resume delay."""
        submitted = time.perf_counter()
        worker_started: float | None = None
        worker_ended: float | None = None
        outcome = "ok"

        def invoke() -> Any:
            nonlocal worker_started, worker_ended
            worker_started = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                worker_ended = time.perf_counter()

        try:
            return await asyncio.to_thread(invoke)
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            resumed = time.perf_counter()
            duration_ms = round((resumed - submitted) * 1000)
            if duration_ms >= _SLOW_WEBUI_STAGE_LOG_MS:
                queue_ms = (
                    round((worker_started - submitted) * 1000)
                    if worker_started is not None
                    else duration_ms
                )
                execution_ms = (
                    round((worker_ended - worker_started) * 1000)
                    if worker_started is not None and worker_ended is not None
                    else 0
                )
                resume_delay_ms = (
                    round((resumed - worker_ended) * 1000)
                    if worker_ended is not None
                    else 0
                )
                self._log.warning(
                    "slow webui stage route={} stage={} duration_ms={} "
                    "threadpool_queue_ms={} execution_ms={} event_loop_resume_ms={} "
                    "outcome={}",
                    route,
                    stage,
                    duration_ms,
                    queue_ms,
                    execution_ms,
                    resume_delay_ms,
                    outcome,
                )

    # -- Token issue --------------------------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self._log.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(token_response_payload(token_value, self.config.token_ttl_s))

    # -- Bootstrap ----------------------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            return _http_error(403, "bootstrap is localhost-only")

        if not self.tokens.can_issue(include_api_token=True):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = self.tokens.issue_token(self.config.token_ttl_s, api_token=True)

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        return _http_json_response(
            {
                "token": token,
                "ws_path": expected_path,
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
                "agent_ready": self.agent_ready(),
                "mcp_status": self.mcp_status(),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    # -- Session routes -----------------------------------------------------

    async def _dispatch_session_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/artifacts/([A-Za-z0-9_-]+)/content$", got)
        if m:
            return await self._handle_registered_artifact_content(request, m.group(1))

        m = re.match(r"^/api/artifacts/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_registered_artifact_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/artifacts/content$", got)
        if m:
            return await self._handle_session_artifact_content(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/artifacts$", got)
        if m:
            return await self._handle_session_artifacts(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/thread$", got)
        if m:
            return await self._handle_session_thread(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/runtime-snapshot$", got)
        if m:
            return await self._handle_session_runtime_snapshot(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/turns/([^/]+)/plan$", got)
        if m:
            return self._handle_turn_plan(request, m.group(1), m.group(2))

        m = re.match(r"^/api/sessions/([^/]+)/runtime-diagnostics$", got)
        if m:
            return await self._handle_session_runtime_diagnostics(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/file-preview$", got)
        if m:
            return self._handle_file_preview(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/automations$", got)
        if m:
            return self._handle_session_automations(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/(archive|delete)$", got)
        if m:
            return self._handle_session_archive(
                request,
                m.group(1),
                legacy_delete_route=m.group(2) == "delete",
            )
        m = re.match(r"^/api/sessions/([^/]+)/restore$", got)
        if m:
            return self._handle_session_restore(request, m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)/purge$", got)
        if m:
            return self._handle_session_purge(request, m.group(1))

        return None

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        include_archived = _query_first(
            _parse_query(request.path),
            "include_archived",
        ) in {"1", "true", "yes"}
        payload = await asyncio.to_thread(
            self._sessions_list_payload,
            include_archived=include_archived,
        )
        return _http_json_response(payload)

    def _session_route_context(
        self,
        request: WsRequest,
        key: str,
    ) -> tuple[str, dict[str, Any]] | Response:
        """Resolve an authenticated, disk-backed WebUI session route.

        Runtime snapshot and diagnostics are served during the WebSocket
        server's HTTP opening-handshake hook.  Keep all validation on the HTTP
        handler itself; calling a method that only exists on the channel turns
        an ordinary REST request into a failed WebSocket handshake.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_webui_readable_session_key(decoded_key):
            state_session = self.state.get_session_by_id(decoded_key)
            if state_session is not None:
                decoded_key = state_session.session_key
        if not _is_webui_readable_session_key(decoded_key):
            return _http_error(404, "session not found")
        lifecycle_state = self.lifecycle.session_state(decoded_key)
        if lifecycle_state == "purged":
            return _http_error(404, "session not found")
        state_session = self.state.get_session(decoded_key)
        if lifecycle_state == "archived" or (
            state_session is not None and state_session.status == "archived"
        ):
            return _http_error(410, "session is archived")
        session_data = self.session_manager.read_session_file(decoded_key)
        if not isinstance(session_data, dict):
            return _http_error(404, "session not found")
        state_project = (
            self.state.get_project(state_session.project_id)
            if state_session is not None
            else None
        )
        metadata = session_data.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else None
        scope = self.workspaces.scope_for_session_metadata(
            metadata,
            state_project=state_project,
        )
        metadata_project_id = (
            metadata.get("project_id")
            if isinstance(metadata, dict)
            and isinstance(metadata.get("project_id"), str)
            else None
        )
        project_lifecycle = (
            (self.lifecycle.project(metadata_project_id) if metadata_project_id else None)
            or self.lifecycle.project_for_path(scope.project_path)
            or (
                self.lifecycle.project(state_project.id)
                or self.lifecycle.project_for_path(state_project.canonical_root_path)
                if state_project is not None
                else None
            )
        )
        if project_lifecycle is not None and project_lifecycle.state == "purged":
            return _http_error(404, "session not found")
        if (
            (state_project is not None and state_project.status == "archived")
            or (
                project_lifecycle is not None
                and project_lifecycle.state == "archived"
            )
        ):
            return _http_error(410, "workspace is archived")
        return decoded_key, session_data

    async def _handle_session_thread(
        self,
        request: WsRequest,
        key: str,
    ) -> Response:
        """Return the single, session-partitioned Thread Resource read model."""
        route = "thread"
        stage_started = time.perf_counter()
        context = self._session_route_context(request, key)
        self._log_slow_stage(route, "session_route_context", stage_started)
        if isinstance(context, Response):
            return context
        session_key, session_data = context
        stage_started = time.perf_counter()
        scope = self.workspaces.scope_for_session_key(session_key)
        try:
            state_session = self._ensure_state_session(
                session_key,
                session_data,
                scope,
            )
        except SessionProjectMismatch:
            return _http_error(409, "session_project_mismatch")
        self._log_slow_stage(route, "state_session_bind", stage_started)

        try:
            await self._run_blocking_stage(
                route,
                "journal_recovery",
                self.journal.ensure_recovered,
                session_key,
            )
        except Exception:
            self._log.exception(
                "thread resource journal recovery failed session={}",
                session_key,
            )
            return _http_error(503, "thread projection unavailable")
        recovery_watermark = self.state.projector_watermark(session_key) or {}
        recovery_error = recovery_watermark.get("error")
        if recovery_error is not None:
            await asyncio.to_thread(
                self.logs.write,
                level="error",
                component="recovery",
                event_name="thread_projection_withheld",
                message=(
                    "thread resource was withheld because event recovery did not "
                    "reach a consistent boundary"
                ),
                project_id=state_session.project_id,
                session_id=state_session.id,
                error_code="THREAD_RECOVERY_INCOMPLETE",
                details={"session_key": session_key, "error": recovery_error},
            )
            return _http_error(
                409,
                "conversation recovery is incomplete; progress and artifacts were withheld",
            )

        raw_messages = session_data.get("messages")
        session_messages = (
            [item for item in raw_messages if isinstance(item, dict)]
            if isinstance(raw_messages, list)
            else None
        )
        query = _parse_query(request.path)
        raw_message_limit = _query_first(query, "message_limit")
        try:
            message_limit = int(raw_message_limit) if raw_message_limit else 200
        except ValueError:
            return _http_error(400, "invalid message_limit")
        message_limit = max(1, min(message_limit, 500))
        raw_before_message = _query_first(query, "before_message_event_seq")
        before_message_event_seq: int | None = None
        if raw_before_message is not None and raw_before_message.strip():
            try:
                before_message_event_seq = int(raw_before_message)
            except ValueError:
                return _http_error(400, "invalid before_message_event_seq")
            if before_message_event_seq <= 0:
                return _http_error(400, "invalid before_message_event_seq")
        event_rows = await self._run_blocking_stage(
            route,
            "message_query",
            self.state.session_display_event_envelopes,
            session_key,
            limit=message_limit + 1,
            before_event_seq=before_message_event_seq,
        )
        has_more_messages = len(event_rows) > message_limit
        if has_more_messages:
            event_rows = event_rows[-message_limit:]
        message_before_cursor = (
            int(event_rows[0].get("event_seq") or 0)
            if event_rows
            else None
        )
        if has_more_messages and event_rows and event_rows[0].get("event") != "user":
            oldest_turn_id = str(event_rows[0].get("turn_id") or "").strip()
            if oldest_turn_id:
                user_anchor = await self._run_blocking_stage(
                    route,
                    "message_page_anchor_query",
                    self.state.session_turn_user_event_envelope,
                    session_key,
                    turn_id=oldest_turn_id,
                    before_event_seq=message_before_cursor,
                )
                if user_anchor is not None:
                    event_rows = [user_anchor, *event_rows]
        stage_started = time.perf_counter()
        thread = build_webui_thread_response(
            session_key,
            event_rows=event_rows,
            session_messages=session_messages,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
        ) or {
            "schemaVersion": 3,
            "sessionKey": session_key,
            "messages": [],
            "has_pending_tool_calls": False,
        }
        self._log_slow_stage(route, "thread_payload_build", stage_started)

        if self.thread_runtime_registry is None:
            runtime = {
                "session_key": session_key,
                "runtime_epoch": None,
                "snapshot_revision": 0,
                "thread_status": {"type": "notLoaded"},
                "active_turn": None,
                "latest_turn": None,
            }
        else:
            runtime = (
                await self.thread_runtime_registry.snapshot(session_key)
            ).payload()

        active_turn = runtime.get("active_turn")
        if isinstance(active_turn, dict) and active_turn.get("id"):
            if active_turn.get("project_id") not in {None, state_session.project_id}:
                return _http_error(409, "runtime snapshot project mismatch")
            if active_turn.get("session_id") not in {None, state_session.id}:
                return _http_error(409, "runtime snapshot session mismatch")
            active_plan = self.state.turn_plan_snapshot(
                session_key=session_key,
                turn_id=str(active_turn["id"]),
            )
            if active_plan is not None:
                active_turn["plan"] = active_plan

        latest_turn = self.state.latest_turn_snapshot(session_key)
        plan_turn = active_turn if isinstance(active_turn, dict) else latest_turn
        plan = (
            self.state.turn_plan_snapshot(
                session_key=session_key,
                turn_id=str(plan_turn["id"]),
            )
            if isinstance(plan_turn, dict) and plan_turn.get("id")
            else None
        )
        stage_started = time.perf_counter()
        artifact_migration = await self._ensure_session_artifact_index(
            session_key,
            session_data,
            scope,
            state_session,
        )
        self._log_slow_stage(route, "artifact_index", stage_started)
        artifact_turn = active_turn if isinstance(active_turn, dict) else latest_turn
        artifact_turn_id = (
            str(artifact_turn["id"])
            if isinstance(artifact_turn, dict) and artifact_turn.get("id")
            else None
        )
        artifacts = await self._run_blocking_stage(
            route,
            "artifact_query",
            self.state.list_session_artifacts,
            session_key,
            turn_id=artifact_turn_id,
        )
        watermark = recovery_watermark
        last_event_seq = int(watermark.get("last_event_seq") or 0)

        raw_after = _query_first(query, "after_event_seq")
        after_event_seq = 0
        if raw_after is not None and raw_after.strip():
            try:
                after_event_seq = int(raw_after)
            except ValueError:
                return _http_error(400, "invalid after_event_seq")
            if after_event_seq < 0:
                return _http_error(400, "invalid after_event_seq")
        incremental = (
            await self._run_blocking_stage(
                route,
                "incremental_event_query",
                self.state.session_event_envelopes,
                session_key,
                after_event_seq=after_event_seq,
                limit=501,
            )
            if raw_after is not None
            else []
        )
        has_more = len(incremental) > 500
        incremental = incremental[:500]
        first_seq = (
            int(incremental[0].get("event_seq") or 0)
            if incremental
            else None
        )
        resync_required = bool(
            raw_after is not None
            and (
                after_event_seq > last_event_seq
                or (
                    after_event_seq < last_event_seq
                    and first_seq != after_event_seq + 1
                )
            )
        )

        metadata = session_data.get("metadata")
        expert_team = (
            public_expert_team_binding(metadata.get(EXPERT_TEAM_SESSION_KEY))
            if isinstance(metadata, dict)
            else None
        )
        artifact_revision = self.state.session_artifact_revision(session_key)
        snapshot_revision = (
            last_event_seq * 1_000_000
            + min(artifact_revision, 999_999)
        )
        payload: dict[str, Any] = {
            "schema_version": 3,
            "project_id": state_session.project_id,
            "session_id": state_session.id,
            "session_key": session_key,
            "last_event_seq": last_event_seq,
            "snapshot_revision": snapshot_revision,
            "runtime_snapshot_revision": int(runtime.get("snapshot_revision") or 0),
            "runtime_epoch": runtime.get("runtime_epoch"),
            "thread_status": runtime.get("thread_status") or {"type": "idle"},
            "active_turn": active_turn,
            "latest_turn": latest_turn,
            "messages": thread.get("messages") or [],
            "message_page": {
                "before_event_seq": message_before_cursor,
                "has_more_before": has_more_messages,
                "loaded_message_count": len(thread.get("messages") or []),
            },
            "has_pending_tool_calls": bool(thread.get("has_pending_tool_calls")),
            "plan": plan,
            "artifact_revision": artifact_revision,
            "artifacts": [registered_artifact_row(record) for record in artifacts],
            "artifact_index_truncated": artifact_migration["truncated"],
            "workspace_scope": scope.payload(),
            "from_event_seq": after_event_seq,
            "to_event_seq": (
                int(incremental[-1].get("event_seq") or after_event_seq)
                if incremental
                else after_event_seq
            ),
            "events": incremental,
            "has_more": has_more,
            "resync_required": resync_required,
        }
        if expert_team is not None:
            payload["expert_team"] = expert_team
        return _http_json_response(payload)

    async def _handle_session_runtime_snapshot(
        self,
        request: WsRequest,
        key: str,
    ) -> Response:
        context = self._session_route_context(request, key)
        if isinstance(context, Response):
            return context
        session_key, session_data = context
        scope = self.workspaces.scope_for_session_key(session_key)
        try:
            state_session = self._ensure_state_session(session_key, session_data, scope)
        except SessionProjectMismatch:
            return _http_error(409, "session project mismatch")

        if self.thread_runtime_registry is None:
            runtime_payload = {
                "session_key": session_key,
                "runtime_epoch": None,
                "snapshot_revision": 0,
                "thread_status": {"type": "notLoaded"},
                "active_turn": None,
                "latest_turn": None,
            }
        else:
            snapshot = await self.thread_runtime_registry.snapshot(session_key)
            runtime_payload = snapshot.payload()

        durable_latest = self.state.latest_turn_snapshot(session_key)
        active_turn = runtime_payload.get("active_turn")
        if isinstance(active_turn, dict) and active_turn.get("id"):
            active_project_id = active_turn.get("project_id")
            active_session_id = active_turn.get("session_id")
            if (
                active_project_id not in {None, state_session.project_id}
                or active_session_id not in {None, state_session.id}
            ):
                self._log.error(
                    "runtime snapshot identity mismatch session={} active_turn={}",
                    session_key,
                    active_turn.get("id"),
                )
                return _http_error(409, "runtime snapshot identity mismatch")
            plan = self.state.turn_plan_snapshot(
                session_key=session_key,
                turn_id=str(active_turn["id"]),
            )
            if plan is not None:
                active_turn["plan"] = plan
        # Durable history owns latest_turn. The process registry owns only
        # active_turn and may be ahead of projection for a few milliseconds.
        runtime_payload["latest_turn"] = durable_latest
        latest_turn = runtime_payload.get("latest_turn")
        if isinstance(latest_turn, dict) and latest_turn.get("id"):
            plan = self.state.turn_plan_snapshot(
                session_key=session_key,
                turn_id=str(latest_turn["id"]),
            )
            if plan is not None:
                latest_turn["plan"] = plan
        runtime_payload.update(
            {
                "project_id": state_session.project_id,
                "session_id": state_session.id,
            }
        )
        return _http_json_response(runtime_payload)

    def _handle_turn_plan(
        self,
        request: WsRequest,
        key: str,
        encoded_turn_id: str,
    ) -> Response:
        context = self._session_route_context(request, key)
        if isinstance(context, Response):
            return context
        session_key, _session_data = context
        turn_id = unquote(encoded_turn_id).strip()
        if not turn_id or "/" in turn_id or len(turn_id) > 200:
            return _http_error(400, "invalid turn id")
        plan = self.state.turn_plan_snapshot(
            session_key=session_key,
            turn_id=turn_id,
        )
        if plan is None:
            return _http_error(404, "turn plan not found")
        return _http_json_response({"plan": plan})

    async def _handle_session_runtime_diagnostics(
        self,
        request: WsRequest,
        key: str,
    ) -> Response:
        snapshot_response = await self._handle_session_runtime_snapshot(request, key)
        if snapshot_response.status_code != 200:
            return snapshot_response
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        snapshot = json.loads(bytes(snapshot_response.body).decode("utf-8"))
        latest = self.state.latest_turn_snapshot(decoded_key)
        plan = (
            self.state.turn_plan_snapshot(
                session_key=decoded_key,
                turn_id=str(latest["id"]),
            )
            if isinstance(latest, dict) and latest.get("id")
            else None
        )
        return _http_json_response(
            {
                "runtime_snapshot": snapshot,
                "latest_terminal_turn": latest,
                "latest_turn_plan": plan,
                "projection_counts": self.state.projection_counts(decoded_key),
                "projector_watermark": self.state.projector_watermark(decoded_key),
                "reconciliation": (
                    {
                        "occurred": latest.get("finish_reason") == "gatewayRestarted",
                        "finish_reason": latest.get("finish_reason"),
                    }
                    if isinstance(latest, dict)
                    else {"occurred": False, "finish_reason": None}
                ),
            }
        )

    async def _ensure_session_artifact_index(
        self,
        session_key: str,
        session_data: dict[str, Any],
        scope: Any,
        state_session: Any,
    ) -> dict[str, int | bool]:
        """One-time migration of legacy file references into the registry.

        Thread Resource is now the primary renderer endpoint, so it must run
        the same bounded migration that the compatibility artifacts endpoint
        used to own.  Keeping it in one helper prevents the two reads from
        producing different artifact sets for the same session.
        """
        stats: dict[str, int | bool] = {
            "truncated": False,
            "migrated_count": 0,
            "migration_failures": 0,
            "pruned_count": 0,
        }
        legacy_payload = await asyncio.to_thread(
            discover_session_artifacts,
            session_key,
            session_data,
            scope=scope,
        )
        stats["truncated"] = legacy_payload.get("truncated") is True
        allowed_paths = {
            str(row["path"])
            for row in legacy_payload.get("artifacts", [])
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        }
        stats["pruned_count"] = await asyncio.to_thread(
            self.state.prune_unverified_referenced_artifacts,
            session_key,
            allowed_paths,
        )
        for row in legacy_payload.get("artifacts", []):
            if not isinstance(row, dict):
                continue
            path = row.get("path")
            turn_id = row.get("_turn_id")
            if not isinstance(path, str) or not isinstance(turn_id, str):
                continue
            await asyncio.to_thread(
                self.state.assign_referenced_artifact_turn,
                session_key,
                path,
                turn_id,
            )
        if state_session.artifact_indexed_at is not None:
            return stats

        for row in legacy_payload.get("artifacts", []):
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                continue
            try:
                await asyncio.to_thread(
                    self.state.register_artifact,
                    session_key,
                    scope.project_path / row["path"],
                    relation_type="referenced",
                    artifact_kind=str(row.get("kind") or "file"),
                    mime_type=str(row.get("mime_type") or "application/octet-stream"),
                    turn_id=(
                        str(row["_turn_id"])
                        if isinstance(row.get("_turn_id"), str)
                        else None
                    ),
                )
                stats["migrated_count"] = int(stats["migrated_count"]) + 1
            except (OSError, StateStoreError):
                stats["migration_failures"] = int(stats["migration_failures"]) + 1
                self._log.debug(
                    "legacy artifact registration failed session={} path={}",
                    session_key,
                    row.get("path"),
                    exc_info=True,
                )
        await asyncio.to_thread(self.state.mark_artifact_indexed, session_key)
        return stats

    async def _handle_session_artifacts(self, request: WsRequest, key: str) -> Response:
        context = self._session_artifact_context(request, key)
        if isinstance(context, Response):
            return context
        decoded_key, session_data = context
        scope = self.workspaces.scope_for_session_key(decoded_key)
        try:
            state_session = self._ensure_state_session(
                decoded_key,
                session_data,
                scope,
            )
        except SessionProjectMismatch:
            return _http_error(409, "session_project_mismatch")

        migration = await self._ensure_session_artifact_index(
            decoded_key,
            session_data,
            scope,
            state_session,
        )
        truncated = bool(migration["truncated"])
        migrated_count = int(migration["migrated_count"])
        migration_failures = int(migration["migration_failures"])
        pruned_count = int(migration["pruned_count"])

        records = await asyncio.to_thread(
            self.state.list_session_artifacts,
            decoded_key,
        )
        await asyncio.to_thread(
            self.logs.write,
            level="warning" if migration_failures else "info",
            component="artifacts",
            event_name="artifact_list_completed",
            message="session artifact registry loaded",
            project_id=state_session.project_id,
            session_id=state_session.id,
            error_code=(
                "LEGACY_ARTIFACT_REGISTRATION_FAILED"
                if migration_failures
                else None
            ),
            details={
                "session_key": decoded_key,
                "artifact_count": len(records),
                "migrated_count": migrated_count,
                "migration_failures": migration_failures,
                "pruned_count": pruned_count,
                "truncated": truncated,
            },
        )
        return _http_json_response(
            {
                "project_id": state_session.project_id,
                "session_id": state_session.id,
                "artifacts": [registered_artifact_row(record) for record in records],
                "truncated": truncated,
            }
        )

    def _ensure_state_session(
        self,
        session_key: str,
        session_data: dict[str, Any],
        scope: Any,
    ) -> Any:
        existing = self.state.get_session(session_key)
        if existing is not None:
            existing_project = self.state.get_project(existing.project_id)
            expected_root = os.path.normcase(os.path.normpath(str(
                Path(scope.project_path).expanduser().resolve(strict=False)
            )))
            actual_root = (
                os.path.normcase(os.path.normpath(existing_project.canonical_root_path))
                if existing_project is not None
                else ""
            )
            if actual_root == expected_root:
                return existing
            project = self.state.ensure_project(
                scope.project_path,
                name=scope.project_name,
            )
            if existing.project_id != project.id:
                raise SessionProjectMismatch(
                    session_key,
                    existing.project_id,
                    project.id,
                )
            return existing
        event_log_path = None
        if self.session_manager is not None:
            event_log_path = self.session_manager.session_path(session_key)
        metadata = session_data.get("metadata")
        _, state_session = self.state.ensure_session_for_project(
            session_key,
            scope.project_path,
            project_name=scope.project_name,
            event_log_path=event_log_path,
            title=(
                str(metadata.get("title") or "")
                if isinstance(metadata, dict)
                else ""
            ),
            metadata=metadata if isinstance(metadata, dict) else None,
            artifact_index_initialized=False,
        )
        # Registering a session must remain cheap. Replaying a large legacy
        # transcript here blocks session/artifact HTTP responses long enough
        # for clients to disconnect during the WebSocket HTTP handshake.
        # SessionEventJournal.append() still performs the idempotent replay
        # before the next durable write, preserving event sequence ordering.
        return state_session

    async def _handle_session_artifact_content(
        self,
        request: WsRequest,
        key: str,
    ) -> Response:
        context = self._session_artifact_context(request, key)
        if isinstance(context, Response):
            return context
        decoded_key, session_data = context
        query = _parse_query(request.path)
        raw_path = _query_first(query, "path")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        try:
            path = await asyncio.to_thread(
                resolve_session_artifact,
                raw_path,
                session_key=decoded_key,
                session_data=session_data,
                scope=scope,
            )
            body = await asyncio.to_thread(
                read_session_artifact,
                path,
                root=scope.project_path,
            )
        except SessionArtifactError as e:
            return _http_error(e.status, e.message)

        download = _query_first(query, "download") in {"1", "true", "yes"}
        disposition = "attachment" if download else "inline"
        encoded_name = quote(path.name, safe="")
        extra_headers = [
            ("Cache-Control", "no-store"),
            ("Content-Disposition", f"{disposition}; filename*=UTF-8''{encoded_name}"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if path.suffix.lower() == ".html":
            extra_headers.append((
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'none'; img-src data: blob:; "
                "font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "sandbox allow-scripts",
            ))
        return _http_response(
            body,
            content_type=artifact_content_type(path),
            extra_headers=extra_headers,
        )

    def _handle_registered_artifact_get(
        self,
        request: WsRequest,
        artifact_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        session_key = _query_first(_parse_query(request.path), "session")
        if not session_key:
            return _http_error(400, "missing session")
        artifact = self.state.get_artifact(artifact_id, session_key=session_key)
        if artifact is None:
            return _http_error(404, "artifact not found")
        return _http_json_response({"artifact": registered_artifact_row(artifact)})

    async def _handle_registered_artifact_content(
        self,
        request: WsRequest,
        artifact_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        session_key = _query_first(query, "session")
        if not session_key:
            await asyncio.to_thread(
                self.logs.write,
                level="warning",
                component="artifacts",
                event_name="artifact_content_rejected",
                message="artifact content request did not include a session",
                artifact_id=artifact_id,
                error_code="MISSING_SESSION",
            )
            return _http_error(400, "missing session")
        try:
            artifact, path = await asyncio.to_thread(
                self.state.resolve_artifact_path,
                artifact_id,
                session_key=session_key,
            )
            project = self.state.get_project(artifact.project_id)
            if project is None:
                await asyncio.to_thread(
                    self.logs.write,
                    level="error",
                    component="artifacts",
                    event_name="artifact_content_failed",
                    message="artifact project could not be resolved",
                    project_id=artifact.project_id,
                    session_id=artifact.session_id,
                    artifact_id=artifact.id,
                    error_code="PROJECT_NOT_FOUND",
                )
                return _http_error(404, "artifact project not found")
            body = await asyncio.to_thread(
                read_session_artifact,
                path,
                root=Path(project.canonical_root_path),
            )
        except (OSError, StateStoreError, SessionArtifactError) as exc:
            await asyncio.to_thread(
                self.logs.write,
                level="warning",
                component="artifacts",
                event_name="artifact_content_failed",
                message="artifact could not be resolved for the requested session",
                artifact_id=artifact_id,
                error_code="ARTIFACT_NOT_FOUND_OR_UNLINKED",
                details={
                    "session_key": session_key,
                    "exception_type": type(exc).__name__,
                },
            )
            return _http_error(404, "artifact not found")

        download = _query_first(query, "download") in {"1", "true", "yes"}
        disposition = "attachment" if download else "inline"
        encoded_name = quote(path.name, safe="")
        extra_headers = [
            ("Cache-Control", "no-store"),
            ("Content-Disposition", f"{disposition}; filename*=UTF-8''{encoded_name}"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if path.suffix.lower() == ".html":
            extra_headers.append((
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'none'; img-src data: blob:; "
                "font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "sandbox allow-scripts",
            ))
        response = _http_response(
            body,
            content_type=artifact.mime_type,
            extra_headers=extra_headers,
        )
        await asyncio.to_thread(
            self.logs.write,
            level="info",
            component="artifacts",
            event_name="artifact_content_served",
            message="artifact content served",
            project_id=artifact.project_id,
            session_id=artifact.session_id,
            artifact_id=artifact.id,
            details={
                "relative_path": artifact.relative_path,
                "content_length": len(body),
                "download": download,
            },
        )
        return response

    def _session_artifact_context(
        self,
        request: WsRequest,
        key: str,
    ) -> tuple[str, dict[str, Any]] | Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        session_data = self.session_manager.read_session_file(decoded_key)
        if session_data is None:
            return _http_error(404, "session not found")
        return decoded_key, session_data

    def _sessions_list_payload(
        self,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        assert self.session_manager is not None
        self.reconcile_archived_lifecycle()
        sessions = list_webui_sessions(self.session_manager)
        from nanobot.session.webui_turns import websocket_turn_wall_started_at

        state_sessions = self.state.list_sessions(include_archived=True)
        state_by_key = {session.session_key: session for session in state_sessions}
        projects_by_id = {
            project.id: project
            for project in self.state.list_projects(include_archived=True)
        }
        cleaned = []
        listed_keys: set[str] = set()
        for s in sessions:
            key = s.get("key")
            if not (
                isinstance(key, str)
                and (key.startswith("websocket:") or key.startswith("cron:"))
            ):
                continue
            existing_state = state_by_key.get(key)
            session_lifecycle = self.lifecycle.session(key)
            if session_lifecycle is not None and session_lifecycle.state == "purged":
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            metadata_data = self.session_manager.read_session_metadata(key)
            metadata = (
                metadata_data.get("metadata")
                if isinstance(metadata_data, dict)
                else None
            )
            state_project = (
                projects_by_id.get(existing_state.project_id)
                if existing_state is not None
                else None
            )
            scope = self.workspaces.scope_for_session_metadata(
                metadata if isinstance(metadata, dict) else None,
                state_project=state_project,
            )
            metadata_project_id = (
                metadata.get("project_id")
                if isinstance(metadata, dict)
                and isinstance(metadata.get("project_id"), str)
                else None
            )
            project_lifecycle = (
                (self.lifecycle.project(metadata_project_id) if metadata_project_id else None)
                or self.lifecycle.project_for_path(scope.project_path)
                or (
                    self.lifecycle.project(existing_state.project_id)
                    if existing_state is not None
                    else None
                )
            )
            if project_lifecycle is not None and project_lifecycle.state == "purged":
                continue
            if existing_state is not None and state_project is not None:
                state_root = os.path.normcase(os.path.normpath(state_project.canonical_root_path))
                scope_root = os.path.normcase(os.path.normpath(str(scope.project_path)))
                if state_root != scope_root:
                    self._log.error(
                        "session projection project mismatch session={} state_project={} metadata_project={}",
                        key,
                        existing_state.project_id,
                        metadata_project_id,
                    )
                    # Fail closed. Rebuildable SQLite must never move a
                    # conversation into another workspace merely to keep a
                    # sidebar row visible.
                    continue
            row["workspace_scope"] = scope.payload()
            if existing_state is not None:
                state_session = existing_state
            else:
                # Legacy sessions are projected once.  After that SQLite owns
                # stable identity, project binding, ordering, and title.
                session_data = self.session_manager.read_session_file(key)
                if not isinstance(session_data, dict):
                    continue
                projection_data = session_data
                if project_lifecycle is not None and not metadata_project_id:
                    projection_metadata = (
                        dict(metadata) if isinstance(metadata, dict) else {}
                    )
                    projection_metadata["project_id"] = project_lifecycle.key
                    projection_data = {
                        **session_data,
                        "metadata": projection_metadata,
                    }
                try:
                    state_session = self._ensure_state_session(
                        key,
                        projection_data,
                        scope,
                    )
                except SessionProjectMismatch:
                    self._log.error(
                        "session project mismatch while listing session={}",
                        key,
                    )
                    continue
                state_by_key[key] = state_session
            should_archive_session = (
                existing_state is not None and existing_state.status == "archived"
            ) or (
                session_lifecycle is not None
                and session_lifecycle.state == "archived"
            )
            should_archive_project = (
                (state_project is not None and state_project.status == "archived")
                or (
                    project_lifecycle is not None
                    and project_lifecycle.state == "archived"
                )
            )
            if should_archive_project:
                project = self.state.get_project(state_session.project_id)
                if project is not None and project.status != "archived":
                    self.state.archive_project(project.id)
                state_session = self.state.get_session(key) or state_session
                if not include_archived:
                    continue
            elif should_archive_session and state_session.status != "archived":
                state_session = self.state.archive_session(key)
            if state_session.status == "archived" and not include_archived:
                continue
            row["session_id"] = state_session.id
            row["project_id"] = state_session.project_id
            row["status"] = state_session.status
            if session_lifecycle is not None:
                row["lifecycle_updated_at"] = session_lifecycle.updated_at
            # Once a session is projected, SQLite owns its ordering and stable
            # identity.  The JSONL index remains useful for preview text.
            row["created_at"] = datetime.fromtimestamp(
                state_session.created_at / 1_000
            ).isoformat()
            row["updated_at"] = datetime.fromtimestamp(
                state_session.updated_at / 1_000
            ).isoformat()
            state_title = _state_title_for_session_list(state_session.title, metadata)
            if state_title:
                row["title"] = state_title
            if isinstance(metadata, dict):
                expert_team = public_expert_team_binding(metadata.get(EXPERT_TEAM_SESSION_KEY))
                if expert_team is not None:
                    row["expert_team"] = expert_team
            if state_session.status != "archived":
                chat_id = key if key.startswith("cron:") else key.split(":", 1)[1]
                started_at = websocket_turn_wall_started_at(chat_id)
                if started_at is not None:
                    row["run_started_at"] = started_at
            cleaned.append(row)
            listed_keys.add(key)

        # A rebuildable JSONL index must not be able to hide an otherwise
        # valid SQLite-backed session.  Merge any projected, disk-backed rows
        # that were absent from the index instead of making the sidebar depend
        # on both stores being refreshed in the same request.
        for state_session in state_sessions:
            if state_session.status == "archived" and not include_archived:
                continue
            key = state_session.session_key
            if key in listed_keys or not (
                key.startswith("websocket:") or key.startswith("cron:")
            ):
                continue
            session_data = self.session_manager.read_session_file(key)
            if not isinstance(session_data, dict):
                continue
            if not is_webui_sidebar_session_data(session_data):
                continue
            metadata = session_data.get("metadata")
            scope = self.workspaces.scope_for_session_metadata(
                metadata if isinstance(metadata, dict) else None,
                state_project=projects_by_id.get(state_session.project_id),
            )
            state_project = projects_by_id.get(state_session.project_id)
            metadata_project_id = (
                metadata.get("project_id")
                if isinstance(metadata, dict)
                and isinstance(metadata.get("project_id"), str)
                else None
            )
            if state_project is not None:
                state_root = os.path.normcase(os.path.normpath(state_project.canonical_root_path))
                scope_root = os.path.normcase(os.path.normpath(str(scope.project_path)))
                if state_root != scope_root:
                    continue
            project_lifecycle = (
                (self.lifecycle.project(metadata_project_id) if metadata_project_id else None)
                or self.lifecycle.project(state_session.project_id)
                or self.lifecycle.project_for_path(scope.project_path)
            )
            session_lifecycle = self.lifecycle.session(key)
            if (
                (project_lifecycle is not None and project_lifecycle.state == "purged")
                or (session_lifecycle is not None and session_lifecycle.state == "purged")
            ):
                continue
            if (
                (state_project is not None and state_project.status == "archived")
                or (
                    project_lifecycle is not None
                    and project_lifecycle.state == "archived"
                )
            ) and not include_archived:
                continue
            preview = ""
            messages = session_data.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        preview = content.strip()[:240]
                        break
            row = {
                "key": key,
                "created_at": datetime.fromtimestamp(
                    state_session.created_at / 1_000
                ).isoformat(),
                "updated_at": datetime.fromtimestamp(
                    state_session.updated_at / 1_000
                ).isoformat(),
                "title": (
                    _state_title_for_session_list(state_session.title, metadata)
                    or (preview if state_session.title else "")
                ),
                "preview": preview,
                "workspace_scope": scope.payload(),
                "session_id": state_session.id,
                "project_id": state_session.project_id,
                "status": state_session.status,
            }
            if session_lifecycle is not None:
                row["lifecycle_updated_at"] = session_lifecycle.updated_at
            if state_session.status != "archived":
                chat_id = key if key.startswith("cron:") else key.split(":", 1)[1]
                started_at = websocket_turn_wall_started_at(chat_id)
                if started_at is not None:
                    row["run_started_at"] = started_at
            if isinstance(metadata, dict):
                expert_team = public_expert_team_binding(
                    metadata.get(EXPERT_TEAM_SESSION_KEY)
                )
                if expert_team is not None:
                    row["expert_team"] = expert_team
            cleaned.append(row)
        cleaned.sort(
            key=lambda row: str(row.get("updated_at") or ""),
            reverse=True,
        )
        return {"sessions": cleaned}

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_webui_readable_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self.session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        self.media.augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_webui_readable_session_key(decoded_key):
            return _http_error(404, "session not found")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        session_messages: list[dict[str, Any]] | None = None
        session_data: dict[str, Any] | None = None
        if self.session_manager is not None:
            session_data = self.session_manager.read_session_file(decoded_key)
            raw_messages = session_data.get("messages") if isinstance(session_data, dict) else None
            if isinstance(raw_messages, list):
                session_messages = [m for m in raw_messages if isinstance(m, dict)]
        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        limit: int | None = None
        if raw_limit is not None and raw_limit.strip():
            try:
                limit = int(raw_limit)
            except ValueError:
                return _http_error(400, "invalid limit")
        direction = _query_first(query, "direction")
        if direction is not None and direction not in {"latest"}:
            return _http_error(400, "invalid direction")
        before = _query_first(query, "before")
        data = build_webui_thread_response(
            decoded_key,
            session_messages=session_messages,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
            limit=limit,
            direction=direction,
            before=before,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        data["workspace_scope"] = scope.payload()
        if isinstance(session_data, dict):
            try:
                state_session = self._ensure_state_session(
                    decoded_key,
                    session_data,
                    scope,
                )
            except SessionProjectMismatch:
                return _http_error(409, "session_project_mismatch")
            data["session_id"] = state_session.id
            data["project_id"] = state_session.project_id
        metadata = session_data.get("metadata") if isinstance(session_data, dict) else None
        if isinstance(metadata, dict):
            expert_team = public_expert_team_binding(metadata.get(EXPERT_TEAM_SESSION_KEY))
            if expert_team is not None:
                data["expert_team"] = expert_team
        return _http_json_response(data)

    def _handle_file_preview(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        path = _query_first(_parse_query(request.path), "path")
        try:
            payload = file_preview_payload(
                path,
                scope=self.workspaces.scope_for_session_key(decoded_key),
            )
        except WebUIFilePreviewError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_session_automations(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        pending_job_ids: set[str] = set()
        if self.cron_pending_job_ids is not None:
            pending_job_ids = self.cron_pending_job_ids(decoded_key)
        return _http_json_response(
            session_automations_payload(
                self.cron_service,
                decoded_key,
                pending_job_ids=pending_job_ids,
            )
        )

    def _handle_session_archive(
        self,
        request: WsRequest,
        key: str,
        *,
        legacy_delete_route: bool = False,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        query = _parse_query(request.path)
        delete_automations = (_query_first(query, "delete_automations") or "").lower()
        automation_jobs = session_automation_jobs(self.cron_service, decoded_key)
        blocking_jobs = session_archive_blocking_jobs(self.cron_service, decoded_key)
        if blocking_jobs and delete_automations not in {"1", "true", "yes"}:
            return _http_json_response(
                {
                    "deleted": False,
                    "blocked_by_automations": True,
                    "automations": serialize_automation_jobs(blocking_jobs),
                }
            )
        if (
            automation_jobs
            and delete_automations in {"1", "true", "yes"}
            and self.cron_service is not None
        ):
            for job in automation_jobs:
                self.cron_service.remove_job(job.id)
        state_session = self.state.get_session(decoded_key)
        if state_session is None:
            session_data = self.session_manager.read_session_file(decoded_key)
            if not isinstance(session_data, dict):
                return _http_error(404, "session not found")
            try:
                state_session = self._ensure_state_session(
                    decoded_key,
                    session_data,
                    self.workspaces.scope_for_session_key(decoded_key),
                )
            except SessionProjectMismatch:
                return _http_error(409, "session_project_mismatch")
        if state_session is None:
            return _http_error(404, "session not found")
        if self.state.active_turn_id(decoded_key) is not None:
            return _http_error(409, "session has an active turn; stop it before archiving")
        project = self.state.get_project(state_session.project_id)
        self.lifecycle.archive_session(
            decoded_key,
            session_id=state_session.id,
            project_id=state_session.project_id,
            title=state_session.title,
            project_root=(project.canonical_root_path if project is not None else None),
        )
        self.state.archive_session(decoded_key)
        payload = {
            "archived": True,
            "session_id": state_session.id,
            "project_id": state_session.project_id,
        }
        if legacy_delete_route:
            payload["deleted"] = True
        return _http_json_response(payload)

    def _handle_session_restore(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if self.lifecycle.session_state(decoded_key) == "purged":
            return _http_error(410, "session was permanently deleted")
        state_session = self.state.get_session(decoded_key)
        if state_session is None and self.session_manager is not None:
            session_data = self.session_manager.read_session_file(decoded_key)
            if isinstance(session_data, dict):
                try:
                    state_session = self._ensure_state_session(
                        decoded_key,
                        session_data,
                        self.workspaces.scope_for_session_key(decoded_key),
                    )
                except SessionProjectMismatch:
                    return _http_error(409, "session_project_mismatch")
        if state_session is None:
            return _http_error(404, "archived session not found")
        project = self.state.get_project(state_session.project_id)
        if project is not None and (
            project.status == "archived"
            or self.lifecycle.project_state(project.id) == "archived"
        ):
            return _http_error(409, "restore the archived workspace first")
        try:
            restored = self.state.restore_session(decoded_key)
        except StateStoreError as exc:
            return _http_error(404, str(exc))
        self.lifecycle.restore_session(decoded_key, session_id=restored.id)
        return _http_json_response(
            {
                "restored": True,
                "session_id": restored.id,
                "project_id": restored.project_id,
            }
        )

    def _handle_session_purge(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        state_session = self.state.get_session(decoded_key)
        if state_session is None:
            if self.lifecycle.session_state(decoded_key) == "purged":
                return _http_json_response({"purged": True, "already_purged": True})
            return _http_error(404, "archived session not found")
        if state_session.status != "archived":
            return _http_error(409, "archive the session before permanent deletion")
        if self.state.active_turn_id(decoded_key) is not None:
            return _http_error(409, "session has an active turn; stop it before deletion")
        automation_jobs = session_automation_jobs(self.cron_service, decoded_key)
        blocking_jobs = session_archive_blocking_jobs(self.cron_service, decoded_key)
        if blocking_jobs:
            return _http_json_response(
                {
                    "purged": False,
                    "blocked_by_automations": True,
                    "automations": serialize_automation_jobs(blocking_jobs),
                },
                status=409,
            )
        self.lifecycle.purge_session(
            decoded_key,
            session_id=state_session.id,
            project_id=state_session.project_id,
            title=state_session.title,
        )
        result = self._purge_session_data(decoded_key, state_session.id)
        if self.cron_service is not None:
            for job in automation_jobs:
                if is_terminal_one_time_automation(job):
                    self.cron_service.remove_job(job.id)
        return _http_json_response(result)

    def _purge_schedule_run_session(self, session_key: str) -> dict[str, Any]:
        """Permanently delete one completed schedule run's conversation."""
        normalized_key = session_key.strip()
        if not normalized_key:
            raise StateStoreError("run session key is required")
        if self.session_manager is None:
            raise StateStoreError("session manager unavailable")
        if self.state.active_turn_id(normalized_key) is not None:
            raise StateStoreError(
                "run session has an active turn; stop it before deletion"
            )
        if session_automation_jobs(self.cron_service, normalized_key):
            raise StateStoreError(
                "run session has linked automations; delete them before deletion"
            )

        existing = self.state.get_session(normalized_key)
        lifecycle_entry = self.lifecycle.session(normalized_key)
        session_id = existing.id if existing is not None else None
        if session_id is None and lifecycle_entry is not None:
            recorded_session_id = lifecycle_entry.metadata.get("session_id")
            if isinstance(recorded_session_id, str) and recorded_session_id:
                session_id = recorded_session_id

        metadata: dict[str, Any] = (
            dict(lifecycle_entry.metadata) if lifecycle_entry is not None else {}
        )
        if existing is not None:
            metadata.update(
                session_id=existing.id,
                project_id=existing.project_id,
                title=existing.title,
            )
        elif session_id is not None:
            metadata["session_id"] = session_id
        metadata["cleanup_completed"] = False

        # Write the durable tombstone first. If the process exits during the
        # physical cleanup, gateway startup will retry _purge_session_data.
        self.lifecycle.purge_session(normalized_key, **metadata)
        return self._purge_session_data(normalized_key, session_id)

    def _purge_session_data(
        self,
        session_key: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Finish a tombstoned permanent deletion; safe to retry after a crash."""
        existing = self.state.get_session(session_key)
        resolved_session_id = session_id or (existing.id if existing is not None else None)
        projection: dict[str, Any] = {"purged": True, "session_key": session_key}
        if existing is not None:
            if existing.status != "archived":
                self.state.archive_session(session_key)
            projection = self.state.purge_session(session_key)

        cleanup_errors: list[str] = []
        removed = {
            "session_journal": False,
            "webui_transcript": False,
            "canonical_events": False,
        }
        try:
            if self.session_manager is not None:
                removed["session_journal"] = self.session_manager.delete_session(session_key)
        except OSError as exc:
            cleanup_errors.append(f"session_journal:{type(exc).__name__}")
        try:
            removed["webui_transcript"] = delete_webui_thread(session_key)
        except OSError as exc:
            cleanup_errors.append(f"webui_transcript:{type(exc).__name__}")
        try:
            removed["canonical_events"] = self.session_event_files.delete(session_key)
        except OSError as exc:
            cleanup_errors.append(f"canonical_events:{type(exc).__name__}")
        if resolved_session_id:
            try:
                self.traces.delete_session(resolved_session_id)
            except (OSError, sqlite3.Error):
                cleanup_errors.append("traces:cleanup_failed")
            try:
                self.logs.delete_session(resolved_session_id)
            except (OSError, sqlite3.Error):
                cleanup_errors.append("logs:cleanup_failed")

        entry = self.lifecycle.session(session_key)
        metadata = dict(entry.metadata) if entry is not None else {}
        if resolved_session_id:
            metadata["session_id"] = resolved_session_id
        metadata["cleanup_completed"] = not cleanup_errors
        self.lifecycle.purge_session(session_key, **metadata)
        return {
            **projection,
            "purged": True,
            "removed": removed,
            "cleanup_pending": bool(cleanup_errors),
            "cleanup_errors": cleanup_errors,
        }

    def reconcile_purged_lifecycle(self) -> None:
        """Complete interrupted purges before disk-backed sessions are listed."""
        if self.session_manager is None:
            return
        for entry in self.lifecycle.session_entries():
            if entry.state != "purged" or entry.metadata.get("cleanup_completed") is True:
                continue
            session_id = entry.metadata.get("session_id")
            self._purge_session_data(
                entry.key,
                str(session_id) if isinstance(session_id, str) else None,
            )

        project_entries = [
            entry
            for entry in self.lifecycle.project_entries()
            if entry.state == "purged"
            and entry.metadata.get("cleanup_completed") is not True
        ]
        if not project_entries:
            return

        # If a crash happened immediately after the project tombstone, session
        # tombstones may not yet exist. Resolve their durable workspace scope
        # from metadata before removing the residual journals.
        for row in list_webui_sessions(self.session_manager):
            session_key = row.get("key")
            if not isinstance(session_key, str):
                continue
            metadata_data = self.session_manager.read_session_metadata(session_key)
            metadata = (
                metadata_data.get("metadata")
                if isinstance(metadata_data, dict)
                else None
            )
            scope = self.workspaces.scope_for_session_metadata(
                metadata if isinstance(metadata, dict) else None,
            )
            project_entry = self.lifecycle.project_for_path(scope.project_path)
            if project_entry is None or project_entry.state != "purged":
                continue
            state_session = self.state.get_session(session_key)
            session_id = state_session.id if state_session is not None else None
            self.lifecycle.purge_session(
                session_key,
                session_id=session_id,
                project_id=project_entry.key,
                cleanup_completed=False,
            )
            self._purge_session_data(session_key, session_id)

        for entry in project_entries:
            project = self._state_project_for_lifecycle(entry)
            if project is not None:
                for session in self.state.list_project_sessions(
                    project.id,
                    include_archived=True,
                ):
                    self.lifecycle.purge_session(
                        session.session_key,
                        session_id=session.id,
                        project_id=project.id,
                        cleanup_completed=False,
                    )
                    self._purge_session_data(session.session_key, session.id)
                if project.status != "archived":
                    try:
                        self.state.archive_project(project.id)
                    except StateStoreError:
                        continue
                self.state.purge_project(project.id)
            metadata = dict(entry.metadata)
            metadata["cleanup_completed"] = True
            self.lifecycle.purge_project(entry.key, **metadata)

    def reconcile_archived_lifecycle(self) -> None:
        """Reapply durable archive intent after a projection rebuild.

        ``state.sqlite`` is only a query projection.  The lifecycle journal is
        authoritative, so an active row recovered from a disk-backed session
        must never make an archived workspace or conversation visible again.
        """
        for entry in self.lifecycle.project_entries():
            if entry.state != "archived":
                continue
            project = self._state_project_for_lifecycle(entry)
            if project is None or project.status == "archived":
                continue
            try:
                self.state.archive_project(project.id)
            except StateStoreError as exc:
                self._log.warning(
                    "could not reapply archived project lifecycle project={} error={}",
                    project.id,
                    exc,
                )

        for entry in self.lifecycle.session_entries():
            if entry.state != "archived":
                continue
            session = self.state.get_session(entry.key)
            if session is None or session.status == "archived":
                continue
            try:
                self.state.archive_session(entry.key)
            except StateStoreError as exc:
                self._log.warning(
                    "could not reapply archived session lifecycle session={} error={}",
                    entry.key,
                    exc,
                )

    # -- Automation routes --------------------------------------------------

    async def _dispatch_automation_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if got == "/api/webui/automations":
            return self._handle_webui_automations(request)
        m = re.match(r"^/api/webui/automations/(enable|disable|delete|run|update)$", got)
        if m:
            return await self._handle_webui_automation_action(request, m.group(1))
        return None

    def _pending_cron_job_ids_for_all(self) -> set[str]:
        if self.cron_service is None or self.cron_pending_job_ids is None:
            return set()
        pending: set[str] = set()
        for job in self.cron_service.list_jobs(include_disabled=True):
            session_key = job.payload.session_key
            if not session_key and job.payload.origin_channel and job.payload.origin_chat_id:
                session_key = f"{job.payload.origin_channel}:{job.payload.origin_chat_id}"
            if session_key:
                pending.update(self.cron_pending_job_ids(session_key))
        return pending

    def _handle_webui_automations(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            all_automations_payload(
                self.cron_service,
                session_manager=self.session_manager,
                pending_job_ids=self._pending_cron_job_ids_for_all(),
            )
        )

    async def _handle_webui_automation_action(
        self,
        request: WsRequest,
        action: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.cron_service is None:
            return _http_error(503, "cron service unavailable")

        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or _query_first(query, "job_id") or "").strip()
        if not job_id:
            return _http_error(400, "missing automation id")
        job = self.cron_service.get_job(job_id)
        if job is None:
            return _http_error(404, "automation not found")
        if job.payload.kind == "system_event":
            return _http_error(403, "system automation is protected")
        if action in {"enable", "run"} and not is_bound_cron_job(job):
            return _http_error(409, "automation has no linked chat")

        if action == "enable":
            if self.cron_service.enable_job(job_id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.cron_service.enable_job(job_id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            result = self.cron_service.remove_job(job_id)
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        elif action == "run":
            if not job.enabled:
                return _http_error(409, "automation is disabled")
            task = asyncio.create_task(self.cron_service.run_job(job_id, force=False))
            task.add_done_callback(self._log_automation_run_result)
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_automation_update(values, current_job=job)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            try:
                result = self.cron_service.update_job(job_id, **parsed)
            except ValueError as exc:
                return _http_error(400, str(exc))
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    @staticmethod
    def _log_automation_run_result(task: asyncio.Task[bool]) -> None:
        try:
            ran = task.result()
        except Exception:
            logger.exception("WebUI automation run-now task failed")
            return
        if not ran:
            logger.warning("WebUI automation run-now task did not execute")

    # -- Media routes -------------------------------------------------------

    def _dispatch_media_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2), request)
        return None

    def _handle_media_fetch(
        self, sig: str, payload: str, request: WsRequest | None = None
    ) -> Response:
        return self.media.serve_signed_media(
            sig,
            payload,
            request=request,
        )

    # -- Misc routes --------------------------------------------------------

    async def _dispatch_misc_routes(
        self, connection: Any, request: WsRequest, got: str
    ) -> Response | None:
        if got == "/api/sessions":
            return await self._handle_sessions_list(request)
        if got == "/api/projects":
            return await self._handle_projects_list(request)
        if got == "/api/data-management/archives":
            return await self._handle_data_management_archives(request)
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/archive$", got)
        if m:
            return await self._handle_project_archive(request, m.group(1))
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/restore$", got)
        if m:
            return await self._handle_project_restore(request, m.group(1))
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/purge$", got)
        if m:
            return await self._handle_project_purge(request, m.group(1))
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/relocate$", got)
        if m:
            return await self._handle_project_relocate(request, m.group(1))
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/export$", got)
        if m:
            return await self._handle_project_export(request, m.group(1))
        m = re.match(r"^/api/projects/([A-Za-z0-9_-]+)/sessions$", got)
        if m:
            return await self._handle_project_sessions(request, m.group(1))
        if got == "/api/diagnostics/logs":
            return await self._handle_diagnostic_logs(request)
        if got == "/api/traces":
            return await self._handle_traces_list(request)
        m = re.match(r"^/api/traces/(trc_[A-Za-z0-9_-]+)/spans$", got)
        if m:
            return await self._handle_trace_spans(request, m.group(1))
        m = re.match(r"^/api/traces/(trc_[A-Za-z0-9_-]+)/context$", got)
        if m:
            return await self._handle_trace_context(request, m.group(1))
        m = re.match(r"^/api/traces/(trc_[A-Za-z0-9_-]+)/export$", got)
        if m:
            return await self._handle_trace_export(request, m.group(1))
        m = re.match(r"^/api/traces/(trc_[A-Za-z0-9_-]+)$", got)
        if m:
            return await self._handle_trace_get(request, m.group(1))
        if got == "/api/commands":
            return self._handle_commands(request)
        if got == "/api/workspaces":
            return self._handle_workspaces(connection, request)
        if got == "/api/webui/skills":
            return self._handle_webui_skills(request)
        m = re.match(r"^/api/webui/skills/([^/]+)$", got)
        if m:
            return self._handle_webui_skill_detail(request, m.group(1))
        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)
        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)
        return None

    async def _handle_traces_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        try:
            limit = int(raw_limit) if raw_limit else 100
        except ValueError:
            return _http_error(400, "invalid limit")
        traces = await asyncio.to_thread(
            self.traces.list_traces,
            project_id=_query_first(query, "project_id"),
            session_id=_query_first(query, "session_id"),
            turn_id=_query_first(query, "turn_id"),
            status=_query_first(query, "status"),
            limit=limit,
        )
        return _http_json_response({"schema_version": 1, "traces": traces})

    async def _handle_trace_get(
        self,
        request: WsRequest,
        trace_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        trace = await asyncio.to_thread(self.traces.get_trace, trace_id)
        if trace is None:
            return _http_error(404, "trace not found")
        return _http_json_response({"schema_version": 1, "trace": trace})

    async def _handle_trace_spans(
        self,
        request: WsRequest,
        trace_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        trace = await asyncio.to_thread(self.traces.get_trace, trace_id)
        if trace is None:
            return _http_error(404, "trace not found")
        spans = await asyncio.to_thread(self.traces.list_spans, trace_id)
        return _http_json_response(
            {"schema_version": 1, "trace_id": trace_id, "spans": spans}
        )

    async def _handle_trace_context(
        self,
        request: WsRequest,
        trace_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        trace = await asyncio.to_thread(self.traces.get_trace, trace_id)
        if trace is None:
            return _http_error(404, "trace not found")
        items = await asyncio.to_thread(self.traces.context_manifest, trace_id)
        return _http_json_response(
            {
                "schema_version": 1,
                "trace_id": trace_id,
                "context_manifest": items,
            }
        )

    async def _handle_trace_export(
        self,
        request: WsRequest,
        trace_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        payload = await asyncio.to_thread(self.traces.export_trace, trace_id)
        if payload is None:
            return _http_error(404, "trace not found")
        return _http_json_response(payload)

    async def _handle_projects_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        route = "projects"
        if self.session_manager is not None:
            # Normal reads are SQLite-only.  Preserve one-time migration for
            # legacy JSONL sessions that predate the projection database
            # without rebuilding every session payload on each project read.
            indexed_sessions = await self._run_blocking_stage(
                route,
                "session_index_scan",
                list_webui_sessions,
                self.session_manager,
            )
            projected_keys = {
                session.session_key
                for session in await self._run_blocking_stage(
                    route,
                    "state_session_query",
                    self.state.list_sessions,
                    include_archived=True,
                )
            }
            projected_keys.update(
                entry.key
                for entry in self.lifecycle.session_entries()
                if entry.state == "purged"
            )
            has_unprojected = any(
                isinstance(row.get("key"), str)
                and (
                    row["key"].startswith("websocket:")
                    or row["key"].startswith("cron:")
                )
                and row["key"] not in projected_keys
                for row in indexed_sessions
            )
            if has_unprojected:
                await self._run_blocking_stage(
                    route,
                    "legacy_session_projection",
                    self._sessions_list_payload,
                )
        await self._run_blocking_stage(
            route,
            "lifecycle_reconcile",
            self.reconcile_archived_lifecycle,
        )
        query = _parse_query(request.path)
        include_archived = _query_first(query, "include_archived") in {"1", "true", "yes"}
        projects = await self._run_blocking_stage(
            route,
            "state_project_query",
            self.state.list_projects,
            include_archived=include_archived,
        )
        rows: list[dict[str, Any]] = []
        for project in projects:
            lifecycle = self._project_lifecycle_entry(project)
            if lifecycle is not None and lifecycle.state == "purged":
                continue
            effective_status = (
                "archived"
                if lifecycle is not None and lifecycle.state == "archived"
                else project.status
            )
            if effective_status == "archived" and not include_archived:
                continue
            rows.append({
                **self._project_payload(project),
                "status": effective_status,
            })
        return _http_json_response(
            {"projects": rows}
        )

    async def _handle_data_management_archives(
        self,
        request: WsRequest,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        session_payload = await asyncio.to_thread(
            self._sessions_list_payload,
            include_archived=True,
        )
        projects = await asyncio.to_thread(
            self.state.list_projects,
            include_archived=True,
        )
        archived_projects = {
            project.id: project
            for project in projects
            if project.status == "archived"
            and (
                (lifecycle := self._project_lifecycle_entry(project)) is None
                or lifecycle.state != "purged"
            )
        }
        archived_project_rows = []
        for project in archived_projects.values():
            sessions = await asyncio.to_thread(
                self.state.list_project_sessions,
                project.id,
                include_archived=True,
            )
            lifecycle = self._project_lifecycle_entry(project)
            archived_project_rows.append({
                **self._project_payload(project),
                "archived_at": (
                    lifecycle.updated_at if lifecycle is not None else project.updated_at
                ),
                "session_count": len(sessions),
                "files_deleted": False,
            })

        represented_paths = {
            os.path.normcase(os.path.normpath(project.canonical_root_path))
            for project in archived_projects.values()
        }
        for lifecycle in self.lifecycle.project_entries():
            if lifecycle.state != "archived":
                continue
            raw_root = lifecycle.metadata.get("canonical_root_path")
            if not isinstance(raw_root, str) or not raw_root:
                raw_root = lifecycle.metadata.get("root_path")
            if not isinstance(raw_root, str) or not raw_root:
                continue
            canonical_root = os.path.normcase(os.path.normpath(str(
                Path(raw_root).expanduser().resolve(strict=False)
            )))
            if canonical_root in represented_paths:
                continue
            display_root = str(lifecycle.metadata.get("root_path") or raw_root)
            display_name = str(
                lifecycle.metadata.get("name")
                or Path(display_root).name
                or display_root
            )
            archived_project_rows.append({
                "id": lifecycle.key,
                "kind": str(lifecycle.metadata.get("kind") or "workspace"),
                "name": display_name,
                "root_path": display_root,
                "status": "archived",
                "created_at": lifecycle.updated_at,
                "updated_at": lifecycle.updated_at,
                "archived_at": lifecycle.updated_at,
                "session_count": sum(
                    1
                    for entry in self.lifecycle.session_entries()
                    if entry.state == "archived"
                    and entry.metadata.get("project_id") == lifecycle.key
                ),
                "files_deleted": False,
            })
            represented_paths.add(canonical_root)

        project_by_id = {project.id: project for project in projects}
        archived_session_rows = []
        for row in session_payload.get("sessions", []):
            if row.get("status") != "archived":
                continue
            project_id = str(row.get("project_id") or "")
            if project_id in archived_projects:
                continue
            session_key = str(row.get("key") or "")
            if self.lifecycle.session_state(session_key) == "purged":
                continue
            project = project_by_id.get(project_id)
            lifecycle = self.lifecycle.session(session_key)
            archived_session_rows.append({
                "session_key": session_key,
                "session_id": row.get("session_id"),
                "project_id": project_id,
                "title": str(row.get("title") or row.get("preview") or ""),
                "preview": str(row.get("preview") or ""),
                "project_name": project.name if project is not None else "",
                "project_root": project.root_path if project is not None else "",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "archived_at": (
                    lifecycle.updated_at
                    if lifecycle is not None
                    else int((project.updated_at if project is not None else 0))
                ),
            })
        archived_session_rows.sort(
            key=lambda row: int(row.get("archived_at") or 0),
            reverse=True,
        )
        archived_project_rows.sort(
            key=lambda row: int(row.get("archived_at") or 0),
            reverse=True,
        )
        return _http_json_response({
            "schema_version": 1,
            "archived_sessions": archived_session_rows,
            "archived_projects": archived_project_rows,
        })

    @staticmethod
    def _project_payload(project: Any) -> dict[str, Any]:
        return {
            "id": project.id,
            "kind": project.kind,
            "name": project.name,
            "root_path": project.root_path,
            "status": project.status,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }

    def _project_lifecycle_entry(self, project: Any) -> Any:
        return (
            self.lifecycle.project(project.id)
            or self.lifecycle.project_for_path(project.canonical_root_path)
        )

    def _state_project_for_lifecycle(self, lifecycle: Any) -> Any:
        project = self.state.get_project(lifecycle.key)
        if project is not None:
            return project
        recorded_path = lifecycle.metadata.get("canonical_root_path")
        if not isinstance(recorded_path, str) or not recorded_path:
            recorded_path = lifecycle.metadata.get("root_path")
        if not isinstance(recorded_path, str) or not recorded_path:
            return None
        canonical = os.path.normcase(os.path.normpath(str(
            Path(recorded_path).expanduser().resolve(strict=False)
        )))
        return next(
            (
                candidate
                for candidate in self.state.list_projects(include_archived=True)
                if os.path.normcase(os.path.normpath(candidate.canonical_root_path))
                == canonical
            ),
            None,
        )

    def _require_project_mutation(self, request: WsRequest) -> Response | None:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if str(getattr(request, "method", "GET")).upper() not in {"GET", "POST", "PUT"}:
            return _http_error(405, "unsupported method")
        return None

    async def _handle_project_archive(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if error := self._require_project_mutation(request):
            return error
        if self.cron_service is not None:
            active_jobs = await asyncio.to_thread(
                self.cron_service.list_jobs,
                include_disabled=True,
            )
            if any(
                job.enabled
                and getattr(job.payload, "project_id", None) == project_id
                for job in active_jobs
            ):
                return _http_error(
                    409,
                    "project has active schedules; pause them before archiving",
                )
        existing = self.state.get_project(project_id)
        if existing is None:
            return _http_error(404, "project not found")
        self.lifecycle.archive_project(
            project_id,
            canonical_root_path=existing.canonical_root_path,
            root_path=existing.root_path,
            name=existing.name,
        )
        try:
            project = await asyncio.to_thread(self.state.archive_project, project_id)
        except StateStoreError as exc:
            status = 404 if str(exc) == "project not found" else 409
            return _http_error(status, str(exc))
        await asyncio.to_thread(
            self.logs.write,
            level="info",
            component="projects",
            event_name="project_archived",
            message="project registration archived without deleting files",
            project_id=project.id,
        )
        return _http_json_response({"project": self._project_payload(project)})

    async def _handle_project_restore(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if error := self._require_project_mutation(request):
            return error
        project = self.state.get_project(project_id)
        lifecycle = (
            self._project_lifecycle_entry(project)
            if project is not None
            else self.lifecycle.project(project_id)
        )
        if lifecycle is not None and lifecycle.state == "purged":
            return _http_error(410, "workspace was permanently deleted")
        try:
            if project is None:
                if lifecycle is None or lifecycle.state != "archived":
                    return _http_error(404, "archived project not found")
                project = await asyncio.to_thread(
                    self._state_project_for_lifecycle,
                    lifecycle,
                )
                if project is None:
                    root_path = lifecycle.metadata.get("root_path")
                    if not isinstance(root_path, str) or not root_path:
                        root_path = lifecycle.metadata.get("canonical_root_path")
                    if not isinstance(root_path, str) or not root_path:
                        return _http_error(409, "archived workspace path is unavailable")
                    project = await asyncio.to_thread(
                        self.state.ensure_project,
                        root_path,
                        name=str(lifecycle.metadata.get("name") or "") or None,
                    )
                if project.status != "archived" and project.kind != "inbox":
                    await asyncio.to_thread(self.state.archive_project, project.id)
            project = await asyncio.to_thread(self.state.restore_project, project.id)
        except StateStoreError as exc:
            status = 404 if str(exc) == "project not found" else 409
            return _http_error(status, str(exc))
        lifecycle_keys = {project_id, project.id}
        if lifecycle is not None:
            lifecycle_keys.add(lifecycle.key)
        for lifecycle_key in lifecycle_keys:
            self.lifecycle.restore_project(
                lifecycle_key,
                canonical_root_path=project.canonical_root_path,
            )
        # Sessions archived individually before the workspace was removed must
        # stay archived when the workspace registration is restored.
        for session in await asyncio.to_thread(
            self.state.list_project_sessions,
            project.id,
            include_archived=True,
        ):
            if self.lifecycle.session_state(session.session_key) == "archived":
                await asyncio.to_thread(self.state.archive_session, session.session_key)
        return _http_json_response({"project": self._project_payload(project)})

    async def _handle_project_purge(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if error := self._require_project_mutation(request):
            return error
        project = self.state.get_project(project_id)
        if project is None:
            lifecycle = self.lifecycle.project(project_id)
            if lifecycle is not None and lifecycle.state == "purged":
                return _http_json_response({"purged": True, "already_purged": True})
            if lifecycle is None or lifecycle.state != "archived":
                return _http_error(404, "archived project not found")
            project = await asyncio.to_thread(
                self._state_project_for_lifecycle,
                lifecycle,
            )
            if project is None:
                metadata = dict(lifecycle.metadata)
                metadata["cleanup_completed"] = False
                self.lifecycle.purge_project(project_id, **metadata)
                await asyncio.to_thread(self.reconcile_purged_lifecycle)
                return _http_json_response({"purged": True, "files_deleted": False})
        project_lifecycle = self._project_lifecycle_entry(project)
        if project.status != "archived" and not (
            project_lifecycle is not None
            and project_lifecycle.state == "archived"
        ):
            return _http_error(409, "archive the workspace before permanent deletion")
        if project.status != "archived":
            try:
                project = await asyncio.to_thread(self.state.archive_project, project.id)
            except StateStoreError as exc:
                return _http_error(409, str(exc))
        sessions = await asyncio.to_thread(
            self.state.list_project_sessions,
            project_id,
            include_archived=True,
        )
        for session in sessions:
            if session_automation_jobs(self.cron_service, session.session_key):
                return _http_error(
                    409,
                    "workspace has linked automations; delete them before permanent deletion",
                )
            if self.state.active_turn_id(session.session_key) is not None:
                return _http_error(409, "workspace has an active task")

        lifecycle_keys = {project_id, project.id}
        if project_lifecycle is not None:
            lifecycle_keys.add(project_lifecycle.key)
        for lifecycle_key in lifecycle_keys:
            self.lifecycle.purge_project(
                lifecycle_key,
                canonical_root_path=project.canonical_root_path,
                root_path=project.root_path,
                name=project.name,
                cleanup_completed=False,
            )
        for session in sessions:
            self.lifecycle.purge_session(
                session.session_key,
                session_id=session.id,
                project_id=project.id,
                cleanup_completed=False,
            )
            await asyncio.to_thread(
                self._purge_session_data,
                session.session_key,
                session.id,
            )
        result = await asyncio.to_thread(self.state.purge_project, project.id)
        for lifecycle_key in lifecycle_keys:
            self.lifecycle.purge_project(
                lifecycle_key,
                canonical_root_path=project.canonical_root_path,
                root_path=project.root_path,
                name=project.name,
                cleanup_completed=True,
            )
        return _http_json_response({**result, "files_deleted": False})

    async def _handle_project_relocate(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if error := self._require_project_mutation(request):
            return error
        query = _parse_query(request.path)
        new_path = (_query_first(query, "path") or "").strip()
        if not new_path:
            return _http_error(400, "missing path")
        try:
            project = await asyncio.to_thread(
                self.state.relocate_project,
                project_id,
                new_path,
            )
        except StateStoreError as exc:
            status = 404 if str(exc) == "project not found" else 409
            return _http_error(status, str(exc))
        await asyncio.to_thread(
            self.logs.write,
            level="info",
            component="projects",
            event_name="project_relocated",
            message="project root was relocated while preserving identity",
            project_id=project.id,
            details={"new_root_path": project.root_path},
        )
        return _http_json_response({"project": self._project_payload(project)})

    async def _handle_project_export(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            manifest = await asyncio.to_thread(
                self.state.project_export_manifest,
                project_id,
            )
        except StateStoreError as exc:
            return _http_error(404, str(exc))
        project = self.state.get_project(project_id)
        assert project is not None
        include_files = _query_first(_parse_query(request.path), "include_files") in {
            "1",
            "true",
            "yes",
        }

        def build_archive() -> bytes:
            output = io.BytesIO()
            root = Path(project.canonical_root_path).resolve(strict=False)
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                archive.writestr(
                    "state-export.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                for session in manifest["sessions"]:
                    event_log = session.get("event_log_path")
                    if not isinstance(event_log, str) or not event_log:
                        continue
                    source = Path(event_log).expanduser().resolve(strict=False)
                    if source.is_file():
                        archive.write(source, f"sessions/{session['id']}.jsonl")
                if include_files:
                    seen: set[str] = set()
                    for artifact in manifest["artifacts"]:
                        relative = str(artifact.get("relative_path") or "")
                        if not relative or relative in seen:
                            continue
                        source = (root / relative).resolve(strict=False)
                        try:
                            source.relative_to(root)
                        except ValueError:
                            continue
                        if source.is_file():
                            seen.add(relative)
                            archive.write(source, f"managed-artifacts/{relative}")
            return output.getvalue()

        body = await asyncio.to_thread(build_archive)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-") or project.id
        return _http_response(
            body,
            content_type="application/zip",
            extra_headers=[
                ("Content-Disposition", f'attachment; filename="{safe_name}-export.zip"'),
                ("Cache-Control", "no-store"),
            ],
        )

    async def _handle_project_sessions(
        self,
        request: WsRequest,
        project_id: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.state.get_project(project_id) is None:
            return _http_error(404, "project not found")
        include_archived = _query_first(
            _parse_query(request.path),
            "include_archived",
        ) in {"1", "true", "yes"}
        sessions = await asyncio.to_thread(
            self.state.list_project_sessions,
            project_id,
            include_archived=include_archived,
        )
        return _http_json_response(
            {
                "project_id": project_id,
                "sessions": [
                    {
                        "id": session.id,
                        "project_id": session.project_id,
                        "session_key": session.session_key,
                        "title": session.title,
                        "status": session.status,
                        "created_at": session.created_at,
                        "updated_at": session.updated_at,
                    }
                    for session in sessions
                ],
            }
        )

    async def _handle_diagnostic_logs(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        try:
            limit = int(raw_limit) if raw_limit else 200
        except ValueError:
            return _http_error(400, "invalid limit")
        records = await asyncio.to_thread(
            self.logs.query,
            project_id=_query_first(query, "project_id"),
            session_id=_query_first(query, "session_id"),
            artifact_id=_query_first(query, "artifact_id"),
            error_code=_query_first(query, "error_code"),
            limit=limit,
        )
        return _http_json_response(
            {"logs": [self._structured_log_payload(record) for record in records]}
        )

    @staticmethod
    def _structured_log_payload(record: StructuredLogRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "timestamp": record.timestamp,
            "level": record.level,
            "component": record.component,
            "event_name": record.event_name,
            "message": record.message,
            "request_id": record.request_id,
            "project_id": record.project_id,
            "session_id": record.session_id,
            "turn_id": record.turn_id,
            "tool_call_id": record.tool_call_id,
            "artifact_id": record.artifact_id,
            "error_code": record.error_code,
            "duration_ms": record.duration_ms,
            "details": record.details,
        }

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_workspaces(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            self.workspaces.payload(
                controls_available=self.workspace_controls_available(connection)
            )
        )

    def _handle_webui_skills(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            )
        )

    def _handle_webui_skill_detail(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        name = unquote(raw_name)
        if not name or "/" in name or "\\" in name:
            return _http_error(400, "invalid skill name")
        payload = webui_skill_detail_payload(
            self.skills_workspace_path,
            name,
            disabled_skills=self.disabled_skills,
        )
        if payload is None:
            return _http_error(404, "skill not found")
        return _http_json_response(payload)

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded)
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self._log.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    # -- Static file serving ------------------------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        assert self.static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self.static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self.static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            index = self.static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self._log.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )


def _automation_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    raw = _case_insensitive_header(request.headers, _AUTOMATION_VALUES_HEADER)
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return values if isinstance(values, dict) else None


def _parse_automation_update(
    values: dict[str, Any],
    *,
    current_job: CronJob | None = None,
) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    if "message" in values:
        raw_message = values.get("message")
        if not isinstance(raw_message, str):
            return "message must be a string"
        message = raw_message.strip()
        if not message:
            return "message cannot be empty"
        update["message"] = message
    if "schedule" in values:
        raw_schedule = values.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule must be an object"
        parsed_schedule = _parse_automation_schedule(raw_schedule)
        if isinstance(parsed_schedule, str):
            return parsed_schedule
        if current_job is not None and _schedule_matches_job(parsed_schedule, current_job):
            return update
        schedule_error = _validate_automation_schedule(parsed_schedule)
        if schedule_error:
            return schedule_error
        update["schedule"] = parsed_schedule
        update["delete_after_run"] = parsed_schedule.kind == "at"
    return update


def _parse_automation_schedule(values: dict[str, Any]) -> CronSchedule | str:
    raw_kind = values.get("kind")
    if not isinstance(raw_kind, str):
        return "schedule kind must be a string"
    kind = raw_kind.strip()
    if kind == "every":
        every_ms = _positive_int(values.get("every_ms"))
        if every_ms is None:
            return "every schedule requires positive every_ms"
        return CronSchedule(kind="every", every_ms=every_ms)
    if kind == "cron":
        raw_expr = values.get("expr")
        if not isinstance(raw_expr, str):
            return "cron schedule requires expr"
        expr = raw_expr.strip()
        if not expr:
            return "cron schedule requires expr"
        raw_tz = values.get("tz")
        if raw_tz is not None and not isinstance(raw_tz, str):
            return "cron schedule timezone must be a string"
        tz = raw_tz.strip() if isinstance(raw_tz, str) else ""
        return CronSchedule(kind="cron", expr=expr, tz=tz or None)
    if kind == "at":
        at_ms = _positive_int(values.get("at_ms"))
        if at_ms is None:
            return "one-time schedule requires positive at_ms"
        return CronSchedule(kind="at", at_ms=at_ms)
    return "unknown schedule kind"


def _schedule_matches_job(schedule: CronSchedule, job: CronJob) -> bool:
    current = job.schedule
    if schedule.kind != current.kind:
        return False
    if schedule.kind == "at":
        return schedule.at_ms == current.at_ms
    if schedule.kind == "every":
        return schedule.every_ms == current.every_ms
    if schedule.kind == "cron":
        return (schedule.expr or "") == (current.expr or "") and (
            schedule.tz or None
        ) == (current.tz or None)
    return False


def _validate_automation_schedule(schedule: CronSchedule) -> str | None:
    if schedule.kind == "at":
        if not schedule.at_ms or schedule.at_ms <= int(time.time() * 1000):
            return "one-time schedule must be in the future"
        return None
    if schedule.kind != "cron":
        return None

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from croniter import croniter

        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
        base = datetime.now(tz=tz)
        croniter(schedule.expr, base).get_next(datetime)
    except Exception:
        return "cron schedule is invalid"
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _is_websocket_channel_session_key(key: str) -> bool:
    return key.startswith("websocket:")

def _is_webui_readable_session_key(key: str) -> bool:
    return key.startswith("websocket:") or key.startswith("cron:")

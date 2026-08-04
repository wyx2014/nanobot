"""Composition helpers for the embedded WebUI gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from loguru import logger as default_logger

from nanobot.storage.logs import StructuredLogStore
from nanobot.observability.trace_store import TraceStore
from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.session_events import SessionEventFileStore, SessionEventService
from nanobot.storage.state import StateStore, open_state_store_with_recovery
from nanobot.webui.gateway_tokens import GatewayTokenStore
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.transcript import WebUITranscriptRecorder
from nanobot.webui.transcript import read_transcript_lines
from nanobot.webui.workspaces import WebUIWorkspaceController
from nanobot.webui.ws_http import GatewayHTTPHandler


@dataclass(frozen=True)
class GatewayServices:
    """Explicit dependencies shared by WebSocket transport and HTTP routes."""

    http: GatewayHTTPHandler
    tokens: GatewayTokenStore
    media: WebUIMediaGateway
    transcripts: WebUITranscriptRecorder
    workspaces: WebUIWorkspaceController
    state: StateStore
    logs: StructuredLogStore
    traces: TraceStore
    journal: SessionEventService
    session_manager: Any | None
    cron_service: Any | None
    cron_pending_job_ids: Callable[[str], set[str]] | None


def build_gateway_services(
    *,
    config: Any,
    bus: Any,
    session_manager: Any | None,
    static_dist_path: Path | None,
    workspace_path: Path,
    default_restrict_to_workspace: bool,
    runtime_model_name: Any | None,
    runtime_surface: str,
    runtime_capabilities_overrides: dict[str, Any] | None,
    runtime_ready: Callable[[], bool] | None = None,
    runtime_mcp_status: Callable[[], str] | None = None,
    disabled_skills: set[str] | None = None,
    cron_service: Any | None = None,
    cron_pending_job_ids: Callable[[str], set[str]] | None = None,
    project_memory_pipeline: Any | None = None,
    thread_runtime_registry: Any | None = None,
    logger: Any = default_logger,
) -> GatewayServices:
    state_recovery = open_state_store_with_recovery(
        workspace_path / ".nanobot" / "state.sqlite",
        default_workspace=workspace_path,
    )
    state = state_recovery.store
    logs = StructuredLogStore(workspace_path / ".nanobot" / "logs.sqlite")
    traces = TraceStore(logs.path)
    if state_recovery.backup_dir is not None:
        logs.write(
            level="error",
            component="recovery",
            event_name="state_database_rebuilt",
            message="corrupt state database was backed up and rebuilt",
            error_code="STATE_DATABASE_CORRUPT",
            details={
                "backup_dir": str(state_recovery.backup_dir),
                "reason": state_recovery.reason,
            },
        )
    logs.write(
        level="info",
        component="gateway",
        event_name="gateway_state_initialized",
        message="gateway state and diagnostic stores initialized",
        details={
            "state_database": str(state.path),
            "logs_database": str(logs.path),
        },
    )
    tokens = GatewayTokenStore()
    media = WebUIMediaGateway(
        workspace_path=workspace_path,
        logger=logger,
        cache_max_bytes=config.media_cache_max_bytes,
        cache_ttl_s=config.media_cache_ttl_s,
        cache_cleanup_interval_s=config.media_cache_cleanup_interval_s,
        cache_startup_delay_s=config.media_cache_startup_delay_s,
    )
    event_files = SessionEventFileStore(
        workspace_path / ".nanobot" / "session-events",
        legacy_reader=read_transcript_lines,
    )
    journal_store = SessionEventJournal(
        state=state,
        logs=logs,
        append_record=event_files.append,
        read_records=event_files.read,
    )
    journal = SessionEventService(journal_store)
    transcripts = WebUITranscriptRecorder(log=logger, journal=journal)
    workspaces = WebUIWorkspaceController(
        session_manager=session_manager,
        default_workspace=workspace_path,
        default_restrict_to_workspace=default_restrict_to_workspace,
        state_store=state,
    )
    http = GatewayHTTPHandler(
        config=config,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        runtime_model_name=runtime_model_name,
        runtime_ready=runtime_ready,
        runtime_mcp_status=runtime_mcp_status,
        runtime_surface=runtime_surface,
        runtime_capabilities_overrides=runtime_capabilities_overrides,
        bus=bus,
        tokens=tokens,
        media=media,
        workspaces=workspaces,
        state_store=state,
        logs_store=logs,
        trace_store=traces,
        journal_store=journal,
        skills_workspace_path=workspace_path,
        disabled_skills=disabled_skills,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
        project_memory_pipeline=project_memory_pipeline,
        thread_runtime_registry=thread_runtime_registry,
        log=logger,
    )
    # If state.sqlite was rebuilt, re-register disk-backed sessions from their
    # own persisted workspace metadata. Historical event projection is
    # intentionally lazy: SessionEventJournal.append() replays one session
    # before its next write. Eagerly replaying every transcript here can block
    # gateway startup for minutes on established workspaces.
    rebuilt_sessions = 0
    if state_recovery.backup_dir is not None and session_manager is not None:
        rebuilt_payload = http._sessions_list_payload()
        rebuilt_sessions = len(rebuilt_payload.get("sessions", []))
    journal_reconciled_runs = 0
    stale_turns = (
        state.incomplete_turn_snapshots()
        if session_manager is not None
        else []
    )
    for stale in stale_turns:
        session_key = stale["session_key"]
        turn_id = stale["turn_id"]
        try:
            journal.ensure_recovered(session_key)
            if state.active_turn_id(session_key) != turn_id:
                continue
            completed_at = time.time_ns() // 1_000_000
            runtime_epoch = str(stale.get("runtime_epoch") or "previous-runtime")
            journal.commit(
                session_key,
                {
                    "event": "turn_completed",
                    "event_id": f"terminal_recovery_{runtime_epoch}_{turn_id}",
                    "chat_id": (
                        session_key.split(":", 1)[1]
                        if ":" in session_key
                        else session_key
                    ),
                    "turn_id": turn_id,
                    "turn": {
                        "id": turn_id,
                        "runtime_epoch": runtime_epoch,
                        "project_id": stale["project_id"],
                        "session_id": stale["session_id"],
                        "status": "interrupted",
                        "started_at": stale["started_at"],
                        "completed_at": completed_at,
                        "duration_ms": max(0, completed_at - stale["started_at"]),
                        "finish_reason": "gatewayRestarted",
                        "error": {
                            "code": "GATEWAY_RESTARTED",
                            "message": "gateway restarted before terminal event",
                            "retryable": True,
                        },
                    },
                },
            )
            journal_reconciled_runs += 1
        except Exception:
            logger.exception(
                "journal-first stale turn recovery failed session={} turn={}",
                session_key,
                turn_id,
            )
    fallback_reconciled_runs = state.reconcile_incomplete_runs()
    reconciled_runs = journal_reconciled_runs + fallback_reconciled_runs
    reconciled_artifacts = state.reconcile_stale_artifacts()
    logs.write(
        level="info",
        component="recovery",
        event_name="gateway_recovery_completed",
        message="gateway state recovery initialized",
        details={
            "database_rebuilt": state_recovery.backup_dir is not None,
            "rebuilt_sessions": rebuilt_sessions,
            "journal_recovery_mode": "lazy_on_append",
            "reconciled_runs": reconciled_runs,
            "journal_reconciled_runs": journal_reconciled_runs,
            "fallback_reconciled_runs": fallback_reconciled_runs,
            "reconciled_artifacts": reconciled_artifacts,
        },
    )
    return GatewayServices(
        http=http,
        tokens=tokens,
        media=media,
        transcripts=transcripts,
        workspaces=workspaces,
        state=state,
        logs=logs,
        traces=traces,
        journal=journal,
        session_manager=session_manager,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
    )

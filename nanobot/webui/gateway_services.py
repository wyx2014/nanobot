"""Composition helpers for the embedded WebUI gateway."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger as default_logger

from nanobot.observability.trace_store import TraceStore
from nanobot.storage.journal import SessionEventJournal
from nanobot.storage.lifecycle import LifecycleRegistry
from nanobot.storage.logs import StructuredLogStore
from nanobot.storage.session_events import SessionEventFileStore, SessionEventService
from nanobot.storage.state import StateStore, open_state_store_with_recovery
from nanobot.webui.gateway_tokens import GatewayTokenStore
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.transcript import WebUITranscriptRecorder, read_transcript_lines
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
    lifecycle: LifecycleRegistry
    session_manager: Any | None
    cron_service: Any | None
    cron_pending_job_ids: Callable[[str], set[str]] | None
    expert_team_turn_router: Callable[..., Awaitable[dict[str, Any] | None]] | None


@dataclass(frozen=True)
class ProjectionBindingRecovery:
    """Recoverable quarantine result for a healthy but mis-bound projection."""

    backup_dir: Path | None
    mismatch_count: int = 0


def _canonical_root(value: str | Path) -> str:
    return os.path.normcase(
        os.path.normpath(str(Path(value).expanduser().resolve(strict=False)))
    )


def quarantine_mismatched_session_projection(
    path: str | Path,
    *,
    session_manager: Any | None,
    logger: Any = default_logger,
) -> ProjectionBindingRecovery:
    """Quarantine a query projection that contradicts durable session identity.

    Conversation JSONL metadata is durable; ``state.sqlite`` is rebuilt query
    state.  A previous recovery bug could attach many old sessions to the
    Inbox project.  Detect that condition before opening the projection and
    move the database aside so normal startup can reconstruct it safely.

    A changed path alone is not enough: when the durable project id still
    matches, the project may have been intentionally relocated.  Conversely,
    the same canonical root with a legacy id is harmless after a directory
    rename and does not require a rebuild.
    """

    database_path = Path(path).expanduser()
    if session_manager is None or not database_path.is_file():
        return ProjectionBindingRecovery(backup_dir=None)

    try:
        connection = sqlite3.connect(
            f"{database_path.resolve(strict=False).as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT s.session_key, s.project_id, p.canonical_root_path
                FROM sessions AS s
                JOIN projects AS p ON p.id = s.project_id
                """
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, sqlite3.OperationalError):
        # Header/schema corruption remains the responsibility of
        # open_state_store_with_recovery(), which records its own reason.
        return ProjectionBindingRecovery(backup_dir=None)

    mismatches = 0
    for row in rows:
        session_key = str(row["session_key"])
        metadata_record = session_manager.read_session_metadata(session_key)
        metadata = (
            metadata_record.get("metadata")
            if isinstance(metadata_record, dict)
            else None
        )
        if not isinstance(metadata, dict):
            continue
        scope = metadata.get("workspace_scope")
        durable_path = scope.get("project_path") if isinstance(scope, dict) else None
        if not isinstance(durable_path, str) or not durable_path.strip():
            continue
        projected_root = _canonical_root(str(row["canonical_root_path"]))
        durable_root = _canonical_root(durable_path)
        if projected_root == durable_root:
            continue
        durable_project_id = metadata.get("project_id")
        identity_matches = (
            isinstance(durable_project_id, str)
            and bool(durable_project_id.strip())
            and durable_project_id == str(row["project_id"])
        )
        if identity_matches:
            continue
        mismatches += 1

    if mismatches == 0:
        return ProjectionBindingRecovery(backup_dir=None)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    recovery_root = database_path.parent / "recovery"
    backup_dir = recovery_root / f"projection-bindings-{timestamp}"
    suffix = 1
    while backup_dir.exists():
        backup_dir = recovery_root / f"projection-bindings-{timestamp}-{suffix}"
        suffix += 1
    moved_files: list[tuple[Path, Path]] = []
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for database_suffix in ("", "-wal", "-shm"):
            source = Path(f"{database_path}{database_suffix}")
            if source.exists():
                destination = backup_dir / source.name
                shutil.move(str(source), str(destination))
                moved_files.append((source, destination))
    except OSError:
        for source, destination in reversed(moved_files):
            if destination.exists() and not source.exists():
                with suppress(OSError):
                    shutil.move(str(destination), str(source))
        with suppress(OSError):
            backup_dir.rmdir()
        logger.exception(
            "failed to quarantine mismatched state projection path={}",
            database_path,
        )
        return ProjectionBindingRecovery(backup_dir=None, mismatch_count=mismatches)

    logger.error(
        "quarantined mismatched session projection path={} backup={} sessions={}",
        database_path,
        backup_dir,
        mismatches,
    )
    return ProjectionBindingRecovery(
        backup_dir=backup_dir,
        mismatch_count=mismatches,
    )


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
    thread_runtime_registry: Any | None = None,
    expert_team_turn_router: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    logger: Any = default_logger,
) -> GatewayServices:
    state_path = workspace_path / ".nanobot" / "state.sqlite"
    binding_recovery = quarantine_mismatched_session_projection(
        state_path,
        session_manager=session_manager,
        logger=logger,
    )
    state_recovery = open_state_store_with_recovery(
        state_path,
        default_workspace=workspace_path,
        # Native desktop startup is user-facing and state.sqlite is a
        # rebuildable projection. StateStore initialization still validates
        # the header/schema/migrations and triggers the same recovery path for
        # databases that cannot be opened; avoid scanning every page of a
        # potentially very large projection before the local socket can bind.
        verify_integrity=runtime_surface != "native",
    )
    state = state_recovery.store
    database_rebuilt = (
        state_recovery.backup_dir is not None
        or binding_recovery.backup_dir is not None
    )
    state.reconcile_default_workspace_project()
    lifecycle = LifecycleRegistry(
        workspace_path / ".nanobot" / "lifecycle.jsonl"
    )
    # Upgrade existing soft archives once while the healthy projection still
    # knows which rows the user hid.  Thereafter lifecycle.jsonl is authoritative
    # and survives a state.sqlite rebuild.
    if not database_rebuilt:
        archived_projects = {
            project.id: project
            for project in state.list_projects(include_archived=True)
            if project.status == "archived"
        }
        for project in archived_projects.values():
            if lifecycle.project_state(project.id) is None:
                lifecycle.archive_project(
                    project.id,
                    canonical_root_path=project.canonical_root_path,
                    root_path=project.root_path,
                    name=project.name,
                )
        for session in state.list_sessions(include_archived=True):
            if (
                session.status == "archived"
                and session.project_id not in archived_projects
                and lifecycle.session_state(session.session_key) is None
            ):
                lifecycle.archive_session(
                    session.session_key,
                    session_id=session.id,
                    project_id=session.project_id,
                    title=session.title,
                )
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
    if binding_recovery.backup_dir is not None:
        logs.write(
            level="error",
            component="recovery",
            event_name="state_projection_bindings_rebuilt",
            message="mis-bound session projection was backed up and rebuilt",
            error_code="STATE_PROJECTION_BINDING_MISMATCH",
            details={
                "backup_dir": str(binding_recovery.backup_dir),
                "mismatch_count": binding_recovery.mismatch_count,
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
            "startup_full_integrity_check": runtime_surface != "native",
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
        lifecycle_registry=lifecycle,
        session_event_files=event_files,
        skills_workspace_path=workspace_path,
        disabled_skills=disabled_skills,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
        thread_runtime_registry=thread_runtime_registry,
        log=logger,
    )
    # A crash between recording a permanent-delete tombstone and cleaning all
    # backing files is completed here before any session can be listed again.
    http.reconcile_archived_lifecycle()
    http.reconcile_purged_lifecycle()
    # If state.sqlite was rebuilt, re-register disk-backed sessions from their
    # own persisted workspace metadata. Historical event projection is
    # intentionally lazy: SessionEventJournal.append() replays one session
    # before its next write. Eagerly replaying every transcript here can block
    # gateway startup for minutes on established workspaces.
    rebuilt_sessions = 0
    if database_rebuilt and session_manager is not None:
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
            "database_rebuilt": database_rebuilt,
            "projection_binding_mismatches": binding_recovery.mismatch_count,
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
        lifecycle=lifecycle,
        session_manager=session_manager,
        cron_service=cron_service,
        cron_pending_job_ids=cron_pending_job_ids,
        expert_team_turn_router=expert_team_turn_router,
    )

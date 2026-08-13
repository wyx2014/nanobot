"""Persisted WebUI project workspace state."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_webui_dir
from nanobot.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScope,
    WorkspaceScopeError,
    build_workspace_scope,
    default_workspace_scope,
    validate_workspace_scope_payload,
)
from nanobot.session.keys import webui_session_key_for_chat_id
from nanobot.storage.state import SessionProjectMismatch, StateStore

WEBUI_WORKSPACE_STATE_SCHEMA_VERSION = 1
PROJECT_ID_METADATA_KEY = "project_id"
SESSION_ID_METADATA_KEY = "session_id"
_MAX_STATE_FILE_BYTES = 128 * 1024
_DEFAULT_ACCESS_MODES = {"default", "full"}
_LEGACY_RESTRICTED_DEFAULT_ACCESS_MODE = "restricted"
_WEBUI_SCOPE_CHANNEL = "websocket"


def webui_workspace_state_path() -> Path:
    return get_webui_dir() / "workspace-state.json"


def default_webui_workspace_state() -> dict[str, Any]:
    return {
        "schema_version": WEBUI_WORKSPACE_STATE_SCHEMA_VERSION,
        "default_access_mode": "default",
        "updated_at": None,
    }


def normalize_webui_workspace_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    state = default_webui_workspace_state()
    updated_at = raw.get("updated_at")
    state["updated_at"] = updated_at if isinstance(updated_at, str) else None
    default_access_mode = raw.get("default_access_mode")
    if default_access_mode in _DEFAULT_ACCESS_MODES:
        state["default_access_mode"] = default_access_mode
    return state


def read_webui_workspace_state() -> dict[str, Any]:
    path = webui_workspace_state_path()
    if not path.is_file():
        return default_webui_workspace_state()
    try:
        if path.stat().st_size > _MAX_STATE_FILE_BYTES:
            logger.warning("webui workspace state too large, ignoring: {}", path)
            return default_webui_workspace_state()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("read webui workspace state failed {}: {}", path, e)
        return default_webui_workspace_state()
    return normalize_webui_workspace_state(raw)


def write_webui_workspace_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = normalize_webui_workspace_state(raw)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_STATE_FILE_BYTES:
        raise ValueError("workspace state is too large")

    path = webui_workspace_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "wb") as f:
        f.write(encoded)
        f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return state
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return state


def read_webui_default_access_mode() -> str:
    state = read_webui_workspace_state()
    mode = state.get("default_access_mode")
    return mode if mode in _DEFAULT_ACCESS_MODES else "default"


def write_webui_default_access_mode(mode: str) -> bool:
    if mode == _LEGACY_RESTRICTED_DEFAULT_ACCESS_MODE:
        mode = "default"
    if mode not in _DEFAULT_ACCESS_MODES:
        raise ValueError("default access mode must be default or full")
    state = read_webui_workspace_state()
    changed = state.get("default_access_mode") != mode
    if changed:
        state["default_access_mode"] = mode
        write_webui_workspace_state(state)
    return changed


def default_scope_for_webui(
    default_workspace: Path,
    default_restrict_to_workspace: bool,
) -> WorkspaceScope:
    mode = read_webui_default_access_mode()
    if mode == "default":
        return default_workspace_scope(
            default_workspace,
            default_restrict_to_workspace,
            source_channel=_WEBUI_SCOPE_CHANNEL,
        )
    return build_workspace_scope(default_workspace, mode, source_channel=_WEBUI_SCOPE_CHANNEL)


def workspaces_payload(
    *,
    default_workspace: Path,
    default_restrict_to_workspace: bool,
    controls_available: bool,
) -> dict[str, Any]:
    default_access_mode = read_webui_default_access_mode()
    default_scope = (
        default_workspace_scope(
            default_workspace,
            default_restrict_to_workspace,
            source_channel=_WEBUI_SCOPE_CHANNEL,
        )
        if default_access_mode == "default"
        else build_workspace_scope(default_workspace, default_access_mode, source_channel=_WEBUI_SCOPE_CHANNEL)
    )
    return {
        "schema_version": WEBUI_WORKSPACE_STATE_SCHEMA_VERSION,
        "default_access_mode": default_access_mode,
        "default_scope": default_scope.payload(),
        "controls": {
            "can_change_project": controls_available,
            "can_use_full_access": controls_available,
        },
    }


class WebUIWorkspaceController:
    """Own WebUI project scope persistence and validation."""

    def __init__(
        self,
        *,
        session_manager: Any | None,
        default_workspace: Path,
        default_restrict_to_workspace: bool,
        state_store: StateStore | None = None,
    ) -> None:
        self._sessions = session_manager
        self._default_workspace = default_workspace
        self._default_restrict_to_workspace = default_restrict_to_workspace
        self._state = state_store

    def default_scope(self) -> WorkspaceScope:
        return default_scope_for_webui(
            self._default_workspace,
            self._default_restrict_to_workspace,
        )

    def scope_for_session_metadata(
        self,
        metadata: dict[str, Any] | None,
        *,
        state_project: Any | None = None,
    ) -> WorkspaceScope:
        """Resolve a workspace scope from already-loaded projection data.

        Session list routes call this in a loop.  Accepting the SQLite project
        and the JSONL metadata record avoids reopening both stores per row.
        """
        metadata_scope: WorkspaceScope | None = None
        if isinstance(metadata, dict) and WORKSPACE_SCOPE_METADATA_KEY in metadata:
            raw_scope = metadata.get(WORKSPACE_SCOPE_METADATA_KEY)
            try:
                metadata_scope = validate_workspace_scope_payload(
                    raw_scope,
                    default_workspace=self._default_workspace,
                    default_restrict_to_workspace=self._default_restrict_to_workspace,
                    source_channel=_WEBUI_SCOPE_CHANNEL,
                )
            except WorkspaceScopeError:
                # Persisted identity must not silently fall back to Inbox just
                # because a user removed the project directory.  Listing and
                # recovery still need the original absolute root so lifecycle
                # tombstones can archive the correct project.  Fresh client
                # input continues to use validate_workspace_scope_payload(),
                # which requires an existing directory.
                raw_path = (
                    raw_scope.get("project_path") or raw_scope.get("path")
                    if isinstance(raw_scope, dict)
                    else None
                )
                raw_mode = (
                    raw_scope.get("access_mode")
                    if isinstance(raw_scope, dict)
                    else None
                )
                candidate = (
                    Path(raw_path).expanduser()
                    if isinstance(raw_path, str) and "\0" not in raw_path
                    else None
                )
                if candidate is not None and candidate.is_absolute():
                    try:
                        metadata_scope = build_workspace_scope(
                            candidate,
                            (
                                raw_mode
                                if isinstance(raw_mode, str)
                                else self.default_scope().access_mode
                            ),
                            source_channel=_WEBUI_SCOPE_CHANNEL,
                        )
                    except WorkspaceScopeError:
                        metadata_scope = None

        if state_project is None:
            return metadata_scope or self.default_scope()

        metadata_project_id = (
            metadata.get(PROJECT_ID_METADATA_KEY)
            if isinstance(metadata, dict)
            else None
        )
        state_project_id = str(getattr(state_project, "id", ""))
        # JSONL metadata and canonical events are the durable identity source
        # after a projection rebuild.  If SQLite points this session at a
        # different project, using its root would silently mix conversations,
        # plans and artifacts.  A matching project id still lets an explicit
        # relocate operation update the path without rewriting old metadata.
        if metadata_scope is not None:
            metadata_root = os.path.normcase(os.path.normpath(str(metadata_scope.project_path)))
            state_root = os.path.normcase(os.path.normpath(str(state_project.canonical_root_path)))
            identity_disagrees = bool(
                isinstance(metadata_project_id, str)
                and metadata_project_id
                and metadata_project_id != state_project_id
            )
            if metadata_root != state_root and (identity_disagrees or not metadata_project_id):
                return metadata_scope

        raw_scope = (
            metadata.get(WORKSPACE_SCOPE_METADATA_KEY)
            if isinstance(metadata, dict)
            else None
        )
        access_mode = (
            raw_scope.get("access_mode")
            if isinstance(raw_scope, dict)
            and isinstance(raw_scope.get("access_mode"), str)
            else self.default_scope().access_mode
        )
        return build_workspace_scope(
            state_project.canonical_root_path,
            access_mode,
            source_channel=_WEBUI_SCOPE_CHANNEL,
        )

    def scope_for_session_key(self, session_key: str) -> WorkspaceScope:
        state_session = self._state.get_session(session_key) if self._state is not None else None
        state_project = (
            self._state.get_project(state_session.project_id)
            if self._state is not None and state_session is not None
            else None
        )
        if self._sessions is None:
            if state_project is None:
                return self.default_scope()
            return build_workspace_scope(
                state_project.canonical_root_path,
                self.default_scope().access_mode,
                source_channel=_WEBUI_SCOPE_CHANNEL,
            )
        metadata_reader = getattr(self._sessions, "read_session_metadata", None)
        if callable(metadata_reader):
            data = metadata_reader(session_key)
        else:
            data = self._sessions.read_session_file(session_key)
        metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
        return self.scope_for_session_metadata(
            metadata,
            state_project=state_project,
        )

    def payload(self, *, controls_available: bool) -> dict[str, Any]:
        return workspaces_payload(
            default_workspace=self._default_workspace,
            default_restrict_to_workspace=self._default_restrict_to_workspace,
            controls_available=controls_available,
        )

    def scope_from_envelope(
        self,
        envelope: dict[str, Any],
        *,
        session_key: str | None,
        controls_available: bool,
    ) -> WorkspaceScope:
        raw = envelope.get(WORKSPACE_SCOPE_METADATA_KEY)
        if raw is None and session_key:
            scope = self.scope_for_session_key(session_key)
        elif raw is None:
            scope = self.default_scope()
        else:
            scope = validate_workspace_scope_payload(
                raw,
                default_workspace=self._default_workspace,
                default_restrict_to_workspace=self._default_restrict_to_workspace,
                source_channel=_WEBUI_SCOPE_CHANNEL,
            )
        if not controls_available and scope.metadata() != self.default_scope().metadata():
            raise WorkspaceScopeError("workspace controls are localhost-only", status=403)
        return scope

    def scope_for_new_chat(
        self,
        envelope: dict[str, Any],
        *,
        controls_available: bool,
    ) -> WorkspaceScope:
        return self.scope_from_envelope(
            envelope,
            session_key=None,
            controls_available=controls_available,
        )

    def scope_for_set_request(
        self,
        envelope: dict[str, Any],
        *,
        chat_id: str,
        chat_running: bool,
        controls_available: bool,
    ) -> WorkspaceScope:
        if chat_running:
            raise WorkspaceScopeError("chat_running", status=409)
        return self.scope_from_envelope(
            envelope,
            session_key=webui_session_key_for_chat_id(chat_id),
            controls_available=controls_available,
        )

    def scope_for_message(
        self,
        envelope: dict[str, Any],
        *,
        chat_id: str,
        chat_running: bool,
        controls_available: bool,
    ) -> WorkspaceScope:
        scope = self.scope_from_envelope(
            envelope,
            session_key=webui_session_key_for_chat_id(chat_id),
            controls_available=controls_available,
        )
        if (
            WORKSPACE_SCOPE_METADATA_KEY in envelope
            and chat_running
            and scope.metadata() != self.scope_for_session_key(
                webui_session_key_for_chat_id(chat_id)
            ).metadata()
        ):
            raise WorkspaceScopeError("chat_running", status=409)
        return scope

    def persist_scope(self, chat_id: str, scope: WorkspaceScope) -> dict[str, str]:
        binding: dict[str, str] = {}
        session_key = webui_session_key_for_chat_id(chat_id)
        session = (
            self._sessions.get_or_create(session_key)
            if self._sessions is not None
            else None
        )
        has_activity = bool(session.messages) if session is not None else False
        if self._state is not None:
            try:
                project, state_session = self._state.ensure_session_for_project(
                    session_key,
                    scope.project_path,
                    project_name=scope.project_name,
                    event_log_path=(
                        self._sessions.session_path(session_key)
                        if self._sessions is not None
                        else None
                    ),
                    title=(
                        str(session.metadata.get("title") or "")
                        if session is not None
                        else ""
                    ),
                    metadata={WORKSPACE_SCOPE_METADATA_KEY: scope.metadata()},
                    artifact_index_initialized=not has_activity,
                    allow_draft_rebind=not has_activity,
                )
            except SessionProjectMismatch as exc:
                raise WorkspaceScopeError(
                    "session_project_mismatch",
                    status=409,
                ) from exc
            if session is not None:
                session.metadata[PROJECT_ID_METADATA_KEY] = project.id
                session.metadata[SESSION_ID_METADATA_KEY] = state_session.id
            binding = {
                "project_id": project.id,
                "session_id": state_session.id,
            }
        if session is not None:
            session.metadata["webui"] = True
            session.metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            self._sessions.save(session)
        return binding

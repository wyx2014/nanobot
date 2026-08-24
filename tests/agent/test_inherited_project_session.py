from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.security.workspace_access import WorkspaceScopeResolver
from nanobot.session.manager import Session
from nanobot.storage.state import StateStore, StateStoreError


def _loop_for_workspace(workspace: Path) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = workspace
    loop.workspace_scopes = WorkspaceScopeResolver(
        default_workspace=workspace,
        default_restrict_to_workspace=False,
    )
    return loop


def test_legacy_cron_child_recovers_scope_from_registered_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_path = tmp_path / "project"
    project_path.mkdir()
    state = StateStore(
        workspace / ".nanobot" / "state.sqlite",
        default_workspace=workspace,
    )
    project = state.ensure_project(project_path)
    session_key = "cron:weekly:run-1"
    session = Session(key=session_key)
    message = InboundMessage(
        channel="websocket",
        sender_id="cron",
        chat_id=session_key,
        content="run",
        metadata={
            "project_id": project.id,
            "_parent_project_id": project.id,
        },
        session_key_override=session_key,
    )
    ctx = SimpleNamespace(msg=message, session=session, session_key=session_key)

    _loop_for_workspace(workspace)._ensure_inherited_project_session(ctx)

    expected_scope = {
        "project_path": project.canonical_root_path,
        "access_mode": "full",
    }
    assert message.metadata["workspace_scope"] == expected_scope
    assert session.metadata["workspace_scope"] == expected_scope
    assert session.metadata["project_id"] == project.id
    assert state.get_session(session_key) is not None


def test_cron_child_rejects_explicit_scope_for_different_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_path = tmp_path / "project"
    project_path.mkdir()
    wrong_path = tmp_path / "wrong-project"
    wrong_path.mkdir()
    state = StateStore(
        workspace / ".nanobot" / "state.sqlite",
        default_workspace=workspace,
    )
    project = state.ensure_project(project_path)
    session_key = "cron:weekly:run-2"
    session = Session(key=session_key)
    message = InboundMessage(
        channel="websocket",
        sender_id="cron",
        chat_id=session_key,
        content="run",
        metadata={
            "project_id": project.id,
            "workspace_scope": {
                "project_path": str(wrong_path),
                "access_mode": "full",
            },
        },
        session_key_override=session_key,
    )
    ctx = SimpleNamespace(msg=message, session=session, session_key=session_key)

    with pytest.raises(
        StateStoreError,
        match="inherited project does not match the effective workspace",
    ):
        _loop_for_workspace(workspace)._ensure_inherited_project_session(ctx)

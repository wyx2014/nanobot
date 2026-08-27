from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketConfig
from nanobot.storage.state import StateStore, open_state_store_with_recovery
from nanobot.webui import gateway_services


@pytest.mark.parametrize(
    ("runtime_surface", "expected_verify_integrity"),
    [("native", False), ("browser", True)],
)
def test_gateway_integrity_scan_policy_matches_runtime_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_surface: str,
    expected_verify_integrity: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    def tracked_open(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        return open_state_store_with_recovery(*args, **kwargs)

    monkeypatch.setattr(
        gateway_services,
        "open_state_store_with_recovery",
        tracked_open,
    )

    gateway_services.build_gateway_services(
        config=WebSocketConfig(),
        bus=MessageBus(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface=runtime_surface,
        runtime_capabilities_overrides=None,
    )

    assert calls[0]["verify_integrity"] is expected_verify_integrity


def test_reconciles_brand_migrated_project_path_without_rebinding_session(
    tmp_path: Path,
) -> None:
    default_workspace = tmp_path / "runtime"
    previous_root = tmp_path / "Documents" / "TPACowork Projects"
    current_root = tmp_path / "Documents" / "TPCowork Projects"
    previous_project = previous_root / "research"
    current_project = current_root / "research"
    default_workspace.mkdir()
    previous_project.mkdir(parents=True)
    state = StateStore(
        default_workspace / "state.sqlite",
        default_workspace=default_workspace,
    )
    project, session = state.ensure_session_for_project(
        "websocket:chat-1",
        previous_project,
    )
    artifact_path = previous_project / "reports" / "research.html"
    artifact_path.parent.mkdir()
    artifact_path.write_text("<h1>research</h1>", encoding="utf-8")
    artifact = state.register_artifact(
        session.session_key,
        artifact_path,
        relation_type="final",
        artifact_kind="file",
        mime_type="text/html",
    )
    previous_root.rename(current_root)

    [missing_artifact] = state.list_session_artifacts(session.session_key)
    assert missing_artifact.id == artifact.id
    assert missing_artifact.status == "missing"

    class MigratedSessionMetadata:
        @staticmethod
        def read_session_metadata(key: str) -> dict[str, Any]:
            assert key == session.session_key
            return {
                "metadata": {
                    "project_id": project.id,
                    "workspace_scope": {"project_path": str(current_project)},
                }
            }

    relocated = gateway_services.reconcile_relocated_session_projects(
        state,
        session_manager=MigratedSessionMetadata(),
    )

    repaired = state.get_project(project.id)
    assert relocated == 1
    assert repaired is not None
    assert repaired.canonical_root_path == str(current_project.resolve())
    assert state.get_session(session.session_key).project_id == project.id

    [repaired_artifact] = state.list_session_artifacts(session.session_key)
    assert repaired_artifact.id == artifact.id
    assert repaired_artifact.status == "ready"
    resolved_artifact, resolved_path = state.resolve_artifact_path(
        artifact.id,
        session_key=session.session_key,
    )
    assert resolved_artifact.status == "ready"
    assert resolved_path == current_project / "reports" / "research.html"

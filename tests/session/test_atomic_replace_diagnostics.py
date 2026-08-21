from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.session.manager import _ACTIVE_SESSION_SAVES, SessionManager
from nanobot.utils import windows_file_diagnostics


def test_non_windows_diagnostic_contains_bounded_file_and_error_context(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl.tmp"
    target = tmp_path / "session.jsonl"
    source.write_text("pending", encoding="utf-8")
    target.write_text("current", encoding="utf-8")
    error = PermissionError(13, "denied", str(target))

    with patch.object(windows_file_diagnostics.sys, "platform", "linux"):
        result = windows_file_diagnostics.collect_atomic_replace_diagnostics(
            source,
            target,
            error,
            context={"operation_id": "save-1"},
        )

    assert result["event"] == "session_atomic_replace_failed"
    assert result["error"]["errno"] == 13
    assert result["source"]["exists"] is True
    assert result["target"]["exists"] is True
    assert result["context"] == {"operation_id": "save-1"}
    assert "windows" not in result


def test_windows_diagnostic_collects_lock_acl_and_defender_evidence(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl.tmp"
    target = tmp_path / "session.jsonl"
    source.write_text("pending", encoding="utf-8")
    target.write_text("current", encoding="utf-8")
    error = PermissionError(13, "denied", str(target))

    with (
        patch.object(windows_file_diagnostics.sys, "platform", "win32"),
        patch.object(
            windows_file_diagnostics,
            "_windows_file_attributes",
            return_value={"status": "ok", "flags": []},
        ),
        patch.object(
            windows_file_diagnostics,
            "_restart_manager_lockers",
            return_value={"status": "ok", "lockers": [{"pid": 42, "app_name": "scanner"}]},
        ),
        patch.object(
            windows_file_diagnostics,
            "_windows_acl_snapshot",
            return_value={"status": "ok", "stdout": "ACL"},
        ),
        patch.object(
            windows_file_diagnostics,
            "_recent_defender_events",
            return_value={"status": "ok", "stdout": "Event ID: 1123"},
        ),
    ):
        result = windows_file_diagnostics.collect_atomic_replace_diagnostics(
            source,
            target,
            error,
        )

    windows = result["windows"]
    assert windows["restart_manager"]["lockers"][0]["pid"] == 42
    assert windows["target_acl"]["stdout"] == "ACL"
    assert "1123" in windows["recent_defender_events"]["stdout"]


def test_session_save_logs_diagnostics_and_preserves_original_error(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:diagnostic")
    session.add_message("user", "hello")
    replace_error = PermissionError(13, "denied")

    with (
        patch("nanobot.session.manager.os.replace", side_effect=replace_error),
        patch(
            "nanobot.session.manager.collect_atomic_replace_diagnostics",
            return_value={
                "event": "session_atomic_replace_failed",
                "windows": {"restart_manager": {"lockers": [{"pid": 42}]}},
            },
        ) as collect,
        patch("nanobot.session.manager.logger.error") as log_error,
    ):
        with pytest.raises(PermissionError) as raised:
            manager.save(session)

    assert raised.value is replace_error
    collect.assert_called_once()
    context = collect.call_args.kwargs["context"]
    assert context["save_operation"]["session_key"] == session.key
    assert context["save_operation"]["started_at"]
    assert context["overlapping_saves_at_start"] == []
    assert context["message_count"] == 1
    assert context["elapsed_ms"] >= 0
    assert context["replace_elapsed_ms"] >= 0
    assert "session_atomic_replace_failed" in log_error.call_args.args[1]
    assert not manager.session_path(session.key).with_suffix(".jsonl.tmp").exists()
    assert _ACTIVE_SESSION_SAVES == {}


def test_diagnostic_failure_never_masks_replace_error(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:diagnostic-failure")
    session.add_message("user", "hello")
    replace_error = PermissionError(13, "denied")

    with (
        patch("nanobot.session.manager.os.replace", side_effect=replace_error),
        patch(
            "nanobot.session.manager.collect_atomic_replace_diagnostics",
            side_effect=RuntimeError("collector failed"),
        ),
        patch("nanobot.session.manager.logger.error") as log_error,
    ):
        with pytest.raises(PermissionError) as raised:
            manager.save(session)

    assert raised.value is replace_error
    assert "diagnostic_collection_error" in log_error.call_args.args[1]
    assert _ACTIVE_SESSION_SAVES == {}


def test_windows_sharing_violation_is_diagnosed(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:sharing-violation")
    session.add_message("user", "hello")
    replace_error = OSError("sharing violation")
    replace_error.winerror = 32  # type: ignore[attr-defined]

    with (
        patch.object(sys, "platform", "win32"),
        patch("nanobot.session.manager.os.replace", side_effect=replace_error),
        patch(
            "nanobot.session.manager.collect_atomic_replace_diagnostics",
            return_value={"event": "session_atomic_replace_failed"},
        ) as collect,
    ):
        with pytest.raises(OSError) as raised:
            manager.save(session)

    assert raised.value is replace_error
    collect.assert_called_once()
    assert _ACTIVE_SESSION_SAVES == {}

from __future__ import annotations

import sys
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import pytest

from nanobot.session.manager import (
    _ACTIVE_SESSION_SAVES,
    SessionManager,
    _tracked_session_file_read,
)
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
    assert context["replace_attempts"] == 1
    assert context["retry_wait_ms"] == 0
    assert context["spacing_wait_ms"] == 0
    assert context["elapsed_ms"] >= 0
    assert context["replace_elapsed_ms"] >= 0
    assert "session_atomic_replace_failed" in log_error.call_args.args[1]
    assert list(manager.sessions_dir.glob("*.tmp")) == []
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
        patch("nanobot.session.manager.time.sleep"),
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


def test_session_save_diagnostic_captures_active_internal_reader(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:active-reader")
    session.add_message("user", "first")
    manager.save(session)
    path = manager.session_path(session.key)
    reader_started = Event()
    release_reader = Event()

    def hold_reader() -> None:
        with _tracked_session_file_read(
            path,
            "webui_session_list_scan",
            fallback_key=path.stem,
        ):
            reader_started.set()
            release_reader.wait(timeout=2)

    reader = Thread(target=hold_reader, name="diagnostic-reader")
    reader.start()
    assert reader_started.wait(timeout=2)

    try:
        replace_error = PermissionError(13, "denied")
        with (
            patch("nanobot.session.manager.os.replace", side_effect=replace_error),
            patch(
                "nanobot.session.manager.collect_atomic_replace_diagnostics",
                return_value={"event": "session_atomic_replace_failed"},
            ) as collect,
        ):
            with pytest.raises(PermissionError):
                manager.save(session)

        context = collect.call_args.kwargs["context"]
        active = context["readers_before_replace"]["active_readers"]
        assert active[0]["operation"] == "webui_session_list_scan"
        assert active[0]["thread_name"] == "diagnostic-reader"
        assert active[0]["details"]["fallback_key"] == path.stem
        overlaps = context["readers_overlapping_replace"]["overlapping_readers"]
        assert overlaps[0]["operation_id"] == active[0]["operation_id"]
    finally:
        release_reader.set()
        reader.join(timeout=2)

    assert not reader.is_alive()


def test_session_save_diagnostic_retains_just_finished_reader(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:recent-reader")
    session.add_message("user", "first")
    manager.save(session)
    path = manager.session_path(session.key)

    with _tracked_session_file_read(path, "read_session_file", session_key=session.key):
        pass

    replace_error = PermissionError(13, "denied")
    with (
        patch("nanobot.session.manager.os.replace", side_effect=replace_error),
        patch(
            "nanobot.session.manager.collect_atomic_replace_diagnostics",
            return_value={"event": "session_atomic_replace_failed"},
        ) as collect,
    ):
        with pytest.raises(PermissionError):
            manager.save(session)

    context = collect.call_args.kwargs["context"]
    recent = context["readers_after_failure"]["recent_readers"]
    matching = [item for item in recent if item["operation"] == "read_session_file"]
    assert matching
    assert matching[-1]["details"]["session_key"] == session.key
    assert context["readers_overlapping_replace"]["overlapping_readers"] == []


def test_session_save_emits_phase_timings_only_after_slow_threshold(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:slow-save")
    session.add_message("user", "hello")

    with (
        patch("nanobot.session.manager._SLOW_SESSION_IO_LOG_MS", 0),
        patch("nanobot.session.manager.logger.warning") as log_warning,
    ):
        manager.save(session)

    slow_save = next(
        call for call in log_warning.call_args_list
        if call.args and str(call.args[0]).startswith("slow session save")
    )
    assert slow_save.args[1] == session.key
    assert slow_save.args[2] >= 0
    assert slow_save.args[3] >= 0
    assert slow_save.args[5] >= 0
    assert slow_save.args[6] >= 0


def test_tracked_session_read_emits_slow_operation_timing(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    with (
        patch("nanobot.session.manager._SLOW_SESSION_IO_LOG_MS", 0),
        patch("nanobot.session.manager.logger.warning") as log_warning,
    ):
        with _tracked_session_file_read(
            path,
            "read_session_file",
            session_key="websocket:slow-read",
        ) as handle:
            handle.read()

    assert log_warning.call_args.args[0].startswith("slow session read")
    assert log_warning.call_args.args[1] == "read_session_file"
    assert log_warning.call_args.args[2] == "websocket:slow-read"
    assert log_warning.call_args.args[3] >= 0

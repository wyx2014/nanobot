"""Tests for atomic session save and corrupt-file repair."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from nanobot.session import manager as session_manager_module
from nanobot.session.manager import Session, SessionManager


def _windows_permission_error(winerror: int) -> PermissionError:
    exc = PermissionError(13, "temporarily blocked")
    exc.winerror = winerror
    return exc


class TestAtomicSave:
    def test_save_creates_valid_jsonl(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:1")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi")

        mgr.save(session)

        path = mgr._get_session_path("test:1")
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        meta = json.loads(lines[0])
        assert meta["_type"] == "metadata"
        assert meta["key"] == "test:1"

        msg1 = json.loads(lines[1])
        assert msg1["role"] == "user"
        assert msg1["content"] == "hello"

    def test_no_tmp_file_left_after_successful_save(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:clean")
        mgr.save(session)

        tmp_files = list(mgr.sessions_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_tmp_file_cleaned_up_on_write_failure(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:fail")

        class BadMessage:
            def __init__(self, data):
                self.data = data

        original_dumps = json.dumps

        def failing_dumps(obj, **kwargs):
            if isinstance(obj, dict) and obj.get("role") == "assistant":
                raise OSError("simulated disk full")
            return original_dumps(obj, **kwargs)

        session = Session(key="test:fail")
        session.messages = [
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "will fail"},
        ]

        import unittest.mock
        with unittest.mock.patch("nanobot.session.manager.json.dumps", side_effect=failing_dumps):
            with pytest.raises(OSError, match="simulated disk full"):
                mgr.save(session)

        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_windows_transient_replace_error_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:retry")
        session.add_message("user", "latest")
        path = mgr._get_session_path(session.key)
        path.write_text("old", encoding="utf-8")
        real_replace = session_manager_module.os.replace
        attempts = 0
        sleeps: list[float] = []
        sources: list[Path] = []

        def flaky_replace(source: Path, destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            sources.append(Path(source))
            if attempts < 3:
                raise _windows_permission_error(5)
            real_replace(source, destination)

        monkeypatch.setattr(session_manager_module.sys, "platform", "win32")
        monkeypatch.setattr(session_manager_module.os, "replace", flaky_replace)
        monkeypatch.setattr(session_manager_module.time, "sleep", sleeps.append)

        mgr.save(session)

        assert attempts == 3
        assert sleeps == [0.05, 0.1]
        assert len(set(sources)) == 1
        assert sources[0].name.startswith(f"{path.name}.")
        assert sources[0].name.endswith(".tmp")
        assert path.read_text(encoding="utf-8") != "old"
        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_windows_replace_retry_exhaustion_preserves_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:retry-exhausted")
        session.add_message("user", "latest")
        path = mgr._get_session_path(session.key)
        path.write_text("old", encoding="utf-8")
        attempts = 0
        sleeps: list[float] = []

        def blocked_replace(_source: Path, _destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            raise _windows_permission_error(32)

        monkeypatch.setattr(session_manager_module.sys, "platform", "win32")
        monkeypatch.setattr(session_manager_module.os, "replace", blocked_replace)
        monkeypatch.setattr(session_manager_module.time, "sleep", sleeps.append)
        monkeypatch.setattr(
            session_manager_module,
            "collect_atomic_replace_diagnostics",
            lambda *_args, **_kwargs: {"event": "test"},
        )

        with pytest.raises(PermissionError):
            mgr.save(session)

        assert attempts == len(
            session_manager_module._WINDOWS_SESSION_REPLACE_RETRY_DELAYS_S
        ) + 1
        assert sleeps == list(
            session_manager_module._WINDOWS_SESSION_REPLACE_RETRY_DELAYS_S
        )
        assert path.read_text(encoding="utf-8") == "old"
        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_unrelated_replace_error_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:no-retry")
        path = mgr._get_session_path(session.key)
        path.write_text("old", encoding="utf-8")
        attempts = 0
        sleeps: list[float] = []

        def blocked_replace(_source: Path, _destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            raise _windows_permission_error(123)

        monkeypatch.setattr(session_manager_module.sys, "platform", "win32")
        monkeypatch.setattr(session_manager_module.os, "replace", blocked_replace)
        monkeypatch.setattr(session_manager_module.time, "sleep", sleeps.append)
        monkeypatch.setattr(
            session_manager_module,
            "collect_atomic_replace_diagnostics",
            lambda *_args, **_kwargs: {"event": "test"},
        )

        with pytest.raises(PermissionError):
            mgr.save(session)

        assert attempts == 1
        assert sleeps == []
        assert path.read_text(encoding="utf-8") == "old"
        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_windows_consecutive_replaces_are_spaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:spacing")
        clock = [1000.0]
        sleeps: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def advance(delay: float) -> None:
            sleeps.append(delay)
            clock[0] += delay

        monkeypatch.setattr(session_manager_module.sys, "platform", "win32")
        monkeypatch.setattr(session_manager_module.time, "monotonic", monotonic)
        monkeypatch.setattr(session_manager_module.time, "sleep", advance)

        mgr.save(session)
        clock[0] += 0.009
        session.add_message("user", "second checkpoint")
        mgr.save(session)

        assert sleeps == [pytest.approx(0.091)]
        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_same_session_writers_are_serialized_and_use_unique_temp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:serialized")
        session.add_message("user", "hello")
        real_replace = session_manager_module.os.replace
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        sources: list[Path] = []

        def slow_replace(source: Path, destination: Path) -> None:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                sources.append(Path(source))
            try:
                time.sleep(0.02)
                real_replace(source, destination)
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr(session_manager_module.os, "replace", slow_replace)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mgr.save, session) for _ in range(2)]
            for future in futures:
                future.result()

        assert max_active == 1
        assert len(set(sources)) == 2
        assert list(mgr.sessions_dir.glob("*.tmp")) == []

    def test_overwrite_preserves_latest_data(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:overwrite")

        session.add_message("user", "first")
        mgr.save(session)

        session.add_message("user", "second")
        mgr.save(session)

        mgr.invalidate("test:overwrite")
        loaded = mgr.get_or_create("test:overwrite")
        assert len(loaded.messages) == 2
        assert loaded.messages[0]["content"] == "first"
        assert loaded.messages[1]["content"] == "second"

    def test_consecutive_saves_are_consistent(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        session = Session(key="test:consistency")

        for i in range(5):
            session.add_message("user", f"msg{i}")
            mgr.save(session)

        mgr.invalidate("test:consistency")
        loaded = mgr.get_or_create("test:consistency")
        assert len(loaded.messages) == 5
        for i in range(5):
            assert loaded.messages[i]["content"] == f"msg{i}"


class TestRepairCorruptFile:
    def _write_corrupt_jsonl(self, path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_truncated_last_line_recovered(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:trunc")

        valid_meta = json.dumps({
            "_type": "metadata",
            "key": "test:trunc",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "metadata": {},
            "last_consolidated": 0,
        })
        valid_msg = json.dumps({"role": "user", "content": "hello"})

        self._write_corrupt_jsonl(path, [
            valid_meta,
            valid_msg,
            '{"role": "assistant", "content": "partial...',
        ])

        session = mgr._load("test:trunc")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "hello"

    def test_corrupt_metadata_line_skipped(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:badmeta")

        self._write_corrupt_jsonl(path, [
            "NOT VALID JSON!!!",
            '{"role": "user", "content": "survived"}',
        ])

        session = mgr._load("test:badmeta")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "survived"

    def test_all_corrupt_lines_returns_none(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:allbad")

        self._write_corrupt_jsonl(path, [
            "garbage line 1",
            "garbage line 2",
            "{{invalid json",
        ])

        session = mgr._load("test:allbad")
        assert session is None

    def test_empty_file_returns_empty_session(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

        session = mgr._load("test:empty")
        assert session is not None
        assert session.messages == []
        assert session.key == "test:empty"

    def test_repair_preserves_valid_messages_amid_corruption(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:mixed")

        self._write_corrupt_jsonl(path, [
            json.dumps({"_type": "metadata", "key": "test:mixed",
                        "created_at": datetime.now().isoformat(),
                        "updated_at": datetime.now().isoformat(),
                        "metadata": {}, "last_consolidated": 0}),
            "BROKEN",
            json.dumps({"role": "user", "content": "msg1"}),
            '{"role": "assistant", "content": "broken',
            json.dumps({"role": "user", "content": "msg2"}),
        ])

        session = mgr._load("test:mixed")
        assert session is not None
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "msg1"
        assert session.messages[1]["content"] == "msg2"

    def test_repair_with_bad_timestamp_uses_fallback(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:badts")

        self._write_corrupt_jsonl(path, [
            json.dumps({"_type": "metadata", "key": "test:badts",
                        "created_at": "not-a-date",
                        "updated_at": "also-bad",
                        "metadata": {}, "last_consolidated": 5}),
            json.dumps({"role": "user", "content": "hi"}),
        ])

        session = mgr._load("test:badts")
        assert session is not None
        # offset 5 exceeds the single loaded message; reset to avoid hiding history (#4066)
        assert session.last_consolidated == 0
        assert isinstance(session.created_at, datetime)

    def test_read_session_file_repairs_corrupt_jsonl(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:read-repair")

        self._write_corrupt_jsonl(path, [
            json.dumps({
                "_type": "metadata",
                "key": "test:read-repair",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "metadata": {"source": "repair"},
                "last_consolidated": 0,
            }),
            json.dumps({"role": "user", "content": "survived"}),
            '{"role": "assistant", "content": "partial...',
        ])

        payload = mgr.read_session_file("test:read-repair")
        assert payload is not None
        assert payload["key"] == "test:read-repair"
        assert payload["metadata"] == {"source": "repair"}
        assert payload["messages"] == [{"role": "user", "content": "survived"}]

    def test_list_sessions_keeps_repaired_corrupt_file(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:list-repair")

        self._write_corrupt_jsonl(path, [
            "NOT VALID JSON",
            json.dumps({
                "_type": "metadata",
                "key": "test:list-repair",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "metadata": {},
                "last_consolidated": 0,
            }),
            json.dumps({"role": "user", "content": "hello"}),
        ])

        sessions = mgr.list_sessions()
        assert any(s["key"] == "test:list-repair" for s in sessions)

    def test_get_or_create_returns_new_session_for_corrupt_file(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        path = mgr._get_session_path("test:fallback")

        self._write_corrupt_jsonl(path, ["{{{{"])

        session = mgr.get_or_create("test:fallback")
        assert session is not None
        assert session.messages == []
        assert session.key == "test:fallback"

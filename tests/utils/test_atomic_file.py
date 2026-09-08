import asyncio
import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.cron.service import CronService
from nanobot.session.manager import SessionManager
from nanobot.utils import atomic_file
from nanobot.webui import token_usage


def windows_error(code=5):
    error = PermissionError(errno.EACCES, "locked")
    error.winerror = code
    return error


@pytest.mark.parametrize("kind", ["cron", "session", "usage"])
@pytest.mark.parametrize("code", [5, 32, 33])
def test_stores_share_retry_policy_and_keep_complete_snapshots(tmp_path, monkeypatch, kind, code):
    monkeypatch.setattr(token_usage, "get_webui_dir", lambda: tmp_path)
    real_replace = atomic_file.os.replace
    attempts, waits = [], []
    def replace(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise windows_error(code)
        real_replace(source, target)
    monkeypatch.setattr(atomic_file.os, "replace", replace)
    monkeypatch.setattr(atomic_file.time, "sleep", waits.append)
    if kind == "cron":
        CronService._atomic_write(tmp_path / "jobs.json", '{"jobs": []}')
    elif kind == "session":
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("test")
        session.add_message("user", "persist me")
        manager.save(session)
        assert "persist me" in manager.session_path("test").read_text()
    else:
        state = token_usage.record_token_usage({"total_tokens": 120})
        assert sum(row["total_tokens"] for row in state["days"].values()) == 120
    assert len(attempts) == 3
    assert waits[:2] == [0.05, 0.1]
    assert len({source for source, _ in attempts}) == 1
    assert not list(tmp_path.rglob("*.tmp"))


def test_exhausted_retry_preserves_old_file_and_collects_diagnostics(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text("old")
    error = windows_error()
    calls = []
    def fail(*args):
        calls.append(args)
        raise error
    diagnostics = []
    monkeypatch.setattr(atomic_file.os, "replace", fail)
    monkeypatch.setattr(atomic_file.time, "sleep", lambda _: None)
    monkeypatch.setattr(atomic_file, "collect_atomic_replace_diagnostics", lambda *args, **kwargs: diagnostics.append(kwargs) or {})
    with pytest.raises(PermissionError) as raised:
        atomic_file.atomic_write(target, "new", label="test")
    assert raised.value is error
    assert len(calls) == 7
    assert diagnostics[0]["context"]["replace_attempts"] == 7
    assert target.read_text() == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_permanent_error_is_not_retried_or_masked_by_cleanup(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    target.write_text("old")
    original = OSError(errno.ENOSPC, "disk full")
    def fail(*args, **kwargs):
        raise original
    monkeypatch.setattr(atomic_file.os, "replace", fail)
    monkeypatch.setattr(atomic_file.time, "sleep", lambda _: pytest.fail("must not retry disk full"))
    monkeypatch.setattr(Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("cleanup denied")))
    with pytest.raises(OSError) as raised:
        atomic_file.atomic_write(target, "new", label="test")
    assert raised.value is original
    assert target.read_text() == "old"


def test_concurrent_usage_updates_keep_all_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(token_usage, "get_webui_dir", lambda: tmp_path)
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: token_usage.record_token_usage({"total_tokens": 10}, now=now), range(12)))
    state = token_usage.read_token_usage_state()
    assert state["days"]["2026-09-07"]["total_tokens"] == 120
    assert state["days"]["2026-09-07"]["requests"] == 12
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failure", ["locked", "corrupt", "schema", "oversize"])
def test_usage_update_never_overwrites_unreadable_history(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(token_usage, "get_webui_dir", lambda: tmp_path)
    token_usage.record_token_usage({"total_tokens": 120})
    target = token_usage.token_usage_state_path()
    if failure == "corrupt":
        target.write_text('{"days":')
    elif failure == "schema":
        target.write_text('{"days": null}')
    elif failure == "oversize":
        monkeypatch.setattr(token_usage, "_MAX_STATE_FILE_BYTES", 1)
    original = target.read_bytes()
    if failure == "locked":
        def fail_open(*args, **kwargs):
            raise windows_error()
        monkeypatch.setattr(token_usage, "open", fail_open, raising=False)
    with pytest.raises((PermissionError, ValueError)):
        token_usage.record_token_usage({"total_tokens": 10})
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


async def test_token_usage_hook_keeps_event_loop_responsive(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow_write(*args, **kwargs):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(token_usage, "record_token_usage", slow_write)
    hook = token_usage.TokenUsageHook()
    task = asyncio.create_task(hook.after_iteration(SimpleNamespace(usage={"total_tokens": 1}, session_key="dream:test")))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
        await task

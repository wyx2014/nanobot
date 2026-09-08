"""Exercise desktop log rotation with real Loguru file sinks."""

import gzip
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from loguru import logger

from nanobot.utils.desktop_logging import (
    DesktopLogRetention,
    DesktopLogRotation,
    add_desktop_file_logging,
)


@contextmanager
def file_logger(path, max_bytes=1024):
    handler = logger.add(
        path,
        format="{message}",
        encoding="utf-8",
        newline="",
        rotation=DesktopLogRotation(max_bytes),
        compression="gz",
        catch=False,
    )
    try:
        yield logger
    finally:
        logger.remove(handler)


def write_at(log, timestamp, message):
    log.patch(lambda record: record.update(time=timestamp)).info(message)


def test_rotates_on_local_midnight_and_keeps_current_path(tmp_path):
    path = tmp_path / "nanobot.log"
    before_midnight = datetime.now().astimezone().replace(hour=23, minute=59, second=59)
    after_midnight = before_midnight + timedelta(seconds=1)

    with file_logger(path) as log:
        write_at(log, before_midnight, "yesterday")
        write_at(log, after_midnight, "today")
        write_at(log, after_midnight + timedelta(seconds=1), "still today")

    archives = list(tmp_path.glob("nanobot.*.log.gz"))
    assert len(archives) == 1
    assert gzip.decompress(archives[0].read_bytes()) == b"yesterday\n"
    assert path.read_text() == "today\nstill today\n"


def test_size_rotation_counts_utf8_bytes(tmp_path):
    path = tmp_path / "nanobot.log"
    with file_logger(path, max_bytes=30) as log:
        log.info("\u4e2d" * 8)
        log.info("\u4e2d" * 2)

    archive, = tmp_path.glob("nanobot.*.log.gz")
    assert gzip.decompress(archive.read_bytes()).decode() == "\u4e2d" * 8 + "\n"
    assert path.read_text(encoding="utf-8") == "\u4e2d" * 2 + "\n"


@pytest.mark.parametrize("old_days,old_size", [(1, 10), (0, 2000)])
def test_rotates_existing_logs_after_restart(tmp_path, old_days, old_size):
    path = tmp_path / "nanobot.log"
    previous = "x" * old_size
    path.write_text(previous)
    modified = time.time() - old_days * 86400
    os.utime(path, (modified, modified))

    with file_logger(path) as log:
        log.info("new launch")

    archive, = tmp_path.glob("nanobot.*.log.gz")
    assert gzip.decompress(archive.read_bytes()).decode() == previous
    assert path.read_text() == "new launch\n"


def test_restart_on_same_day_appends_without_rotation(tmp_path):
    path = tmp_path / "nanobot.log"
    with file_logger(path) as log:
        log.info("first launch")
    with file_logger(path) as log:
        log.info("second launch")

    assert path.read_text() == "first launch\nsecond launch\n"
    assert list(tmp_path.glob("*.gz")) == []


def test_repeated_size_rotation_preserves_all_entries(tmp_path):
    path = tmp_path / "nanobot.log"
    with file_logger(path, max_bytes=5) as log:
        for entry in ("first", "second", "third"):
            log.info(entry)

    archives = list(tmp_path.glob("nanobot.*.log.gz"))
    assert len(archives) == 2
    assert {gzip.decompress(archive.read_bytes()) for archive in archives} == {
        b"first\n", b"second\n",
    }
    assert path.read_text() == "third\n"


def make_archive(tmp_path, index, age_days, size=10, compressed=False):
    suffix = ".log.gz" if compressed else ".log"
    path = tmp_path / f"nanobot.2026-08-01_00-00-00_{index:06d}{suffix}"
    path.write_bytes(b"x" * size)
    modified = time.time() - age_days * 86400
    os.utime(path, (modified, modified))
    return path


def test_retention_removes_expired_and_oldest_archives_within_disk_budget(tmp_path):
    active = tmp_path / "nanobot.log"
    active.write_text("active")
    expired = make_archive(tmp_path, 1, 366)
    oldest = make_archive(tmp_path, 2, 364)
    newer = make_archive(tmp_path, 3, 180, compressed=True)
    newest = make_archive(tmp_path, 4, 30)
    unrelated = tmp_path / "nanobot.manual-backup.log"
    unrelated.write_text("keep")
    startup = tmp_path / "startup.2026-08-01_00-00-00_000000.log"
    startup.write_text("keep")
    directory = tmp_path / "nanobot.2026-08-01_00-00-00_000005.log"
    directory.mkdir()

    DesktopLogRetention(active, max_bytes=20).cleanup()

    assert not expired.exists()
    assert not oldest.exists()
    assert all(path.exists() for path in (active, newer, newest, unrelated, startup, directory))


def test_file_sink_cleans_on_launch_and_keeps_info_level_and_channel(tmp_path):
    expired = make_archive(tmp_path, 1, 366)
    path = tmp_path / "nanobot.log"
    handler = add_desktop_file_logging(path, "{level} | {extra[channel]} | {message}")
    try:
        assert not expired.exists()
        logger.debug("debug omitted")
        logger.info("ready")
        logger.bind(channel="desktop").warning("warning retained")
    finally:
        logger.remove(handler)

    assert path.read_text() == "INFO | - | ready\nWARNING | desktop | warning retained\n"


def test_retention_failure_does_not_prevent_new_log_writes(tmp_path, monkeypatch):
    path = tmp_path / "nanobot.log"
    make_archive(tmp_path, 1, 366)

    def denied(_path):
        raise PermissionError("archive is locked")

    monkeypatch.setattr(type(path), "unlink", denied)
    handler = add_desktop_file_logging(path, "{message}")
    try:
        logger.info("still writable")
    finally:
        logger.remove(handler)
    assert path.read_text() == "still writable\n"

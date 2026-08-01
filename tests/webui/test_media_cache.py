from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.webui.media_cache import (
    cleanup_media_cache,
    media_cache_status,
    stage_media_cache_path,
)
from nanobot.webui.media_gateway import WebUIMediaGateway


def _write_with_mtime(path: Path, body: bytes, mtime: float) -> Path:
    path.write_bytes(body)
    os.utime(path, (mtime, mtime))
    return path


def test_stage_media_cache_reuses_unchanged_source(tmp_path: Path) -> None:
    source = tmp_path / "project" / "研究报告.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF stable")
    cache = tmp_path / "media" / "websocket"

    first = stage_media_cache_path(source, cache)
    second = stage_media_cache_path(source, cache)

    assert first == second
    assert first.name.startswith("cache-v1-")
    assert first.read_bytes() == source.read_bytes()
    assert [path for path in cache.iterdir() if path.is_file()] == [first]


def test_stage_media_cache_changes_identity_after_source_edit(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_text("first", encoding="utf-8")
    cache = tmp_path / "cache"

    first = stage_media_cache_path(source, cache)
    source.write_text("second version", encoding="utf-8")
    second = stage_media_cache_path(source, cache)

    assert first != second
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second version"


def test_cleanup_removes_legacy_expired_and_lru_but_keeps_uploads(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "websocket"
    cache.mkdir()
    now = 10_000.0
    legacy = _write_with_mtime(cache / f"{'a' * 12}-report.pdf", b"legacy", now)
    expired = _write_with_mtime(
        cache / f"cache-v1-{'b' * 24}-expired.pdf",
        b"expired",
        now - 500,
    )
    older = _write_with_mtime(
        cache / f"cache-v1-{'c' * 24}-older.pdf",
        b"123456",
        now - 50,
    )
    newest = _write_with_mtime(
        cache / f"cache-v1-{'d' * 24}-newest.pdf",
        b"abcdef",
        now - 10,
    )
    upload = _write_with_mtime(cache / f"{'e' * 12}.png", b"upload", now - 1000)

    report = cleanup_media_cache(cache, max_bytes=8, ttl_s=100, now=now)

    assert not legacy.exists()
    assert not expired.exists()
    assert not older.exists()
    assert newest.exists()
    assert upload.exists()
    assert report["removed_count"] == 3
    assert report["cache_bytes"] == newest.stat().st_size
    assert report["unmanaged_bytes"] == upload.stat().st_size
    assert report["over_quota"] is False


def test_clear_all_only_deletes_managed_transport_cache(tmp_path: Path) -> None:
    cache = tmp_path / "websocket"
    cache.mkdir()
    staged = cache / f"cache-v1-{'a' * 24}-report.pdf"
    legacy = cache / f"{'b' * 12}-old-report.pdf"
    upload = cache / f"{'c' * 12}.jpg"
    for path in (staged, legacy, upload):
        path.write_bytes(b"x")

    report = cleanup_media_cache(cache, clear_all=True)

    assert not staged.exists()
    assert not legacy.exists()
    assert upload.exists()
    assert report["removed_count"] == 2
    status = media_cache_status(cache)
    assert status["cache_file_count"] == 0
    assert status["total_file_count"] == 1


@pytest.mark.asyncio
async def test_gateway_maintenance_cleans_cache_without_user_action(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    websocket_cache = media_root / "websocket"
    websocket_cache.mkdir(parents=True)
    legacy = websocket_cache / f"{'d' * 12}-old-preview.pdf"
    legacy.write_bytes(b"obsolete preview")

    gateway = WebUIMediaGateway(
        workspace_path=tmp_path,
        logger=MagicMock(),
        media_dir=lambda channel=None: (
            websocket_cache if channel == "websocket" else media_root
        ),
        cache_startup_delay_s=0,
        cache_cleanup_interval_s=3600,
    )
    gateway.start_cache_maintenance()
    try:
        for _ in range(100):
            if not legacy.exists():
                break
            await asyncio.sleep(0.01)
        assert not legacy.exists()
    finally:
        await gateway.stop_cache_maintenance()

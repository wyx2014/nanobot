"""Bounded staging cache for WebUI media previews.

Generated artifacts remain authoritative in their project directories and in
the SQLite artifact registry.  This module only manages transport copies that
must live below ``media/websocket`` before the signed media endpoint can serve
them.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat as stat_module
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import safe_filename

DEFAULT_MEDIA_CACHE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_MEDIA_CACHE_TTL_S = 7 * 24 * 60 * 60
DEFAULT_MEDIA_CACHE_CLEANUP_INTERVAL_S = 6 * 60 * 60
DEFAULT_MEDIA_CACHE_STARTUP_DELAY_S = 30

_CACHE_VERSION = "v1"
_CACHE_PREFIX = f"cache-{_CACHE_VERSION}-"
_CACHE_FILE_RE = re.compile(r"^cache-v1-[0-9a-f]{24}-.+")
# Before the bounded cache existed, every replay created
# ``<12 hex chars>-<original name>``.  WebSocket uploads use
# ``<12 hex chars>.<extension>`` instead, so the hyphen is the safe boundary
# that lets us reclaim only obsolete transport copies.
_LEGACY_CACHE_FILE_RE = re.compile(r"^[0-9a-f]{12}-.+")
_CACHE_TEMP_RE = re.compile(r"^\.cache-v1-[0-9a-f]{24}-.+\.[0-9a-f]{12}\.tmp$")
_CACHE_NAME_MAX_CHARS = 180
_TEMP_FILE_TTL_S = 60 * 60


@dataclass(frozen=True)
class _CacheEntry:
    path: Path
    size: int
    accessed_at: float
    kind: str


def _bounded_safe_name(name: str) -> str:
    cleaned = safe_filename(name) or "attachment"
    if cleaned in {".", ".."}:
        cleaned = "attachment"
    if len(cleaned) <= _CACHE_NAME_MAX_CHARS:
        return cleaned
    suffix = Path(cleaned).suffix[:20]
    stem_budget = max(1, _CACHE_NAME_MAX_CHARS - len(suffix))
    stem = cleaned[:-len(suffix)] if suffix else cleaned
    return f"{stem[:stem_budget]}{suffix}"


def _source_cache_key(path: Path, source_stat: os.stat_result) -> str:
    resolved = path.expanduser().resolve(strict=True)
    identity = "\0".join(
        (
            str(resolved),
            str(source_stat.st_dev),
            str(source_stat.st_ino),
            str(source_stat.st_size),
            str(source_stat.st_mtime_ns),
            str(source_stat.st_ctime_ns),
        )
    )
    return hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


def stage_media_cache_path(path: Path, target_dir: Path) -> Path:
    """Return a stable cached copy of *path* under *target_dir*.

    Replaying the same session calls this function repeatedly.  The cache key
    includes the resolved source path and file identity, so an unchanged
    artifact reuses one copy while an edited artifact receives a fresh URL.
    Writes use a same-directory temporary file and ``os.replace`` to remain
    safe when multiple session reads race.
    """

    source = path.expanduser()
    source_stat = source.stat()
    if not stat_module.S_ISREG(source_stat.st_mode):
        raise OSError(f"media source is not a regular file: {source}")

    target_dir.mkdir(parents=True, exist_ok=True)
    cache_key = _source_cache_key(source, source_stat)
    safe_name = _bounded_safe_name(source.name)
    staged = target_dir / f"{_CACHE_PREFIX}{cache_key}-{safe_name}"

    try:
        staged_stat = staged.stat()
    except OSError:
        staged_stat = None
    if (
        staged_stat is not None
        and stat_module.S_ISREG(staged_stat.st_mode)
        and staged_stat.st_size == source_stat.st_size
    ):
        touch_media_cache_path(staged)
        return staged

    temporary = target_dir / f".{staged.name}.{uuid.uuid4().hex[:12]}.tmp"
    try:
        shutil.copyfile(source, temporary)
        copied_stat = temporary.stat()
        if copied_stat.st_size != source_stat.st_size:
            raise OSError("staged media size changed during copy")
        os.replace(temporary, staged)
        touch_media_cache_path(staged, force=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return staged


def is_managed_media_cache_name(name: str) -> bool:
    """Return whether *name* is a WebUI transport-cache file."""

    return bool(
        _CACHE_FILE_RE.fullmatch(name)
        or _LEGACY_CACHE_FILE_RE.fullmatch(name)
        or _CACHE_TEMP_RE.fullmatch(name)
    )


def touch_media_cache_path(path: Path, *, force: bool = False) -> None:
    """Refresh cache LRU time without writing on every range request."""

    if not _CACHE_FILE_RE.fullmatch(path.name):
        return
    try:
        current = path.stat().st_mtime
        now = time.time()
        if force or now - current >= 60:
            os.utime(path, (now, now))
    except OSError:
        return


def _entry_kind(name: str) -> str | None:
    if _CACHE_FILE_RE.fullmatch(name):
        return "cache"
    if _LEGACY_CACHE_FILE_RE.fullmatch(name):
        return "legacy"
    if _CACHE_TEMP_RE.fullmatch(name):
        return "temporary"
    return None


def _scan_cache(target_dir: Path) -> tuple[list[_CacheEntry], int, int, int]:
    entries: list[_CacheEntry] = []
    total_bytes = 0
    total_file_count = 0
    scan_errors = 0
    if not target_dir.is_dir():
        return entries, 0, 0, 1
    try:
        for path in target_dir.iterdir():
            try:
                file_stat = path.lstat()
            except OSError:
                scan_errors += 1
                continue
            if not stat_module.S_ISREG(file_stat.st_mode):
                continue
            total_bytes += file_stat.st_size
            total_file_count += 1
            kind = _entry_kind(path.name)
            if kind is not None:
                entries.append(
                    _CacheEntry(
                        path=path,
                        size=file_stat.st_size,
                        accessed_at=file_stat.st_mtime,
                        kind=kind,
                    )
                )
    except OSError:
        scan_errors += 1
    return entries, total_bytes, total_file_count, scan_errors


def _status_payload(
    *,
    target_dir: Path,
    entries: list[_CacheEntry],
    total_bytes: int,
    total_file_count: int,
    scan_errors: int,
    max_bytes: int,
    ttl_s: int,
) -> dict[str, Any]:
    cache_bytes = sum(entry.size for entry in entries)
    legacy_entries = [entry for entry in entries if entry.kind == "legacy"]
    accessed = [entry.accessed_at for entry in entries]
    return {
        "directory": str(target_dir),
        "cache_bytes": cache_bytes,
        "cache_file_count": len(entries),
        "total_bytes": max(0, total_bytes),
        "total_file_count": max(0, total_file_count),
        "unmanaged_bytes": max(0, total_bytes - cache_bytes),
        "legacy_bytes": sum(entry.size for entry in legacy_entries),
        "legacy_file_count": len(legacy_entries),
        "max_bytes": max_bytes,
        "ttl_seconds": ttl_s,
        "over_quota": cache_bytes > max_bytes,
        "oldest_accessed_at": min(accessed) if accessed else None,
        "newest_accessed_at": max(accessed) if accessed else None,
        "scan_errors": scan_errors,
    }


def media_cache_status(
    target_dir: Path,
    *,
    max_bytes: int = DEFAULT_MEDIA_CACHE_MAX_BYTES,
    ttl_s: int = DEFAULT_MEDIA_CACHE_TTL_S,
) -> dict[str, Any]:
    """Return bounded-cache usage without mutating the directory."""

    entries, total_bytes, total_file_count, scan_errors = _scan_cache(target_dir)
    return _status_payload(
        target_dir=target_dir,
        entries=entries,
        total_bytes=total_bytes,
        total_file_count=total_file_count,
        scan_errors=scan_errors,
        max_bytes=max_bytes,
        ttl_s=ttl_s,
    )


def cleanup_media_cache(
    target_dir: Path,
    *,
    max_bytes: int = DEFAULT_MEDIA_CACHE_MAX_BYTES,
    ttl_s: int = DEFAULT_MEDIA_CACHE_TTL_S,
    clear_all: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove obsolete staged copies and enforce the LRU byte budget.

    Files uploaded by the user are intentionally unmanaged and never selected
    for deletion here.  Legacy random-prefix staging copies are always safe to
    reclaim after a gateway restart because their old signed URLs cannot be
    validated by the new gateway secret.
    """

    current_time = time.time() if now is None else now
    entries, total_bytes, total_file_count, scan_errors = _scan_cache(target_dir)
    removed_paths: set[Path] = set()
    removed_bytes = 0
    failed_count = 0

    def remove(entry: _CacheEntry) -> bool:
        nonlocal removed_bytes, failed_count
        if entry.path in removed_paths:
            return True
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            failed_count += 1
            return False
        removed_paths.add(entry.path)
        removed_bytes += entry.size
        return True

    for entry in entries:
        expired = current_time - entry.accessed_at > ttl_s
        stale_temporary = (
            entry.kind == "temporary"
            and current_time - entry.accessed_at > _TEMP_FILE_TTL_S
        )
        if clear_all or entry.kind == "legacy" or expired or stale_temporary:
            remove(entry)

    survivors = [entry for entry in entries if entry.path not in removed_paths]
    survivor_bytes = sum(entry.size for entry in survivors)
    if not clear_all and survivor_bytes > max_bytes:
        for entry in sorted(survivors, key=lambda item: item.accessed_at):
            if survivor_bytes <= max_bytes:
                break
            if remove(entry):
                survivor_bytes -= entry.size

    remaining = [entry for entry in entries if entry.path not in removed_paths]
    status = _status_payload(
        target_dir=target_dir,
        entries=remaining,
        total_bytes=total_bytes - removed_bytes,
        total_file_count=total_file_count - len(removed_paths),
        scan_errors=scan_errors,
        max_bytes=max_bytes,
        ttl_s=ttl_s,
    )
    status.update(
        {
            "removed_count": len(removed_paths),
            "removed_bytes": removed_bytes,
            "failed_count": failed_count,
            "cleaned_at": current_time,
        }
    )
    return status

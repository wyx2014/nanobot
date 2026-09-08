"""Atomic persistence with bounded retries for Windows sharing conflicts."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from nanobot.utils.windows_file_diagnostics import collect_atomic_replace_diagnostics

REPLACE_RETRY_DELAYS_S = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)
WINDOWS_REPLACE_ERRORS = {5, 32, 33}


@dataclass
class ReplaceStats:
    attempts: int = 0
    retry_wait_s: float = 0.0


def is_retryable_replace_error(error: OSError) -> bool:
    return getattr(error, "winerror", None) in WINDOWS_REPLACE_ERRORS


def replace_with_retry(
    source: Path, target: Path, *, label: str,
    stats: ReplaceStats | None = None, diagnose: bool = True,
) -> ReplaceStats:
    stats = stats if stats is not None else ReplaceStats()
    try:
        for attempt, delay in enumerate((*REPLACE_RETRY_DELAYS_S, None), start=1):
            stats.attempts = attempt
            try:
                os.replace(source, target)
                break
            except OSError as exc:
                if not is_retryable_replace_error(exc) or delay is None:
                    raise
                logger.warning(
                    "{} atomic replace temporarily blocked for {} (attempt {}/{}, retry_in_ms={}): {}",
                    label, target, attempt, len(REPLACE_RETRY_DELAYS_S) + 1,
                    round(delay * 1000), exc,
                )
                time.sleep(delay)
                stats.retry_wait_s += delay
    except OSError as exc:
        if diagnose and (isinstance(exc, PermissionError) or is_retryable_replace_error(exc)):
            try:
                diagnostic = collect_atomic_replace_diagnostics(source, target, exc, context={
                    "store": label, "replace_attempts": stats.attempts,
                    "retry_wait_ms": round(stats.retry_wait_s * 1000),
                })
                diagnostic["event"] = "atomic_replace_failed"
                logger.error("{} atomic replace diagnostic: {}", label, json.dumps(diagnostic, default=str))
            except Exception as diagnostic_error:
                logger.error("{} save failed for {}; diagnostics unavailable: {}", label, target, diagnostic_error)
        raise
    if stats.attempts > 1:
        logger.info("{} atomic replace recovered for {} after {} attempts (retry_wait_ms={})",
                    label, target, stats.attempts, round(stats.retry_wait_s * 1000))
    return stats


def atomic_write(path: Path, content: str | bytes, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path, label=label)
        with suppress(PermissionError):
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        # Cleanup failures must not hide the original write/replace error.
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise

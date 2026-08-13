"""Task-local identity for one cron execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class CronRunContext:
    """Identity allocated before a cron callback starts running."""

    job_id: str
    run_id: str
    session_key: str | None = None


_CURRENT_CRON_RUN: ContextVar[CronRunContext | None] = ContextVar(
    "nanobot_current_cron_run",
    default=None,
)


def current_cron_run_context() -> CronRunContext | None:
    """Return the cron execution bound to the current asyncio task."""
    return _CURRENT_CRON_RUN.get()


@contextmanager
def bind_cron_run_context(context: CronRunContext) -> Iterator[CronRunContext]:
    """Bind one execution identity for the duration of its callback."""
    token = _CURRENT_CRON_RUN.set(context)
    try:
        yield context
    finally:
        _CURRENT_CRON_RUN.reset(token)

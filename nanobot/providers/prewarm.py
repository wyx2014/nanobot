"""Best-effort provider prewarming for latency-sensitive runtimes."""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from nanobot.providers.base import LLMProvider
from nanobot.providers.fallback_provider import FallbackProvider

DESKTOP_PROVIDER_PREWARM_TIMEOUT_S = 5.0


def _primary_provider(provider: LLMProvider) -> LLMProvider:
    """Return the configured primary without invoking fallback models."""
    while isinstance(provider, FallbackProvider):
        provider = provider._primary
    return provider


async def prewarm_provider(
    provider: LLMProvider,
    model: str,
    *,
    timeout_s: float = DESKTOP_PROVIDER_PREWARM_TIMEOUT_S,
) -> bool:
    """Issue one tiny, tool-free request without touching sessions or usage hooks.

    This is deliberately best-effort: desktop startup and real user turns must
    remain available when the provider is slow, unavailable, or rejects the
    minimal request.  Fallback models are not tried, so one prewarm schedules at
    most one external model request.
    """
    target = _primary_provider(provider)
    started_at = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            target.chat(
                messages=[{"role": "user", "content": "ping"}],
                tools=None,
                model=model,
                max_tokens=1,
                temperature=0.0,
                reasoning_effort=None,
                tool_choice=None,
            ),
            timeout=max(0.1, timeout_s),
        )
    except TimeoutError:
        logger.debug(
            "Provider prewarm timed out after {:.1f}s for model '{}'",
            timeout_s,
            model,
        )
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Provider prewarm failed for model '{}': {}", model, exc)
        return False

    if response.finish_reason == "error":
        logger.debug(
            "Provider prewarm was rejected for model '{}': {}",
            model,
            (response.content or "unknown error")[:160],
        )
        return False

    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    logger.info(
        "Provider prewarm completed for model '{}' in {} ms",
        model,
        elapsed_ms,
    )
    return True

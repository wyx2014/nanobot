from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.fallback_provider import FallbackProvider
from nanobot.providers.prewarm import prewarm_provider


@pytest.mark.asyncio
async def test_prewarm_uses_one_tiny_tool_free_request() -> None:
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(content="OK", finish_reason="stop")

    assert await prewarm_provider(provider, "test-model") is True

    provider.chat.assert_awaited_once_with(
        messages=[{"role": "user", "content": "ping"}],
        tools=None,
        model="test-model",
        max_tokens=1,
        temperature=0.0,
        reasoning_effort=None,
        tool_choice=None,
    )


@pytest.mark.asyncio
async def test_prewarm_times_out_without_raising() -> None:
    cancelled = asyncio.Event()

    async def slow_chat(**_kwargs: object) -> LLMResponse:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return LLMResponse(content="late", finish_reason="stop")

    provider = AsyncMock()
    provider.chat.side_effect = slow_chat

    assert await prewarm_provider(provider, "slow-model", timeout_s=0.01) is False
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_prewarm_does_not_invoke_configured_fallbacks() -> None:
    primary = AsyncMock()
    primary.chat.return_value = LLMResponse(
        content="provider unavailable",
        finish_reason="error",
    )
    fallback_factory = AsyncMock()
    provider = FallbackProvider(
        primary=primary,
        fallback_presets=[object()],
        provider_factory=fallback_factory,
    )

    assert await prewarm_provider(provider, "primary-model") is False

    primary.chat.assert_awaited_once()
    fallback_factory.assert_not_called()

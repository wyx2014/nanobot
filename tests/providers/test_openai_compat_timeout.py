import asyncio
import time
from unittest.mock import patch, sentinel

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import ProviderSpec


def _assert_openai_compat_timeout(timeout) -> None:
    assert timeout == 120.0


async def test_openai_compat_provider_defers_sdk_client_until_first_use() -> None:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        mock_async_openai.assert_not_called()
        await provider._ensure_client()

    kwargs = mock_async_openai.call_args.kwargs
    _assert_openai_compat_timeout(kwargs["timeout"])
    # Cloud endpoints pass http_client=None so the SDK creates its own
    # DefaultAsyncHttpxClient, which already handles proxy env vars,
    # connection limits, and redirects correctly.
    assert kwargs["http_client"] is None


async def test_openai_compat_provider_sets_timeout_on_local_http_client() -> None:
    spec = ProviderSpec(
        name="local",
        keywords=(),
        env_key="",
        is_local=True,
        default_api_base="http://127.0.0.1:11434/v1",
    )

    with (
        patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai,
        patch(
            "httpx.AsyncClient",
            return_value=sentinel.http_client,
        ) as mock_http_client,
    ):
        provider = OpenAICompatProvider(spec=spec)
        mock_async_openai.assert_not_called()
        await provider._ensure_client()

    client_kwargs = mock_http_client.call_args.kwargs
    _assert_openai_compat_timeout(client_kwargs["timeout"])
    assert client_kwargs["limits"].keepalive_expiry == 0

    openai_kwargs = mock_async_openai.call_args.kwargs
    _assert_openai_compat_timeout(openai_kwargs["timeout"])
    assert openai_kwargs["http_client"] is sentinel.http_client


async def test_openai_compat_provider_timeout_can_be_overridden_by_env(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_OPENAI_COMPAT_TIMEOUT_S", "45")

    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        await provider._ensure_client()

    assert mock_async_openai.call_args.kwargs["timeout"] == 45.0


async def test_openai_compat_provider_initializes_client_off_event_loop(monkeypatch) -> None:
    calls = []

    async def fake_to_thread(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "nanobot.providers.openai_compat_provider.asyncio.to_thread",
        fake_to_thread,
    )
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
        await provider._ensure_client()

    assert calls == [provider._initialize_client_sync]


async def test_slow_sdk_initialization_keeps_event_loop_responsive(monkeypatch) -> None:
    provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
    client = object()

    def slow_initialization() -> None:
        time.sleep(0.1)
        provider._client = client

    monkeypatch.setattr(provider, "_initialize_client_sync", slow_initialization)
    initialization = asyncio.create_task(provider._ensure_client())
    await asyncio.sleep(0.01)

    assert initialization.done() is False
    assert await initialization is client


async def test_timed_out_prewarm_reuses_inflight_sdk_initialization(monkeypatch) -> None:
    provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
    client = object()
    calls = 0

    def slow_initialization() -> None:
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        provider._client = client

    monkeypatch.setattr(provider, "_initialize_client_sync", slow_initialization)
    try:
        await asyncio.wait_for(provider._ensure_client(), timeout=0.01)
    except TimeoutError:
        pass

    assert await provider._ensure_client() is client
    assert calls == 1

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.webui.ws_http import GatewayHTTPHandler


@pytest.mark.asyncio
async def test_blocking_stage_log_separates_queue_execution_and_loop_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nanobot.webui.ws_http._SLOW_WEBUI_STAGE_LOG_MS", 0)
    handler = object.__new__(GatewayHTTPHandler)
    handler._log = MagicMock()

    result = await handler._run_blocking_stage(
        "thread",
        "message_query",
        lambda: "done",
    )

    assert result == "done"
    message, route, stage, duration, queue, execution, resume, outcome = (
        handler._log.warning.call_args.args
    )
    assert message.startswith("slow webui stage")
    assert route == "thread"
    assert stage == "message_query"
    assert duration >= 0
    assert queue >= 0
    assert execution >= 0
    assert resume >= 0
    assert outcome == "ok"


@pytest.mark.asyncio
async def test_blocking_stage_logs_worker_exception_without_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nanobot.webui.ws_http._SLOW_WEBUI_STAGE_LOG_MS", 0)
    handler = object.__new__(GatewayHTTPHandler)
    handler._log = MagicMock()

    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await handler._run_blocking_stage("projects", "state_project_query", fail)

    assert handler._log.warning.call_args.args[-1] == "RuntimeError"

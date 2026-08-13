from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.bound_runner import run_bound_cron_job
from nanobot.cron.run_context import CronRunContext, bind_cron_run_context
from nanobot.cron.types import CronJob, CronPayload


class _Agent:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.message: InboundMessage | None = None

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage:
        self.message = msg
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")


@pytest.mark.asyncio
async def test_bound_webui_run_reuses_preallocated_identity_and_routes_to_child_chat() -> None:
    job = CronJob(
        id="daily-news",
        name="Daily news",
        payload=CronPayload(
            message="Summarize today's news",
            session_key="websocket:parent-chat",
            origin_channel="websocket",
            origin_chat_id="parent-chat",
        ),
    )
    agent = _Agent()
    cron = SimpleNamespace(write_run_record=lambda *_args: None)
    context = CronRunContext(
        job_id=job.id,
        run_id="1723456789000:abcd1234",
        session_key="cron:daily-news:1723456789000:abcd1234",
    )

    with bind_cron_run_context(context):
        result = await run_bound_cron_job(job, agent=agent, cron=cron)

    assert result.run_id == context.run_id
    assert result.session_key == context.session_key
    assert agent.message is not None
    assert agent.message.chat_id == context.session_key
    assert agent.message.session_key_override == context.session_key
    assert agent.message.metadata["_webui_transcript_session_key"] == context.session_key

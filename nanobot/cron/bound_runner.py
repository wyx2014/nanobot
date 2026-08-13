"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any, Protocol

from nanobot.agent.tools.cron import CronTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.run_context import current_cron_run_context
from nanobot.cron.session_delivery import origin_delivery_context
from nanobot.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanobot.cron.types import CronJob, CronJobExecutionResult
from nanobot.cron.webui_metadata import cron_proactive_delivery_metadata
from nanobot.security.project_context import PROJECT_CONTEXT_METADATA_KEY
from nanobot.utils.prompt_templates import render_template


class BoundCronAgent(Protocol):
    tools: Any

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class CronRunRecorder(Protocol):
    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        ...


def _cron_prompt_ref(prompt: str) -> dict[str, Any]:
    return {
        "id": "cron.agent_turn.reminder",
        "version": 1,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _bound_session_delivery_context(
    job: CronJob,
    *,
    turn_seed: str,
    source_label: str | None,
) -> tuple[str, str, dict[str, Any]]:
    channel, chat_id, metadata = origin_delivery_context(job)

    if channel == "websocket":
        metadata["webui"] = True
        metadata.update(
            cron_proactive_delivery_metadata(
                "websocket",
                metadata,
                turn_seed=turn_seed,
                source_label=source_label,
            )
        )

    return channel, chat_id, metadata


async def run_bound_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
) -> CronJobExecutionResult:
    """Execute a session-bound cron job as a normal agent session turn."""
    session_key = job.payload.session_key
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_context = current_cron_run_context()
    if (
        run_context is not None
        and run_context.job_id == job.id
        and run_context.session_key
    ):
        run_id = run_context.run_id
        run_session_key = run_context.session_key
    else:
        run_id = f"{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
        run_session_key = f"cron:{job.id}:{run_id}"
    channel, chat_id, metadata = _bound_session_delivery_context(
        job,
        turn_seed=run_session_key,
        source_label=job.name,
    )
    raw_project_context = metadata.get(PROJECT_CONTEXT_METADATA_KEY)
    origin_project_id = (
        raw_project_context.get("project_id")
        if isinstance(raw_project_context, dict)
        else None
    )
    if (
        job.payload.project_id
        and origin_project_id
        and origin_project_id != job.payload.project_id
    ):
        raise ValueError(
            f"cron job {job.id} project identity does not match its origin metadata"
        )
    if job.payload.project_id:
        metadata["project_id"] = job.payload.project_id
        metadata["_parent_project_id"] = job.payload.project_id
    # A cron run is a new child session. Never reuse the parent's session ID
    # as the active ProjectContext identity for that child.
    metadata.pop(PROJECT_CONTEXT_METADATA_KEY, None)
    metadata[CRON_TRIGGER_META] = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "prompt_ref": prompt_ref,
        "persist_content": (
            f"Scheduled cron job triggered: {job.name}\n\n{job.payload.message}"
        ),
    }
    metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    metadata["_webui_transcript_session_key"] = run_session_key
    run_record_base: dict[str, Any] = {
        "job_id": job.id,
        "job_name": job.name,
        "parent_session_key": session_key,
        "session_key": run_session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
    }

    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "queued",
        },
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    delivery_chat_id = run_session_key if channel == "websocket" else chat_id
    try:
        resp = await agent.submit_cron_turn(
            InboundMessage(
                channel=channel,
                sender_id="cron",
                chat_id=delivery_chat_id,
                content=prompt,
                metadata=metadata,
                session_key_override=run_session_key,
            )
        )
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
        },
    )
    return CronJobExecutionResult(
        response=response,
        run_id=run_id,
        session_key=run_session_key,
    )

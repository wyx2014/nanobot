from __future__ import annotations

import json

import pytest

from nanobot.bus.runtime_events import RuntimeEventContext
from nanobot.observability.trace_collector import TraceCollector
from nanobot.observability.trace_store import TraceStore
from nanobot.runtime.turn_lifecycle import (
    FinishReason,
    ThreadRuntimeRegistry,
    TurnLifecycleManager,
    TurnStatus,
)


def test_trace_store_builds_run_span_tree_and_redacts_secrets(tmp_path) -> None:
    store = TraceStore(tmp_path / "logs.sqlite")
    store.begin_trace(
        trace_id="trc_a",
        project_id="prj_a",
        session_id="ses_a",
        turn_id="turn_a",
        runtime_epoch="epoch_a",
        started_at=1_000,
        attributes={"model": "model-a"},
    )
    store.begin_run(
        run_id="run_a",
        trace_id="trc_a",
        parent_run_id=None,
        parent_span_id=None,
        agent_kind="main",
        agent_label=None,
        project_id="prj_a",
        session_id="ses_a",
        turn_id="turn_a",
        provider="provider-a",
        model="model-a",
        started_at=1_010,
    )
    store.begin_span(
        span_id="spn_a",
        trace_id="trc_a",
        run_id="run_a",
        parent_span_id=None,
        kind="llm",
        name="llm.call",
        started_at=1_020,
        attributes={"authorization": "Bearer should-not-survive"},
    )
    store.finish_span(
        span_id="spn_a",
        status="completed",
        ended_at=1_120,
        usage={
            "input_tokens": 10,
            "output_tokens": 4,
            "cached_input_tokens": 3,
            "total_tokens": 14,
        },
        ttft_ms=25,
        attributes={"note": "api_key=another-secret"},
    )
    store.add_context_item(
        trace_id="trc_a",
        run_id="run_a",
        item_kind="project_document",
        source_id="doc-a",
        source_locator="docs/a.md",
        content_hash="sha256:abc",
        token_estimate=42,
        selected_reason="project_scope",
        rank=1.0,
        metadata={"cookie": "private-cookie"},
    )
    store.finish_run(
        run_id="run_a",
        status="completed",
        ended_at=1_130,
        stop_reason="completed",
    )
    store.finish_trace(
        trace_id="trc_a",
        status="completed",
        ended_at=1_140,
    )

    exported = store.export_trace("trc_a")
    assert exported is not None
    assert exported["trace"]["total_tokens"] == 14
    assert exported["trace"]["runs"][0]["total_tokens"] == 14
    assert exported["spans"][0]["ttft_ms"] == 25
    assert exported["spans"][0]["sequence_no"] == 1
    assert exported["context_manifest"][0]["content_hash"] == "sha256:abc"
    serialized = json.dumps(exported)
    assert "should-not-survive" not in serialized
    assert "another-secret" not in serialized
    assert "private-cookie" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_turn_lifecycle_creates_and_closes_one_trace(tmp_path) -> None:
    collector = TraceCollector(TraceStore(tmp_path / "logs.sqlite"))
    registry = ThreadRuntimeRegistry(
        runtime_epoch="epoch-a",
        trace_collector=collector,
    )
    lifecycle = TurnLifecycleManager(registry)
    active = await lifecycle.start_turn(
        context=RuntimeEventContext(
            channel="websocket",
            chat_id="chat-a",
            session_key="websocket:chat-a",
            metadata={},
        ),
        turn_id="turn-a",
        project_id="prj-a",
        session_id="ses-a",
        started_at=1.0,
    )
    await lifecycle.finish_turn(
        session_key="websocket:chat-a",
        expected_turn_id="turn-a",
        status=TurnStatus.COMPLETED,
        finish_reason=FinishReason.SUCCESS,
    )
    await collector.flush()

    trace = collector.store.get_trace(active.trace_id)
    assert trace is not None
    assert trace["turn_id"] == "turn-a"
    assert trace["status"] == "completed"
    assert trace["runtime_epoch"] == "epoch-a"
    await collector.close()


def test_trace_store_recovers_previous_runtime_as_abandoned(tmp_path) -> None:
    store = TraceStore(tmp_path / "logs.sqlite")
    store.begin_trace(
        trace_id="trc_old",
        project_id=None,
        session_id=None,
        turn_id="turn-old",
        runtime_epoch="old-epoch",
        started_at=1,
    )

    assert store.recover_abandoned(runtime_epoch="new-epoch", ended_at=10) == 1
    trace = store.get_trace("trc_old")
    assert trace is not None
    assert trace["status"] == "abandoned"
    assert trace["error_code"] == "GATEWAY_RESTARTED"

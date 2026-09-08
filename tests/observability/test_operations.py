import asyncio
import threading

import pytest

from nanobot.observability import operations
from nanobot.runtime.trace_context import TraceContext, bind_trace_context
from nanobot.storage.logs import StructuredLogStore


@pytest.fixture
def capture(tmp_path, monkeypatch):
    store = StructuredLogStore(tmp_path / "logs.sqlite")
    recorder = operations.OperationRecorder(store)
    monkeypatch.setattr(operations, "_RECORDER", recorder)
    yield store, recorder
    recorder.close()


@pytest.mark.asyncio
async def test_concurrent_operations_keep_trace_and_request_identity(capture):
    store, recorder = capture

    async def run(identity):
        with operations.operation_context(request_id=identity, client_action_id=identity):
            with bind_trace_context(TraceContext(trace_id="trc_" + identity, run_id="run_" + identity)):
                with operations.operation("http.request", route="/api/test"):
                    await asyncio.sleep(0)
                    with operations.operation("storage.write"):
                        await asyncio.sleep(0)

    await asyncio.gather(run("one"), run("two"))
    recorder.close()
    records = store.query(limit=100)
    assert len(records) == 8
    for row in records:
        assert row.trace_id == "trc_" + row.request_id
        assert row.details["client_action_id"] == row.request_id
        assert row.details["status"] in {"started", "completed"}
    terminals = [row for row in records if row.details["status"] == "completed"]
    assert all(row.duration_ms is not None for row in terminals)


@pytest.mark.asyncio
async def test_failure_cancellation_and_returned_error_are_distinct(capture):
    store, recorder = capture
    with pytest.raises(ValueError):
        with operations.operation("presentation.render", content="private prompt", api_key="private key"):
            raise ValueError("sensitive response")
    with pytest.raises(asyncio.CancelledError):
        with operations.operation("mcp.call"):
            raise asyncio.CancelledError()
    with operations.operation("http.request"):
        operations.fail_current_operation("HTTP_401")
    recorder.close()
    records = store.query(limit=100)
    assert {(row.event_name, row.details["status"]) for row in records} >= {
        ("presentation.render", "failed"), ("mcp.call", "cancelled"), ("http.request", "failed"),
    }
    assert "sensitive response" not in str(records)
    assert "private" not in str(records)


def test_queue_is_bounded_and_writer_failure_does_not_escape(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class FailingStore:
        def write(self, **kwargs):
            entered.set()
            release.wait(2)
            raise OSError("disk full")

    recorder = operations.OperationRecorder(FailingStore(), max_queue=2)
    monkeypatch.setattr(operations, "_RECORDER", recorder)
    try:
        operations.record_operation("storage.write")
        assert entered.wait(1)
        for _ in range(10):
            operations.record_operation("storage.write")
        assert recorder.queue.qsize() == 2
    finally:
        release.set()
        recorder.close()
    assert recorder.dropped == 11
    assert recorder.write_failures == 3


def test_rejected_ids_and_unknown_fields_are_not_logged(capture):
    store, recorder = capture
    with operations.operation_context(request_id="bad\nheader"):
        operations.record_operation("http.request", details={"headers": {"Authorization": "secret"}})
    recorder.close()
    row = store.query()[0]
    assert row.request_id is None
    assert "Authorization" not in str(row.details)
    assert operations.safe_route("/api/sessions/private/thread?token=secret") == "/api/sessions/:id/thread"


@pytest.mark.asyncio
async def test_turn_lifecycle_links_client_action_to_authoritative_trace(capture):
    from nanobot.bus.runtime_events import RuntimeEventContext
    from nanobot.runtime.turn_lifecycle import FinishReason, ThreadRuntimeRegistry, TurnStatus

    store, recorder = capture
    registry = ThreadRuntimeRegistry(runtime_epoch="epoch-test")
    context = RuntimeEventContext(channel="websocket", chat_id="chat", session_key="websocket:chat",
                                  metadata={"client_action_id": "action-test"})
    active = await registry.start_turn(context=context, turn_id="turn-test", session_id="session-test")
    await registry.finish_turn(session_key=context.session_key, expected_turn_id=active.id,
                               status=TurnStatus.COMPLETED, finish_reason=FinishReason.SUCCESS)
    recorder.close()
    rows = store.query(trace_id=active.trace_id)
    assert len(rows) == 2
    assert {row.details["status"] for row in rows} == {"started", "completed"}
    assert all(row.details["client_action_id"] == "action-test" and row.turn_id == "turn-test" for row in rows)


def test_operation_token_counts_survive_secret_redaction(capture):
    store, recorder = capture
    operations.record_operation("trace.span", details={"input_tokens": 42, "output_tokens": 12,
                                "api_key": "secret", "stack": "/Users/private-name/code.py:42"})
    recorder.close()
    row = store.query()[0]
    assert row.details["input_tokens"] == 42
    assert row.details["output_tokens"] == 12
    assert "secret" not in str(row)
    assert "private-name" not in str(row)

import json
import sqlite3
import time

import pytest

from nanobot.observability import diagnostic_export
from nanobot.observability.trace_store import TraceStore
from nanobot.storage.logs import StructuredLogStore


def test_read_only_projection_filters_scope_and_preserves_metadata(tmp_path):
    path = tmp_path / "logs.sqlite"
    logs = StructuredLogStore(path)
    TraceStore(path)
    now = int(time.time() * 1000)
    for session in ("one", "two", None):
        logs.write(level="error", component="operations.http", message="private response",
                   session_id=session, event_name="http.request", details={"input_tokens": 12, "body": "private body"})
    result = diagnostic_export.collect_database(path, now - 10000, now + 10000, "one")
    assert len(result["tables"]["logs"]) == 1
    assert {row["session_id"] for row in result["tables"]["logs"]} == {"one"}
    assert "private" not in json.dumps(result)
    assert result["tables"]["logs"][0]["details"]["input_tokens"] == 12
    assert result["sources"]["logs"]["cutoff_rowid"] == 3
    assert len(diagnostic_export.collect_database(path, now - 10000, now + 10000, None)["tables"]["logs"]) == 3
    assert diagnostic_export.collect_database(path, 0, 1, None)["tables"]["logs"] == []


def test_session_export_includes_only_related_traces_and_security_events(tmp_path):
    path = tmp_path / "logs.sqlite"
    logs = StructuredLogStore(path)
    TraceStore(path)
    now = int(time.time() * 1000)
    with sqlite3.connect(path) as connection:
        for session in ("one", "two"):
            connection.execute("INSERT INTO traces(id, session_id, runtime_epoch, status, started_at, created_at) VALUES (?, ?, 'epoch', 'running', ?, ?)",
                               (f"trace-{session}", session, now, now))
        for session in ("one", "two", None):
            connection.execute("INSERT INTO security_events(timestamp, category, action, decision, result, risk, summary, session_id) VALUES (?, 'test', 'test', 'allow', 'ok', 'low', 'omitted', ?)", (now, session))
    for trace in ("trace-one", "trace-two", None):
        logs.write(level="info", component="test", message="omitted", trace_id=trace)
    result = diagnostic_export.collect_database(path, now - 10000, now + 10000, "one")
    assert [row["trace_id"] for row in result["tables"]["logs"]] == ["trace-one"]
    assert [row["id"] for row in result["tables"]["traces"]] == ["trace-one"]
    assert [row["session_id"] for row in result["tables"]["security_events"]] == ["one"]


def test_traces_are_not_silently_truncated_at_200_items(tmp_path):
    path = tmp_path / "logs.sqlite"
    StructuredLogStore(path)
    TraceStore(path)
    now = int(time.time() * 1000)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO traces(id, runtime_epoch, status, started_at, created_at) VALUES ('trc', 'epoch', 'running', ?, ?)", (now, now))
        connection.execute("INSERT INTO agent_runs(id, trace_id, agent_kind, status, started_at) VALUES ('run', 'trc', 'root', 'running', ?)", (now,))
        connection.executemany("INSERT INTO trace_spans(id, trace_id, run_id, sequence_no, kind, name, status, started_at, attributes_json) VALUES (?, 'trc', 'run', ?, 'tool', 'exec', 'running', ?, ?)",
                               [(f"span-{i}", i, now, '{"body":"private"}') for i in range(250)])
    result = diagnostic_export.collect_database(path, now - 1000, now + 1000, None)
    assert len(result["tables"]["trace_spans"]) == 250
    assert result["sources"]["trace_spans"]["status"] == "included"
    assert "private" not in json.dumps(result)


def test_corrupt_details_and_budget_are_explicit(tmp_path, monkeypatch):
    path = tmp_path / "logs.sqlite"
    logs = StructuredLogStore(path)
    TraceStore(path)
    now = int(time.time() * 1000)
    for _ in range(4):
        logs.write(level="info", component="test", message="omitted")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE logs SET details_json = '{broken' WHERE id = 1")
    result = diagnostic_export.collect_database(path, now - 1000, now + 1000, None)
    assert result["sources"]["logs"]["malformed_records"] == 1
    assert result["sources"]["logs"]["status"] == "truncated"
    monkeypatch.setattr(diagnostic_export, "MAX_BYTES", 1)
    result = diagnostic_export.collect_database(path, now - 1000, now + 1000, None)
    assert result["sources"]["logs"]["reason"] == "BYTE_LIMIT"


@pytest.mark.parametrize("query", [{}, {"start_ms": ["0"], "end_ms": ["9999999999999"]},
                                  {"start_ms": ["1"], "end_ms": ["10"], "session_id": ["../secret"]}])
def test_export_rejects_invalid_scope(query):
    with pytest.raises((ValueError, TypeError)):
        diagnostic_export.parse_window(query)


def test_runtime_snapshot_omits_error_messages_and_plan_content():
    result = diagnostic_export.runtime_projection({"active_turn": {"id": "turn", "status": "failed", "error": {"message": "private"}, "plan": "private"}})
    assert result["active_turn"]["id"] == "turn"
    assert "private" not in json.dumps(result)


def test_one_read_transaction_excludes_writes_after_its_watermark(tmp_path, monkeypatch):
    path = tmp_path / "logs.sqlite"
    logs = StructuredLogStore(path)
    TraceStore(path)
    now = int(time.time() * 1000)
    logs.write(level="info", component="test", message="before snapshot")
    connect = sqlite3.connect

    class ConcurrentConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "SELECT COALESCE(MAX(rowid), 0) FROM logs":
                with connect(path) as writer:
                    writer.execute("INSERT INTO logs(timestamp, level, component, message) VALUES (?, 'info', 'test', 'after snapshot')", (now,))
            return super().execute(sql, parameters)

    monkeypatch.setattr(diagnostic_export.sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=ConcurrentConnection, **kwargs))
    result = diagnostic_export.collect_database(path, now - 1000, now + 1000, None)
    assert result["sources"]["logs"]["cutoff_rowid"] == 1
    assert len(result["tables"]["logs"]) == 1
    with connect(path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 2

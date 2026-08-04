import sqlite3
from pathlib import Path

from nanobot.storage.logs import StructuredLogStore


def test_structured_logs_are_queryable_and_redact_secrets(tmp_path: Path) -> None:
    store = StructuredLogStore(tmp_path / "logs.sqlite")
    row_id = store.write(
        level="ERROR",
        component="artifacts",
        event_name="artifact_content_failed",
        message="preview failed",
        project_id="prj-a",
        session_id="ses-a",
        artifact_id="art-a",
        error_code="FILE_MISSING",
        details={
            "relative_path": "reports/a.pdf",
            "Authorization": "Bearer secret",
            "nested": {"api_key": "secret"},
        },
    )

    [record] = store.query(session_id="ses-a")
    assert record.id == row_id
    assert record.artifact_id == "art-a"
    assert record.error_code == "FILE_MISSING"
    assert record.details == {
        "Authorization": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
        "relative_path": "reports/a.pdf",
    }
    assert store.query(project_id="prj-other") == []


def test_structured_log_query_is_bounded(tmp_path: Path) -> None:
    store = StructuredLogStore(tmp_path / "logs.sqlite")
    for index in range(5):
        store.write(
            level="info",
            component="gateway",
            message=f"event {index}",
        )

    rows = store.query(limit=2)
    assert len(rows) == 2
    assert rows[0].message == "event 4"


def test_structured_log_store_migrates_pre_trace_schema_before_indexing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "logs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                level TEXT NOT NULL,
                component TEXT NOT NULL,
                event_name TEXT,
                message TEXT NOT NULL,
                request_id TEXT,
                project_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                tool_call_id TEXT,
                artifact_id TEXT,
                error_code TEXT,
                duration_ms INTEGER,
                details_json TEXT
            )
            """
        )

    store = StructuredLogStore(path)
    store.write(
        level="info",
        component="gateway",
        message="migrated",
        trace_id="trace-a",
        run_id="run-a",
        span_id="span-a",
    )

    [record] = store.query(trace_id="trace-a")
    assert record.trace_id == "trace-a"
    assert record.run_id == "run-a"
    assert record.span_id == "span-a"

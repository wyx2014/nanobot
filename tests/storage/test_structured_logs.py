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

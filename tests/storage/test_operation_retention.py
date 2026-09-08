import sqlite3

from nanobot.storage.logs import StructuredLogStore


def test_noisy_session_cannot_evict_other_sessions_or_its_error_partition(tmp_path):
    store = StructuredLogStore(tmp_path / "logs.sqlite")
    store.write(level="info", component="operations.http", session_id="quiet", message="quiet")
    store.write(level="error", component="operations.http", session_id="noisy", message="failure")
    for _ in range(8):
        store.write(level="info", component="operations.http", session_id="noisy", message="noise")
    store.write(level="info", component="audit", session_id="noisy", message="unrelated")
    store.prune_operations(partition_rows=2)
    rows = store.query(limit=100)
    assert len(rows) == 5
    assert any(row.session_id == "quiet" for row in rows)
    assert any(row.level == "error" for row in rows)
    assert any(row.component == "audit" for row in rows)


def test_byte_budgets_and_corrupt_historic_details_are_handled(tmp_path):
    store = StructuredLogStore(tmp_path / "logs.sqlite")
    for _ in range(5):
        store.write(level="info", component="operations.http", message="x" * 500)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE logs SET details_json = '{broken' WHERE id = 1")
    store.prune_operations(max_bytes=1800, partition_bytes=2000)
    assert len(store.query(limit=100)) == 2

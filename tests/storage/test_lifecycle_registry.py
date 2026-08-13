from pathlib import Path

from nanobot.storage.lifecycle import LifecycleRegistry


def test_lifecycle_registry_survives_restart_and_restore(tmp_path: Path) -> None:
    path = tmp_path / ".nanobot" / "lifecycle.jsonl"
    registry = LifecycleRegistry(path)

    registry.archive_session(
        "websocket:archived",
        session_id="ses-1",
        project_id="prj-1",
    )
    registry.archive_project(
        "prj-1",
        canonical_root_path=str(tmp_path / "project"),
    )

    reopened = LifecycleRegistry(path)
    assert reopened.session_state("websocket:archived") == "archived"
    assert reopened.project_state("prj-1") == "archived"
    assert reopened.project_for_path(tmp_path / "project").key == "prj-1"

    reopened.restore_session("websocket:archived")
    assert LifecycleRegistry(path).session_state("websocket:archived") is None


def test_lifecycle_registry_keeps_purge_tombstone_and_quarantines_torn_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".nanobot" / "lifecycle.jsonl"
    registry = LifecycleRegistry(path)
    registry.purge_session(
        "websocket:purged",
        session_id="ses-purged",
        cleanup_completed=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"kind":"session"')

    reopened = LifecycleRegistry(path)

    assert reopened.session_state("websocket:purged") == "purged"
    assert any(path.parent.glob("lifecycle.jsonl.corrupt-*"))
    assert path.read_text(encoding="utf-8").endswith("\n")

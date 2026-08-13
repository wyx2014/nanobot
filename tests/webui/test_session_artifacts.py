from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nanobot.security.workspace_access import build_workspace_scope
from nanobot.webui import session_artifacts as artifacts_module
from nanobot.webui.session_artifacts import (
    SessionArtifactError,
    discover_session_artifacts,
    read_session_artifact,
    resolve_session_artifact,
)


def _session_data(*, created_at: datetime, messages: list[dict] | None = None) -> dict:
    return {
        "key": "websocket:artifact-test",
        "created_at": created_at.isoformat(),
        "messages": messages or [],
        "metadata": {},
    }


def test_discovery_keeps_explicit_outputs_and_ignores_unrelated_recent_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts_module, "read_transcript_lines", lambda _key: [])
    now = datetime.now().astimezone()
    old = tmp_path / "old.txt"
    old.write_text("old", encoding="utf-8")
    old_timestamp = (now - timedelta(days=1)).timestamp()
    os.utime(old, (old_timestamp, old_timestamp))

    recent = tmp_path / "reports" / "summary.pdf"
    recent.parent.mkdir()
    recent.write_bytes(b"%PDF-report")
    unrelated_recent = tmp_path / "reports" / "another-session.pdf"
    unrelated_recent.write_bytes(b"%PDF-other-session")
    ignored = tmp_path / "node_modules" / "noise.js"
    ignored.parent.mkdir()
    ignored.write_text("noise", encoding="utf-8")
    ordinary_result_path = tmp_path / "not-an-artifact.txt"
    ordinary_result_path.write_text("old ordinary result", encoding="utf-8")
    os.utime(ordinary_result_path, (old_timestamp, old_timestamp))

    session = _session_data(
        created_at=now - timedelta(seconds=10),
        messages=[
            {
                "role": "tool",
                "content": json.dumps({
                    "files": [
                        {"path": str(old)},
                        {"path": str(recent)},
                    ],
                }),
            },
            {
                "role": "tool",
                "content": json.dumps({
                    "path": str(ordinary_result_path),
                    "text": "ordinary tool result",
                }),
            },
        ],
    )

    payload = discover_session_artifacts(
        "websocket:artifact-test",
        session,
        scope=build_workspace_scope(tmp_path, "restricted"),
    )

    rows = {row["path"]: row for row in payload["artifacts"]}
    assert set(rows) == {"old.txt", "reports/summary.pdf"}
    assert "reports/another-session.pdf" not in rows
    assert rows["reports/summary.pdf"]["kind"] == "document"
    assert rows["reports/summary.pdf"]["mime_type"] == "application/pdf"
    assert rows["reports/summary.pdf"]["preview_url"].startswith(
        "/api/sessions/websocket%3Aartifact-test/artifacts/content?"
    )
    assert all(not Path(row["path"]).is_absolute() for row in rows.values())
    assert all("local_path" not in row for row in rows.values())


def test_discovery_caps_explicit_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts_module, "read_transcript_lines", lambda _key: [])
    files = []
    for index in range(5):
        path = tmp_path / f"result-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        files.append({"path": str(path)})
    session = _session_data(
        created_at=datetime.now().astimezone() - timedelta(seconds=10),
        messages=[{"role": "tool", "content": json.dumps({"files": files})}],
    )

    payload = discover_session_artifacts(
        "websocket:artifact-test",
        session,
        scope=build_workspace_scope(tmp_path, "restricted"),
        max_items=2,
    )

    assert payload["truncated"] is True
    assert len(payload["artifacts"]) <= 2


def test_discovery_preserves_the_turn_that_explicitly_emitted_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "report.md"
    report.write_text("report", encoding="utf-8")
    monkeypatch.setattr(
        artifacts_module,
        "read_transcript_lines",
        lambda _key: [{
            "event": "file_edit",
            "turn_id": "turn-report",
            "edits": [{"absolute_path": str(report)}],
        }],
    )

    payload = discover_session_artifacts(
        "websocket:artifact-test",
        _session_data(created_at=datetime.now().astimezone()),
        scope=build_workspace_scope(tmp_path, "restricted"),
    )

    assert payload["artifacts"][0]["path"] == "report.md"
    assert payload["artifacts"][0]["_turn_id"] == "turn-report"


def test_content_rechecks_containment_after_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts_module, "read_transcript_lines", lambda _key: [])
    target = tmp_path / "report.docx"
    target.write_bytes(b"office-data")
    session = _session_data(
        created_at=datetime.now().astimezone() - timedelta(seconds=10),
        messages=[{
            "role": "tool",
            "content": json.dumps({"files": [{"path": str(target)}]}),
        }],
    )
    scope = build_workspace_scope(tmp_path, "restricted")

    resolved = resolve_session_artifact(
        "report.docx",
        session_key="websocket:artifact-test",
        session_data=session,
        scope=scope,
    )
    assert read_session_artifact(resolved, root=tmp_path) == b"office-data"

    outside = tmp_path.parent / "outside-artifact-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(SessionArtifactError, match="artifact not found"):
        read_session_artifact(resolved, root=tmp_path)


def test_resolve_rejects_unlisted_old_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts_module, "read_transcript_lines", lambda _key: [])
    old = tmp_path / "old.txt"
    old.write_text("old", encoding="utf-8")
    timestamp = (datetime.now().astimezone() - timedelta(days=1)).timestamp()
    os.utime(old, (timestamp, timestamp))
    session = _session_data(created_at=datetime.now().astimezone())

    with pytest.raises(SessionArtifactError) as exc:
        resolve_session_artifact(
            "old.txt",
            session_key="websocket:artifact-test",
            session_data=session,
            scope=build_workspace_scope(tmp_path, "restricted"),
        )

    assert exc.value.status == 404

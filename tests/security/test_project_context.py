from pathlib import Path

import pytest

from nanobot.security.project_context import (
    bind_project_context,
    current_project_context,
    project_context_from_metadata,
    require_project_context,
    reset_project_context,
)


def test_project_context_is_bound_and_reset(tmp_path: Path) -> None:
    context = project_context_from_metadata(
        {
            "project_id": "prj-a",
            "session_id": "ses-a",
            "session_key": "websocket:chat-a",
        },
        session_key="websocket:chat-a",
        root_path=tmp_path,
    )
    assert context is not None
    token = bind_project_context(context)
    try:
        assert require_project_context() == context
        assert current_project_context() is context
        assert context.root_path == tmp_path.resolve()
    finally:
        reset_project_context(token)
    assert current_project_context() is None


def test_project_context_rejects_mismatched_session_key(tmp_path: Path) -> None:
    assert project_context_from_metadata(
        {
            "project_id": "prj-a",
            "session_id": "ses-a",
            "session_key": "websocket:other",
        },
        session_key="websocket:chat-a",
        root_path=tmp_path,
    ) is None

    with pytest.raises(RuntimeError, match="required"):
        require_project_context()

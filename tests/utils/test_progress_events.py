from nanobot.utils.progress_events import build_tool_event_display


def test_write_stdin_is_presented_as_command_not_file_write() -> None:
    display = build_tool_event_display(
        "write_stdin",
        {"session_id": "abc123", "wait_for": "ready"},
    )

    assert display == {"category": "command", "importance": "primary"}

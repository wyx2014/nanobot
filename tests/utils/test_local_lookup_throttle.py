"""Tests for consecutive identical local lookup throttling."""

from __future__ import annotations

from nanobot.utils.runtime import (
    local_lookup_signature,
    repeated_local_lookup_error,
)


def test_local_lookup_signature_is_stable_for_argument_order():
    left = local_lookup_signature(
        "read_file",
        {"path": "/workspace/a.md", "offset": 10, "limit": 20},
    )
    right = local_lookup_signature(
        "read_file",
        {"limit": 20, "offset": 10, "path": "/workspace/a.md"},
    )

    assert left == right


def test_force_read_bypasses_local_lookup_throttle():
    assert local_lookup_signature(
        "read_file",
        {"path": "/workspace/a.md", "force": True},
    ) is None


def test_third_consecutive_identical_local_lookup_is_blocked():
    state: dict[str, object] = {}
    arguments = {"path": "/workspace/a.md"}

    assert repeated_local_lookup_error("read_file", arguments, state) is None
    assert repeated_local_lookup_error("read_file", arguments, state) is None
    blocked = repeated_local_lookup_error("read_file", arguments, state)

    assert blocked is not None
    assert "repeated local tool call blocked" in blocked
    assert "Do not issue the same call again" in blocked


def test_different_local_lookup_resets_consecutive_count():
    state: dict[str, object] = {}

    repeated_local_lookup_error("read_file", {"path": "/workspace/a.md"}, state)
    repeated_local_lookup_error("read_file", {"path": "/workspace/a.md"}, state)
    assert repeated_local_lookup_error(
        "read_file",
        {"path": "/workspace/b.md"},
        state,
    ) is None
    assert repeated_local_lookup_error(
        "read_file",
        {"path": "/workspace/a.md"},
        state,
    ) is None


def test_non_lookup_tool_resets_consecutive_count():
    state: dict[str, object] = {}

    repeated_local_lookup_error("list_dir", {"path": "/workspace"}, state)
    repeated_local_lookup_error("list_dir", {"path": "/workspace"}, state)
    assert repeated_local_lookup_error(
        "write_file",
        {"path": "/workspace/a.md"},
        state,
    ) is None
    assert repeated_local_lookup_error("list_dir", {"path": "/workspace"}, state) is None

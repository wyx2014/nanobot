"""Tests for append-only WebUI transcript replay."""

from __future__ import annotations

import json

from nanobot.webui.transcript import (
    WEBUI_TRANSCRIPT_SCHEMA_VERSION,
    WebUITranscriptRecorder,
    append_fork_marker,
    append_transcript_object,
    backfill_missing_user_events,
    build_webui_thread_response,
    fork_transcript_before_user_index,
    read_transcript_lines,
    replay_transcript_to_ui_messages,
    webui_transcript_path,
    webui_transcript_segments_dir,
    write_session_messages_as_transcript,
)


def test_append_and_read_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t1"
    append_transcript_object(key, {"event": "user", "chat_id": "t1", "text": "hello"})
    lines = read_transcript_lines(key)
    assert len(lines) == 1
    assert lines[0]["text"] == "hello"


def test_corrupt_jsonl_tail_is_quarantined_before_future_appends(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:corrupt-tail"
    append_transcript_object(key, {"event": "user", "text": "valid"})
    path = webui_transcript_path(key)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken-json\n")
        handle.write('{"event":"message","text":"must-be-quarantined"}\n')

    assert [row["text"] for row in read_transcript_lines(key)] == ["valid"]
    backups = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(backups) == 1
    assert "must-be-quarantined" in backups[0].read_text(encoding="utf-8")

    append_transcript_object(key, {"event": "message", "text": "after-repair"})
    assert [row["text"] for row in read_transcript_lines(key)] == [
        "valid",
        "after-repair",
    ]


def test_interactive_prompt_replay_resolves_answer_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:prompt-replay"
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": "prompt-replay",
            "text": "",
            "interactive_prompt": {
                "promptId": "prompt:1",
                "question": "How much time can you spend each day?",
                "options": [{"id": "30m", "label": "30-60 min"}],
                "status": "pending",
            },
        },
    )
    append_transcript_object(
        key,
        {
            "event": "user",
            "chat_id": "prompt-replay",
            "text": "30-60 min",
            "interactive_prompt_answer": {
                "promptId": "prompt:1",
                "answerType": "option",
                "optionId": "30m",
            },
        },
    )

    out = build_webui_thread_response(key)

    assert out is not None
    assistant = out["messages"][0]
    assert assistant["content"] == ""
    prompt = assistant["interactivePrompt"]
    assert prompt["status"] == "answered"
    assert prompt["answeredOptionId"] == "30m"
    assert prompt["answeredText"] == "30-60 min"
    assert out["messages"][1]["interactivePromptAnswer"]["promptId"] == "prompt:1"


def test_grouped_interactive_prompt_replay_resolves_answers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:prompt-group-replay"
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": "prompt-group-replay",
            "text": "",
            "interactive_prompt": {
                "promptId": "prompt:g1",
                "question": "A couple of quick questions",
                "options": [],
                "questions": [
                    {
                        "id": "topic",
                        "question": "What are you learning?",
                        "options": [{"id": "language", "label": "A spoken language"}],
                    },
                    {
                        "id": "time",
                        "question": "How much time can you dedicate per day?",
                        "options": [{"id": "30m", "label": "30-60 min"}],
                    },
                ],
                "status": "pending",
            },
        },
    )
    append_transcript_object(
        key,
        {
            "event": "user",
            "chat_id": "prompt-group-replay",
            "text": "Q: What are you learning? A: A spoken language\n\nQ: How much time can you dedicate per day? A: 30-60 min",
            "interactive_prompt_answer": {
                "promptId": "prompt:g1",
                "answerType": "group",
                "answers": [
                    {
                        "questionId": "topic",
                        "answerType": "option",
                        "optionId": "language",
                        "text": "A spoken language",
                    },
                    {
                        "questionId": "time",
                        "answerType": "option",
                        "optionId": "30m",
                        "text": "30-60 min",
                    },
                ],
            },
        },
    )

    out = build_webui_thread_response(key)

    assert out is not None
    prompt = out["messages"][0]["interactivePrompt"]
    assert prompt["status"] == "answered"
    assert prompt["questions"][0]["answeredOptionId"] == "language"
    assert prompt["questions"][0]["answeredText"] == "A spoken language"
    assert prompt["questions"][1]["answeredOptionId"] == "30m"
    assert prompt["questions"][1]["answeredText"] == "30-60 min"


def test_append_user_message_persists_interactive_prompt_answer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:prompt-live-answer"
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": "prompt-live-answer",
            "text": "",
            "interactive_prompt": {
                "promptId": "prompt:live",
                "question": "Pick one",
                "options": [{"id": "a", "label": "A"}],
                "status": "pending",
            },
        },
    )
    recorder = WebUITranscriptRecorder()
    recorder.append_user_message(
        "prompt-live-answer",
        "A",
        metadata={
            "interactive_prompt_answer": {
                "promptId": "prompt:live",
                "answerType": "option",
                "optionId": "a",
            },
        },
    )

    out = build_webui_thread_response(key)

    assert out is not None
    prompt = out["messages"][0]["interactivePrompt"]
    assert prompt["status"] == "answered"
    assert prompt["answeredOptionId"] == "a"
    assert out["messages"][1]["interactivePromptAnswer"]["promptId"] == "prompt:live"


def test_thread_response_overlays_session_answered_prompt_for_legacy_transcript(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:legacy-prompt"
    pending_prompt = {
        "promptId": "prompt:legacy",
        "question": "Pick one",
        "options": [{"id": "a", "label": "A"}],
        "status": "pending",
    }
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": "legacy-prompt",
            "text": "",
            "interactive_prompt": pending_prompt,
        },
    )
    append_transcript_object(
        key,
        {
            "event": "user",
            "chat_id": "legacy-prompt",
            "text": "A",
        },
    )
    answered_prompt = {
        **pending_prompt,
        "status": "answered",
        "answeredOptionId": "a",
        "answeredText": "A",
    }

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "assistant", "content": "", "_interactive_prompt": answered_prompt},
            {"role": "user", "content": "A"},
        ],
    )

    assert out is not None
    prompt = out["messages"][0]["interactivePrompt"]
    assert prompt["status"] == "answered"
    assert prompt["answeredOptionId"] == "a"
    assert prompt["answeredText"] == "A"


def test_recorder_can_write_to_explicit_session_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    recorder = WebUITranscriptRecorder()
    recorder.prepare_and_append(
        "origin-chat",
        {"event": "message", "chat_id": "origin-chat", "text": "done"},
        metadata={"_webui_transcript_session_key": "cron:job:123:abcd"},
        phase="answer",
    )

    assert read_transcript_lines("websocket:origin-chat") == []
    lines = read_transcript_lines("cron:job:123:abcd")
    assert len(lines) == 1
    assert lines[0]["text"] == "done"


def _force_small_transcript_budget(monkeypatch, *, limit: int = 520, target: int = 260) -> None:
    monkeypatch.setattr("nanobot.webui.transcript._MAX_TRANSCRIPT_FILE_BYTES", limit)
    monkeypatch.setattr("nanobot.webui.transcript._TARGET_ACTIVE_TRANSCRIPT_BYTES", target)


def _append_numbered_turn(key: str, chat_id: str, idx: int) -> None:
    append_transcript_object(
        key,
        {"event": "user", "chat_id": chat_id, "text": f"question {idx} " + ("x" * 24)},
    )
    append_transcript_object(
        key,
        {"event": "message", "chat_id": chat_id, "text": f"answer {idx} " + ("y" * 24)},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": chat_id})


def _write_segmented_turns(tmp_path, monkeypatch, key: str, chat_id: str, count: int) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    _force_small_transcript_budget(monkeypatch)
    for idx in range(1, count + 1):
        _append_numbered_turn(key, chat_id, idx)


def _message_contents(payload: dict) -> list[str]:
    return [str(message.get("content") or "") for message in payload["messages"]]


def _numbered_turn_texts(start: int, end: int) -> list[str]:
    return [
        text
        for idx in range(start, end + 1)
        for text in (f"question {idx} " + ("x" * 24), f"answer {idx} " + ("y" * 24))
    ]


def test_segmented_transcript_rotation_preserves_full_history(tmp_path, monkeypatch) -> None:
    key = "websocket:segmented"
    _write_segmented_turns(tmp_path, monkeypatch, key, "segmented", 6)

    segment_dir = webui_transcript_segments_dir(key)
    assert segment_dir.is_dir()
    assert (segment_dir / "manifest.json").is_file()

    lines = read_transcript_lines(key)
    contents = [str(line.get("text") or "") for line in lines if line.get("event") in {"user", "message"}]
    assert contents == _numbered_turn_texts(1, 6)


def test_segmented_transcript_paginates_latest_and_older_without_overlap(
    tmp_path,
    monkeypatch,
) -> None:
    key = "websocket:paged"
    _write_segmented_turns(tmp_path, monkeypatch, key, "paged", 6)

    latest = build_webui_thread_response(key, limit=4, direction="latest")
    assert latest is not None
    assert latest["page"]["has_more_before"] is True
    assert latest["page"]["user_message_offset"] == 4
    assert _message_contents(latest) == _numbered_turn_texts(5, 6)

    older = build_webui_thread_response(
        key,
        limit=4,
        before=latest["page"]["before_cursor"],
    )
    assert older is not None
    assert older["page"]["user_message_offset"] == 2
    assert _message_contents(older) == _numbered_turn_texts(3, 4)


def test_page_cursor_survives_active_rotation_after_latest_page(
    tmp_path,
    monkeypatch,
) -> None:
    key = "websocket:stable-cursor"
    _write_segmented_turns(tmp_path, monkeypatch, key, "stable-cursor", 7)

    latest = build_webui_thread_response(key, limit=4, direction="latest")
    assert latest is not None
    cursor = latest["page"]["before_cursor"]
    assert cursor
    assert _message_contents(latest) == _numbered_turn_texts(6, 7)

    for idx in range(8, 13):
        _append_numbered_turn(key, "stable-cursor", idx)

    older = build_webui_thread_response(key, limit=4, before=cursor)

    assert older is not None
    assert _message_contents(older) == _numbered_turn_texts(4, 5)


def test_segment_manifest_can_be_rebuilt_when_missing_or_corrupt(tmp_path, monkeypatch) -> None:
    key = "websocket:manifest"
    _write_segmented_turns(tmp_path, monkeypatch, key, "manifest", 4)

    manifest = webui_transcript_segments_dir(key) / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")

    lines = read_transcript_lines(key)

    assert len([line for line in lines if line.get("event") == "user"]) == 4
    assert manifest.read_text(encoding="utf-8").lstrip().startswith("{")


def test_delete_webui_transcript_removes_segments(tmp_path, monkeypatch) -> None:
    from nanobot.webui.thread_disk import webui_thread_file_path
    from nanobot.webui.transcript import delete_webui_transcript, webui_transcript_path

    key = "websocket:delete-segments"
    _write_segmented_turns(tmp_path, monkeypatch, key, "delete-segments", 4)
    legacy_path = webui_thread_file_path(key)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"messages":[]}', encoding="utf-8")

    assert webui_transcript_segments_dir(key).is_dir()
    assert delete_webui_transcript(key) is True
    assert not legacy_path.exists()
    assert not webui_transcript_path(key).exists()
    assert not webui_transcript_segments_dir(key).exists()


def test_fork_transcript_reads_across_segments(tmp_path, monkeypatch) -> None:
    source = "websocket:seg-source"
    _write_segmented_turns(tmp_path, monkeypatch, source, "seg-source", 5)

    ok = fork_transcript_before_user_index(source, "websocket:seg-fork", 3)

    assert ok is True
    forked = build_webui_thread_response("websocket:seg-fork")
    assert forked is not None
    assert _message_contents(forked) == _numbered_turn_texts(1, 3)


def test_fork_transcript_before_user_index_copies_only_prefix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    source = "websocket:source"
    for ev in (
        {"event": "user", "chat_id": "source", "text": "round1"},
        {"event": "message", "chat_id": "source", "text": "answer1"},
        {"event": "turn_end", "chat_id": "source"},
        {"event": "user", "chat_id": "source", "text": "round2 fork me"},
        {"event": "message", "chat_id": "source", "text": "answer2"},
        {"event": "user", "chat_id": "source", "text": "round3 must not appear"},
    ):
        append_transcript_object(source, ev)

    ok = fork_transcript_before_user_index(source, "websocket:fork", 1)

    assert ok is True
    lines = read_transcript_lines("websocket:fork")
    assert [line.get("text") for line in lines] == ["round1", "answer1", None]
    assert all(line.get("chat_id") == "fork" for line in lines)
    assert "round2 fork me" not in "\n".join(str(line.get("text")) for line in lines)
    assert "round3 must not appear" not in "\n".join(str(line.get("text")) for line in lines)


def test_fork_transcript_rejects_out_of_range_user_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    source = "websocket:source"
    append_transcript_object(source, {"event": "user", "chat_id": "source", "text": "round1"})

    assert fork_transcript_before_user_index(source, "websocket:fork", 2) is False
    assert read_transcript_lines("websocket:fork") == []


def test_build_response_reports_fork_boundary_from_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:fork"
    for ev in (
        {"event": "user", "chat_id": "fork", "text": "round1"},
        {"event": "message", "chat_id": "fork", "text": "answer1"},
    ):
        append_transcript_object(key, ev)
    append_fork_marker(key)
    append_transcript_object(key, {"event": "user", "chat_id": "fork", "text": "new branch"})

    out = build_webui_thread_response(key)

    assert out is not None
    assert [m["content"] for m in out["messages"]] == ["round1", "answer1", "new branch"]
    assert out["fork_boundary_message_count"] == 2


def test_nested_fork_drops_inherited_fork_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    source = "websocket:source"
    for ev in (
        {"event": "user", "chat_id": "source", "text": "round1"},
        {"event": "message", "chat_id": "source", "text": "answer1"},
    ):
        append_transcript_object(source, ev)
    append_fork_marker(source)
    for ev in (
        {"event": "user", "chat_id": "source", "text": "round2"},
        {"event": "message", "chat_id": "source", "text": "answer2"},
    ):
        append_transcript_object(source, ev)

    ok = fork_transcript_before_user_index(source, "websocket:nested", 2)
    append_fork_marker("websocket:nested")

    lines = read_transcript_lines("websocket:nested")
    out = build_webui_thread_response("websocket:nested")

    assert ok is True
    assert [line.get("event") for line in lines] == [
        "user",
        "message",
        "user",
        "message",
        "fork_marker",
    ]
    assert out is not None
    assert [m["content"] for m in out["messages"]] == ["round1", "answer1", "round2", "answer2"]
    assert out["fork_boundary_message_count"] == 4


def test_write_session_messages_as_transcript_builds_canonical_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)

    write_session_messages_as_transcript(
        "websocket:fork",
        [
            {"role": "user", "content": "round1"},
            {"role": "assistant", "content": "answer1"},
        ],
    )

    lines = read_transcript_lines("websocket:fork")
    assert lines == [
        {"event": "user", "chat_id": "fork", "text": "round1"},
        {"event": "message", "chat_id": "fork", "text": "answer1"},
    ]
    msgs = replay_transcript_to_ui_messages(lines)
    assert [m["content"] for m in msgs] == ["round1", "answer1"]


def test_replay_delta_and_turn_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t2"
    for ev in (
        {"event": "user", "chat_id": "t2", "text": "q"},
        {"event": "reasoning_delta", "chat_id": "t2", "text": "think"},
        {"event": "reasoning_end", "chat_id": "t2"},
        {"event": "delta", "chat_id": "t2", "text": "a"},
        {"event": "stream_end", "chat_id": "t2"},
        {
            "event": "turn_end",
            "chat_id": "t2",
            "latency_ms": 42,
            "usage": {"prompt_tokens": 120, "completion_tokens": 34},
        },
    ):
        append_transcript_object(key, ev)
    lines = read_transcript_lines(key)
    msgs = replay_transcript_to_ui_messages(lines)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "q"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "a"
    assert msgs[1]["reasoning"] == "think"
    assert msgs[1]["reasoningDurationMs"] >= 0
    assert msgs[1]["reasoningCompletedAt"] >= msgs[1]["reasoningStartedAt"]
    assert msgs[1]["latencyMs"] == 42
    assert msgs[1]["usage"] == {"inputTokens": 120, "outputTokens": 34}


def test_replay_preserves_event_times_and_nested_turn_timing() -> None:
    started_at = 1_785_222_059_843
    completed_at = 1_785_222_083_788
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "user",
            "chat_id": "timing",
            "text": "今天天气",
            "turn_id": "turn-timing",
            "recorded_at": started_at,
        },
        {
            "event": "message",
            "chat_id": "timing",
            "text": "",
            "kind": "progress",
            "turn_id": "turn-timing",
            "recorded_at": started_at + 6_000,
            "tool_events": [{
                "phase": "end",
                "call_id": "weather",
                "name": "exec",
                "occurred_at": started_at + 5_900,
                "result": "sunny",
            }],
        },
        {
            "event": "delta",
            "chat_id": "timing",
            "text": "晴天",
            "turn_id": "turn-timing",
            "recorded_at": completed_at - 1_000,
        },
        {
            "event": "turn_completed",
            "chat_id": "timing",
            "turn_id": "turn-timing",
            "recorded_at": completed_at,
            "turn": {
                "id": "turn-timing",
                "status": "completed",
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": completed_at - started_at,
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 34,
                    "total_tokens": 1234,
                },
            },
        },
    ])

    assert [message["createdAt"] for message in msgs] == [
        started_at,
        started_at + 6_000,
        completed_at - 1_000,
    ]
    assert msgs[-1]["latencyMs"] == completed_at - started_at
    assert msgs[-1]["completedAt"] == completed_at
    assert msgs[-1]["usage"] == {
        "inputTokens": 1200,
        "outputTokens": 34,
    }


def test_thread_response_does_not_mark_completed_message_tool_tail_pending(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:cron-tail"
    turn_id = "cron:job:run"
    for ev in (
        {
            "event": "message",
            "chat_id": "cron-tail",
            "text": 'message({"content":"Cron test"})',
            "kind": "tool_hint",
            "tool_events": [{
                "phase": "start",
                "call_id": "call-message",
                "name": "message",
                "arguments": {"content": "Cron test"},
            }],
            "turn_id": turn_id,
            "turn_phase": "activity",
            "turn_seq": 5,
        },
        {
            "event": "message",
            "chat_id": "cron-tail",
            "text": "Cron test",
            "source": {"kind": "cron", "label": "one-min-test"},
            "turn_id": turn_id,
            "turn_phase": "answer",
            "turn_seq": 6,
        },
        {
            "event": "message",
            "chat_id": "cron-tail",
            "text": "",
            "kind": "progress",
            "tool_events": [{
                "phase": "end",
                "call_id": "call-message",
                "name": "message",
                "arguments": {"content": "Cron test"},
                "result": "ok",
            }],
            "turn_id": turn_id,
            "turn_phase": "activity",
            "turn_seq": 7,
        },
        {
            "event": "turn_end",
            "chat_id": "cron-tail",
            "turn_id": turn_id,
            "turn_phase": "complete",
            "turn_seq": 8,
        },
    ):
        append_transcript_object(key, ev)

    out = build_webui_thread_response(key)

    assert out is not None
    assert out["has_pending_tool_calls"] is False
    assert out["messages"][-1]["kind"] == "trace"
    assert out["messages"][-2]["content"] == "Cron test"


def test_thread_response_marks_unfinished_tool_tail_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:active-tail"
    append_transcript_object(
        key,
        {
            "event": "message",
            "chat_id": "active-tail",
            "text": 'exec({"command":"date"})',
            "kind": "tool_hint",
        },
    )

    out = build_webui_thread_response(key)

    assert out is not None
    assert out["has_pending_tool_calls"] is True


def test_replay_preserves_turn_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-turn"
    for ev in (
        {
            "event": "user",
            "chat_id": "t-turn",
            "text": "q",
            "turn_id": "turn-1",
            "turn_phase": "user",
            "turn_seq": 1,
        },
        {
            "event": "reasoning_delta",
            "chat_id": "t-turn",
            "text": "think",
            "turn_id": "turn-1",
            "turn_phase": "reasoning",
            "turn_seq": 2,
        },
        {
            "event": "delta",
            "chat_id": "t-turn",
            "text": "a",
            "turn_id": "turn-1",
            "turn_phase": "answer",
            "turn_seq": 3,
        },
        {
            "event": "turn_end",
            "chat_id": "t-turn",
            "latency_ms": 12,
            "turn_id": "turn-1",
            "turn_phase": "complete",
            "turn_seq": 4,
        },
    ):
        append_transcript_object(key, ev)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert msgs[0]["turnId"] == "turn-1"
    assert msgs[0]["turnPhase"] == "user"
    assert msgs[0]["turnSeq"] == 1
    assert msgs[1]["turnId"] == "turn-1"
    assert msgs[1]["turnPhase"] == "answer"
    assert msgs[1]["turnSeq"] == 3


def test_replay_reused_turn_id_after_turn_end_starts_new_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-reused-turn"

    def event(
        event: str,
        phase: str,
        seq: int,
        text: str | None = None,
        source: dict[str, str] | None = None,
    ) -> dict[str, object]:
        out = {
            "event": event,
            "chat_id": "t-reused-turn",
            "turn_id": "turn-1",
            "turn_phase": phase,
            "turn_seq": seq,
        }
        if text is not None:
            out["text"] = text
        if source is not None:
            out["source"] = source
        return out

    for record in (
        event("user", "user", 1, "remind me later"),
        event("message", "answer", 2, "Reminder set."),
        event("turn_end", "complete", 3),
        event(
            "message", "answer", 1, "Time to drink water.",
            {"kind": "cron", "label": "drink water"},
        ),
        event("turn_end", "complete", 2),
    ):
        append_transcript_object(key, record)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert [m["content"] for m in msgs] == [
        "remind me later",
        "Reminder set.",
        "Time to drink water.",
    ]
    assert msgs[1]["turnId"] == "turn-1"
    assert msgs[2]["turnId"].startswith("turn-1:replay:")
    assert msgs[2]["turnId"] != msgs[1]["turnId"]
    assert msgs[2]["source"] == {"kind": "cron", "label": "drink water"}


def test_build_response_restores_session_users_for_legacy_transcript(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:legacy-users"
    append_transcript_object(
        key,
        {"event": "message", "chat_id": "legacy-users", "text": "assistant one"},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": "legacy-users"})
    append_transcript_object(
        key,
        {"event": "message", "chat_id": "legacy-users", "text": "assistant two"},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": "legacy-users"})

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "user", "content": "prompt one", "timestamp": "2026-06-02T10:00:00"},
            {"role": "assistant", "content": "assistant one"},
            {"role": "user", "content": "prompt two", "timestamp": "2026-06-02T10:01:00"},
            {"role": "assistant", "content": "assistant two"},
        ],
    )

    assert out is not None
    assert [(m["role"], m["content"]) for m in out["messages"]] == [
        ("user", "prompt one"),
        ("assistant", "assistant one"),
        ("user", "prompt two"),
        ("assistant", "assistant two"),
    ]


def test_build_response_restores_generated_html_from_session_tool_result(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:legacy-artifact"
    artifact = tmp_path / "reports" / "company-report.html"
    artifact.parent.mkdir()
    artifact.write_text("<!doctype html><title>report</title>", encoding="utf-8")
    append_transcript_object(
        key,
        {"event": "message", "chat_id": "legacy-artifact", "text": "final answer"},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": "legacy-artifact"})

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "user", "content": "research company"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "write-1", "function": {"name": "write_file"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "write-1",
                "content": json.dumps({"files": [{"path": str(artifact)}]}),
            },
            {"role": "assistant", "content": "final answer"},
        ],
        augment_assistant_media=lambda paths: [
            {
                "url": "/api/media/signed",
                "local_path": path,
                "name": artifact.name,
                "kind": "file",
            }
            for path in paths
        ],
    )

    assert out is not None
    assistant = next(message for message in out["messages"] if message["role"] == "assistant")
    assert assistant["media"] == [
        {
            "url": "/api/media/signed",
            "local_path": str(artifact),
            "name": "company-report.html",
            "kind": "file",
        }
    ]


def test_build_response_restores_session_users_without_duplicating_new_transcript_users(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:mixed-users"
    append_transcript_object(
        key,
        {"event": "message", "chat_id": "mixed-users", "text": "old assistant"},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": "mixed-users"})
    append_transcript_object(key, {"event": "user", "chat_id": "mixed-users", "text": "new prompt"})
    append_transcript_object(
        key,
        {"event": "message", "chat_id": "mixed-users", "text": "new assistant"},
    )
    append_transcript_object(key, {"event": "turn_end", "chat_id": "mixed-users"})

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "user", "content": "new prompt"},
            {"role": "assistant", "content": "new assistant"},
        ],
    )

    assert out is not None
    assert [(m["role"], m["content"]) for m in out["messages"]] == [
        ("user", "old prompt"),
        ("assistant", "old assistant"),
        ("user", "new prompt"),
        ("assistant", "new assistant"),
    ]


def test_backfill_missing_user_events_from_session_messages() -> None:
    lines = [
        {"event": "delta", "chat_id": "legacy", "text": "a1"},
        {"event": "stream_end", "chat_id": "legacy"},
        {"event": "turn_end", "chat_id": "legacy"},
        {"event": "delta", "chat_id": "legacy", "text": "a2"},
        {"event": "stream_end", "chat_id": "legacy"},
        {"event": "turn_end", "chat_id": "legacy"},
    ]
    backfilled = backfill_missing_user_events(
        lines,
        [
            {"role": "user", "content": "q1", "cli_apps": [{"name": "codex"}]},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2", "mcp_presets": [{"name": "browser"}]},
            {"role": "assistant", "content": "a2"},
        ],
        session_key="websocket:legacy",
    )

    msgs = replay_transcript_to_ui_messages(backfilled)

    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "q1"
    assert msgs[0]["cliApps"] == [{"name": "codex"}]
    assert msgs[2]["content"] == "q2"
    assert msgs[2]["mcpPresets"] == [{"name": "browser"}]


def test_replay_augments_assistant_text() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-img", "text": "draw"},
            {"event": "delta", "chat_id": "t-img", "text": "![Diagram](diagram.png)"},
            {"event": "stream_end", "chat_id": "t-img"},
        ],
        augment_assistant_text=lambda text: text.replace("diagram.png", "/api/media/sig/payload"),
    )

    assert msgs[1]["content"] == "![Diagram](/api/media/sig/payload)"


def test_replay_uses_stream_end_final_text() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-img", "text": "draw"},
            {"event": "stream_end", "chat_id": "t-img", "text": "![Diagram](/api/media/sig/payload)"},
        ],
    )

    assert msgs[1]["content"] == "![Diagram](/api/media/sig/payload)"


def test_replay_replaces_streamed_report_with_authoritative_attachment_message() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {
                "event": "user",
                "chat_id": "t-report",
                "text": "分析青岛啤酒",
                "turn_id": "turn-report",
            },
            {
                "event": "delta",
                "chat_id": "t-report",
                "text": "青岛啤酒（600600.SH）投资研究报告",
                "turn_id": "turn-report",
            },
            {
                "event": "stream_end",
                "chat_id": "t-report",
                "turn_id": "turn-report",
            },
            {
                "event": "message",
                "chat_id": "t-report",
                "text": "青岛啤酒（600600.SH）投资研究报告",
                "media_urls": [{
                    "url": "/api/media/report",
                    "name": "青岛啤酒投资研究报告.html",
                }],
                "replace_stream": True,
                "turn_id": "turn-report",
            },
            {
                "event": "turn_end",
                "chat_id": "t-report",
                "turn_id": "turn-report",
            },
        ],
    )

    assistant = [message for message in msgs if message["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "青岛啤酒（600600.SH）投资研究报告"
    assert assistant[0]["media"][0]["name"] == "青岛啤酒投资研究报告.html"


def test_replay_replaces_streamed_report_when_terminal_was_persisted_first() -> None:
    """A durable terminal row must not split its late authoritative answer."""
    final_text = "中科曙光四视角并行投研报告已完成，数据抽检全部通过（7/7项，偏差0.00%）。"
    msgs = replay_transcript_to_ui_messages(
        [
            {
                "event": "user",
                "chat_id": "t-late-report",
                "text": "帮我分析下中科曙光A股",
                "turn_id": "turn-late-report",
            },
            {
                "event": "delta",
                "chat_id": "t-late-report",
                "text": final_text,
                "turn_id": "turn-late-report",
            },
            {
                "event": "turn_completed",
                "chat_id": "t-late-report",
                "turn_id": "turn-late-report",
                "turn": {
                    "id": "turn-late-report",
                    "status": "completed",
                },
            },
            {
                "event": "stream_end",
                "chat_id": "t-late-report",
                "turn_id": "turn-late-report",
            },
            {
                "event": "message",
                "chat_id": "t-late-report",
                "text": final_text,
                "media_urls": [{
                    "url": "/api/media/report",
                    "name": "中科曙光四视角并行投研报告.html",
                }],
                "replace_stream": True,
                "turn_id": "turn-late-report",
            },
        ],
    )

    assistant = [
        message
        for message in msgs
        if message["role"] == "assistant" and message.get("kind") != "trace"
    ]
    assert len(assistant) == 1
    assert assistant[0]["turnId"] == "turn-late-report"
    assert assistant[0]["content"] == final_text
    assert assistant[0]["media"][0]["name"] == "中科曙光四视角并行投研报告.html"


def test_replay_keeps_structured_progress_after_media_message() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-pdf", "text": "生成 PDF", "turn_id": "turn-pdf"},
            {
                "event": "message",
                "chat_id": "t-pdf",
                "kind": "progress",
                "text": "",
                "agent_ui": {
                    "kind": "task_progress",
                    "note": "正在转换为 PDF",
                    "current_step_id": "pdf",
                    "steps": [{"id": "pdf", "title": "转换为 PDF", "status": "running"}],
                },
                "tool_events": [{
                    "phase": "start",
                    "call_id": "call-pdf",
                    "name": "convert_to_pdf",
                    "arguments": {"path": "report.md"},
                }],
                "turn_id": "turn-pdf",
            },
            {
                "event": "message",
                "chat_id": "t-pdf",
                "text": "PDF 已生成",
                "media_urls": [{"url": "/api/media/report", "name": "report.pdf"}],
                "turn_id": "turn-pdf",
            },
            {
                "event": "message",
                "chat_id": "t-pdf",
                "kind": "progress",
                "text": "",
                "agent_ui": {
                    "kind": "task_progress",
                    "note": "PDF 转换完成",
                    "steps": [{"id": "pdf", "title": "转换为 PDF", "status": "completed"}],
                },
                "tool_events": [{
                    "phase": "end",
                    "call_id": "call-pdf",
                    "name": "convert_to_pdf",
                    "arguments": {"path": "report.md"},
                    "result": "ok",
                }],
                "turn_id": "turn-pdf",
            },
            {"event": "turn_end", "chat_id": "t-pdf", "turn_id": "turn-pdf"},
        ],
    )

    progress = [
        message
        for message in msgs
        if isinstance(message.get("agentUI"), dict)
        and message["agentUI"].get("kind") == "task_progress"
    ]
    assert len(progress) == 2
    assert progress[-1]["agentUI"]["steps"][0]["status"] == "completed"
    assert progress[-1]["agentUI"]["note"] == "PDF 转换完成"
    assert progress[-1]["toolEvents"][0]["phase"] == "end"
    assert any(
        message.get("role") == "assistant"
        and message.get("media", [{}])[0].get("name") == "report.pdf"
        for message in msgs
    )


def test_replay_repairs_legacy_exact_streamed_attachment_duplicate() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "delta", "chat_id": "legacy", "text": "完整报告"},
            {"event": "stream_end", "chat_id": "legacy"},
            {
                "event": "message",
                "chat_id": "legacy",
                "text": "完整报告",
                "media_urls": [{"url": "/api/media/report", "name": "report.html"}],
            },
            {"event": "turn_end", "chat_id": "legacy"},
        ],
    )

    assistant = [message for message in msgs if message["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["media"][0]["name"] == "report.html"


def test_build_response_backfills_legacy_sse_only_transcripts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-legacy"
    for ev in (
        {"event": "delta", "chat_id": "t-legacy", "text": "first answer"},
        {"event": "stream_end", "chat_id": "t-legacy"},
        {"event": "turn_end", "chat_id": "t-legacy"},
        {"event": "message", "chat_id": "t-legacy", "text": "second answer"},
        {"event": "turn_end", "chat_id": "t-legacy"},
    ):
        append_transcript_object(key, ev)

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ],
    )

    assert out is not None
    assert [message["role"] for message in out["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["content"] for message in out["messages"]] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]


def test_backfill_does_not_duplicate_existing_user_transcript(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-current"
    for ev in (
        {"event": "user", "chat_id": "t-current", "text": "already stored"},
        {"event": "message", "chat_id": "t-current", "text": "answer"},
        {"event": "turn_end", "chat_id": "t-current"},
    ):
        append_transcript_object(key, ev)

    out = build_webui_thread_response(
        key,
        session_messages=[{"role": "user", "content": "already stored"}],
    )

    assert out is not None
    assert [message["role"] for message in out["messages"]] == ["user", "assistant"]
    assert out["messages"][0]["content"] == "already stored"


def test_backfill_does_not_misalign_when_session_only_has_transcript_tail(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-tail"
    for ev in (
        {"event": "message", "chat_id": "t-tail", "text": "old answer"},
        {"event": "turn_end", "chat_id": "t-tail"},
        {"event": "message", "chat_id": "t-tail", "text": "tail answer"},
        {"event": "turn_end", "chat_id": "t-tail"},
    ):
        append_transcript_object(key, ev)

    out = build_webui_thread_response(
        key,
        session_messages=[
            {"role": "user", "content": "tail question"},
            {"role": "assistant", "content": "tail answer"},
        ],
    )

    assert out is not None
    assert [message["role"] for message in out["messages"]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["content"] for message in out["messages"]] == [
        "old answer",
        "tail question",
        "tail answer",
    ]


def test_replay_infers_video_media_from_attachment_name() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-video", "text": "render"},
            {
                "event": "message",
                "chat_id": "t-video",
                "text": "video ready",
                "media_urls": [{"url": "/api/media/sig/payload", "name": "intro.mp4"}],
            },
        ],
    )

    assert msgs[1]["media"] == [
        {"kind": "video", "url": "/api/media/sig/payload", "name": "intro.mp4"},
    ]


def test_replay_resigns_assistant_media_paths_before_stale_urls() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-video-resign", "text": "render"},
            {
                "event": "message",
                "chat_id": "t-video-resign",
                "text": "video ready",
                "media": ["/tmp/intro.mp4"],
                "media_urls": [{"url": "/api/media/old-sig/old-payload", "name": "intro.mp4"}],
            },
        ],
        augment_assistant_media=lambda paths: [
            {"kind": "video", "url": f"/api/media/new-sig/{paths[0].split('/')[-1]}", "name": "intro.mp4"},
        ],
    )

    assert msgs[1]["media"] == [
        {"kind": "video", "url": "/api/media/new-sig/intro.mp4", "name": "intro.mp4"},
    ]


def test_replay_infers_svg_media_from_attachment_name() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-svg", "text": "send svg"},
            {
                "event": "message",
                "chat_id": "t-svg",
                "text": "chart ready",
                "media_urls": [{"url": "/api/media/sig/payload", "name": "chart.svg"}],
            },
        ],
    )

    assert msgs[1]["media"] == [
        {"kind": "image", "url": "/api/media/sig/payload", "name": "chart.svg"},
    ]


def test_replay_infers_file_media_from_attachment_name() -> None:
    msgs = replay_transcript_to_ui_messages(
        [
            {"event": "user", "chat_id": "t-file-media", "text": "send html"},
            {
                "event": "message",
                "chat_id": "t-file-media",
                "text": "file ready",
                "media_urls": [{"url": "/api/media/sig/payload", "name": "index.html"}],
            },
        ],
    )

    assert msgs[1]["media"] == [
        {"kind": "file", "url": "/api/media/sig/payload", "name": "index.html"},
    ]


def test_replay_file_edit_event_creates_file_activity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-file"
    for ev in (
        {"event": "user", "chat_id": "t-file", "text": "edit"},
        {
            "event": "message",
            "chat_id": "t-file",
            "text": 'write_file({"path":"foo.txt"})',
            "kind": "tool_hint",
        },
        {
            "event": "file_edit",
            "chat_id": "t-file",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "foo.txt",
                    "phase": "end",
                    "added": 2,
                    "deleted": 1,
                    "approximate": False,
                    "status": "done",
                },
            ],
        },
    ):
        append_transcript_object(key, ev)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert len(msgs) == 3
    assert msgs[1]["kind"] == "trace"
    assert msgs[1]["traces"] == ['write_file({"path":"foo.txt"})']
    assert "fileEdits" not in msgs[1]
    assert msgs[2]["kind"] == "trace"
    assert msgs[2]["traces"] == []
    assert msgs[2]["fileEdits"] == [
        {
            "version": 1,
            "call_id": "call-write",
            "tool": "write_file",
            "path": "foo.txt",
            "phase": "end",
            "added": 2,
            "deleted": 1,
            "approximate": False,
            "status": "done",
        },
    ]
    assert msgs[2]["activitySegmentId"]
    assert msgs[2]["activitySegmentId"] != msgs[1]["activitySegmentId"]


def test_replay_file_edit_absorbs_matching_write_tool_event() -> None:
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "message",
            "chat_id": "t-file",
            "text": 'write_file({"path":"foo.txt"})',
            "kind": "tool_hint",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "call-write",
                    "name": "write_file",
                    "arguments": {"path": "foo.txt", "content": "hello\n"},
                },
            ],
        },
        {
            "event": "file_edit",
            "chat_id": "t-file",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "foo.txt",
                    "phase": "start",
                    "added": 1,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                },
            ],
        },
        {
            "event": "message",
            "chat_id": "t-file",
            "text": "",
            "kind": "progress",
            "tool_events": [
                {
                    "phase": "end",
                    "call_id": "call-write",
                    "name": "write_file",
                    "arguments": {"path": "foo.txt", "content": "hello\n"},
                    "result": "ok",
                },
            ],
        },
    ])

    assert len(msgs) == 1
    assert msgs[0]["kind"] == "trace"
    assert msgs[0]["traces"] == []
    assert "toolEvents" not in msgs[0]
    assert msgs[0]["fileEdits"] == [
        {
            "version": 1,
            "call_id": "call-write",
            "tool": "write_file",
            "path": "foo.txt",
            "phase": "start",
            "added": 1,
            "deleted": 0,
            "approximate": True,
            "status": "editing",
        },
    ]


def test_replay_keeps_every_file_from_one_apply_patch_call() -> None:
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "message",
            "chat_id": "t-file",
            "text": "apply_patch()",
            "kind": "tool_hint",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "call-patch",
                    "name": "apply_patch",
                    "arguments": {"edits": []},
                },
            ],
        },
        {
            "event": "file_edit",
            "chat_id": "t-file",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-patch",
                    "tool": "apply_patch",
                    "path": "USER.md",
                    "phase": "end",
                    "added": 0,
                    "deleted": 3,
                    "approximate": False,
                    "status": "done",
                },
                {
                    "version": 1,
                    "call_id": "call-patch",
                    "tool": "apply_patch",
                    "path": "MEMORY.md",
                    "phase": "end",
                    "added": 0,
                    "deleted": 4,
                    "approximate": False,
                    "status": "done",
                },
            ],
        },
    ])

    assert len(msgs) == 1
    assert msgs[0]["traces"] == []
    assert "toolEvents" not in msgs[0]
    assert [edit["path"] for edit in msgs[0]["fileEdits"]] == ["USER.md", "MEMORY.md"]


def test_replay_keeps_interrupted_pre_tool_text_in_activity() -> None:
    msgs = replay_transcript_to_ui_messages([
        {"event": "delta", "chat_id": "t-stream", "text": "I will inspect first."},
        {"event": "stream_end", "chat_id": "t-stream"},
        {
            "event": "message",
            "chat_id": "t-stream",
            "text": 'exec({"cmd":"ls"})',
            "kind": "tool_hint",
        },
        {
            "event": "stream_end",
            "chat_id": "t-stream",
            "text": "Done. Open index.html to play.",
        },
    ])

    assert len(msgs) == 3
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == ""
    assert msgs[0]["reasoning"] == "I will inspect first."
    assert "isStreaming" not in msgs[0]
    assert msgs[1]["kind"] == "trace"
    assert msgs[1]["traces"] == ['exec({"cmd":"ls"})']
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "Done. Open index.html to play."


def test_replay_tool_events_dedupes_finish_after_start() -> None:
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "message",
            "chat_id": "t-tool",
            "text": 'exec({"cmd":"ls"})',
            "kind": "tool_hint",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "call-exec",
                    "name": "exec",
                    "arguments": {"cmd": "ls"},
                },
            ],
        },
        {
            "event": "message",
            "chat_id": "t-tool",
            "text": "",
            "kind": "progress",
            "tool_events": [
                {
                    "phase": "end",
                    "call_id": "call-exec",
                    "name": "exec",
                    "arguments": {"cmd": "ls"},
                    "result": "ok",
                },
                {
                    "phase": "end",
                    "call_id": "call-read",
                    "name": "read_file",
                    "arguments": {"path": "notes.md"},
                    "result": "done",
                },
            ],
        },
    ])

    assert len(msgs) == 1
    assert msgs[0]["traces"] == [
        'exec({"cmd": "ls"})',
        'read_file({"path": "notes.md"})',
    ]
    assert msgs[0]["toolEvents"][0]["phase"] == "end"
    assert msgs[0]["toolEvents"][0]["call_id"] == "call-exec"


def test_replay_tool_events_keeps_phase_update_when_trace_is_deduped() -> None:
    args = {"name": "github", "args": ["repo", "view"], "json": "true"}
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "message",
            "chat_id": "t-tool",
            "text": "",
            "kind": "tool_hint",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "call-cli",
                    "name": "run_cli_app",
                    "arguments": args,
                },
            ],
        },
        {
            "event": "message",
            "chat_id": "t-tool",
            "text": "",
            "kind": "progress",
            "tool_events": [
                {
                    "phase": "error",
                    "call_id": "call-cli",
                    "name": "run_cli_app",
                    "arguments": args,
                    "error": "Error: CLI app 'github' not found",
                },
            ],
        },
    ])

    assert len(msgs) == 1
    assert msgs[0]["traces"] == [
        'run_cli_app({"name": "github", "args": ["repo", "view"], "json": "true"})',
    ]
    assert msgs[0]["toolEvents"][0]["phase"] == "error"
    assert msgs[0]["toolEvents"][0]["error"] == "Error: CLI app 'github' not found"


def test_replay_keeps_consecutive_task_progress_snapshots() -> None:
    msgs = replay_transcript_to_ui_messages([
        {
            "event": "message",
            "chat_id": "t-plan",
            "text": "",
            "kind": "progress",
            "agent_ui": {
                "kind": "task_progress",
                "note": "开始查询行业数据",
                "current_step_id": "research",
                "steps": [
                    {"id": "research", "title": "查询行业数据", "status": "running"},
                    {"id": "draft", "title": "撰写报告", "status": "pending"},
                ],
            },
        },
        {
            "event": "message",
            "chat_id": "t-plan",
            "text": "",
            "kind": "progress",
            "agent_ui": {
                "kind": "task_progress",
                "note": "数据已收集，开始撰写报告",
                "current_step_id": "draft",
                "steps": [
                    {"id": "research", "title": "查询行业数据", "status": "completed"},
                    {"id": "draft", "title": "撰写报告", "status": "running"},
                ],
            },
        },
    ])

    assert len(msgs) == 2
    assert [msg["agentUI"]["current_step_id"] for msg in msgs] == ["research", "draft"]
    assert [msg["agentUI"]["note"] for msg in msgs] == [
        "开始查询行业数据",
        "数据已收集，开始撰写报告",
    ]


def test_replay_file_edit_progress_merges_after_interleaved_activity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-file-progress"
    for ev in (
        {"event": "user", "chat_id": "t-file-progress", "text": "edit"},
        {
            "event": "message",
            "chat_id": "t-file-progress",
            "text": 'write_file({"path":"foo.txt"})',
            "kind": "tool_hint",
        },
        {
            "event": "file_edit",
            "chat_id": "t-file-progress",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "foo.txt",
                    "phase": "start",
                    "added": 12,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                },
            ],
        },
        {
            "event": "message",
            "chat_id": "t-file-progress",
            "text": "still working",
            "kind": "progress",
        },
        {
            "event": "file_edit",
            "chat_id": "t-file-progress",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "foo.txt",
                    "phase": "end",
                    "added": 30,
                    "deleted": 0,
                    "approximate": False,
                    "status": "done",
                },
            ],
        },
    ):
        append_transcript_object(key, ev)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))
    file_edit_messages = [msg for msg in msgs if msg.get("fileEdits")]

    assert len(file_edit_messages) == 1
    assert file_edit_messages[0]["fileEdits"] == [
        {
            "version": 1,
            "call_id": "call-write",
            "tool": "write_file",
            "path": "foo.txt",
            "phase": "end",
            "added": 30,
            "deleted": 0,
            "approximate": False,
            "status": "done",
        },
    ]


def test_replay_file_edit_pending_placeholder_upgrades_to_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-file-pending"
    for ev in (
        {"event": "user", "chat_id": "t-file-pending", "text": "write"},
        {
            "event": "file_edit",
            "chat_id": "t-file-pending",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "",
                    "phase": "start",
                    "added": 1,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                    "pending": True,
                },
            ],
        },
        {
            "event": "file_edit",
            "chat_id": "t-file-pending",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-write",
                    "tool": "write_file",
                    "path": "foo.txt",
                    "phase": "start",
                    "added": 12,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                },
            ],
        },
    ):
        append_transcript_object(key, ev)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))
    file_edit_messages = [msg for msg in msgs if msg.get("fileEdits")]

    assert len(file_edit_messages) == 1
    assert file_edit_messages[0]["fileEdits"] == [
        {
            "version": 1,
            "call_id": "call-write",
            "tool": "write_file",
            "path": "foo.txt",
            "phase": "start",
            "added": 12,
            "deleted": 0,
            "approximate": True,
            "status": "editing",
        },
    ]


def test_replay_keeps_new_file_edit_after_reasoning_in_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-file-order"
    for ev in (
        {"event": "user", "chat_id": "t-file-order", "text": "edit"},
        {
            "event": "file_edit",
            "chat_id": "t-file-order",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-one",
                    "tool": "write_file",
                    "path": "one.txt",
                    "phase": "start",
                    "added": 10,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                },
            ],
        },
        {"event": "reasoning_delta", "chat_id": "t-file-order", "text": "Check next."},
        {"event": "reasoning_end", "chat_id": "t-file-order"},
        {
            "event": "file_edit",
            "chat_id": "t-file-order",
            "edits": [
                {
                    "version": 1,
                    "call_id": "call-two",
                    "tool": "write_file",
                    "path": "two.txt",
                    "phase": "start",
                    "added": 20,
                    "deleted": 0,
                    "approximate": True,
                    "status": "editing",
                },
            ],
        },
    ):
        append_transcript_object(key, ev)

    msgs = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert [msg.get("fileEdits", [{}])[0].get("path") if msg.get("fileEdits") else msg.get("reasoning") for msg in msgs[1:]] == [
        "one.txt",
        "Check next.",
        "two.txt",
    ]
    file_edit_segments = [
        msg.get("activitySegmentId")
        for msg in msgs
        if msg.get("fileEdits")
    ]
    assert len(file_edit_segments) == 2
    assert file_edit_segments[0] != file_edit_segments[1]


def test_replay_keeps_public_narration_in_trace_not_answer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-narration"
    for event in (
        {"event": "user", "chat_id": "t-narration", "text": "analyze"},
        {
            "event": "delta",
            "chat_id": "t-narration",
            "stream_id": "stream-1",
            "text": "I will fetch detailed sources.",
        },
        {
            "event": "stream_end",
            "chat_id": "t-narration",
            "stream_id": "stream-1",
            "resuming": True,
            "stream_kind": "narration",
        },
        {
            "event": "narration_delta",
            "chat_id": "t-narration",
            "stream_id": "stream-1",
            "replaces_stream_id": "stream-1",
            "text": "I will fetch detailed sources.",
        },
        {
            "event": "narration_end",
            "chat_id": "t-narration",
            "stream_id": "stream-1",
            "replaces_stream_id": "stream-1",
        },
        {
            "event": "message",
            "chat_id": "t-narration",
            "text": "Final answer.",
        },
        {"event": "turn_end", "chat_id": "t-narration"},
    ):
        append_transcript_object(key, event)

    messages = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert [message["role"] for message in messages] == ["user", "tool", "assistant"]
    trace = messages[1]
    assert trace["kind"] == "trace"
    assert trace["content"] == ""
    assert trace["narration"] == "I will fetch detailed sources."
    assert messages[2]["content"] == "Final answer."
    assert all(
        message.get("content") != "I will fetch detailed sources."
        for message in messages
        if message.get("role") == "assistant"
    )


def test_replay_preserves_reasoning_before_narration_and_keeps_tool_hint_separate(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t-reasoning-narration"
    for event in (
        {"event": "user", "chat_id": "t-reasoning-narration", "text": "analyze"},
        {
            "event": "reasoning_delta",
            "chat_id": "t-reasoning-narration",
            "text": "I should inspect primary sources.",
        },
        {
            "event": "delta",
            "chat_id": "t-reasoning-narration",
            "stream_id": "stream-1",
            "text": "I will fetch detailed sources.",
        },
        {
            "event": "stream_end",
            "chat_id": "t-reasoning-narration",
            "stream_id": "stream-1",
            "resuming": True,
            "stream_kind": "narration",
        },
        {
            "event": "narration_delta",
            "chat_id": "t-reasoning-narration",
            "stream_id": "stream-1",
            "replaces_stream_id": "stream-1",
            "text": "I will fetch detailed sources.",
        },
        {
            "event": "narration_end",
            "chat_id": "t-reasoning-narration",
            "stream_id": "stream-1",
            "replaces_stream_id": "stream-1",
        },
        {
            "event": "message",
            "chat_id": "t-reasoning-narration",
            "kind": "tool_hint",
            "text": "Searched the web — market",
        },
        {
            "event": "message",
            "chat_id": "t-reasoning-narration",
            "text": "Final answer.",
        },
        {"event": "turn_end", "chat_id": "t-reasoning-narration"},
    ):
        append_transcript_object(key, event)

    messages = replay_transcript_to_ui_messages(read_transcript_lines(key))

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    reasoning, narration, tool_hint, answer = messages[1:]
    assert reasoning["content"] == ""
    assert reasoning["reasoning"] == "I should inspect primary sources."
    assert not reasoning.get("isStreaming")
    assert not reasoning.get("reasoningStreaming")
    assert "streamId" not in reasoning
    assert narration["content"] == ""
    assert narration["narration"] == "I will fetch detailed sources."
    assert narration["activitySegmentId"] == reasoning["activitySegmentId"]
    assert tool_hint["content"] == "Searched the web — market"
    assert tool_hint["traces"] == ["Searched the web — market"]
    assert "narration" not in tool_hint
    assert answer["content"] == "Final answer."


def test_build_response_schema(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t3"
    append_transcript_object(key, {"event": "user", "chat_id": "t3", "text": "x"})
    out = build_webui_thread_response(key, augment_user_media=None)
    assert out is not None
    assert out["schemaVersion"] == WEBUI_TRANSCRIPT_SCHEMA_VERSION
    assert out["sessionKey"] == key
    assert len(out["messages"]) == 1

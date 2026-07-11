from nanobot.agent.hook import AgentHookContext
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)


def test_tool_event_metadata_is_stable_across_start_and_finish() -> None:
    call = ToolCallRequest(
        id="call-search",
        name="web_search",
        arguments={"query": "IDC market size"},
    )

    start = build_tool_event_start_payload(
        call,
        sequence=2001,
        batch_id="turn-1:2",
    )
    context = AgentHookContext(
        iteration=2,
        messages=[],
        tool_calls=[call],
        tool_results=["done"],
        tool_events=[{"status": "ok", "detail": "done"}],
    )
    finish = build_tool_event_finish_payloads(
        context,
        sequence_base=2001,
        batch_id="turn-1:2",
    )[0]

    assert start["sequence"] == finish["sequence"] == 2001
    assert start["batch_id"] == finish["batch_id"] == "turn-1:2"
    assert start["display"] == finish["display"] == {
        "category": "search",
        "importance": "primary",
        "subject": "IDC market size",
    }
    assert isinstance(start["occurred_at"], int)
    assert isinstance(finish["occurred_at"], int)


def test_read_only_discovery_is_marked_secondary() -> None:
    call = ToolCallRequest(
        id="call-read",
        name="read_file",
        arguments={"path": "reports/idc.md"},
    )

    event = build_tool_event_start_payload(call)

    assert event["display"] == {
        "category": "read",
        "importance": "secondary",
        "subject": "reports/idc.md",
    }


def test_structured_file_result_is_forwarded_as_activity_evidence() -> None:
    call = ToolCallRequest(
        id="call-pdf",
        name="create_pdf",
        arguments={"source_path": "report.md"},
    )
    file_result = {
        "text": "PDF created successfully",
        "files": [{
            "path": "/workspace/report.pdf",
            "name": "report.pdf",
            "mime_type": "application/pdf",
            "size": 1024,
        }],
    }
    context = AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[call],
        tool_results=[file_result],
        tool_events=[{"status": "ok", "detail": "done"}],
    )

    event = build_tool_event_finish_payloads(context)[0]

    assert event["result"] == file_result
    assert event["files"] == file_result["files"]


async def test_explicit_task_progress_suppresses_automatic_plan() -> None:
    captured = []

    async def on_progress(content, *, tool_hint=False, tool_events=None):
        captured.append((content, tool_hint, tool_events))

    hook = AgentProgressHook(
        on_progress=on_progress,
        chat_id="chat-1",
        message_id="turn-1",
    )
    context = AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[
            ToolCallRequest(
                id="explicit-plan",
                name="update_task_progress",
                arguments={
                    "steps": [{"id": "research", "title": "研究", "status": "running"}],
                },
            ),
            ToolCallRequest(id="search", name="web_search", arguments={"query": "IDC"}),
            ToolCallRequest(id="exec", name="exec", arguments={"command": "python fetch.py"}),
        ],
    )

    await hook.before_execute_tools(context)

    events = captured[0][2]
    plan_events = [event for event in events if event["name"] == "update_task_progress"]
    assert len(plan_events) == 1
    assert plan_events[0]["call_id"] == "explicit-plan"

"""Audit privacy, lifecycle and task ownership contracts."""

import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.security.audit import (
    AuditedToolResult,
    audit_http_hooks,
    redact_security_text,
    tool_audit_outcome,
)
from nanobot.security.protection import SecurityService
from nanobot.storage.logs import StructuredLogStore


@pytest.fixture
def service(tmp_path: Path) -> SecurityService:
    return SecurityService(StructuredLogStore(tmp_path / "logs.sqlite"), tmp_path)


def begin(service, *, path="report.md", session="ws:chat", turn="turn-a", label=None):
    assessment = service.assess(tool_name="read_file", params={"path": path}, tool=None, workspace=service.workspace)
    return service.begin_audit(assessment, tool_call_id=f"read-{path}", tool_name="read_file",
                               session_key=session, turn_id=turn, agent_label=label)


@pytest.mark.parametrize("command,secret", [
    ('curl --password "private phrase" https://example.com/', 'private phrase'),
    ('curl -H "Authorization: Bearer sensitive-bearer" https://example.com/', 'sensitive-bearer'),
    ('API_KEY="private phrase" tool', 'private phrase'),
    ('echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature', 'eyJhbGci'),
    ('echo eyJhbGciOiJIUzI1NiJ9.eyJzd', 'eyJhbGci'),
    ('curl https://user:private-pass@example.com/path?search=private-query', 'private-query'),
    ('curl --data "confidential document" https://example.com', 'confidential document'),
    ('tool <<EOF\nconfidential document\nEOF', 'confidential document'),
    ('curl -u user:private-password https://example.com', 'private-password'),
    ('AWS_SECRET_ACCESS_KEY=private-credential tool', 'private-credential'),
])
def test_redacts_full_command_before_truncation(command, secret):
    assert secret not in redact_security_text(command)
    assert secret not in redact_security_text(command + "x" * 4000)


def test_storage_boundary_and_legacy_export_do_not_expose_payloads(service):
    event_id = service.logs.begin_security_event(category="mcp", action="call", decision="allow", risk="normal",
        target="tool --token private-token", summary="password=private-password",
        details={"arguments": {"query": "private document"}, "prompt": "private prompt"})
    with sqlite3.connect(service.logs.path) as db:
        row = db.execute("SELECT target, summary, details_json FROM security_events WHERE id = ?", (event_id,)).fetchone()
        assert "private" not in repr(row)
        db.execute("UPDATE security_events SET target = ?, details_json = ? WHERE id = ?",
                   ("tool --token legacy-secret", '{"arguments":{"query":"legacy-document"}}', event_id))
    [record] = service.logs.query_security_events()
    assert "legacy-secret" not in record.target
    assert "legacy-document" not in json.dumps(record.details)


def test_routine_reads_group_only_within_same_task_and_keep_failures(service):
    for path in ("a.md", "b.md", "a.md"):
        service.complete_audit(begin(service, path=path), result="succeeded")
    service.complete_audit(begin(service, path="missing.md"), result="failed")
    service.complete_audit(begin(service, session="ws:other"), result="succeeded")
    service.complete_audit(begin(service, turn="turn-b"), result="succeeded")
    records = service.logs.query_security_events()
    assert len(records) == 4
    grouped = next(row for row in records if row.details.get("operation_count") == 3)
    assert len(grouped.details["paths"]) == 2
    assert service.logs.query_security_events(search="b.md")[0].id == grouped.id
    assert grouped.tool_call_id is None
    assert len([row for row in records if row.result == "failed"]) == 1


def test_sensitive_reads_never_group_and_paths_are_bounded(service):
    for _ in range(2):
        service.complete_audit(begin(service, path=".env"), result="succeeded")
    assert len(service.logs.query_security_events()) == 2
    for index in range(25):
        service.complete_audit(begin(service, path=f"ordinary-{index}.md"), result="succeeded")
    aggregate = next(row for row in service.logs.query_security_events() if row.details.get("operation_count") == 25)
    assert len(aggregate.details["paths"]) == 20
    assert aggregate.details["paths_truncated"] is True


@pytest.mark.parametrize("tool_result,outcome", [
    (AuditedToolResult("Exit code: 7", result="failed", exit_code=7), "failed"),
    (AuditedToolResult("Error: Command timed out", result="timed_out"), "timed_out"),
])
async def test_approved_failure_is_one_operation_and_model_requests_are_grouped(service, tool_result, outcome):
    provider = MagicMock()
    provider.api_base = "https://models.example.com/v1?key=private"
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="call-a", name="exec", arguments={"command": "git reset --hard"})]),
        LLMResponse(content="done"),
    ])
    tools = ToolRegistry()
    tool = MagicMock()
    tool.name = "exec"
    tool.to_schema.return_value = {"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}}
    tool.execute = AsyncMock(return_value=tool_result)
    tool.cast_params.side_effect = lambda params: params
    tool.validate_params.return_value = []
    tools.register(tool)

    async def approve(payload):
        assert service.approvals.resolve(payload["approval_id"], "allow_turn", chat_id="chat")

    await AgentRunner(provider).run(AgentRunSpec(initial_messages=[], tools=tools, model="test",
        session_key="ws:chat", workspace=service.workspace, security_service=service,
        security_interactive=True, security_chat_id="chat", security_turn_id="turn",
        security_approval_callback=approve, max_iterations=3, max_tool_result_chars=4000))
    [record] = service.logs.query_security_events(category="authorization")
    assert record.result == outcome
    assert record.decision == "approved"
    if outcome == "failed":
        assert record.details["exit_code"] == 7
    assert [stage["result"] for stage in record.details["lifecycle"]] == ["pending", "executing", outcome]
    assert service.logs.query_security_events(result="approved")[0].id == record.id
    [model] = service.logs.query_security_events(category="network")
    assert model.details["operation_count"] == 2
    assert "private" not in model.target


async def test_approval_timeout_and_cancellation_are_distinct(service):
    from nanobot.security.protection import SecurityApprovalBroker

    broker = SecurityApprovalBroker()
    assert await broker.request({"approval_id": "a"}, AsyncMock(), chat_id="c", timeout_s=0.001) == "timed_out"
    handle = begin(service)
    service.complete_audit(handle, result="cancelled")
    assert service.logs.query_security_events()[0].result == "cancelled"


def test_process_polling_updates_original_command(service):
    assessment = service.assess(tool_name="exec", params={"command": "python script.py"}, tool=None, workspace=service.workspace)
    handle = service.begin_audit(assessment, tool_call_id="exec-1", tool_name="exec", session_key="ws:chat", turn_id="a")
    service.complete_audit(handle, result="running", details={"process_session_id": "proc-1"})
    poll = service.assess(tool_name="write_stdin", params={"session_id": "proc-1"}, tool=None, workspace=service.workspace)
    next_handle = service.begin_audit(poll, tool_call_id="poll-1", tool_name="write_stdin", session_key="ws:chat", turn_id="b")
    assert next_handle.event_id == handle.event_id
    service.complete_audit(next_handle, result="succeeded", details={"exit_code": 0})
    [record] = service.logs.query_security_events()
    assert record.tool_call_id == "exec-1"
    assert record.turn_id == "a"
    assert record.result == "succeeded"


async def test_http_metadata_is_scoped_and_never_records_body(service):
    handle = begin(service)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201)), event_hooks=audit_http_hooks()) as client:
        with service.network_context(handle):
            await client.post("https://example.com/upload?secret=private-query", content="private-document")
        await client.get("https://outside.example.com/")
    service.complete_audit(handle, result="succeeded")
    [record] = service.logs.query_security_events()
    http = record.details["http_activity"]
    assert http["request_count"] == 1
    assert http["requests"][0]["status_code"] == 201
    assert "private" not in json.dumps(http)
    assert "outside.example.com" not in json.dumps(http)


def test_policy_diff_and_atomic_clear_are_visible(service):
    service.update_policy({"network_deny_domains": ["example.com"]})
    [record] = service.logs.query_security_events(category="settings")
    assert record.details["changes"] == {"network_deny_domains": {"before": [], "after": ["example.com"]}}
    assert service.logs.clear_security_events(record_admin=True) == 1
    [record] = service.logs.query_security_events()
    assert record.action == "clear_audit"
    assert record.details["deleted_count"] == 1


def test_clear_during_operation_keeps_eventual_outcome(service):
    handle = begin(service)
    service.logs.clear_security_events(record_admin=True)
    assert service.complete_audit(handle, result="failed")
    records = service.logs.query_security_events()
    assert {r.action for r in records} == {"clear_audit", "read"}
    assert next(r for r in records if r.action == "read").result == "failed"


def test_tool_metadata_survives_session_copy_and_json_serialization():
    value = AuditedToolResult("tool text", result="failed", exit_code=7)
    copied = deepcopy({"content": value})["content"]
    assert copied.audit_result == "failed"
    assert copied.audit_details == {"exit_code": 7}
    assert json.loads(json.dumps({"content": copied})) == {"content": "tool text"}


@pytest.mark.parametrize("name,value,outcome", [
    ("web_fetch", '{"error":"connection reset","url":"https://example.com"}', "failed"),
    ("web_fetch", '{"error":"Redirect blocked: private URL"}', "blocked"),
    ("web_fetch", '{"status":403,"text":"denied"}', "failed"),
    ("mcp_demo", "(MCP tool call timed out after 30s)", "timed_out"),
    ("mcp_demo", "(MCP tool call blocked: workspace violation)", "blocked"),
    ("exec", "Error: Command blocked by core safety protection", "blocked"),
])
def test_known_tool_error_protocols_are_not_success(name, value, outcome):
    assert tool_audit_outcome(name, value)[0] == outcome


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell execution contract")
async def test_actual_command_exit_and_session_status(tmp_path):
    from nanobot.agent.tools.exec_session import ExecSessionManager, WriteStdinTool
    from nanobot.agent.tools.shell import ExecTool

    manager = ExecSessionManager()
    tool = ExecTool(working_dir=str(tmp_path), session_manager=manager)
    failed = await tool.execute(command="exit 7", shell="sh", login=False)
    assert failed.audit_result == "failed"
    assert failed.audit_details["exit_code"] == 7
    running = await tool.execute(command="sleep 0.1; exit 3", shell="sh", login=False, yield_time_ms=0)
    assert running.audit_result == "running"
    completed = await WriteStdinTool(manager=manager).execute(
        session_id=running.audit_details["process_session_id"], yield_time_ms=1000,
    )
    assert completed.audit_result == "failed"
    assert completed.audit_details["exit_code"] == 3

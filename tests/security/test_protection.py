from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.security.network import validate_configured_network_target
from nanobot.security.protection import (
    SecurityAssessment,
    SecurityPolicyError,
    SecurityPolicyStore,
    SecurityService,
)
from nanobot.storage.logs import StructuredLogStore


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SecurityService:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return SecurityService(
        StructuredLogStore(tmp_path / ".nanobot" / "logs.sqlite"),
        tmp_path,
    )


def test_full_access_normal_command_is_allowed(service: SecurityService, tmp_path: Path) -> None:
    result = service.assess(
        tool_name="exec",
        params={"command": "npm test", "working_dir": str(tmp_path)},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "allow"
    assert result.mutating is True


def test_internal_state_tools_are_not_misclassified_as_file_operations(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    tool = type("InternalTool", (), {"read_only": False})()

    result = service.assess(
        tool_name="update_task_progress",
        params={"steps": []},
        tool=tool,
        workspace=tmp_path,
    )

    assert result.category == "tool"
    assert result.audit_required is False
    assert result.mutating is False


def test_policy_reports_three_year_audit_retention(service: SecurityService) -> None:
    assert service.policy_payload()["components"]["audit"]["retention_days"] == 1095


def test_catastrophic_commands_are_immutable_blocks(service: SecurityService, tmp_path: Path) -> None:
    for command in (
        "diskpart",
        "mkfs.ext4 /dev/sda",
        "rm -rf /",
        "rm -rf $HOME",
        "find / -type f -delete",
        r"Remove-Item -Recurse -Force C:\*",
        "Clear-Disk -Number 0 -RemoveData",
    ):
        result = service.assess(
            tool_name="exec",
            params={"command": command},
            tool=None,
            workspace=tmp_path,
        )
        assert result.decision == "block"
        assert result.risk == "critical"


def test_powershell_recursive_delete_requires_approval(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    result = service.assess(
        tool_name="exec",
        params={
            "command": r"Remove-Item .\build -Recurse -Force",
            "working_dir": str(tmp_path),
        },
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "require_approval"
    assert result.rule_id == "command.recursive_delete"


@pytest.mark.parametrize("flags", ["-rf", "-fr", "-fR", "-vfr", "-f -r", "--recursive"])
def test_recursive_delete_requires_approval_unless_user_allows_its_path(
    service: SecurityService,
    tmp_path: Path,
    flags: str,
) -> None:
    build_dir = tmp_path / "project" / "build"
    build_dir.mkdir(parents=True)
    result = service.assess(
        tool_name="exec",
        params={"command": f"rm {flags} build", "working_dir": str(build_dir.parent)},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "require_approval"
    assert result.rule_id == "command.recursive_delete"

    service.update_policy({"file_allow_paths": [str(build_dir)]})
    allowed = service.assess(
        tool_name="exec",
        params={"command": f"rm {flags} build", "working_dir": str(build_dir.parent)},
        tool=None,
        workspace=tmp_path,
    )
    assert allowed.decision == "allow"
    assert allowed.risk == "normal"
    assert allowed.rule_id == "file.user_allowed_path"


def test_custom_approval_path_wins_for_structured_file_writes(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "finance"
    protected.mkdir()
    policy = service.policy.load()
    policy["approval_paths"] = [str(protected)]
    service.update_policy(policy)

    result = service.assess(
        tool_name="write_file",
        params={"path": str(protected / "report.md"), "content": "x"},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "require_approval"
    assert result.rule_id == "file.protected_path"


def test_approval_path_only_prompts_for_shell_mutations(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "finance"
    service.update_policy({"approval_paths": [str(protected)]})

    read = service.assess(
        tool_name="exec",
        params={"command": f"cat {protected / 'report.md'}"},
        tool=None,
        workspace=tmp_path,
    )
    write = service.assess(
        tool_name="exec",
        params={"command": f"echo updated > {protected / 'report.md'}"},
        tool=None,
        workspace=tmp_path,
    )

    assert read.decision == "allow"
    assert write.decision == "require_approval"
    assert write.rule_id == "file.protected_path"


@pytest.mark.parametrize("command", [
    "cp payload authorized_keys",
    "mv payload authorized_keys",
    "touch authorized_keys",
    "echo updated >authorized_keys",
    "echo updated >>authorized_keys",
    'echo updated > "authorized keys"',
    "echo updated 2>errors.log",
    "echo updated >123",
    "printf updated | cat >authorized_keys",
    "Copy-Item payload authorized_keys",
    "Set-Content authorized_keys updated",
])
def test_relative_shell_writes_require_approval_in_protected_cwd(
    service: SecurityService, tmp_path: Path, command: str,
) -> None:
    protected = tmp_path / "finance"
    service.update_policy({"approval_paths": [str(protected)]})
    result = service.assess(
        tool_name="exec",
        params={"command": command, "working_dir": str(protected)},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "require_approval"
    assert result.rule_id == "file.protected_path"


@pytest.mark.parametrize("tool_name, params", [
    ("create_presentation", {"source_path": "deck.yaml", "output_path": "finance/deck.pptx"}),
    ("create_presentation", {"source_path": "finance/deck.yaml"}),
    ("create_presentation", {
        "source_path": "deck.yaml", "output_path": "deck.pptx", "preview_path": "finance/deck.pdf",
    }),
    ("import_presentation_asset", {"source_path": "image.png", "output_path": "finance/image.png"}),
])
def test_presentation_outputs_require_path_approval(
    service: SecurityService, tmp_path: Path, tool_name: str, params: dict,
) -> None:
    service.update_policy({"approval_paths": [str(tmp_path / "finance")]})
    result = service.assess(tool_name=tool_name, params=params, tool=None, workspace=tmp_path)
    assert result.decision == "require_approval"
    assert result.rule_id == "file.protected_path"
    assert result.mutating is True
    assert result.audit_required is True


@pytest.mark.parametrize("tool_name", ["create_presentation", "import_presentation_asset"])
def test_normal_presentation_writes_are_audited(
    service: SecurityService, tmp_path: Path, tool_name: str,
) -> None:
    result = service.assess(
        tool_name=tool_name,
        params={"source_path": "deck.yaml", "output_path": "output.pptx"},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "allow"
    assert result.mutating is True
    assert result.audit_required is True


def test_sensitive_reads_are_allowed_and_audited(service: SecurityService, tmp_path: Path) -> None:
    result = service.assess(
        tool_name="read_file",
        params={"path": str(tmp_path / ".ssh" / "id_ed25519")},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "allow"
    assert result.risk == "sensitive"
    assert result.audit_required is True


def test_normal_file_reads_include_a_human_readable_target(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    document = tmp_path / "reports" / "brief.md"

    result = service.assess(
        tool_name="read_file",
        params={"path": str(document)},
        tool=None,
        workspace=tmp_path,
    )

    assert result.category == "file"
    assert result.action == "read"
    assert result.target == str(document)
    assert result.summary == "通过文件工具读取文件"
    assert result.audit_required is True


def test_audit_details_redact_structured_credentials(service: SecurityService) -> None:
    assessment = SecurityAssessment(
        decision="allow",
        risk="normal",
        category="mcp",
        action="call",
        rule_id="mcp.tool_call",
        summary="MCP call",
        details={
            "arguments": {
                "api_key": "sk-secret-value",
                "nested": {"accessToken": "private-token"},
                "query": "public company name",
            }
        },
    )

    handle = service.begin_audit(
        assessment,
        tool_call_id="call-a",
        tool_name="mcp_demo",
        session_key="websocket:chat-a",
        turn_id="turn-a",
    )
    service.complete_audit(handle, result="succeeded")

    [record] = service.logs.query_security_events(limit=1)
    assert record.details["arguments"] == "[CONTENT OMITTED]"


def test_security_state_sidecars_are_hard_blocked(service: SecurityService, tmp_path: Path) -> None:
    result = service.assess(
        tool_name="write_file",
        params={"path": str(tmp_path / ".nanobot" / "state.sqlite-wal"), "content": "x"},
        tool=None,
        workspace=tmp_path,
    )
    assert result.decision == "block"
    assert result.rule_id == "core.audit_tamper"


def test_policy_migrates_legacy_trusted_paths_without_preserving_exceptions(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "finance"
    service.policy.path.parent.mkdir(parents=True, exist_ok=True)
    service.policy.path.write_text(json.dumps({
        "schema_version": 1,
        "trusted_paths": [str(tmp_path / "nanobot-temp")],
        "approval_paths": [str(protected)],
    }), encoding="utf-8")

    policy = service.policy.load()

    assert "trusted_paths" not in policy
    assert policy["file_allow_paths"] == []
    assert str(protected.resolve()) in policy["approval_paths"]
    assert str((tmp_path / "nanobot-temp").resolve()) not in policy["approval_paths"]


def test_default_approval_paths_are_platform_specific(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac = SecurityPolicyStore._default_approval_candidates(
        home, platform_name="darwin", environ={},
    )
    windows = SecurityPolicyStore._default_approval_candidates(
        home, platform_name="win32", environ={},
    )

    assert home / ".ssh" in mac
    assert home / "Library" / "Keychains" in mac
    assert home / "Library" / "LaunchAgents" in mac
    assert home / ".ssh" in windows
    assert home / "AppData" / "Roaming" / "gnupg" in windows
    assert home / "AppData" / "Roaming" / "Microsoft" / "Credentials" in windows
    assert (
        home
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        in windows
    )


def test_partial_policy_update_preserves_other_sections(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "finance"
    service.update_policy({"approval_paths": [str(protected)]})
    service.update_policy({"command_approval_prefixes": ["git push"]})

    policy = service.policy.load()

    assert str(protected.resolve()) in policy["approval_paths"]
    assert policy["command_approval_prefixes"] == ["git push"]


def test_file_allowlist_rejects_broad_system_and_approval_overlaps(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    with pytest.raises(SecurityPolicyError):
        service.update_policy({"file_allow_paths": [str(Path.home())]})

    protected = tmp_path / "finance"
    with pytest.raises(SecurityPolicyError):
        service.update_policy({
            "file_allow_paths": [str(protected)],
            "approval_paths": [str(protected / "private")],
        })


def test_command_ask_prefix_takes_priority_over_allow_prefix(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    service.update_policy({
        "command_allow_prefixes": ["git push"],
        "command_approval_prefixes": ["git push"],
    })

    result = service.assess(
        tool_name="exec",
        params={"command": "git push origin main"},
        tool=None,
        workspace=tmp_path,
    )

    assert result.decision == "require_approval"
    assert result.rule_id == "command.user_approval"


def test_command_allow_prefix_skips_configurable_risk_but_not_core_blocks(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    service.update_policy({"command_allow_prefixes": ["git reset", "diskpart"]})

    allowed = service.assess(
        tool_name="exec",
        params={"command": "git reset --hard"},
        tool=None,
        workspace=tmp_path,
    )
    blocked = service.assess(
        tool_name="exec",
        params={"command": "diskpart"},
        tool=None,
        workspace=tmp_path,
    )

    assert allowed.decision == "allow"
    assert allowed.rule_id == "command.user_allowed"
    assert blocked.decision == "block"
    assert blocked.risk == "critical"


def test_denied_network_domain_blocks_web_fetch_and_subdomains(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    service.update_policy({"network_deny_domains": ["example.com"]})

    result = service.assess(
        tool_name="web_fetch",
        params={"url": "https://api.example.com/report"},
        tool=None,
        workspace=tmp_path,
    )

    assert result.decision == "block"
    assert result.rule_id == "network.denied_domain"


def test_allowed_web_tools_are_audited_with_useful_targets(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    fetched = service.assess(
        tool_name="web_fetch",
        params={"url": "https://example.com/report"},
        tool=None,
        workspace=tmp_path,
    )
    searched = service.assess(
        tool_name="web_search",
        params={"query": "重庆啤酒财报"},
        tool=None,
        workspace=tmp_path,
    )

    assert fetched.category == "network"
    assert fetched.action == "fetch"
    assert fetched.target == "https://example.com/report"
    assert fetched.audit_required is True
    assert searched.category == "network"
    assert searched.action == "search"
    assert searched.target == "web_search"
    assert searched.details["data_categories"] == ["search_terms"]
    assert searched.audit_required is True


def test_block_all_network_keeps_allowed_domain_exception(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    service.update_policy({
        "network_block_all": True,
        "network_allow_domains": ["allowed.example"],
    })

    allowed = service.assess(
        tool_name="web_fetch",
        params={"url": "https://api.allowed.example/data"},
        tool=None,
        workspace=tmp_path,
    )
    blocked = service.assess(
        tool_name="exec",
        params={"command": "curl https://blocked.example/data"},
        tool=None,
        workspace=tmp_path,
    )
    search = service.assess(
        tool_name="web_search",
        params={"query": "nanobot"},
        tool=None,
        workspace=tmp_path,
    )

    assert allowed.decision == "allow"
    assert blocked.rule_id == "network.block_all"
    assert search.rule_id == "network.block_all"


def test_network_context_applies_domain_rules_to_redirect_validation(
    service: SecurityService,
) -> None:
    service.update_policy({"network_deny_domains": ["redirect.example"]})

    with service.network_context():
        allowed, message = validate_configured_network_target(
            "https://sub.redirect.example/landing"
        )

    assert allowed is False
    assert "denied domain" in message
    assert validate_configured_network_target("https://redirect.example")[0] is True


def test_command_allow_prefix_does_not_cover_compound_commands(
    service: SecurityService,
    tmp_path: Path,
) -> None:
    service.update_policy({"command_allow_prefixes": ["git status"]})

    result = service.assess(
        tool_name="exec",
        params={"command": "git status && git reset --hard"},
        tool=None,
        workspace=tmp_path,
    )

    assert result.decision == "require_approval"
    assert result.rule_id == "command.destructive_git"


@pytest.mark.asyncio
async def test_approval_is_reused_only_by_the_turn_grant(service: SecurityService) -> None:
    assessment = service.assess(
        tool_name="exec",
        params={"command": "git reset --hard"},
        tool=None,
        workspace=service.workspace,
    )
    grants: set[str] = set()
    sent: list[dict] = []

    async def callback(payload: dict) -> None:
        sent.append(payload)
        asyncio.get_running_loop().call_soon(
            lambda: service.approvals.resolve(
                payload["approval_id"],
                "allow_turn",
                chat_id="chat-a",
            )
        )

    allowed, result = await service.authorize(
        assessment,
        callback=callback,
        chat_id="chat-a",
        turn_grants=grants,
        interactive=True,
        tool_call_id="call-a",
        tool_name="exec",
    )
    assert allowed is True
    assert result == "approved"
    assert len(sent) == 1

    allowed, result = await service.authorize(
        assessment,
        callback=callback,
        chat_id="chat-a",
        turn_grants=grants,
        interactive=True,
        tool_call_id="call-b",
        tool_name="exec",
    )
    assert allowed is True
    assert result == "approved_for_turn"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_unattended_high_risk_operation_is_blocked(service: SecurityService) -> None:
    assessment = service.assess(
        tool_name="exec",
        params={"command": "git clean -fd"},
        tool=None,
        workspace=service.workspace,
    )
    allowed, result = await service.authorize(
        assessment,
        callback=None,
        chat_id=None,
        turn_grants=set(),
        interactive=False,
        tool_call_id="call-a",
        tool_name="exec",
    )
    assert allowed is False
    assert result == "blocked_unattended"

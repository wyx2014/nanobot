from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.webui import expert_teams


def test_resume_detection_is_explicit_and_does_not_capture_new_research() -> None:
    assert expert_teams.expert_team_resume_requested("补充上次缺失的现金流数据") is True
    assert expert_teams.expert_team_resume_requested("这是补充数据", has_media=True) is True
    assert expert_teams.expert_team_resume_requested("补充分析另一家公司") is False
    assert expert_teams.expert_team_resume_requested("帮我分析工商银行") is False


@pytest.mark.asyncio
async def test_model_router_treats_bare_stock_name_as_asset_research() -> None:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content='{"action":"run","target":"比亚迪","reason":"specific listed company"}',
        usage={"prompt_tokens": 120, "completion_tokens": 20},
    ))
    usage: list[dict[str, int]] = []

    decision = await expert_teams.classify_expert_team_turn_with_model(
        provider=provider,
        model="test-model",
        history=[],
        user_message="比亚迪",
        usage_callback=usage.append,
    )

    assert decision == {
        "action": "run",
        "reason": "specific listed company",
        "target": "比亚迪",
    }
    assert usage == [{"prompt_tokens": 120, "completion_tokens": 20}]
    request = provider.chat_with_retry.await_args.kwargs
    assert request["model"] == "test-model"
    assert request["max_tokens"] == 220
    assert request["temperature"] == 0
    assert request["reasoning_effort"] == "none"
    assert "比亚迪" in request["messages"][0]["content"]
    assert '"current_user_message": "比亚迪"' in request["messages"][1]["content"]


@pytest.mark.asyncio
async def test_model_router_routes_supply_chain_theme_to_bottleneck_team() -> None:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content=(
            '{"action":"run","target":"AI基础设施供应链",'
            '"reason":"bounded physical supply-chain theme"}'
        ),
        usage={"prompt_tokens": 130, "completion_tokens": 22},
    ))

    decision = await expert_teams.classify_expert_team_turn_with_model(
        provider=provider,
        model="test-model",
        history=[],
        user_message="帮我找 AI 基础设施供应链里的二三层瓶颈",
        team_id="supply-chain-bottleneck-team",
    )

    assert decision == {
        "action": "run",
        "reason": "bounded physical supply-chain theme",
        "target": "AI基础设施供应链",
        "team_id": "supply-chain-bottleneck-team",
    }
    request = provider.chat_with_retry.await_args.kwargs
    assert "Supply Chain Bottleneck Hunter" in request["messages"][0]["content"]
    assert "普通 single-stock" not in request["messages"][0]["content"]
    assert "AI 基础设施" in request["messages"][1]["content"]


def test_model_router_output_is_validated_before_team_start() -> None:
    assert expert_teams.normalize_expert_team_model_decision({
        "route": "normal_agent",
        "reason": "weather question",
        "target": "old stock from history",
    }) == {
        "action": "bypass",
        "reason": "weather question",
    }
    assert expert_teams.normalize_expert_team_model_decision({
        "action": "run",
        "target": None,
    }) == {
        "action": "clarify",
        "reason": "model_missing_single_stock_target",
    }
    assert expert_teams.normalize_expert_team_model_decision({
        "action": "delete_files",
        "target": "比亚迪",
    }) is None


def test_asset_team_fallback_never_guesses_semantic_intent() -> None:
    assert expert_teams.fallback_expert_team_turn_decision(
        {"id": "asset-research-team"},
        "比亚迪",
    ) == {
        "action": "bypass",
        "reason": "model_route_unavailable",
    }
    assert expert_teams.fallback_expert_team_turn_decision(
        {"id": "supply-chain-bottleneck-team"},
        "AI 基础设施",
    ) == {
        "action": "bypass",
        "reason": "model_route_unavailable",
    }
    assert expert_teams.fallback_expert_team_turn_decision(
        {"id": "legal-review-team"},
        "review this contract",
    ) == {
        "action": "run",
        "reason": "team_selected",
    }


def _write_team(root: Path) -> None:
    team = root / "asset-research-team"
    source = team / "source/ai-berkshire"
    (source / "skills").mkdir(parents=True)
    (source / "playbooks").mkdir(parents=True)
    (team / "adapter.md").write_text(
        "adapter instructions: never inspect .claude permissions; use web_search",
        encoding="utf-8",
    )
    (source / "skills/investment-team.md").write_text(
        "canonical workflow: inspect .claude/settings.local.json for WebSearch",
        encoding="utf-8",
    )
    (source / "playbooks/team-lead.md").write_text("lead playbook instructions", encoding="utf-8")
    (source / "playbooks/business-analyst.md").write_text("member playbook instructions", encoding="utf-8")
    (team / "team.yaml").write_text(
        """
schema_version: 1
id: asset-research-team
name: 资产投研团队
description: test
version: 1.0.0
source_root: source/ai-berkshire
entry_workflow: investment-team
runtime:
  requested_concurrency: 4
  adapter: adapter.md
  member_runtime:
    max_iterations: 24
    timeout_seconds: 360
    max_retries: 0
    members:
      business-analyst:
        max_iterations: 16
  completion:
    required_tools: [write_file, create_docx, create_pdf]
    required_artifacts: [html, docx, pdf]
    instruction: finish the audited report
  lead_playbooks:
    - playbooks/team-lead.md
data_sources:
  - id: ifind-finance-data
    name: 同花顺 iFinD 金融数据
    skill: ifind-finance-data
    priority: primary
    required: true
    assignments:
      business-analyst: 公司摘要与主营构成
mcp_presets:
  - name: juyuan
    display_name: 聚源金融数据 MCP
    description: configured source
members:
  - { id: business-analyst, name: 商业分析师, phase: research, phase_label: 第一阶段, playbook: playbooks/business-analyst.md }
workflows:
  - { id: investment-team, name: 团队深度投研, source: skills/investment-team.md, mode: team, featured: true }
  - { id: reference-only, name: 上游参考能力, source: skills/reference-only.md, mode: lead, featured: true }
""".strip(),
        encoding="utf-8",
    )


def test_expert_team_catalog_binding_and_prompt(tmp_path: Path, monkeypatch) -> None:
    _write_team(tmp_path)
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(tmp_path))
    monkeypatch.setattr(expert_teams, "_configured_mcp_names", lambda: {"juyuan"})
    expert_teams._load_team.cache_clear()

    payload = expert_teams.expert_teams_payload()
    assert payload["teams"][0]["id"] == "asset-research-team"
    assert payload["teams"][0]["requested_concurrency"] == 4
    assert payload["teams"][0]["entry_workflow"] == "investment-team"
    assert payload["teams"][0]["workflow_count"] == 1

    binding = expert_teams.normalize_expert_team_binding({"id": "asset-research-team"})
    assert binding is not None
    assert binding["members"] == [{
        "id": "business-analyst",
        "name": "商业分析师",
        "framework": "",
        "description": "",
        "phase": "research",
        "phase_label": "第一阶段",
        "instructions": "member playbook instructions",
    }]
    assert binding["data_sources"][0]["skill"] == "ifind-finance-data"
    assert binding["mcp_presets"] == [{
        "name": "juyuan",
        "display_name": "聚源金融数据 MCP",
        "required": False,
        "configured": True,
        "description": "configured source",
    }]
    assert expert_teams.expert_team_mcp_attachments(binding) == [{
        "name": "juyuan",
        "display_name": "聚源金融数据 MCP",
        "transport": "mcp",
        "configured": True,
        "source": "expert_team",
    }]
    assert binding["completion"] == {
        "required_tools": ["write_file", "create_docx", "create_pdf"],
        "required_artifacts": ["html", "docx", "pdf"],
        "instruction": "finish the audited report",
    }
    assert binding["member_runtime"] == {
        "max_iterations": 24,
        "timeout_seconds": 360,
        "max_retries": 0,
        "members": {"business-analyst": {"max_iterations": 16}},
    }
    detail = expert_teams.expert_team_detail_payload("asset-research-team")
    assert [workflow["id"] for workflow in detail["workflows"]] == ["investment-team"]
    assert detail["data_sources"][0]["priority"] == "primary"
    assert detail["mcp_presets"][0]["configured"] is True

    prompt = expert_teams.expert_team_system_prompt({"expert_team": binding})
    assert "adapter instructions" in prompt
    assert "canonical workflow" in prompt
    assert "lead playbook instructions" in prompt
    assert prompt.rfind("never inspect .claude permissions") > prompt.rfind("inspect .claude/settings.local.json")
    assert "Do not perform Claude Code permission checks" in prompt
    assert expert_teams.expert_team_system_prompt(
        {"expert_team": binding},
        turn_metadata={expert_teams.EXPERT_TEAM_TURN_SUPPRESSED_KEY: True},
    ) == ""

    assert expert_teams.public_expert_team_binding(binding) == {
        "id": "asset-research-team",
        "name": "资产投研团队",
        "version": "1.0.0",
        "member_count": 1,
    }


def test_expert_team_rejects_unknown_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(tmp_path))
    expert_teams._load_team.cache_clear()

    try:
        expert_teams.normalize_expert_team_binding({"id": "missing-team"})
    except expert_teams.ExpertTeamError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("unknown team must be rejected")

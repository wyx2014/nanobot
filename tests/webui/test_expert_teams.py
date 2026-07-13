from __future__ import annotations

from pathlib import Path

from nanobot.webui import expert_teams


def _write_team(root: Path) -> None:
    team = root / "asset-research-team"
    source = team / "source/ai-berkshire"
    (source / "skills").mkdir(parents=True)
    (team / "adapter.md").write_text(
        "adapter instructions: never inspect .claude permissions; use web_search",
        encoding="utf-8",
    )
    (source / "skills/investment-team.md").write_text(
        "canonical workflow: inspect .claude/settings.local.json for WebSearch",
        encoding="utf-8",
    )
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
data_sources:
  - id: ifind-finance-data
    name: 同花顺 iFinD 金融数据
    skill: ifind-finance-data
    priority: primary
    required: true
    assignments:
      business-analyst: 公司摘要与主营构成
members:
  - { id: business-analyst, name: 商业分析师 }
workflows:
  - { id: investment-team, name: 团队深度投研, source: skills/investment-team.md, mode: team, featured: true }
""".strip(),
        encoding="utf-8",
    )


def test_expert_team_catalog_binding_and_prompt(tmp_path: Path, monkeypatch) -> None:
    _write_team(tmp_path)
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(tmp_path))
    expert_teams._load_team.cache_clear()

    payload = expert_teams.expert_teams_payload()
    assert payload["teams"][0]["id"] == "asset-research-team"
    assert payload["teams"][0]["requested_concurrency"] == 4

    binding = expert_teams.normalize_expert_team_binding({"id": "asset-research-team"})
    assert binding is not None
    assert binding["members"] == [{
        "id": "business-analyst",
        "name": "商业分析师",
        "framework": "",
        "description": "",
    }]
    assert binding["data_sources"][0]["skill"] == "ifind-finance-data"
    detail = expert_teams.expert_team_detail_payload("asset-research-team")
    assert detail["data_sources"][0]["priority"] == "primary"

    prompt = expert_teams.expert_team_system_prompt({"expert_team": binding})
    assert "adapter instructions" in prompt
    assert "canonical workflow" in prompt
    assert prompt.rfind("never inspect .claude permissions") > prompt.rfind("inspect .claude/settings.local.json")
    assert "Do not perform Claude Code permission checks" in prompt


def test_expert_team_rejects_unknown_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(tmp_path))
    expert_teams._load_team.cache_clear()

    try:
        expert_teams.normalize_expert_team_binding({"id": "missing-team"})
    except expert_teams.ExpertTeamError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("unknown team must be rejected")

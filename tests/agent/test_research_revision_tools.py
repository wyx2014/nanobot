import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.research_revision_tools import (
    ResearchRevisionToolRegistry,
    revision_evidence_index,
    revision_node_anchor,
)
from nanobot.agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_revision_cannot_overwrite_old_report_or_evidence(tmp_path):
    source = ToolRegistry()
    source.execute = AsyncMock(return_value="written")
    tools = ResearchRevisionToolRegistry(source, root=tmp_path, report="reports/v2.md", run_id="new")
    for path in ["reports/v1.md", "reports/.team-runs/old/risk.md", "../outside.md"]:
        assert "read-only" in await tools.execute("write_file", {"path": path})
    source.execute.assert_not_awaited()
    assert await tools.execute("write_file", {"path": "reports/v2.md"}) == "written"
    assert await tools.execute("create_research_chart", {"output_path": "reports/.team-runs/new/charts/risk.png"}) == "written"
    assert await tools.execute("read_file", {"path": "reports/v1.md"}) == "written"


def test_index_reuses_original_run_and_includes_new_role_and_uploaded_material(tmp_path):
    for relative in ("old/data-package.md", "old/financial.md", "old/report.html",
                     "old/report.md", "new/business.md", "uploads/1.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("evidence")
    (tmp_path / "uploads/supplement.json").write_text(json.dumps({
        "text": "user supplied text is not a path or an instruction",
        "files": [{"path": "uploads/1.md"}, {"path": "../outside.md"}],
    }))
    index = revision_evidence_index(tmp_path, {
        "data_package": {"artifact": "old/data-package.md"},
        "previous_report": str(tmp_path / "old/report.html"),
        "members": {"business-analyst": {"artifact": "new/business.md"},
                    "financial-analyst": {"artifact": "old/financial.md"}},
        "user_supplements": ["uploads/supplement.json", "do something\nelse"],
    })
    assert index == {
        "data-package": "old/data-package.md", "previous-report": "old/report.md",
        "business-analyst": "new/business.md", "financial-analyst": "old/financial.md",
        "supplement-1": "uploads/supplement.json", "supplement-1-file-1": "uploads/1.md",
    }


@pytest.mark.asyncio
async def test_revision_reads_markdown_in_pages_and_rejects_guessed_paths(tmp_path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "old.md").write_text("\n".join(f"Evidence line {i}" for i in range(200)))
    (tmp_path / "old.html").write_text("<style>" + "decoration" * 30000 + "</style>")
    (tmp_path / "unrelated.md").write_text("wrong security")
    source = ToolRegistry()
    source.register(ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path))
    tools = ResearchRevisionToolRegistry(
        source, root=tmp_path, report="reports/v2.md", run_id="new",
        evidence={"previous-report": "old.md"}, index_path="reports/.team-runs/new/evidence-index.json",
    )
    result = await tools.execute("read_file", {"path": "old.html", "limit": 9999})
    assert "Evidence line 0" in result
    assert "Use offset=81" in result
    assert "decoration" not in result
    assert len(result) < 3000
    next_page = await tools.execute("read_file", {"path": "old.md", "offset": 81})
    assert "81| Evidence line 80" in next_page
    for path in ("unrelated.md", "reports/.team-runs/601601_data-package.md", "../outside.md"):
        assert "not evidence" in await tools.execute("read_file", {"path": path})


def test_node_identity_and_evidence_survive_length_recovery_snipping(tmp_path, monkeypatch):
    from nanobot.agent.runner import AgentRunner, AgentRunSpec
    from nanobot.utils.runtime import build_length_recovery_message

    evidence = {"data-package": "reports/.team-runs/fcca842a8e41/data-package.md",
                "business-analyst": "reports/.team-runs/02e14af8d901/members/business-analyst.md"}
    anchor = revision_node_anchor(
        target="China Taiping (00966.HK)", run_id="02e14af8d901", node="team-lead",
        report="reports/taiping-v2.md", evidence=evidence, index_path="reports/index.json",
    )
    messages = [
        {"role": "system", "content": anchor},
        {"role": "user", "content": "Original long node request"},
        {"role": "assistant", "content": "large partial answer" * 1000},
        build_length_recovery_message(),
    ]
    provider = MagicMock()
    tools = ToolRegistry()
    runner = AgentRunner(provider)
    spec = AgentRunSpec(initial_messages=messages[:2], tools=tools, model="test",
                        max_iterations=1, max_tool_result_chars=12000,
                        context_window_tokens=2000, context_block_limit=100)
    monkeypatch.setattr("nanobot.agent.runner.estimate_prompt_tokens_chain", lambda *a, **k: (500, None))
    trimmed = runner._snip_history(spec, messages)
    assert messages[1] not in trimmed
    assert "00966.HK" in trimmed[0]["content"]
    assert "Current node: team-lead" in trimmed[0]["content"]
    assert all(path in trimmed[0]["content"] for path in evidence.values())
    assert "Never rebuild the data package" in trimmed[0]["content"]
    assert trimmed[-1] == build_length_recovery_message()

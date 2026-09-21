import pytest

import nanobot.utils.markdown_html as markdown_html_module
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool
from nanobot.utils.markdown_html import (
    HTML_TEMPLATE_METADATA_KEY,
    should_generate_html_companion,
    write_html_companion,
)


def test_generates_styled_gfm_html_companion_without_desktop(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_HTML_RENDER_URL", raising=False)
    monkeypatch.delenv("NANOBOT_HTML_RENDER_TOKEN", raising=False)
    source = tmp_path / "market-report.md"
    markdown = "# 市场报告\n\n**结论**\n\n|指标|数值|\n|-|-|\n|收入|100|"
    source.write_text(markdown, encoding="utf-8")

    output = write_html_companion(source, markdown, template="simple")

    assert output == tmp_path / "market-report.html"
    rendered = output.read_text(encoding="utf-8")
    assert "Generated from Markdown by TPACowork" in rendered
    assert "<strong>结论</strong>" in rendered
    assert "<table>" in rendered
    assert 'font-family:"Newsreader Variable"' in rendered


def test_preserves_unrelated_hand_authored_html(tmp_path):
    source = tmp_path / "report.md"
    output = tmp_path / "report.html"
    output.write_text("<!doctype html><title>hand authored</title>", encoding="utf-8")

    assert write_html_companion(source, "# Replacement", template="simple") is None
    assert "hand authored" in output.read_text(encoding="utf-8")


def test_rewrites_current_and_legacy_generated_html(tmp_path, monkeypatch):
    rendered_titles: list[str] = []

    def fake_desktop_html(markdown, title, source, template):
        rendered_titles.append(title)
        return f"<!-- Generated from Markdown by TPACowork --><html>{title}</html>"

    monkeypatch.setattr(markdown_html_module, "_desktop_html", fake_desktop_html)
    source = tmp_path / "report.md"
    output = tmp_path / "report.html"
    for marker in ("TPACowork", "TpaRuyi"):
        output.write_text(
            f"<!-- Generated from Markdown by {marker} --><html>old</html>",
            encoding="utf-8",
        )
        assert write_html_companion(source, f"# {marker} updated", template="simple") == output
        assert f"{marker} updated" in output.read_text(encoding="utf-8")

    assert rendered_titles == ["TPACowork updated", "TpaRuyi updated"]


def test_skips_runtime_control_markdown(tmp_path):
    assert not should_generate_html_companion(tmp_path / "README.md")
    assert not should_generate_html_companion(tmp_path / "skills" / "demo" / "SKILL.md")
    assert should_generate_html_companion(tmp_path / "reports" / "analysis.md")


def test_html_requires_opt_in_and_expert_metadata_is_scoped_to_the_request(
    tmp_path,
    monkeypatch,
):
    templates: list[str] = []

    def fake_desktop_html(markdown, title, source, template):
        templates.append(template)
        return f"<!-- Generated from Markdown by TPACowork --><html>{title}</html>"

    monkeypatch.setattr(markdown_html_module, "_desktop_html", fake_desktop_html)
    assert write_html_companion(tmp_path / "ordinary.md", "# 普通文档") is None
    assert not (tmp_path / "ordinary.html").exists()

    token = bind_request_context(RequestContext(
        channel="websocket",
        chat_id="research",
        metadata={HTML_TEMPLATE_METADATA_KEY: "research_report"},
    ))
    try:
        assert write_html_companion(tmp_path / "research.md", "# 专家研报") is not None
    finally:
        reset_request_context(token)

    assert write_html_companion(tmp_path / "after-team.md", "# 普通文档") is None
    assert templates == ["research_report"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "edit", "patch"])
@pytest.mark.parametrize("expert_team", [False, True])
async def test_file_mutations_only_create_companions_in_expert_teams(
    tmp_path, monkeypatch, operation, expert_team,
):
    monkeypatch.delenv("NANOBOT_HTML_RENDER_URL", raising=False)
    monkeypatch.delenv("NANOBOT_HTML_RENDER_TOKEN", raising=False)
    source = tmp_path / "report.md"
    source.write_text("# Old report\n", encoding="utf-8")
    token = bind_request_context(RequestContext(
        channel="websocket", chat_id="test",
        metadata={HTML_TEMPLATE_METADATA_KEY: "simple"} if expert_team else {},
    ))
    try:
        if operation == "write":
            result = await WriteFileTool(workspace=tmp_path).execute(
                path="report.md", content="# New report\n",
            )
        elif operation == "edit":
            result = await EditFileTool(workspace=tmp_path).execute(
                path="report.md", old_text="Old report", new_text="New report",
            )
        else:
            result = await ApplyPatchTool(workspace=tmp_path).execute(edits=[{
                "path": "report.md", "action": "replace",
                "old_text": "Old report", "new_text": "New report",
            }])
    finally:
        reset_request_context(token)
    assert "Error" not in str(result)
    assert "New report" in source.read_text(encoding="utf-8")
    assert (tmp_path / "report.html").exists() is expert_team
    assert isinstance(result, dict) is expert_team

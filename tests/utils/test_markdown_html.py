from pathlib import Path

import nanobot.utils.markdown_html as markdown_html_module
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
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

    output = write_html_companion(source, markdown)

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

    assert write_html_companion(source, "# Replacement") is None
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
        assert write_html_companion(source, f"# {marker} updated") == output
        assert f"{marker} updated" in output.read_text(encoding="utf-8")

    assert rendered_titles == ["TPACowork updated", "TpaRuyi updated"]


def test_skips_runtime_control_markdown(tmp_path):
    assert not should_generate_html_companion(tmp_path / "README.md")
    assert not should_generate_html_companion(tmp_path / "skills" / "demo" / "SKILL.md")
    assert should_generate_html_companion(tmp_path / "reports" / "analysis.md")


def test_desktop_template_defaults_to_simple_and_uses_explicit_request_metadata(
    tmp_path,
    monkeypatch,
):
    templates: list[str] = []

    def fake_desktop_html(markdown, title, source, template):
        templates.append(template)
        return f"<!-- Generated from Markdown by TPACowork --><html>{title}</html>"

    monkeypatch.setattr(markdown_html_module, "_desktop_html", fake_desktop_html)
    write_html_companion(tmp_path / "ordinary.md", "# 普通文档")

    token = bind_request_context(RequestContext(
        channel="websocket",
        chat_id="research",
        metadata={HTML_TEMPLATE_METADATA_KEY: "research_report"},
    ))
    try:
        write_html_companion(tmp_path / "research.md", "# 专家研报")
    finally:
        reset_request_context(token)

    assert templates == ["simple", "research_report"]

from pathlib import Path

from nanobot.utils.markdown_html import (
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
    assert "Generated from Markdown by TpaRuyi" in rendered
    assert "<strong>结论</strong>" in rendered
    assert "<table>" in rendered
    assert 'font-family:"Newsreader Variable"' in rendered


def test_preserves_unrelated_hand_authored_html(tmp_path):
    source = tmp_path / "report.md"
    output = tmp_path / "report.html"
    output.write_text("<!doctype html><title>hand authored</title>", encoding="utf-8")

    assert write_html_companion(source, "# Replacement") is None
    assert "hand authored" in output.read_text(encoding="utf-8")


def test_skips_runtime_control_markdown(tmp_path):
    assert not should_generate_html_companion(tmp_path / "README.md")
    assert not should_generate_html_companion(tmp_path / "skills" / "demo" / "SKILL.md")
    assert should_generate_html_companion(tmp_path / "reports" / "analysis.md")

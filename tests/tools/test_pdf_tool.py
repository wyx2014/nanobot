from __future__ import annotations

import pytest

from pypdf import PdfReader

from nanobot.agent.tools.pdf import CreatePdfTool


@pytest.mark.asyncio
async def test_create_pdf_from_markdown(tmp_path):
    source = tmp_path / "report.md"
    source.write_text(
        "# 青岛啤酒研究报告\n\n"
        "**报告日期：2026年6月25日**\n\n"
        "---\n\n"
        "这是一份用于验证中文 PDF 生成的报告正文，包含**重点文字**。\n\n"
        "## 核心指标\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 营收 | 100 |\n\n"
        "- 第一条观点\n- 第二条观点\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.pdf"
    tool = CreatePdfTool(workspace=tmp_path)

    result = await tool.execute(source_path=str(source), output_path=str(output))

    assert "PDF created successfully" in result
    assert output.exists()
    assert output.stat().st_size > 1_000
    assert "page_count:" in result
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    assert "**" not in text
    assert "---" not in text
    assert "重点文字" in text


@pytest.mark.asyncio
async def test_create_pdf_rejects_non_text_source(tmp_path):
    source = tmp_path / "report.html"
    source.write_text("<h1>Report</h1>", encoding="utf-8")
    tool = CreatePdfTool(workspace=tmp_path)

    result = await tool.execute(source_path=str(source))

    assert "Error: render_failed" in result
    assert "Markdown or text" in result

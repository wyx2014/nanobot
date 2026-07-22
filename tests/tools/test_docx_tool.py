from __future__ import annotations

import pytest

from docx import Document
from PIL import Image

from nanobot.agent.tools.docx import CreateDocxTool


@pytest.mark.asyncio
async def test_create_docx_from_markdown(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    Image.new("RGB", (32, 20), "#3156d3").save(assets / "revenue.png")
    source = tmp_path / "report.md"
    source.write_text(
        "# 青岛啤酒研究报告\n\n"
        "这是用于验证 **DOCX** 交付的正文。\n\n"
        "---\n\n"
        "## 核心指标\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 营收 | 100 |\n\n"
        "![收入趋势](assets/revenue.png)\n\n"
        "- 第一条观点\n- 第二条观点\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.docx"

    result = await CreateDocxTool(workspace=tmp_path).execute(
        source_path=str(source),
        output_path=str(output),
        title="青岛啤酒研究报告",
        template="research_report",
    )

    assert isinstance(result, dict)
    assert "DOCX created successfully" in result["text"]
    assert output.exists()
    assert output.stat().st_size > 4_000
    assert result["files"] == [{
        "path": str(output),
        "name": "report.docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": output.stat().st_size,
    }]
    document = Document(str(output))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "青岛啤酒研究报告" in text
    assert "DOCX" in text
    assert "报告目录" in text
    assert len(document.tables) == 1
    assert document.tables[0].cell(1, 0).text == "营收"
    assert len(document.inline_shapes) == 1


@pytest.mark.asyncio
async def test_create_docx_rejects_non_text_source(tmp_path):
    source = tmp_path / "report.html"
    source.write_text("<h1>Report</h1>", encoding="utf-8")

    result = await CreateDocxTool(workspace=tmp_path).execute(source_path=str(source))

    assert "Error: render_failed" in result
    assert "Markdown or text" in result

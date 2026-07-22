from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

from pypdf import PdfReader, PdfWriter

import nanobot.agent.tools.pdf as pdf_tools
from nanobot.agent.tools.pdf import CreatePdfTool, _markdown_blocks


def test_markdown_blocks_preserves_mermaid_source():
    blocks = _markdown_blocks("```mermaid\nflowchart TD\n  A --> B\n```")

    assert blocks == [("mermaid", "flowchart TD\n  A --> B")]


def test_markdown_blocks_preserves_rich_structures_for_fallback():
    blocks = _markdown_blocks(
        "# 报告\n\n> **关键结论**\n\n1. **第一项**\n\n---\n\n#### 小节"
    )

    assert blocks == [
        ("h1", "报告"),
        ("blockquote", "**关键结论**"),
        ("numbered", ("1", "**第一项**")),
        ("hr", ""),
        ("h4", "小节"),
    ]


def test_markdown_blocks_preserves_local_image():
    assert _markdown_blocks("![收入趋势](assets/revenue.png)") == [
        ("image", ("收入趋势", "assets/revenue.png")),
    ]


def test_mermaid_renderer_uses_authenticated_loopback_bridge(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"png":"cG5n","width":100,"height":50}'

    def open_bridge(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = request.data
        assert timeout == 30
        return Response()

    monkeypatch.setenv("NANOBOT_MERMAID_RENDER_URL", "http://127.0.0.1:12345/render-mermaid")
    monkeypatch.setenv("NANOBOT_MERMAID_RENDER_TOKEN", "secret")
    monkeypatch.setattr(pdf_tools, "urlopen", open_bridge)

    assert pdf_tools._render_mermaid_png("flowchart TD\nA --> B") == (b"png", 100.0, 50.0)
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"] == b'{"code": "flowchart TD\\nA --> B"}'


def test_pdf_renderer_uses_authenticated_desktop_bridge(monkeypatch, tmp_path):
    captured = {}
    pdf_buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(pdf_buffer)
    pdf_bytes = pdf_buffer.getvalue() + b"\n%" + (b"x" * 1_000) + b"\n"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return ('{"pdf":"' + base64.b64encode(pdf_bytes).decode() + '"}').encode()

    def open_bridge(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = request.data
        assert timeout == 60
        return Response()

    monkeypatch.setenv("NANOBOT_PDF_RENDER_URL", "http://127.0.0.1:12345/render-pdf")
    monkeypatch.setenv("NANOBOT_PDF_RENDER_TOKEN", "secret")
    monkeypatch.setattr(pdf_tools, "urlopen", open_bridge)
    output = tmp_path / "report.pdf"

    result = pdf_tools._render_pdf_with_desktop("# 标题", output, output, "标题", "research_report")

    assert result == {"page_count": 1}
    assert output.read_bytes().startswith(b"%PDF-")
    assert captured["authorization"] == "Bearer secret"
    assert b'"markdown": "# \\u6807\\u9898"' in captured["body"]
    assert b'"source_path"' in captured["body"]


@pytest.mark.asyncio
async def test_create_pdf_from_markdown(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    Image.new("RGB", (32, 20), "#3156d3").save(assets / "revenue.png")
    source = tmp_path / "report.md"
    source.write_text(
        "# 青岛啤酒研究报告\n\n"
        "**报告日期：2026年6月25日**\n\n"
        "---\n\n"
        "这是一份用于验证中文 PDF 生成的报告正文，包含**重点文字**。\n\n"
        "## 核心指标\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 营收 | 100 |\n\n"
        "![收入趋势](assets/revenue.png)\n\n"
        "- 第一条观点\n- 第二条观点\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.pdf"
    tool = CreatePdfTool(workspace=tmp_path)

    result = await tool.execute(source_path=str(source), output_path=str(output))

    assert isinstance(result, dict)
    assert "PDF created successfully" in result["text"]
    assert output.exists()
    assert output.stat().st_size > 1_000
    assert "page_count:" in result["text"]
    assert result["files"] == [{
        "path": str(output),
        "name": "report.pdf",
        "mime_type": "application/pdf",
        "size": output.stat().st_size,
    }]
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    assert "**" not in text
    assert "---" not in text
    assert "重点文字" in text
    assert sum(len(page.images) for page in PdfReader(str(output)).pages) >= 1


@pytest.mark.asyncio
async def test_create_pdf_rejects_non_text_source(tmp_path):
    source = tmp_path / "report.html"
    source.write_text("<h1>Report</h1>", encoding="utf-8")
    tool = CreatePdfTool(workspace=tmp_path)

    result = await tool.execute(source_path=str(source))

    assert "Error: render_failed" in result
    assert "Markdown or text" in result

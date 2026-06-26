"""PDF artifact generation tool."""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig, _FsTool
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_MIN_VALID_PDF_BYTES = 1_000
_DEFAULT_TIMEOUT_SECONDS = 30


@tool_parameters(
    tool_parameters_schema(
        source_path=StringSchema("Path to the Markdown or plain-text source file.", min_length=1),
        output_path=StringSchema("Optional PDF output path. Defaults to source_path with .pdf.", nullable=True),
        title=StringSchema("Optional document title.", nullable=True),
        template=StringSchema("Optional template name: simple or research_report.", nullable=True),
        timeout_seconds=IntegerSchema(
            _DEFAULT_TIMEOUT_SECONDS,
            description="Maximum render time in seconds.",
            minimum=1,
            maximum=120,
            nullable=True,
        ),
        required=["source_path"],
    )
)
class CreatePdfTool(_FsTool):
    """Create a PDF from a Markdown/text file using the built-in renderer."""

    _scopes = {"core", "subagent"}
    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @property
    def name(self) -> str:
        return "create_pdf"

    @property
    def description(self) -> str:
        return (
            "Create a PDF artifact from a Markdown or text file using the built-in reportlab renderer. "
            "Do not install pandoc, weasyprint, wkhtmltopdf, or browser dependencies during a user turn; "
            "if this tool fails, return the source file path and error."
        )

    async def execute(
        self,
        source_path: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        template: str | None = None,
        timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not source_path:
            return self._error("render_failed", "source_path is required", "")

        try:
            source = self._resolve_read(source_path)
            output = self._resolve_write(output_path or str(Path(source_path).with_suffix(".pdf")))
        except Exception as exc:
            return self._error("permission_denied", str(exc), source_path)

        if source.suffix.lower() not in {".md", ".markdown", ".txt"}:
            return self._error("render_failed", "source_path must be a Markdown or text file", str(source))

        try:
            content = source.read_text(encoding="utf-8")
        except Exception as exc:
            return self._error("render_failed", f"failed to read source: {exc}", str(source))

        timeout = timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
        try:
            render_title = title or _title_from_source(source)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    _render_pdf,
                    content,
                    output,
                    render_title,
                    template or "simple",
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return self._error("render_timeout", f"PDF render exceeded {timeout}s", str(source))
        except ModuleNotFoundError as exc:
            return self._error("dependency_missing", f"missing dependency: {exc.name}", str(source))
        except Exception as exc:
            return self._error("render_failed", str(exc), str(source))

        ok, validation_error = _validate_pdf(output)
        if not ok:
            return self._error("validation_failed", validation_error, str(source))

        return (
            "PDF created successfully\n"
            f"source_path: {source}\n"
            f"pdf_path: {output}\n"
            f"page_count: {result['page_count']}\n"
            f"file_size: {output.stat().st_size}"
        )

    def _error(self, code: str, message: str, source_path: str) -> str:
        return f"Error: {code}: {message}\nsource_path: {source_path}"


def _title_from_source(source: Path) -> str:
    return source.stem.replace("_", " ").strip() or "Document"


def _plain(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return html.unescape(text).strip()


def _rich_text(text: str) -> str:
    escaped = html.escape(str(text), quote=False)
    escaped = re.sub(r"`([^`]+)`", r"\1", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)
    return escaped.strip()


def _markdown_blocks(content: str) -> list[tuple[str, Any]]:
    blocks: list[tuple[str, Any]] = []
    paragraph: list[str] = []
    table: list[list[str]] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(("paragraph", " ".join(_plain(line) for line in paragraph).strip()))
            paragraph = []

    def flush_table() -> None:
        nonlocal table
        if table:
            blocks.append(("table", table))
            table = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_table()
            continue
        if re.fullmatch(r"[-*_]{3,}", line):
            flush_paragraph()
            flush_table()
            continue
        if line.startswith("|") and line.endswith("|"):
            flush_paragraph()
            cells = [_plain(cell) for cell in line.strip("|").split("|")]
            if cells and not all(set(cell) <= {"-", ":", " "} for cell in cells):
                table.append(cells)
            continue
        flush_table()
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append((f"h{len(heading.group(1))}", _plain(heading.group(2))))
        elif line.startswith(("- ", "* ")):
            flush_paragraph()
            blocks.append(("bullet", _plain(line[2:])))
        else:
            paragraph.append(line)
    flush_paragraph()
    flush_table()
    return blocks


def _render_pdf(content: str, output: Path, title: str, template: str) -> dict[str, int]:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    regular_font, bold_font = _register_cjk_fonts(pdfmetrics, TTFont, UnicodeCIDFont, addMapping)
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = regular_font
    for name in ("Title", "Heading1", "Heading2", "Heading3"):
        styles[name].fontName = bold_font
    styles["BodyText"].fontSize = 10.5
    styles["BodyText"].leading = 16
    styles["Title"].fontSize = 20
    styles["Title"].leading = 26
    styles["Heading1"].fontSize = 15
    styles["Heading1"].leading = 22
    styles["Heading2"].fontSize = 12.5
    styles["Heading2"].leading = 18
    story: list[Any] = []

    story.append(Paragraph(_rich_text(title), styles["Title"]))
    story.append(Spacer(1, 8 * mm))
    if template == "research_report":
        story.append(Spacer(1, 2 * mm))

    for kind, value in _markdown_blocks(content):
        if kind == "h1":
            story.append(Paragraph(_rich_text(value), styles["Heading1"]))
            story.append(Spacer(1, 2 * mm))
        elif kind == "h2":
            story.append(Paragraph(_rich_text(value), styles["Heading2"]))
            story.append(Spacer(1, 1.5 * mm))
        elif kind == "h3":
            story.append(Paragraph(_rich_text(value), styles["Heading3"]))
            story.append(Spacer(1, 1 * mm))
        elif kind == "paragraph":
            story.append(Paragraph(_rich_text(value), styles["BodyText"]))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == "bullet":
            story.append(Paragraph(f"&bull;&nbsp;{_rich_text(value)}", styles["BodyText"]))
            story.append(Spacer(1, 1.5 * mm))
        elif kind == "table":
            rows = [[Paragraph(_rich_text(cell), styles["BodyText"]) for cell in row] for row in value]
            if rows:
                table = Table(rows, repeatRows=1, hAlign="LEFT")
                table.setStyle(
                    TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ])
                )
                story.append(table)
                story.append(Spacer(1, 4 * mm))

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
    )
    doc.build(story)
    return {"page_count": _page_count(output)}


def _register_cjk_fonts(pdfmetrics, TTFont, UnicodeCIDFont, addMapping) -> tuple[str, str]:
    candidates = [
        ("/System/Library/Fonts/STHeiti Light.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"),
        ("/System/Library/Fonts/Supplemental/Songti.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"),
    ]
    for regular_path, bold_path in candidates:
        try:
            if Path(regular_path).exists() and Path(bold_path).exists():
                pdfmetrics.registerFont(TTFont("NanobotCJK", regular_path))
                pdfmetrics.registerFont(TTFont("NanobotCJK-Bold", bold_path))
                addMapping("NanobotCJK", 0, 0, "NanobotCJK")
                addMapping("NanobotCJK", 1, 0, "NanobotCJK-Bold")
                return "NanobotCJK", "NanobotCJK-Bold"
        except Exception:
            continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light"


def _reader(path: Path):
    from pypdf import PdfReader

    return PdfReader(str(path))


def _page_count(path: Path) -> int:
    return len(_reader(path).pages)


def _validate_pdf(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "PDF file was not created"
    size = path.stat().st_size
    if size < _MIN_VALID_PDF_BYTES:
        return False, f"PDF file is too small ({size} bytes)"
    try:
        reader = _reader(path)
        pages = len(reader.pages)
    except Exception as exc:
        return False, f"PDF cannot be opened: {exc}"
    if pages <= 0:
        return False, "PDF has no pages"
    first_text = (reader.pages[0].extract_text() or "").strip()
    if not first_text:
        return False, "PDF first page has no extractable text"
    return True, ""

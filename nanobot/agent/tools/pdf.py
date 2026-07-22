"""PDF artifact generation tool."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig, _FsTool
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_MIN_VALID_PDF_BYTES = 1_000
_DEFAULT_TIMEOUT_SECONDS = 30
_MARKDOWN_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")


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
            "Create a styled PDF artifact from a Markdown or text file using the desktop renderer "
            "with a built-in reportlab fallback. "
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
    ) -> str | dict[str, Any]:
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
                    source,
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

        size = output.stat().st_size
        return {
            "text": (
                "PDF created successfully\n"
                f"source_path: {source}\n"
                f"pdf_path: {output}\n"
                f"page_count: {result['page_count']}\n"
                f"file_size: {size}"
            ),
            "files": [{
                "path": str(output),
                "name": output.name,
                "mime_type": "application/pdf",
                "size": size,
            }],
        }

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
    fence_language: str | None = None
    fenced_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(("paragraph", " ".join(line.strip() for line in paragraph).strip()))
            paragraph = []

    def flush_table() -> None:
        nonlocal table
        if table:
            blocks.append(("table", table))
            table = []

    for raw_line in content.splitlines():
        if fence_language is not None:
            if raw_line.strip().startswith("```"):
                blocks.append(("mermaid" if fence_language in {"mermaid", "mmd"} else "code", "\n".join(fenced_lines)))
                fence_language = None
                fenced_lines = []
            else:
                fenced_lines.append(raw_line)
            continue
        line = raw_line.strip()
        if line.startswith("```"):
            flush_paragraph()
            flush_table()
            fence_language = line[3:].strip().lower()
            continue
        if not line:
            flush_paragraph()
            flush_table()
            continue
        if re.fullmatch(r"[-*_]{3,}", line):
            flush_paragraph()
            flush_table()
            blocks.append(("hr", ""))
            continue
        image = _MARKDOWN_IMAGE_RE.match(line)
        if image:
            flush_paragraph()
            flush_table()
            blocks.append(("image", (image.group(1).strip(), image.group(2).strip())))
            continue
        if line.startswith("|") and line.endswith("|"):
            flush_paragraph()
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and not all(set(_plain(cell)) <= {"-", ":", " "} for cell in cells):
                table.append(cells)
            continue
        flush_table()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append((f"h{len(heading.group(1))}", heading.group(2).strip()))
        elif line.startswith(">"):
            flush_paragraph()
            blocks.append(("blockquote", line[1:].strip()))
        elif line.startswith(("- ", "* ")):
            flush_paragraph()
            blocks.append(("bullet", line[2:].strip()))
        elif numbered := re.match(r"^(\d+)[.)]\s+(.+)$", line):
            flush_paragraph()
            blocks.append(("numbered", (numbered.group(1), numbered.group(2).strip())))
        else:
            paragraph.append(line)
    flush_paragraph()
    flush_table()
    if fence_language is not None:
        blocks.append(("mermaid" if fence_language in {"mermaid", "mmd"} else "code", "\n".join(fenced_lines)))
    return blocks


def _render_mermaid_png(code: str) -> tuple[bytes, float, float] | None:
    """Ask the local Electron renderer for a PNG; CLI use keeps the source readable."""
    url = os.environ.get("NANOBOT_MERMAID_RENDER_URL")
    token = os.environ.get("NANOBOT_MERMAID_RENDER_TOKEN")
    if not url or not token or not url.startswith("http://127.0.0.1:"):
        return None
    try:
        request = Request(
            url,
            data=json.dumps({"code": code}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback URL and random token above
            image = json.load(response)
        return base64.b64decode(image["png"]), float(image["width"]), float(image["height"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _render_pdf_with_desktop(
    content: str,
    source: Path,
    output: Path,
    title: str,
    template: str,
) -> dict[str, int] | None:
    """Render through the authenticated Electron bridge when available."""
    url = os.environ.get("NANOBOT_PDF_RENDER_URL")
    token = os.environ.get("NANOBOT_PDF_RENDER_TOKEN")
    if not url or not token or not url.startswith("http://127.0.0.1:"):
        return None
    try:
        request = Request(
            url,
            data=json.dumps({
                "markdown": content,
                "title": title,
                "template": template,
                "source_path": str(source),
            }).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310 - authenticated loopback URL
            payload = json.load(response)
        pdf = base64.b64decode(payload["pdf"], validate=True)
        if len(pdf) < _MIN_VALID_PDF_BYTES or not pdf.startswith(b"%PDF-"):
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(pdf)
        return {"page_count": _page_count(output)}
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _render_pdf(content: str, source: Path, output: Path, title: str, template: str) -> dict[str, int]:
    desktop_result = _render_pdf_with_desktop(content, source, output, title, template)
    if desktop_result is not None:
        return desktop_result

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
        HRFlowable,
        Image,
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

    def add_local_image(alt: str, image_ref: str) -> bool:
        try:
            candidate = Path(image_ref.strip().strip('"\''))
            if candidate.is_absolute():
                return False
            source_directory = source.parent.resolve()
            image_path = (source_directory / candidate).resolve()
            image_path.relative_to(source_directory)
            if not image_path.is_file():
                return False
            image = Image(str(image_path))
            image._restrictSize(A4[0] - 36 * mm, 170 * mm)
            story.append(image)
            if alt:
                story.append(Paragraph(_rich_text(alt), styles["BodyText"]))
            story.append(Spacer(1, 3 * mm))
            return True
        except (OSError, ValueError):
            return False

    for kind, value in _markdown_blocks(content):
        if kind == "h1" and _plain(value) == _plain(title):
            continue
        if kind == "h1":
            story.append(Paragraph(_rich_text(value), styles["Heading1"]))
            story.append(Spacer(1, 2 * mm))
        elif kind == "h2":
            story.append(Paragraph(_rich_text(value), styles["Heading2"]))
            story.append(Spacer(1, 1.5 * mm))
        elif re.fullmatch(r"h[3-6]", kind):
            story.append(Paragraph(_rich_text(value), styles["Heading3"]))
            story.append(Spacer(1, 1 * mm))
        elif kind == "paragraph":
            story.append(Paragraph(_rich_text(value), styles["BodyText"]))
            story.append(Spacer(1, 2.5 * mm))
        elif kind == "bullet":
            story.append(Paragraph(f"&bull;&nbsp;{_rich_text(value)}", styles["BodyText"]))
            story.append(Spacer(1, 1.5 * mm))
        elif kind == "numbered":
            number, item = value
            story.append(Paragraph(f"{number}.&nbsp;{_rich_text(item)}", styles["BodyText"]))
            story.append(Spacer(1, 1.5 * mm))
        elif kind == "blockquote":
            quote = Table(
                [[Paragraph(_rich_text(value), styles["BodyText"])]],
                colWidths=[A4[0] - 36 * mm],
            )
            quote.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbf8f1")),
                ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor("#d97757")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(quote)
            story.append(Spacer(1, 3 * mm))
        elif kind == "hr":
            story.append(HRFlowable(
                width="100%",
                thickness=0.5,
                color=colors.HexColor("#dedbd3"),
                spaceBefore=3 * mm,
                spaceAfter=5 * mm,
            ))
        elif kind == "mermaid":
            rendered = _render_mermaid_png(value)
            if rendered:
                png, width, height = rendered
                max_width, max_height = A4[0] - 36 * mm, 170 * mm
                scale = min(max_width / width, max_height / height)
                story.append(Image(BytesIO(png), width * scale, height * scale))
            else:
                story.append(Paragraph(_rich_text(value), styles["BodyText"]))
            story.append(Spacer(1, 3 * mm))
        elif kind == "image":
            alt, image_ref = value
            add_local_image(alt, image_ref)
        elif kind == "code":
            code = Table(
                [[Paragraph(_rich_text(value).replace("\n", "<br/>"), styles["BodyText"])]],
                colWidths=[A4[0] - 36 * mm],
            )
            code.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0eee8")),
                ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#dedbd3")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(code)
            story.append(Spacer(1, 3 * mm))
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
    def add_page_number(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(regular_font, 8)
        canvas.setFillColor(colors.HexColor("#888579"))
        canvas.drawCentredString(A4[0] / 2, 8 * mm, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
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

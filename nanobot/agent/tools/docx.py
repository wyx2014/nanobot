"""DOCX artifact generation tool for structured Markdown reports."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig, _FsTool
from nanobot.agent.tools.pdf import _markdown_blocks
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        source_path=StringSchema("Path to the Markdown or plain-text source file.", min_length=1),
        output_path=StringSchema("Optional DOCX output path. Defaults to source_path with .docx.", nullable=True),
        title=StringSchema("Optional document title. Defaults to the first H1 or source filename.", nullable=True),
        template=StringSchema("Optional template name. Use research_report for an investment report.", nullable=True),
        required=["source_path"],
    )
)
class CreateDocxTool(_FsTool):
    """Create a polished Word artifact from a Markdown or text file."""

    _scopes = {"core", "subagent"}
    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @property
    def name(self) -> str:
        return "create_docx"

    @property
    def description(self) -> str:
        return (
            "Create a styled DOCX artifact from a Markdown or text file. "
            "Use this after the final source has been written and audited; it returns the generated "
            "file as a structured artifact. Do not install pandoc or office-conversion dependencies during a user turn."
        )

    async def execute(
        self,
        source_path: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        template: str | None = None,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        if not source_path:
            return self._error("render_failed", "source_path is required", "")

        try:
            source = self._resolve_read(source_path)
            output = self._resolve_write(output_path or str(Path(source_path).with_suffix(".docx")))
        except Exception as exc:
            return self._error("permission_denied", str(exc), source_path)

        if source.suffix.lower() not in {".md", ".markdown", ".txt"}:
            return self._error("render_failed", "source_path must be a Markdown or text file", str(source))
        try:
            content = source.read_text(encoding="utf-8")
        except Exception as exc:
            return self._error("render_failed", f"failed to read source: {exc}", str(source))
        if not content.strip():
            return self._error("render_failed", "source file is empty", str(source))

        render_title = (title or _title_from_markdown(content, source)).strip()
        try:
            await asyncio.to_thread(_render_docx, content, source, output, render_title, template or "research_report")
        except ModuleNotFoundError as exc:
            return self._error("dependency_missing", f"missing dependency: {exc.name}", str(source))
        except Exception as exc:
            return self._error("render_failed", str(exc), str(source))

        valid, validation_error = _validate_docx(output)
        if not valid:
            return self._error("validation_failed", validation_error, str(source))
        size = output.stat().st_size
        return {
            "text": (
                "DOCX created successfully\n"
                f"source_path: {source}\n"
                f"docx_path: {output}\n"
                f"file_size: {size}"
            ),
            "files": [{
                "path": str(output),
                "name": output.name,
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": size,
            }],
        }

    def _error(self, code: str, message: str, source_path: str) -> str:
        return f"Error: {code}: {message}\nsource_path: {source_path}"


def _title_from_markdown(content: str, source: Path) -> str:
    match = re.search(r"^\s*#\s+(.+?)\s*$", content, flags=re.MULTILINE)
    if match:
        return _plain_markdown(match.group(1)) or source.stem
    return source.stem.replace("_", " ").replace("-", " ").strip() or "Document"


def _plain_markdown(value: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    return text.strip()


_INLINE_TOKEN_RE = re.compile(r"(\*\*.+?\*\*|__.+?__|`.+?`|\*[^*]+?\*|_[^_]+?_)")


def _set_run_text(run: Any, value: str) -> None:
    run.add_text(_plain_markdown(value))


def _add_inline(paragraph: Any, value: str) -> None:
    """Render a conservative Markdown inline subset without external conversion tools."""
    for token in _INLINE_TOKEN_RE.split(value):
        if not token:
            continue
        run = paragraph.add_run()
        if token.startswith("**") and token.endswith("**"):
            run.bold = True
            _set_run_text(run, token[2:-2])
        elif token.startswith("__") and token.endswith("__"):
            run.bold = True
            _set_run_text(run, token[2:-2])
        elif token.startswith("`") and token.endswith("`"):
            run.font.name = "Menlo"
            _set_run_text(run, token[1:-1])
        elif token.startswith("*") and token.endswith("*"):
            run.italic = True
            _set_run_text(run, token[1:-1])
        elif token.startswith("_") and token.endswith("_"):
            run.italic = True
            _set_run_text(run, token[1:-1])
        else:
            _set_run_text(run, token)


def _set_style_fonts(style: Any, *, western: str, east_asian: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    style.font.name = western
    r_pr = style.element.get_or_add_rPr()
    r_fonts = r_pr.rFonts
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    r_fonts.set(qn("w:eastAsia"), east_asian)


def _add_page_number(paragraph: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    fld_char_begin = OxmlElement("w:fldChar")
    fld_char_begin.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = "PAGE"
    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_begin)
    run._r.append(instr_text)
    run._r.append(fld_char_end)


def _shade_cell(cell: Any, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), fill)
    tc_pr.append(shading)


def _add_table(document: Any, rows: list[list[str]]) -> None:
    if not rows:
        return
    column_count = max(len(row) for row in rows)
    table = document.add_table(rows=0, cols=column_count)
    table.style = "Table Grid"
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for column_index in range(column_count):
            cell = cells[column_index]
            value = values[column_index] if column_index < len(values) else ""
            paragraph = cell.paragraphs[0]
            _add_inline(paragraph, value)
            if row_index == 0:
                _shade_cell(cell, "EDE8DF")
                for run in paragraph.runs:
                    run.bold = True
    document.add_paragraph()


def _add_image(document: Any, source: Path, image_ref: str, caption: str) -> bool:
    candidate = Path(image_ref.strip().strip('"\''))
    try:
        if candidate.is_absolute():
            return False
        source_directory = source.parent.resolve()
        resolved = (source_directory / candidate).resolve()
        resolved.relative_to(source_directory)
        if not resolved.is_file():
            return False
        document.add_picture(str(resolved))
        if caption:
            paragraph = document.add_paragraph(style="Caption")
            paragraph.alignment = 1
            paragraph.add_run(caption)
        return True
    except Exception:
        return False


def _research_toc(content: str, title: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for kind, value in _markdown_blocks(content):
        if not re.fullmatch(r"h[1-3]", kind):
            continue
        level = int(kind[1:])
        heading = _plain_markdown(value)
        if heading and not (level == 1 and heading == title):
            entries.append((level, heading))
    return entries[:36]


def _render_docx(content: str, source: Path, output: Path, title: str, template: str) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2.1)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    document.core_properties.title = title
    document.core_properties.subject = "Research report"
    document.core_properties.keywords = "research, report, investment"

    normal = document.styles["Normal"]
    normal.font.size = Pt(10.5)
    _set_style_fonts(normal, western="Aptos", east_asian="Microsoft YaHei")
    for level in range(1, 4):
        heading = document.styles[f"Heading {level}"]
        heading.font.color.rgb = RGBColor(41, 38, 27)
        heading.font.size = Pt({1: 16, 2: 13, 3: 11}[level])
        _set_style_fonts(heading, western="Aptos Display", east_asian="Microsoft YaHei")
    caption = document.styles["Caption"]
    caption.font.size = Pt(9)
    _set_style_fonts(caption, western="Aptos", east_asian="Microsoft YaHei")

    cover = document.add_heading(title, level=0)
    cover.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if template == "research_report":
        subtitle = document.add_paragraph("研究报告 · 多角色交叉质证与数据审校")
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subtitle.runs[0].font.color.rgb = RGBColor(99, 95, 83)
        header = section.header.paragraphs[0]
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        header.add_run("TPARUYI · ASSET RESEARCH")
        document.add_page_break()
        document.add_heading("报告目录", level=1)
        for level, heading in _research_toc(content, title):
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.35 + (level - 1) * 0.42)
            run = paragraph.add_run(heading)
            run.bold = level <= 2
        document.add_page_break()
    else:
        document.add_page_break()

    for kind, value in _markdown_blocks(content):
        if re.fullmatch(r"h[1-6]", kind):
            level = int(kind[1:])
            heading_text = _plain_markdown(value)
            if level == 1 and heading_text == title:
                continue
            document.add_heading(heading_text, level=min(3, max(1, level - 1)))
        elif kind == "image":
            caption, image_ref = value
            _add_image(document, source, image_ref, caption)
        elif kind == "paragraph":
            paragraph = document.add_paragraph()
            _add_inline(paragraph, value)
        elif kind == "bullet":
            paragraph = document.add_paragraph(style="List Bullet")
            _add_inline(paragraph, value)
        elif kind == "numbered":
            _number, item = value
            paragraph = document.add_paragraph(style="List Number")
            _add_inline(paragraph, item)
        elif kind == "blockquote":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.7)
            paragraph.paragraph_format.right_indent = Cm(0.4)
            _add_inline(paragraph, value)
            for run in paragraph.runs:
                run.italic = True
        elif kind == "hr":
            document.add_paragraph("—" * 30).alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif kind == "code":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.5)
            run = paragraph.add_run(value)
            run.font.name = "Menlo"
            run.font.size = Pt(8.5)
        elif kind == "mermaid":
            paragraph = document.add_paragraph()
            paragraph.add_run("图示源（请在 HTML/PDF 版本查看渲染图）：\n").bold = True
            run = paragraph.add_run(value)
            run.font.name = "Menlo"
            run.font.size = Pt(8.5)
        elif kind == "table":
            _add_table(document, value)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("TpaRuyi · ")
    _add_page_number(footer)
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output))


def _validate_docx(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "DOCX file was not created"
    size = path.stat().st_size
    if size < 4_000:
        return False, f"DOCX file is too small ({size} bytes)"
    try:
        from docx import Document

        document = Document(str(path))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
    except Exception as exc:
        return False, f"DOCX cannot be opened: {exc}"
    if not text:
        return False, "DOCX contains no readable text"
    return True, ""

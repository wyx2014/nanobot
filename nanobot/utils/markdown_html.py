"""Generate safe, styled HTML companions for user-facing Markdown artifacts."""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

_GENERATED_MARKER = "<!-- Generated from Markdown by TPACowork -->"
_GENERATED_MARKERS = {
    _GENERATED_MARKER,
    "<!-- Generated from Markdown by TpaRuyi -->",
}
HTML_TEMPLATE_METADATA_KEY = "_html_template"
_HTML_TEMPLATES = {"simple", "research_report"}
_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_CONTROL_FILES = {
    "agents.md", "skill.md", "soul.md", "user.md", "memory.md", "readme.md",
}
_CONTROL_DIRS = {".git", ".nanobot", "node_modules", "skills", "memory"}


def should_generate_html_companion(source: Path) -> bool:
    if source.suffix.lower() not in _MARKDOWN_SUFFIXES:
        return False
    if source.name.lower() in _CONTROL_FILES:
        return False
    return not any(part.lower() in _CONTROL_DIRS for part in source.parts)


def _title(markdown: str, source: Path) -> str:
    match = re.search(r"^\s*#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    if match:
        return re.sub(r"[`*_]", "", match.group(1)).strip()
    return source.stem.replace("_", " ").replace("-", " ").strip() or "Document"


def _desktop_html(
    markdown: str,
    title: str,
    source: Path,
    template: str,
) -> str | None:
    url = os.environ.get("NANOBOT_HTML_RENDER_URL")
    token = os.environ.get("NANOBOT_HTML_RENDER_TOKEN")
    if not url or not token or not url.startswith("http://127.0.0.1:"):
        return None
    try:
        request = Request(
            url,
            data=json.dumps({
                "markdown": markdown,
                "title": title,
                "source_path": str(source),
                "template": template,
            }).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310 - authenticated loopback URL
            payload = json.load(response)
        rendered = payload.get("html")
        return rendered if isinstance(rendered, str) and "<html" in rendered.lower() else None
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _fallback_html(markdown: str, title: str) -> str:
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table").render(markdown)
    safe_title = html.escape(title, quote=True)
    return f"""{_GENERATED_MARKER}
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
*{{box-sizing:border-box}}html{{background:#f5f3ee}}body{{max-width:980px;margin:0 auto;padding:48px 56px 80px;color:#191814;background:#fff;font-family:"Newsreader Variable","Newsreader","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:17px;line-height:1.78;overflow-wrap:anywhere}}h1,h2,h3,h4{{line-height:1.3;color:#191814}}h1{{font-size:36px;margin:0 0 28px}}h2{{font-size:28px;margin:42px 0 16px;border-bottom:1px solid #dedbd3;padding-bottom:8px}}h3{{font-size:22px;margin:32px 0 12px}}p{{margin:0 0 16px}}a{{color:#b85f3f;text-decoration:none}}a:hover{{text-decoration:underline}}blockquote{{margin:24px 0;padding:14px 20px;border-left:4px solid #d97757;background:#fbf8f1;color:#3d3929}}table{{width:100%;margin:24px 0 32px;border-collapse:separate;border-spacing:0;border:1px solid #dedbd3;border-radius:10px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;font-size:14px;line-height:1.55}}th,td{{padding:11px 13px;text-align:left;vertical-align:top;border-right:1px solid #eeeae1;border-bottom:1px solid #eeeae1}}th{{background:#f4f1e8}}tr:last-child td{{border-bottom:0}}th:last-child,td:last-child{{border-right:0}}code{{padding:2px 5px;border-radius:4px;background:#f0eee8;color:#a65439}}pre{{padding:18px;border-radius:10px;background:#24231f;color:#f7f4ed;white-space:pre-wrap}}pre code{{padding:0;background:transparent;color:inherit}}img{{max-width:100%;height:auto}}hr{{margin:32px 0;border:0;border-top:1px solid #dedbd3}}@media(max-width:700px){{body{{padding:28px 22px 56px;font-size:16px}}h1{{font-size:30px}}h2{{font-size:24px}}}}
</style>
</head>
<body>{body}</body>
</html>"""


def _active_html_template() -> str:
    """Resolve the explicit per-node template without guessing from content."""
    try:
        from nanobot.agent.tools.context import current_request_context

        context = current_request_context()
        template = (
            context.metadata.get(HTML_TEMPLATE_METADATA_KEY)
            if context is not None
            else None
        )
    except (AttributeError, ImportError):
        template = None
    return template if template in _HTML_TEMPLATES else "simple"


def write_html_companion(
    source: Path,
    markdown: str,
    *,
    template: str | None = None,
) -> Path | None:
    """Write `<source-stem>.html`, preserving unrelated hand-authored HTML."""
    if not should_generate_html_companion(source) or not markdown.strip():
        return None
    output = source.with_suffix(".html")
    if output.exists():
        try:
            prefix = output.read_text(encoding="utf-8")[:256]
            if not any(marker in prefix for marker in _GENERATED_MARKERS):
                return None
        except OSError:
            return None
    title = _title(markdown, source)
    resolved_template = template if template in _HTML_TEMPLATES else _active_html_template()
    rendered = (
        _desktop_html(markdown, title, source, resolved_template)
        or _fallback_html(markdown, title)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return output


def markdown_write_result(text: str, companions: list[Path]) -> str | dict[str, Any]:
    unique = list(dict.fromkeys(companions))
    if not unique:
        return text
    return {
        "text": text + "\n" + "\n".join(f"html_path: {path}" for path in unique),
        "files": [
            {
                "path": str(path),
                "name": path.name,
                "mime_type": "text/html",
                "size": path.stat().st_size,
            }
            for path in unique
        ],
    }

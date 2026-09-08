"""Corporate PowerPoint artifact generation tool."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig, _FsTool
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanobot.agent.tools.web import _stream_with_safe_redirects

_SKILL_DIR = BUILTIN_SKILLS_DIR / "corporate-ppt"
_SCRIPTS_DIR = _SKILL_DIR / "scripts"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_ASSET_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_MAX_ASSET_BYTES = 25 * 1024 * 1024
_MAX_SOURCE_PAGE_BYTES = 2 * 1024 * 1024
_ASSET_USER_AGENT = (
    "TPCowork/0.0.1 (corporate presentation asset importer; "
    "https://github.com/HKUDS/nanobot) python-httpx"
)


@tool_parameters(
    tool_parameters_schema(
        source_path=StringSchema(
            "Path to a CorporateDeck YAML or JSON specification.",
            min_length=1,
        ),
        output_path=StringSchema(
            "Optional PPTX output path. Defaults to source_path with .pptx.",
            nullable=True,
        ),
        preview_path=StringSchema(
            "Optional PDF preview output path. Uses local LibreOffice or macOS Quick Look; "
            "PPTX creation remains successful if preview rendering is unavailable.",
            nullable=True,
        ),
        force=BooleanSchema(
            description="Replace existing PPTX and preview files. Defaults to false.",
            default=False,
            nullable=True,
        ),
        required=["source_path"],
    )
)
class CreatePresentationTool(_FsTool):
    """Create and validate an editable company-branded PowerPoint artifact."""

    _scopes = {"core", "subagent"}
    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @property
    def name(self) -> str:
        return "create_presentation"

    @property
    def description(self) -> str:
        return (
            "Create and validate an editable corporate PPTX from a CorporateDeck YAML or JSON "
            "specification using the bundled company template. Returns the PPTX as a structured "
            "artifact and can optionally render a local PDF preview. "
            "Use the corporate-ppt skill for the format; do not install office-conversion tools "
            "during a user turn."
        )

    async def execute(
        self,
        source_path: str | None = None,
        output_path: str | None = None,
        preview_path: str | None = None,
        force: bool | None = False,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        from nanobot.agent.tools.context import current_request_context
        context = current_request_context()
        if context and isinstance(context.metadata.get("presentation"), dict):
            return "Error: this turn has a selected presentation; use export_presentation with its document_id"
        if not source_path:
            return self._error("render_failed", "source_path is required", "")

        try:
            source = self._resolve_read(source_path)
            output = self._resolve_write(output_path or str(Path(source_path).with_suffix(".pptx")))
            preview = self._resolve_write(preview_path) if preview_path else None
        except Exception as exc:
            return self._error("permission_denied", str(exc), source_path)

        if source.suffix.lower() not in {".yaml", ".yml", ".json"}:
            return self._error(
                "render_failed",
                "source_path must be a CorporateDeck YAML or JSON file",
                str(source),
            )
        if output.suffix.lower() != ".pptx":
            return self._error("render_failed", "output_path must end in .pptx", str(source))
        if preview is not None and preview.suffix.lower() != ".pdf":
            return self._error("render_failed", "preview_path must end in .pdf", str(source))

        try:
            generation = await asyncio.to_thread(
                _run_skill_script,
                "generate_deck.py",
                [
                    str(source),
                    "--output",
                    str(output),
                    *(["--force"] if force else []),
                ],
            )
            validation = await asyncio.to_thread(
                _run_skill_script,
                "validate_deck.py",
                [str(output), "--spec", str(source)],
            )
        except PresentationScriptError as exc:
            return self._error(exc.code, exc.message, str(source))

        if not validation.get("ok"):
            errors = "; ".join(str(item) for item in validation.get("errors", []))
            return self._error("validation_failed", errors or "unknown validation error", str(source))

        files = [_artifact(output, _PPTX_MIME)]
        messages = [
            "PPTX created successfully",
            f"source_path: {source}",
            f"pptx_path: {output}",
            f"slide_count: {validation.get('slides')}",
        ]
        warnings = [
            *[str(item) for item in generation.get("warnings", [])],
            *[str(item) for item in validation.get("warnings", [])],
        ]

        if preview is not None:
            try:
                await asyncio.to_thread(
                    _run_skill_script,
                    "render_preview.py",
                    [
                        str(output),
                        "--output",
                        str(preview),
                        *(["--force"] if force else []),
                    ],
                )
                files.append(_artifact(preview, "application/pdf"))
                messages.append(f"preview_path: {preview}")
            except PresentationScriptError as exc:
                warnings.append(f"PDF preview unavailable: {exc.message}")

        if warnings:
            messages.append("warnings:")
            messages.extend(f"- {warning}" for warning in dict.fromkeys(warnings))
        return {"text": "\n".join(messages), "files": files}

    @staticmethod
    def _error(code: str, message: str, source_path: str) -> str:
        return f"Error: {code}: {message}\nsource_path: {source_path}"


@tool_parameters(
    tool_parameters_schema(
        source_path=StringSchema(
            "Optional local image path. Generated-image artifacts in the nanobot media directory are allowed.",
            nullable=True,
        ),
        source_url=StringSchema(
            "Optional direct public HTTP(S) image URL. Pair it with source_page_url when known.",
            nullable=True,
        ),
        source_page_url=StringSchema(
            "Optional credible public page that owns or describes the image. When source_url is "
            "omitted, the importer resolves the page's Open Graph or Twitter image metadata. "
            "When source_url is supplied, this page is used for attribution and as the HTTP Referer.",
            nullable=True,
        ),
        output_path=StringSchema(
            "Destination .png, .jpg or .jpeg path below the active project, normally media/<name>.",
            min_length=1,
        ),
        force=BooleanSchema(
            description="Replace an existing destination image. Defaults to false.",
            default=False,
            nullable=True,
        ),
        required=["output_path"],
    )
)
class ImportPresentationAssetTool(_FsTool):
    """Normalize a local or public image into a presentation project."""

    _scopes = {"core", "subagent"}
    config_key = "file"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._web_enabled = True
        self._proxy: str | None = None
        self._user_agent = _ASSET_USER_AGENT

    @classmethod
    def create(cls, ctx: Any):
        tool = super().create(ctx)
        tool._web_enabled = bool(ctx.config.web.enable)
        tool._proxy = ctx.config.web.proxy
        if ctx.config.web.user_agent:
            tool._user_agent = f"{ctx.config.web.user_agent} {_ASSET_USER_AGENT}"
        return tool

    @property
    def name(self) -> str:
        return "import_presentation_asset"

    @property
    def description(self) -> str:
        return (
            "Import a user-provided, generated, or verified public image into a presentation "
            "project's media directory. For public assets, prefer a verified source_page_url; "
            "the tool can resolve page image metadata, or source_url may be supplied with the "
            "owning page. Do not guess direct image URLs or use web_fetch for image binaries. "
            "The tool normalizes images to PNG/JPEG, enforces workspace and network safety, and "
            "returns the local path and attribution source for CorporateDeck."
        )

    async def execute(
        self,
        source_path: str | None = None,
        source_url: str | None = None,
        source_page_url: str | None = None,
        output_path: str | None = None,
        force: bool | None = False,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        if source_path and (source_url or source_page_url):
            return "Error: source_path cannot be combined with source_url or source_page_url"
        if not source_path and not (source_url or source_page_url):
            return "Error: provide source_path, source_url, or source_page_url"
        if not output_path:
            return "Error: output_path is required"
        try:
            output = self._resolve_write(output_path)
        except Exception as exc:
            return f"Error: permission_denied: {exc}"
        if output.suffix.lower() not in _ASSET_EXTENSIONS:
            return "Error: output_path must end in .png, .jpg or .jpeg"
        if output.exists() and not force:
            return f"Error: output already exists; pass force=true to replace it: {output}"

        try:
            if source_path:
                source = self._resolve_read(source_path)
                if not source.is_file():
                    return f"Error: source image not found: {source_path}"
                if source.stat().st_size > _MAX_ASSET_BYTES:
                    return "Error: source image exceeds 25 MB"
                raw = await asyncio.to_thread(source.read_bytes)
                provenance = str(source)
            else:
                if not self._web_enabled:
                    return "Error: public asset import requires enabled web tools"
                page_url = str(source_page_url).strip() if source_page_url else None
                image_url = str(source_url).strip() if source_url else None
                if page_url:
                    await self._validate_public_url(page_url, "source page")
                if image_url is None:
                    image_url = await self._resolve_page_image(str(page_url))
                raw = await self._download(image_url, referer=page_url)
                provenance = page_url or image_url
            encoded, mime_type = await asyncio.to_thread(_normalize_image, raw, output.suffix)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(encoded)
        except (
            OSError,
            RuntimeError,
            UnidentifiedImageError,
            ValueError,
            httpx.HTTPError,
            Image.DecompressionBombError,
        ) as exc:
            return f"Error: asset_import_failed: {_describe_asset_error(exc)}"

        retrieval = f"\nresolved_image_url: {image_url}" if not source_path else ""
        return {
            "text": (
                f"Presentation asset imported\npath: {output}\nsource: {provenance}"
                f"{retrieval}\n"
                "Add the source page or internal document label to the slide's source field."
            ),
            "files": [_artifact(output, mime_type)],
        }

    async def _validate_public_url(self, url: str, label: str) -> None:
        from nanobot.agent.tools.web import _validate_url_safe

        is_valid, error = _validate_url_safe(url)
        if not is_valid:
            raise ValueError(f"{label} blocked: {error}")

    async def _resolve_page_image(self, page_url: str) -> str:
        headers = {
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "User-Agent": self._user_agent,
        }
        async with httpx.AsyncClient(proxy=self._proxy, timeout=30.0) as client:
            response, stream, error = await _stream_with_safe_redirects(
                client,
                page_url,
                headers=headers,
            )
            if error:
                raise ValueError(error)
            if response is None:
                raise ValueError("source page request failed")
            try:
                _raise_for_asset_status(response, "source page")
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("image/"):
                    return str(response.url)
                if content_type and not (
                    content_type.startswith("text/html")
                    or content_type.startswith("application/xhtml+xml")
                ):
                    raise ValueError(
                        f"source page returned unsupported content type: {content_type}"
                    )
                raw = await _read_limited_response(
                    response,
                    limit=_MAX_SOURCE_PAGE_BYTES,
                    too_large_message="source page exceeds 2 MB",
                )
            finally:
                if stream is not None:
                    await stream.__aexit__(None, None, None)

        parser = _PageImageMetadataParser()
        parser.feed(raw.decode(response.encoding or "utf-8", errors="replace"))
        candidate = parser.best_candidate
        if not candidate:
            raise ValueError(
                "source page has no Open Graph or Twitter image metadata; "
                "supply a verified direct source_url with this source_page_url"
            )
        resolved = urljoin(str(response.url), candidate)
        await self._validate_public_url(resolved, "resolved image")
        return resolved

    async def _download(self, url: str, *, referer: str | None = None) -> bytes:
        headers = {
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.1",
            "User-Agent": self._user_agent,
        }
        if referer:
            headers["Referer"] = referer
        async with httpx.AsyncClient(proxy=self._proxy, timeout=30.0) as client:
            response, stream, error = await _stream_with_safe_redirects(
                client,
                url.strip(),
                headers=headers,
            )
            if error:
                raise ValueError(error)
            if response is None:
                raise ValueError("image download failed")
            try:
                _raise_for_asset_status(response, "image URL")
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(f"URL did not return an image: {content_type}")
                return await _read_limited_response(
                    response,
                    limit=_MAX_ASSET_BYTES,
                    too_large_message="downloaded image exceeds 25 MB",
                )
            finally:
                if stream is not None:
                    await stream.__aexit__(None, None, None)


class _PageImageMetadataParser(HTMLParser):
    """Collect high-confidence page preview images without executing page scripts."""

    _PRIORITY = {
        "og:image:secure_url": 0,
        "og:image:url": 1,
        "og:image": 2,
        "twitter:image": 3,
        "twitter:image:src": 4,
        "image_src": 5,
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._candidates: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if name in self._PRIORITY and content:
                self._candidates.append((self._PRIORITY[name], content))
        elif tag.lower() == "link":
            rel = {part.lower() for part in attributes.get("rel", "").split()}
            href = attributes.get("href", "").strip()
            if "image_src" in rel and href:
                self._candidates.append((self._PRIORITY["image_src"], href))

    @property
    def best_candidate(self) -> str | None:
        if not self._candidates:
            return None
        return min(self._candidates, key=lambda item: item[0])[1]


async def _read_limited_response(
    response: httpx.Response,
    *,
    limit: int,
    too_large_message: str,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError(too_large_message)
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_for_asset_status(response: httpx.Response, label: str) -> None:
    if response.is_error:
        raise ValueError(
            f"{label} returned HTTP {response.status_code} for {response.url}; "
            "use a different verified source instead of guessing another URL"
        )


def _describe_asset_error(exc: Exception) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    lowered = detail.lower()
    if isinstance(exc, httpx.TimeoutException):
        return f"network_timeout: {detail}"
    if isinstance(exc, httpx.ConnectError) and any(
        marker in lowered
        for marker in ("certificate", "cert_verify", "ssl", "tls")
    ):
        return (
            "tls_error: TLS certificate verification failed; use another credible public source "
            "or configure the approved proxy/CA. TLS verification was not disabled. "
            f"Details: {detail}"
        )
    if isinstance(exc, UnidentifiedImageError):
        return f"unsupported_image: downloaded bytes are not a supported raster image ({detail})"
    if "did not return an image" in lowered:
        return f"non_image_response: {detail}"
    if "http 403" in lowered:
        return f"http_forbidden: {detail}"
    if "http 404" in lowered:
        return f"http_not_found: {detail}"
    return detail


class PresentationScriptError(RuntimeError):
    """A deterministic skill script failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_image(raw: bytes, output_suffix: str) -> tuple[bytes, str]:
    with Image.open(BytesIO(raw)) as opened:
        opened.load()
        image = ImageOps.exif_transpose(opened)
        output = BytesIO()
        if output_suffix.lower() == ".png":
            if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                image = image.convert("RGBA")
            image.save(output, format="PNG", optimize=True)
            mime_type = "image/png"
        else:
            if image.mode != "RGB":
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            image.save(output, format="JPEG", quality=92, optimize=True)
            mime_type = "image/jpeg"
    return output.getvalue(), mime_type


def _run_skill_script(script_name: str, arguments: list[str]) -> dict[str, Any]:
    script = (_SCRIPTS_DIR / script_name).resolve()
    if not script.is_file():
        raise PresentationScriptError("dependency_missing", f"bundled script not found: {script}")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PresentationScriptError("render_failed", f"{script_name} failed: {exc}") from exc

    stream = completed.stdout if completed.returncode == 0 else (completed.stdout or completed.stderr)
    payload = _last_json_object(stream)
    if completed.returncode != 0:
        message = str(payload.get("error") or "").strip()
        if not message:
            errors = payload.get("errors")
            if isinstance(errors, list):
                message = "; ".join(str(item) for item in errors)
        if not message:
            detail = "\n".join(
                value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
            )
            message = detail or f"{script_name} exited with {completed.returncode}"
        code = "validation_failed" if script_name == "validate_deck.py" else "render_failed"
        raise PresentationScriptError(code, message)
    if not payload:
        raise PresentationScriptError("render_failed", f"{script_name} returned no JSON result")
    return payload


def _last_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def _artifact(path: Path, mime_type: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "name": path.name,
        "mime_type": mime_type,
        "size": path.stat().st_size,
    }

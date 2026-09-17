"""Extract text and structured fields from images with a managed vision service."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.config.paths import get_media_dir
from nanobot.security.workspace_access import current_tool_workspace
from nanobot.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path

IMAGE_EXTRACT_API_URL_ENV = "NANOBOT_IMAGE_EXTRACT_API_URL"
IMAGE_EXTRACT_API_KEY_ENV = "NANOBOT_IMAGE_EXTRACT_API_KEY"
IMAGE_EXTRACT_MODEL_ENV = "NANOBOT_IMAGE_EXTRACT_MODEL"

_DEFAULT_PROMPT = "提取图中的全部内容"
_DEFAULT_MAX_TOKENS = 4096
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class ImageExtractionError(RuntimeError):
    """Raised when the managed image extraction request cannot complete."""


def _image_mime(path: Path, raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    suffix_mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if suffix_mime:
        return suffix_mime
    raise ImageExtractionError(
        "unsupported image format; expected png, jpg, jpeg, gif, webp, or bmp"
    )


def _response_text(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        preview = json.dumps(payload, ensure_ascii=False)[:1000]
        raise ImageExtractionError(f"unexpected service response: {preview}") from exc
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text = "\n".join(
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if text:
            return text
    raise ImageExtractionError("image extraction service returned an empty result")


class ImageExtractionClient:
    """Small standard-library client shared by the tool and bundled CLI."""

    def __init__(self, *, api_url: str, api_key: str, model: str, timeout: int = 120) -> None:
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        if not self.api_url or not self.api_key or not self.model:
            raise ImageExtractionError("image extraction service is not configured")

    @classmethod
    def from_environment(cls) -> ImageExtractionClient:
        return cls(
            api_url=os.environ.get(IMAGE_EXTRACT_API_URL_ENV, ""),
            api_key=os.environ.get(IMAGE_EXTRACT_API_KEY_ENV, ""),
            model=os.environ.get(IMAGE_EXTRACT_MODEL_ENV, ""),
        )

    def extract(self, image_path: str | Path, prompt: str, max_tokens: int) -> str:
        path = Path(image_path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ImageExtractionError(f"cannot read image: {exc}") from exc
        if not raw:
            raise ImageExtractionError("image file is empty")
        if len(raw) > _MAX_IMAGE_BYTES:
            raise ImageExtractionError("image exceeds the 25 MB limit")

        mime = _image_mime(path, raw)
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt.strip() or _DEFAULT_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                            },
                        },
                    ],
                }
            ],
            "stream": False,
            "max_tokens": max_tokens,
            "do_sample": True,
            "repetition_penalty": 1.0,
            "temperature": 0.01,
            "top_p": 0.001,
            "top_k": 1,
            "model": self.model,
        }
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except OSError:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise ImageExtractionError(f"service returned HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageExtractionError(f"cannot reach image extraction service: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageExtractionError("image extraction service returned invalid JSON") from exc
        return _response_text(result)


@tool_parameters(
    tool_parameters_schema(
        image_path=StringSchema(
            "Local image path. The file must be in the active workspace or nanobot media directory.",
            min_length=1,
        ),
        prompt=StringSchema(
            "What to extract, such as all text, a bank account number, or invoice fields.",
        ),
        max_tokens=IntegerSchema(
            description="Maximum output tokens, default 4096.",
            minimum=1,
            maximum=16384,
        ),
        required=["image_path"],
    )
)
class ImageExtractTool(Tool):
    """Extract text or requested structured fields from a local image."""

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return all(
            os.environ.get(name, "").strip()
            for name in (
                IMAGE_EXTRACT_API_URL_ENV,
                IMAGE_EXTRACT_API_KEY_ENV,
                IMAGE_EXTRACT_MODEL_ENV,
            )
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            workspace=ctx.workspace,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            client=ImageExtractionClient.from_environment(),
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        restrict_to_workspace: bool,
        client: ImageExtractionClient,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.restrict_to_workspace = restrict_to_workspace
        self.client = client

    @property
    def name(self) -> str:
        return "extract_image"

    @property
    def description(self) -> str:
        return (
            "Extract all visible text or requested fields from a local image with the managed "
            "multimodal model. Supports PNG, JPEG, GIF, WebP, and BMP."
        )

    @property
    def read_only(self) -> bool:
        return True

    def _resolve_image(self, value: str) -> Path:
        access = current_tool_workspace(
            self.workspace,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        workspace = access.project_path or self.workspace
        try:
            resolved = resolve_allowed_path(
                value,
                workspace=workspace,
                allowed_root=access.allowed_root,
                extra_allowed_roots=[get_media_dir()] if access.allowed_root is not None else None,
                strict=True,
            )
        except (WorkspaceBoundaryError, OSError) as exc:
            raise ImageExtractionError(str(exc)) from exc
        if not resolved.is_file():
            raise ImageExtractionError(f"image is not a file: {value}")
        return resolved

    async def execute(
        self,
        image_path: str,
        prompt: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            path = self._resolve_image(image_path)
            return await asyncio.to_thread(
                self.client.extract,
                path,
                prompt or _DEFAULT_PROMPT,
                max_tokens or _DEFAULT_MAX_TOKENS,
            )
        except ImageExtractionError as exc:
            return f"Error: {exc}"

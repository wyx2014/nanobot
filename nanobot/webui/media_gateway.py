"""Media gateway services shared by WebUI HTTP routes and WebSocket frames."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.config.paths import get_media_dir
from nanobot.webui.media_cache import (
    DEFAULT_MEDIA_CACHE_CLEANUP_INTERVAL_S,
    DEFAULT_MEDIA_CACHE_MAX_BYTES,
    DEFAULT_MEDIA_CACHE_STARTUP_DELAY_S,
    DEFAULT_MEDIA_CACHE_TTL_S,
    cleanup_media_cache,
    media_cache_status,
)
from nanobot.webui.media_api import (
    attach_signed_media_urls,
    serve_signed_media,
    sign_media_path,
    sign_or_stage_media_path,
    signed_media_attachments,
)
from nanobot.webui.transcript import rewrite_local_markdown_images


class WebUIMediaGateway:
    """Own media URL signing and WebUI markdown/media augmentation."""

    def __init__(
        self,
        *,
        workspace_path: Path,
        logger: Any,
        media_dir: Callable[[str | None], Path] | None = None,
        secret: bytes | None = None,
        cache_max_bytes: int = DEFAULT_MEDIA_CACHE_MAX_BYTES,
        cache_ttl_s: int = DEFAULT_MEDIA_CACHE_TTL_S,
        cache_cleanup_interval_s: int = DEFAULT_MEDIA_CACHE_CLEANUP_INTERVAL_S,
        cache_startup_delay_s: int = DEFAULT_MEDIA_CACHE_STARTUP_DELAY_S,
    ) -> None:
        self.workspace_path = workspace_path
        self.logger = logger
        self._media_dir = media_dir or (lambda channel=None: get_media_dir(channel))
        self.secret = secret or secrets.token_bytes(32)
        self.cache_max_bytes = cache_max_bytes
        self.cache_ttl_s = cache_ttl_s
        self.cache_cleanup_interval_s = cache_cleanup_interval_s
        self.cache_startup_delay_s = cache_startup_delay_s
        self._cache_maintenance_task: asyncio.Task[None] | None = None
        self._last_cache_cleanup: dict[str, Any] | None = None
        self._cache_cleanup_lock = threading.Lock()

    def cache_status(self) -> dict[str, Any]:
        with self._cache_cleanup_lock:
            payload = media_cache_status(
                self._media_dir("websocket"),
                max_bytes=self.cache_max_bytes,
                ttl_s=self.cache_ttl_s,
            )
        payload["cleanup_interval_seconds"] = self.cache_cleanup_interval_s
        payload["last_cleanup"] = self._last_cache_cleanup
        return payload

    def cleanup_cache(self, *, clear_all: bool = False) -> dict[str, Any]:
        with self._cache_cleanup_lock:
            report = cleanup_media_cache(
                self._media_dir("websocket"),
                max_bytes=self.cache_max_bytes,
                ttl_s=self.cache_ttl_s,
                clear_all=clear_all,
            )
        report["cleanup_interval_seconds"] = self.cache_cleanup_interval_s
        self._last_cache_cleanup = {
            "cleaned_at": report["cleaned_at"],
            "removed_count": report["removed_count"],
            "removed_bytes": report["removed_bytes"],
            "failed_count": report["failed_count"],
        }
        report["last_cleanup"] = self._last_cache_cleanup
        return report

    def start_cache_maintenance(self) -> None:
        """Start delayed cache cleanup without extending gateway startup."""

        if self._cache_maintenance_task is not None:
            return
        self._cache_maintenance_task = asyncio.create_task(
            self._cache_maintenance_loop(),
            name="webui-media-cache-maintenance",
        )

    async def stop_cache_maintenance(self) -> None:
        task = self._cache_maintenance_task
        self._cache_maintenance_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cache_maintenance_loop(self) -> None:
        try:
            if self.cache_startup_delay_s:
                await asyncio.sleep(self.cache_startup_delay_s)
            while True:
                report = await asyncio.to_thread(self.cleanup_cache)
                removed_count = int(report.get("removed_count") or 0)
                if removed_count:
                    self.logger.info(
                        "WebUI media cache cleanup removed {} files ({} bytes); cache now {} bytes",
                        removed_count,
                        int(report.get("removed_bytes") or 0),
                        int(report.get("cache_bytes") or 0),
                    )
                failed_count = int(report.get("failed_count") or 0)
                if failed_count:
                    self.logger.warning(
                        "WebUI media cache cleanup could not remove {} files",
                        failed_count,
                    )
                await asyncio.sleep(self.cache_cleanup_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("WebUI media cache maintenance failed")

    def serve_signed_media(
        self,
        sig: str,
        payload: str,
        *,
        request: WsRequest | None = None,
    ) -> Response:
        return serve_signed_media(
            sig,
            payload,
            secret=self.secret,
            request=request,
            media_dir=self._media_dir,
        )

    def sign_media_path(self, abs_path: Path) -> str | None:
        return sign_media_path(
            abs_path,
            secret=self.secret,
            media_dir=self._media_dir,
        )

    def sign_or_stage_media_path(self, path: Path) -> dict[str, Any] | None:
        return sign_or_stage_media_path(
            path,
            secret=self.secret,
            media_dir=self._media_dir,
            logger=self.logger,
        )

    def rewrite_local_markdown_images(
        self,
        text: str,
        *,
        workspace_path: Path | None = None,
    ) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=workspace_path or self.workspace_path,
            sign_path=self.sign_or_stage_media_path,
        )

    def augment_media_urls(self, payload: dict[str, Any]) -> None:
        attach_signed_media_urls(payload, sign_path=self.sign_media_path)

    def augment_transcript_media(self, paths: list[str]) -> list[dict[str, Any]]:
        return signed_media_attachments(
            paths,
            sign_path=self.sign_or_stage_media_path,
        )

    def augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        return self.augment_transcript_media(paths)

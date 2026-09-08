"""Daily, size-bounded file logging for the Electron desktop gateway."""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from loguru import logger

MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
RETENTION_DAYS = 365


class DesktopLogRotation:
    def __init__(self, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.max_bytes = max_bytes
        self._day: date | None = None

    def __call__(self, message, file: TextIO) -> bool:
        today = message.record["time"].date()
        file.seek(0, os.SEEK_END)
        size = file.tell()
        if self._day is None:
            # Recover the active day after a restart, including pre-upgrade logs.
            self._day = datetime.fromtimestamp(os.fstat(file.fileno()).st_mtime).date()
        rotate = size > 0 and (
            today != self._day or size + len(message.encode("utf-8")) > self.max_bytes
        )
        self._day = today
        return rotate


class DesktopLogRetention:
    def __init__(
        self,
        file_path: Path,
        *,
        max_bytes: int = MAX_ARCHIVE_BYTES,
        days: int = RETENTION_DAYS,
    ) -> None:
        self.file_path = file_path.absolute()
        self.max_bytes = max_bytes
        self.days = days
        self._archive_name = re.compile(
            re.escape(file_path.stem)
            + r"\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}(?:\.\d+)?"
            + re.escape(file_path.suffix)
            + r"(?:\.gz)?"
        )

    def __call__(self, paths: list[str]) -> None:
        cutoff = time.time() - self.days * 86400
        archives = []
        for value in paths:
            path = Path(value).absolute()
            if path.parent != self.file_path.parent or not self._archive_name.fullmatch(path.name):
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                archives.append((stat.st_mtime, path, stat.st_size))
            except OSError:
                continue

        retained_bytes = 0
        for modified, path, size in sorted(archives, reverse=True):
            if modified < cutoff or retained_bytes + size > self.max_bytes:
                try:
                    path.unlink()
                except OSError as exc:
                    # Loguru callbacks must not re-enter the logger (sink lock).
                    print(f"Could not remove desktop log archive {path}: {exc}", file=sys.stderr)
                    retained_bytes += size
            else:
                retained_bytes += size

    def cleanup(self) -> None:
        # Loguru normally runs retention only on rotation. Also clean on launch.
        try:
            self([str(path) for path in self.file_path.parent.iterdir()])
        except OSError as exc:
            print(f"Could not clean desktop log archives: {exc}", file=sys.stderr)


def add_desktop_file_logging(file_path: Path, log_format: str) -> int:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    retention = DesktopLogRetention(file_path)
    retention.cleanup()
    return logger.add(
        file_path,
        format=log_format,
        level="INFO",
        encoding="utf-8",
        newline="",
        colorize=False,
        rotation=DesktopLogRotation(),
        retention=retention,
        compression="gz",
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )

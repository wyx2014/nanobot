"""Allocate versioned deliverable paths without mixing scratch files with outputs."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_VERSIONED_STEM = re.compile(r"^(.*?)(\d{10})(?:v([2-9]\d*|1\d+))?$", re.IGNORECASE)
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _scratch_paths(workspace: Path) -> tuple[Path, Path]:
    scratch = (workspace / "tmp").resolve()
    if not scratch.is_relative_to(workspace):
        raise ValueError("workspace tmp directory must not point outside the project")
    if scratch != workspace / "tmp":
        raise ValueError("workspace tmp directory must not be a symlink")
    reservations = (scratch / ".output-reservations").resolve()
    if not reservations.is_relative_to(scratch):
        raise ValueError("output reservations must remain inside workspace tmp")
    return scratch, reservations


def output_stem(filename: str) -> tuple[str, str]:
    """Keep the business name and extension; replace an existing time/version suffix."""
    if (
        not filename or filename != filename.strip()
        or _INVALID_FILENAME.search(filename) or filename.endswith((".", " "))
        or filename in {".", ".."}
    ):
        raise ValueError("filename must be a single Windows-compatible filename")
    path = Path(filename)
    if not path.suffix or not path.stem or path.stem.startswith("."):
        raise ValueError("filename must include a business name and a file extension")
    extension = next((suffix for suffix in (".tar.gz", ".tar.bz2", ".tar.xz")
                      if filename.lower().endswith(suffix)), path.suffix)
    stem = filename[:-len(extension)]
    match = _VERSIONED_STEM.fullmatch(stem)
    if match and match[1]:
        try:
            datetime.strptime(match[2], "%Y%m%d%H")
        except ValueError:
            pass
        else:
            stem = match[1]
    return stem, extension


def prepare_output_paths(
    workspace: Path,
    directory: Path,
    filename: str,
    *,
    timezone: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Reserve an output name across concurrent turns; scratch/reservations stay in tmp.

    Do not create an empty final artifact: the generator writes the reserved
    path only after preparing its content. Reservations also prevent reusing a
    previously allocated version after a failed or interrupted generation.
    """
    stem, extension = output_stem(filename)
    local_time = now or datetime.now().astimezone()
    if timezone:
        try:
            local_time = local_time.astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown output timezone: {timezone}") from exc
    timestamp = local_time.strftime("%Y%m%d%H")
    workspace = workspace.expanduser().resolve()
    directory = directory.expanduser().resolve()
    scratch, reservations = _scratch_paths(workspace)
    relative_parts = directory.relative_to(workspace).parts if directory.is_relative_to(workspace) else ()
    if directory.is_relative_to(scratch) or any(part.casefold() == "tmp" for part in relative_parts):
        raise ValueError("final deliverables must be outside tmp")
    directory.mkdir(parents=True, exist_ok=True)
    reservations.mkdir(parents=True, exist_ok=True)
    existing = {entry.name.casefold() for entry in directory.iterdir()}
    prefix = f"{stem}{timestamp}"
    pattern = re.compile(re.escape(prefix.casefold()) + r"(?:v([2-9]\d*|1\d+))?"
                         + re.escape(extension.casefold()))
    version = max((int(match[1] or 1) for name in existing
                   if (match := pattern.fullmatch(name))), default=0) + 1
    while True:
        suffix = "" if version == 1 else f"v{version}"
        output = directory / f"{prefix}{suffix}{extension}"
        version += 1
        if output.name.casefold() in existing or output.exists():
            continue
        key = hashlib.sha256(str(output).casefold().encode("utf-8")).hexdigest()
        reservation = reservations / f"{key}.txt"
        try:
            with reservation.open("x", encoding="utf-8") as marker:
                marker.write(str(output))
        except FileExistsError:
            continue
        # Recheck after claiming the name in case another producer created it.
        if output.exists():
            continue
        work_dir = Path(tempfile.mkdtemp(prefix="output-", dir=scratch))
        return {
            "output_path": str(output),
            "filename": output.name,
            "tmp_dir": str(work_dir),
            "timestamp": timestamp,
        }


def prepared_output_path(workspace: Path, output: Path, *, timezone: str | None = None) -> Path:
    """Use a reserved name once, or allocate a new version for a document tool."""
    workspace = workspace.resolve()
    output = output.resolve()
    scratch, reservations = _scratch_paths(workspace)
    # Explicit scratch outputs are intermediate files, never delivered artifacts.
    if output.is_relative_to(scratch):
        return output
    key = hashlib.sha256(str(output).casefold().encode("utf-8")).hexdigest()
    marker = reservations / f"{key}.txt"
    if not output.exists() and marker.is_file() and marker.read_text(encoding="utf-8") == str(output):
        try:
            with marker.with_suffix(".claimed").open("x"):
                return output
        except FileExistsError:
            pass
    prepared = prepare_output_paths(workspace, output.parent, output.name, timezone=timezone)
    return prepared_output_path(workspace, Path(prepared["output_path"]), timezone=timezone)


def publish_output(staged: Path, output: Path) -> None:
    """Copy a validated file across volumes without overwriting an existing version."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        try:
            with staged.open("rb") as source:
                shutil.copyfileobj(source, destination)
            destination.flush()
        except BaseException:
            destination.close()
            output.unlink(missing_ok=True)
            raise

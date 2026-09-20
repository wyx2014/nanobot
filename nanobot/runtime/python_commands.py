"""Application-owned Python command aliases; never change system PATH or installations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from nanobot.config.paths import get_config_path

PYTHON_ENV_KEY = "NANOBOT_PYTHON_EXECUTABLE"


def python_command_dir(*, prepare: bool = True) -> Path:
    """Use one set of launchers with an interpreter selected in each process's environment."""
    directory = get_config_path().parent.expanduser().resolve() / "python-commands"
    if not prepare:
        return directory
    if directory.is_symlink():
        raise ValueError("Managed Python command directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    windows = os.name == "nt"
    for name in ("python", "python3", "pip", "pip3"):
        arguments = " -m pip" if name.startswith("pip") else ""
        if windows:
            filename = name + ".cmd"
            content = (
                '@echo off\r\n'
                f'"%{PYTHON_ENV_KEY}%"{arguments} %*\r\n'
                'exit /b %errorlevel%\r\n'
            )
        else:
            filename = name
            content = f'#!/bin/sh\nexec "${PYTHON_ENV_KEY}"{arguments} "$@"\n'
        target = directory / filename
        if target.is_symlink():
            raise ValueError("Managed Python commands must not be symlinks")
        if target.is_file() and target.read_bytes() == content.encode("utf-8"):
            if not windows and not os.access(target, os.X_OK):
                target.chmod(0o700)
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content.encode("utf-8"))
            if not windows:
                temporary.chmod(0o700)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return directory

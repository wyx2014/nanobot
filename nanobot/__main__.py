"""
Entry point for running nanobot as a module: python -m nanobot
"""

import os
import sys
import time

_desktop_entry_started_at = time.perf_counter()
_desktop_gateway = len(sys.argv) > 1 and sys.argv[1] == "desktop-gateway"

# Desktop only needs the local gateway. Mark that path before importing the
# Typer command module so it can skip prompt-toolkit and streaming renderer
# imports that belong exclusively to the interactive terminal UI.
if _desktop_gateway:
    os.environ.setdefault("NANOBOT_DESKTOP_GATEWAY", "1")
    print(
        f"[startup] python-entry pid={os.getpid()}",
        file=sys.stderr,
        flush=True,
    )

from nanobot.cli.commands import app

if _desktop_gateway:
    print(
        "[startup] cli-imports-complete "
        f"elapsed_ms={(time.perf_counter() - _desktop_entry_started_at) * 1000:.1f}",
        file=sys.stderr,
        flush=True,
    )

if __name__ == "__main__":
    app()

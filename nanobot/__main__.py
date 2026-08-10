"""
Entry point for running nanobot as a module: python -m nanobot
"""

import os
import sys

# Desktop only needs the local gateway. Mark that path before importing the
# Typer command module so it can skip prompt-toolkit and streaming renderer
# imports that belong exclusively to the interactive terminal UI.
if len(sys.argv) > 1 and sys.argv[1] == "desktop-gateway":
    os.environ.setdefault("NANOBOT_DESKTOP_GATEWAY", "1")

from nanobot.cli.commands import app

if __name__ == "__main__":
    app()

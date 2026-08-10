"""Desktop gateway startup should not import the interactive terminal stack."""

from __future__ import annotations

import os
import subprocess
import sys


def test_desktop_gateway_import_profile_skips_terminal_modules() -> None:
    env = os.environ.copy()
    env["NANOBOT_DESKTOP_GATEWAY"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import nanobot.cli.commands; "
                "print('prompt_toolkit' in sys.modules); "
                "print('nanobot.cli.stream' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.splitlines() == ["False", "False"]

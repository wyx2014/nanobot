from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketConfig
from nanobot.storage.state import open_state_store_with_recovery
from nanobot.webui import gateway_services


@pytest.mark.parametrize(
    ("runtime_surface", "expected_verify_integrity"),
    [("native", False), ("browser", True)],
)
def test_gateway_integrity_scan_policy_matches_runtime_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_surface: str,
    expected_verify_integrity: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    def tracked_open(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        return open_state_store_with_recovery(*args, **kwargs)

    monkeypatch.setattr(
        gateway_services,
        "open_state_store_with_recovery",
        tracked_open,
    )

    gateway_services.build_gateway_services(
        config=WebSocketConfig(),
        bus=MessageBus(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface=runtime_surface,
        runtime_capabilities_overrides=None,
    )

    assert calls[0]["verify_integrity"] is expected_verify_integrity

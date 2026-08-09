from __future__ import annotations

import re
import tomllib
from pathlib import Path


_OPTIONAL_DESKTOP_PACKAGES = {
    "boto3",
    "dingtalk-stream",
    "lark-oapi",
    "python-telegram-bot",
    "qq-botpy",
    "slack-sdk",
    "slackify-markdown",
}


def _package_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()


def test_desktop_profile_excludes_optional_channels_and_bedrock() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    base_names = {_package_name(item) for item in project["dependencies"]}
    desktop_names = {
        _package_name(item)
        for item in project["optional-dependencies"]["desktop"]
    }

    assert base_names.isdisjoint(_OPTIONAL_DESKTOP_PACKAGES)
    assert desktop_names.isdisjoint(_OPTIONAL_DESKTOP_PACKAGES)
    assert "aiohttp" in desktop_names


def test_removed_desktop_packages_remain_available_as_explicit_extras() -> None:
    project_root = Path(__file__).resolve().parents[1]
    extras = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["optional-dependencies"]
    optional_names = {
        _package_name(item)
        for extra in ("channels", "bedrock")
        for item in extras[extra]
    }

    assert _OPTIONAL_DESKTOP_PACKAGES <= optional_names

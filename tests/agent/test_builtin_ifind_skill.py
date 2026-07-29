"""Regression tests for the bundled iFinD financial-data Skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def test_removed_legacy_skills_are_not_builtin_discoverable(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)
    names = {
        entry["name"]
        for entry in loader.list_skills(filter_unavailable=False)
    }

    assert names.isdisjoint({
        "tmux",
        "update-setup",
        "summarize",
        "image-generation",
        "github",
    })


def test_builtin_ifind_skill_is_registry_discoverable(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)

    entries = {
        entry["name"]: entry
        for entry in loader.list_skills(filter_unavailable=False)
    }
    entry = entries["ifind-finance-data"]
    context = loader.load_skills_for_context(["ifind-finance-data"])
    requirements = loader.get_skill_requirements("ifind-finance-data")

    assert entry["source"] == "builtin"
    assert Path(entry["path"]).parent == BUILTIN_SKILLS_DIR / "ifind-finance-data"
    assert requirements["missing_files"] == []
    assert "Skill directory:" in context
    assert "禁止通过 `find_files`" in context
    assert "港股或美股：`global_stock`" in context


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_builtin_ifind_config_check_is_fast_and_does_not_echo_token(
    tmp_path: Path,
) -> None:
    config = tmp_path / "ifind-config.json"
    token = "test-secret-token"
    config.write_text(json.dumps({"auth_token": token}), encoding="utf-8")
    script = BUILTIN_SKILLS_DIR / "ifind-finance-data" / "scripts" / "call-node.js"
    env = {
        **os.environ,
        "IFIND_MCP_CONFIG": str(config),
    }
    env.pop("IFIND_AUTH_TOKEN", None)

    completed = subprocess.run(
        [shutil.which("node") or "node", str(script), "--check"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 0
    assert '"configured":true' in completed.stdout.replace(" ", "")
    assert token not in completed.stdout
    assert token not in completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_builtin_ifind_missing_config_fails_without_network_search() -> None:
    script = BUILTIN_SKILLS_DIR / "ifind-finance-data" / "scripts" / "call-node.js"
    env = dict(os.environ)
    env.pop("IFIND_AUTH_TOKEN", None)
    env.pop("IFIND_MCP_CONFIG", None)

    completed = subprocess.run(
        [shutil.which("node") or "node", str(script), "--check"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert "iFinD is not configured" in completed.stderr
    assert "Do not search the filesystem" in completed.stderr

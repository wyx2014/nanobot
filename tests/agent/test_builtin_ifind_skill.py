"""Regression tests for removed bundled skills staying out of the builtin set."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader

# Skills that were bundled in the past but are no longer shipped as builtin
# skills. They must never reappear through the builtin directory.
_REMOVED_BUILTIN_SKILLS = {
    "tmux",
    "update-setup",
    "summarize",
    "image-generation",
    "github",
    "ifind-finance-data",
}


def test_removed_legacy_skills_are_not_builtin_discoverable(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)
    names = {
        entry["name"]
        for entry in loader.list_skills(filter_unavailable=False)
    }

    assert names.isdisjoint(_REMOVED_BUILTIN_SKILLS)

from __future__ import annotations

from pathlib import Path

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from nanobot.agent.tools.image_extract import (
    IMAGE_EXTRACT_API_KEY_ENV,
    IMAGE_EXTRACT_API_URL_ENV,
    IMAGE_EXTRACT_MODEL_ENV,
)


def test_image_extract_skill_is_bundled_and_available_with_managed_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(IMAGE_EXTRACT_API_URL_ENV, "http://vision.example")
    monkeypatch.setenv(IMAGE_EXTRACT_API_KEY_ENV, "key")
    monkeypatch.setenv(IMAGE_EXTRACT_MODEL_ENV, "qwen-vl")
    loader = SkillsLoader(tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)

    entries = {entry["name"]: entry for entry in loader.list_skills()}

    assert entries["image-extract"]["source"] == "builtin"
    assert loader.get_skill_availability("image-extract") == (True, "")
    assert "extract_image" in (loader.load_skill("image-extract") or "")

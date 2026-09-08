"""Versioned skill directories keep the same identity across settings actions."""

from pathlib import Path

import pytest

from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.webui.skills_api import WebUISkillsError, skills_action, skills_payload


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / ".nanobot" / "config.json"
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    save_config(config, config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setattr("nanobot.agent.skills.BUILTIN_SKILLS_DIR", builtin)
    return tmp_path


def test_copied_versioned_skill_can_be_read_and_toggled(workspace: Path) -> None:
    name = "libai-1.0.4"
    skill_file = workspace / "skills" / name / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    content = "---\nname: libai-skill\ndescription: 润色专家\n---\n\n# 李白\n"
    skill_file.write_text(content, encoding="utf-8")

    assert skills_payload()["skills"][0]["name"] == name
    detail = skills_action("detail", {"name": [name]})["skills"][0]
    assert detail["name"] == name
    assert detail["path"] == str(skill_file)
    assert detail["metadata"]["name"] == "libai-skill"
    assert detail["content"] == content
    assert detail["available"] is True

    disabled = skills_action("disable", {"name": [name]})
    assert disabled["skills"][0]["enabled"] is False
    assert disabled["disabled"] == [name]
    enabled = skills_action("enable", {"name": [name]})
    assert enabled["skills"][0]["enabled"] is True
    assert enabled["disabled"] == []
    assert skill_file.read_text(encoding="utf-8") == content


def test_save_and_delete_keep_other_versions(workspace: Path) -> None:
    for name in ("libai-1.0.3", "libai-1.0.4"):
        skills_action("save", {"name": [name], "content": [f"# {name}\n"]})

    payload = skills_action("delete", {"name": ["libai-1.0.4"]})

    assert [skill["name"] for skill in payload["skills"]] == ["libai-1.0.3"]
    assert not (workspace / "skills" / "libai-1.0.4").exists()
    assert (workspace / "skills" / "libai-1.0.3" / "SKILL.md").read_text() == "# libai-1.0.3\n"


@pytest.mark.parametrize("name", [".", "..", "../libai", "libai/../../outside", r"libai\..\outside", "/tmp/libai", "a" * 65])
@pytest.mark.parametrize("action", ["detail", "save", "delete", "enable", "disable"])
def test_version_support_still_rejects_invalid_paths(workspace: Path, name: str, action: str) -> None:
    with pytest.raises(WebUISkillsError, match="invalid skill name") as exc:
        skills_action(action, {"name": [name], "content": ["# Invalid\n"]})
    assert exc.value.status == 400
    assert not (workspace / "skills").exists()

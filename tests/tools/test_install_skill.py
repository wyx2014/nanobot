"""User-created skills are installed into the personal catalog from scoped projects."""

import json
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.filesystem import WriteFileTool
from nanobot.agent.tools.install_skill import InstallSkillTool
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.security.protection import SecurityService
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)
from nanobot.storage.logs import StructuredLogStore
from nanobot.webui.skills_api import skills_payload


@pytest.fixture
def project(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    active_project = tmp_path / "research"
    profile.mkdir()
    active_project.mkdir()
    config = Config()
    config.agents.defaults.workspace = str(profile)
    config_path = tmp_path / "config.json"
    save_config(config, config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    token = bind_workspace_scope(build_workspace_scope(active_project, "restricted"))
    yield profile, active_project
    reset_workspace_scope(token)


def draft(project: Path, name: str = "weekly-brief") -> Path:
    folder = project / ".skill-drafts" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Prepare weekly investment summaries.\n---\n\n# Weekly brief\n",
        encoding="utf-8",
    )
    (folder / "scripts").mkdir()
    (folder / "scripts" / "report.py").write_text("print('ready')\n")
    (folder / "assets").mkdir()
    (folder / "assets" / "template.bin").write_bytes(b"\x00\x01template")
    return folder


@pytest.mark.asyncio
async def test_scoped_project_installs_all_resources_into_my_skills(project):
    profile, active = project
    source = draft(active)
    tool = InstallSkillTool(profile, restrict_to_workspace=True)
    result = json.loads(await tool.execute(str(source.relative_to(active))))
    target = profile / "skills" / source.name
    assert result["ok"] is True
    assert Path(result["path"]) == target / "SKILL.md"
    for relative in ("SKILL.md", "scripts/report.py", "assets/template.bin"):
        assert (target / relative).read_bytes() == (source / relative).read_bytes()
    assert not (active / "skills").exists()
    entry = next(skill for skill in skills_payload()["skills"] if skill["name"] == source.name)
    assert entry["source"] == "workspace"
    assert entry["path"] == str(target / "SKILL.md")
    # Installing is separate from authorizing the skill in every project.
    assert source.name not in {
        item["name"] for item in SkillsLoader(profile).list_skills(allowed_workspace_skills=set())
    }
    # The managed installation does not grant ordinary file tools profile write access.
    writer = WriteFileTool(workspace=profile, allowed_dir=profile)
    denied = await writer.execute(path=str(profile / "USER.md"), content="must not write")
    assert "outside allowed directory" in denied
    assert not (profile / "USER.md").exists()


@pytest.mark.asyncio
async def test_installer_preserves_existing_personal_and_builtin_skills(project):
    profile, active = project
    source = draft(active)
    tool = InstallSkillTool(profile)
    assert json.loads(await tool.execute(str(source)))["ok"]
    target = profile / "skills" / source.name / "SKILL.md"
    original = target.read_text()
    (source / "SKILL.md").write_text(original + "replacement")
    assert "existing files were preserved" in await tool.execute(str(source))
    assert target.read_text() == original
    builtin_draft = draft(active, "skill-creator")
    assert "built-in skill" in await tool.execute(str(builtin_draft))
    assert not (profile / "skills" / "skill-creator").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "# Missing frontmatter", "---\nname: wrong-name\ndescription: Valid\n---\n",
    "---\nname: weekly-brief\n---\n", "---\nname: [invalid\n---\n",
])
async def test_invalid_skill_cannot_enter_personal_catalog(project, content):
    profile, active = project
    source = draft(active)
    (source / "SKILL.md").write_text(content)
    assert (await InstallSkillTool(profile).execute(str(source))).startswith("Error installing skill:")
    assert not (profile / "skills").exists()


@pytest.mark.asyncio
async def test_source_stays_inside_active_project(project):
    profile, active = project
    outside = draft(profile)
    result = await InstallSkillTool(profile).execute(str(outside))
    assert "outside allowed directory" in result
    assert not (profile / "skills").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("link_target", ["resource", "library"])
async def test_symlinks_cannot_redirect_skill_reads_or_writes(project, link_target):
    profile, active = project
    source = draft(active)
    outside = profile / "outside"
    outside.mkdir()
    if link_target == "resource":
        (source / "linked").symlink_to(outside, target_is_directory=True)
    else:
        (profile / "skills").symlink_to(outside, target_is_directory=True)
    result = await InstallSkillTool(profile).execute(str(source))
    assert "symlink" in result
    assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_failed_copy_preserves_draft_and_does_not_publish_half_a_skill(project, monkeypatch):
    profile, active = project
    source = draft(active)

    def fail_copy(*args):
        raise OSError("disk full")

    monkeypatch.setattr("nanobot.agent.tools.install_skill.shutil.copy2", fail_copy)
    assert "disk full" in await InstallSkillTool(profile).execute(str(source))
    assert not (profile / "skills" / source.name).exists()
    assert (source / "SKILL.md").is_file()


def test_installation_uses_existing_audit_and_explicit_path_protection(project):
    profile, active = project
    source = draft(active)
    tool = InstallSkillTool(profile)
    service = SecurityService(StructuredLogStore(profile / ".nanobot" / "logs.sqlite"), profile)
    arguments = dict(tool_name=tool.name, params={"source_dir": str(source)}, tool=tool, workspace=active)
    assessment = service.assess(**arguments)
    assert assessment.decision == "allow"
    assert assessment.mutating and assessment.audit_required
    target = profile / "skills" / source.name / "scripts" / "report.py"
    assert str(target) in assessment.details["paths"]
    service.update_policy({"approval_paths": [str(target)]})
    assert service.assess(**arguments).decision == "require_approval"
    (source / "SKILL.md").write_text("---\nname: [invalid\n---\n")
    assert service.assess(**arguments).decision == "block"


def test_model_sees_same_personal_destination_when_project_changes(project):
    profile, active = project
    builder = ContextBuilder(profile)
    for current in (active, profile):
        identity = builder._get_identity(workspace=current)
        assert f"{profile}/skills/{{skill-name}}/SKILL.md" in identity
    assert f"{active}/skills/{{skill-name}}/SKILL.md" not in builder._get_identity(workspace=active)

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.docx import CreateDocxTool
from nanobot.agent.tools.pdf import CreatePdfTool
from nanobot.agent.tools.prepare_output import PrepareOutputTool
from nanobot.config.schema import ToolsConfig
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)
from nanobot.utils.output_paths import (
    output_stem,
    prepare_output_paths,
    prepared_output_path,
    publish_output,
)

NOW = datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc)


def prepare(workspace, filename="周报.docx", *, now=NOW):
    return prepare_output_paths(workspace, workspace, filename, timezone="Asia/Shanghai", now=now)


def test_hourly_names_and_revisions_preserve_previous_files(tmp_path):
    first = prepare(tmp_path)
    Path(first["output_path"]).write_bytes(b"first version")
    second = prepare(tmp_path, first["filename"])
    Path(second["output_path"]).write_bytes(b"second version")
    third = prepare(tmp_path, second["filename"])
    next_hour = prepare(tmp_path, second["filename"], now=NOW.replace(hour=3))
    assert [item["filename"] for item in [first, second, third, next_hour]] == [
        "周报2026092010.docx", "周报2026092010v2.docx",
        "周报2026092010v3.docx", "周报2026092011.docx",
    ]
    assert Path(first["output_path"]).read_bytes() == b"first version"
    assert Path(second["output_path"]).read_bytes() == b"second version"
    assert not Path(third["output_path"]).exists()
    assert Path(first["tmp_dir"]).is_relative_to(tmp_path / "tmp")
    assert Path(first["tmp_dir"]).is_dir()


def test_concurrent_sessions_reserve_distinct_names_without_empty_deliverables(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: prepare(tmp_path), range(12)))
    assert {result["filename"] for result in results} == {
        "周报2026092010.docx", *{f"周报2026092010v{i}.docx" for i in range(2, 13)},
    }
    assert len({result["tmp_dir"] for result in results}) == 12
    assert [path.name for path in tmp_path.iterdir()] == ["tmp"]


def test_existing_versions_are_detected_case_insensitively_and_not_reused(tmp_path):
    (tmp_path / "Report2026092010v9.DOCX").write_bytes(b"existing")
    result = prepare(tmp_path, "report.docx")
    assert result["filename"] == "report2026092010v10.docx"


def test_concurrent_producers_cannot_claim_one_reserved_output_twice(tmp_path):
    reserved = prepare(tmp_path)
    output = Path(reserved["output_path"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: prepared_output_path(tmp_path, output), range(8)))
    assert len(set(claimed)) == 8
    assert output in claimed
    assert not any(path.exists() for path in claimed)


def test_publish_does_not_replace_a_file_created_after_reservation(tmp_path):
    reserved = prepare(tmp_path)
    output = Path(reserved["output_path"])
    output.write_bytes(b"existing version")
    staged = Path(reserved["tmp_dir"]) / "report.docx"
    staged.write_bytes(b"new version")
    with pytest.raises(FileExistsError):
        publish_output(staged, output)
    assert output.read_bytes() == b"existing version"


@pytest.mark.parametrize(("filename", "stem", "extension"), [
    ("周报2026092010v2.docx", "周报", ".docx"),
    ("数据包2026092010v12.tar.gz", "数据包", ".tar.gz"),
    ("2026年度预算.xlsx", "2026年度预算", ".xlsx"),
    ("数据9999999999.csv", "数据9999999999", ".csv"),
])
def test_output_stem_only_removes_valid_generated_suffixes(filename, stem, extension):
    assert output_stem(filename) == (stem, extension)


@pytest.mark.parametrize("filename", ["../a.docx", "..\\a.docx", "C:\\a.docx", "a?.docx", "a", "", ".env", "a.docx "])
def test_invalid_names_do_not_create_directories(tmp_path, filename):
    with pytest.raises(ValueError):
        prepare(tmp_path, filename)
    assert list(tmp_path.iterdir()) == []


def test_scratch_symlink_cannot_escape_workspace_even_in_full_access(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace / "tmp").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="must not point outside"):
        prepare(workspace)
    with pytest.raises(ValueError, match="must not point outside"):
        prepared_output_path(workspace, workspace / "周报.docx")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("directory", ["tmp", "TMP", "reports/tmp"])
def test_final_output_cannot_be_allocated_in_hidden_scratch_directory(tmp_path, directory):
    with pytest.raises(ValueError, match="outside tmp"):
        prepare_output_paths(tmp_path, tmp_path / directory, "周报.docx")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_tool_uses_bound_workspace_and_preserves_security_limits(tmp_path):
    default = tmp_path / "default"
    default.mkdir()
    project = tmp_path / "selected"
    project.mkdir()
    tool = PrepareOutputTool.create(ToolContext(
        config=ToolsConfig(), workspace=str(default), timezone="Asia/Shanghai",
    ))
    token = bind_workspace_scope(build_workspace_scope(project, "restricted"))
    try:
        result = await tool.execute(filename="月报.xlsx")
        assert isinstance(result, dict), result
        assert Path(result["output_path"]).parent == project
        assert Path(result["tmp_dir"]).is_relative_to(project / "tmp")
        assert await tool.execute(filename="月报.xlsx", directory="tmp") == (
            "Error preparing output: final deliverables must be outside tmp"
        )
        assert "Error" in await tool.execute(filename="月报.xlsx", directory="../outside")
    finally:
        reset_workspace_scope(token)
    assert list(default.iterdir()) == []


@pytest.mark.asyncio
async def test_prepared_paths_work_with_word_generation_and_keep_versions_separate(tmp_path):
    from docx import Document

    output_tool = PrepareOutputTool(workspace=tmp_path, allowed_dir=tmp_path)
    word_tool = CreateDocxTool(workspace=tmp_path, allowed_dir=tmp_path)
    first = await output_tool.execute(filename="项目报告.docx")
    result = await word_tool.execute(content="# 项目报告\n\n第一版内容。", output_path=first["output_path"])
    assert isinstance(result, dict), result
    first_bytes = Path(first["output_path"]).read_bytes()
    second = await output_tool.execute(filename=first["filename"])
    result = await word_tool.execute(content="# 项目报告\n\n第二版补充数据。", output_path=second["output_path"])
    assert isinstance(result, dict), result
    assert result["files"][0]["path"] == second["output_path"]
    assert second["filename"].endswith("v2.docx")
    assert Path(first["output_path"]).read_bytes() == first_bytes
    assert "第二版补充数据。" in "\n".join(p.text for p in Document(second["output_path"]).paragraphs)
    assert {path.name for path in tmp_path.iterdir()} == {"tmp", first["filename"], second["filename"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["docx", "pdf"])
async def test_document_tools_version_direct_calls_and_keep_failed_outputs_in_tmp(tmp_path, monkeypatch, kind):
    import nanobot.agent.tools.docx as docx_module
    import nanobot.agent.tools.pdf as pdf_module
    import nanobot.utils.output_paths as output_module

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(output_module, "datetime", FixedTime)
    tool = (CreateDocxTool if kind == "docx" else CreatePdfTool).create(ToolContext(
        config=ToolsConfig(), workspace=str(tmp_path), timezone="Asia/Shanghai",
    ))
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    source = scratch / "source.md"
    source.write_text("# 周报\n\n本周完成数据更新与核验。", encoding="utf-8")
    result = await tool.execute(source_path=str(source), output_path=str(tmp_path / f"周报.{kind}"))
    assert isinstance(result, dict), result
    first = Path(result["files"][0]["path"])
    before = first.read_bytes()
    assert first.name == f"周报2026092010.{kind}"
    result = await tool.execute(source_path=str(source), output_path=str(first))
    assert isinstance(result, dict), result
    second = Path(result["files"][0]["path"])
    assert second.name == f"周报2026092010v2.{kind}"
    assert first.read_bytes() == before

    def broken_renderer(content, source, output, *args, **kwargs):
        assert output.is_relative_to(scratch)
        output.write_bytes(b"incomplete document")
        return {"page_count": 1}

    monkeypatch.setattr(docx_module if kind == "docx" else pdf_module, f"_render_{kind}", broken_renderer)
    failure = await tool.execute(source_path=str(source), output_path=str(first))
    assert "validation_failed" in failure
    assert {path.name for path in tmp_path.iterdir()} == {"tmp", first.name, second.name}


def test_prepare_output_is_audited_as_a_write(tmp_path):
    from nanobot.security.protection import SecurityService
    from nanobot.storage.logs import StructuredLogStore

    security = SecurityService(StructuredLogStore(tmp_path / ".nanobot/logs.sqlite"), tmp_path)
    result = security.assess(
        tool_name="prepare_output", params={"filename": "周报.docx", "directory": "reports"},
        tool=None, workspace=tmp_path,
    )
    assert result.mutating and result.audit_required
    assert str(tmp_path / "reports") in result.target

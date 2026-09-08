from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pptx import Presentation

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.presentation_export import ExportPresentationTool, _PortableHTML
from nanobot.presentations import PresentationError, PresentationService, presentation_runtime_lines
from nanobot.security.protection import SecurityService
from nanobot.storage.logs import StructuredLogStore
from nanobot.webui.transcript import replay_transcript_to_ui_messages


@pytest.fixture
def service(tmp_path):
    return PresentationService(tmp_path)


@pytest.fixture
def context():
    token = bind_request_context(RequestContext("websocket", "chat-a", session_key="websocket:chat-a"))
    yield
    reset_request_context(token)


def bind(service, template="taiping-standard", document_id="document-001"):
    return service.bind({"template_id": template, "document_id": document_id, "sample_first": True},
                        session_key="websocket:chat-a", project_root=service.workspace, title="年度分析")


def guizang(service, tmp_path):
    source = tmp_path / "installed-guizang"
    (source / "assets").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Presentation guide")
    (source / "assets/template.html").write_text("<html><!-- SLIDES_HERE --></html>")
    service.root.mkdir(parents=True, exist_ok=True)
    (service.root / "sources.json").write_text(json.dumps({"guizang": str(source)}))
    return source


def test_binding_survives_restart_and_pins_source(service, tmp_path):
    source = guizang(service, tmp_path)
    document = bind(service, "guizang-editorial")
    (source / "SKILL.md").write_text("Changed upstream")
    restarted = PresentationService(tmp_path)
    rebound = bind(restarted, "guizang-editorial")
    assert rebound["project_path"] == document["project_path"]
    assert rebound["source_digest"] == document["source_digest"]
    assert (Path(rebound["project_path"]) / ".source/SKILL.md").read_text() == "# Presentation guide"
    assert len(restarted.list_documents("websocket:chat-a")) == 1
    assert restarted.list_documents("websocket:other") == []


def test_binding_rejects_other_session_template_changes_and_invalid_pages(service):
    document = bind(service)
    with pytest.raises(PresentationError, match="conversation"):
        service.bind(document, session_key="websocket:other", project_root=service.workspace, title="x")
    with pytest.raises(PresentationError, match="new document"):
        bind(service, "guizang-editorial")
    service.complete(document["document_id"], artifacts=[], page_count=3)
    with pytest.raises(PresentationError, match="page does not exist"):
        service.bind({**document, "page": 4}, session_key="websocket:chat-a", project_root=service.workspace, title="x")
    with pytest.raises(PresentationError, match="ID"):
        bind(service, document_id="../escape")


def test_project_symlink_and_snapshot_symlink_are_rejected(service, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "presentations").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PresentationError):
        bind(service)
    (tmp_path / "presentations").unlink()
    source = guizang(service, tmp_path)
    (source / "scripts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PresentationError, match="symlinks"):
        bind(service, "guizang-editorial")


@pytest.mark.asyncio
async def test_taiping_real_export_uses_snapshot_and_records_artifacts(service, context, monkeypatch):
    document = bind(service)
    folder = Path(document["project_path"])
    (folder / "deck.yaml").write_text(yaml.safe_dump({"version": 1, "slides": [
        {"type": "cover", "title": "年度经营分析"},
        {"type": "title-body", "title": "核心结论", "bullets": ["经营质量持续改善"]},
        {"type": "closing"},
    ]}, allow_unicode=True), encoding="utf-8")
    from nanobot.agent.tools import presentation_export
    script = presentation_export._script

    async def without_pdf(arguments, cwd, **kwargs):
        if any("render_preview.py" in value for value in arguments):
            raise PresentationError("No preview renderer in test")
        assert str(folder / ".source/assets/ppt-template.pptx") in arguments
        return await script(arguments, cwd, **kwargs)

    monkeypatch.setattr(presentation_export, "_script", without_pdf)
    monkeypatch.setattr(PresentationService, "source", lambda *_: None)
    result = await ExportPresentationTool(workspace=service.workspace).execute(document["document_id"])
    assert isinstance(result, dict), result
    assert len(Presentation(folder / "presentation.pptx").slides) == 3
    saved = service.document(document["document_id"])
    assert saved["status"] == "ready" and saved["page_count"] == 3
    assert json.loads((folder / "presentation.json").read_text())["status"] == "ready"


@pytest.mark.asyncio
async def test_html_assets_export_and_failed_revision_preserves_output(service, tmp_path, context):
    guizang(service, tmp_path)
    document = bind(service, "guizang-editorial")
    folder = Path(document["project_path"])
    (folder / "media/chart.png").write_bytes(b"local-image")
    (folder / "index.html").write_text('<html><section class="slide"><img src="media/chart.png"></section></html>')
    tool = ExportPresentationTool(workspace=service.workspace)
    result = await tool.execute(document["document_id"])
    assert isinstance(result, dict), result
    output = (folder / "presentation.html").read_text()
    assert "data:image/png;base64," in output and 'src="media/chart.png"' not in output
    (folder / "index.html").write_text("<!-- SLIDES_HERE -->")
    assert (await tool.execute(document["document_id"])).startswith("Error:")
    assert (folder / "presentation.html").read_text() == output
    (folder / ".source/SKILL.md").write_text("Modified template")
    assert "snapshot has changed" in await tool.execute(document["document_id"])


def test_portable_html_rejects_external_local_path(tmp_path):
    parser = _PortableHTML(tmp_path)
    with pytest.raises(PresentationError, match="inside"):
        parser.feed('<img src="../private.png">')


def test_export_audits_actual_project_and_protects_metadata(service, context):
    document = bind(service)
    security = SecurityService(StructuredLogStore(service.workspace / ".nanobot/logs.sqlite"), service.workspace)
    assessment = security.assess(tool_name="export_presentation", params={"document_id": document["document_id"]},
                                 tool=None, workspace=service.workspace)
    assert assessment.mutating and assessment.audit_required
    assert document["project_path"] in assessment.target
    for path in (service.root / "sources.json", Path(document["project_path"]) / ".source/scripts/run.py"):
        blocked = security.assess(tool_name="write_file", params={"path": str(path)}, tool=None, workspace=service.workspace)
        assert blocked.decision == "block"


def test_runtime_and_transcript_keep_selection_without_leaking_paths(service):
    document = bind(service)
    lines = presentation_runtime_lines({"presentation": document})
    assert any("three representative sample pages" in line for line in lines)
    rows = replay_transcript_to_ui_messages([{"event": "user", "text": "生成样稿", "presentation": document}])
    assert rows[0]["presentation"]["template_id"] == "taiping-standard"
    assert "project_path" not in rows[0]["presentation"]

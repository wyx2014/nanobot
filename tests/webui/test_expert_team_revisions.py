import base64
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from nanobot.graph.workflows.asset_research import MEMBER_NODES
from nanobot.webui.expert_team_revisions import ExpertTeamRevisionService


@pytest.fixture
def revision(tmp_path):
    members = {}
    for member in MEMBER_NODES:
        path = tmp_path / "reports" / f"{member}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("runtime deadline exceeded" if member == "risk-assessor" else "cached report")
        members[member] = {"status": "failed" if member == "risk-assessor" else "completed",
                           "artifact": f"reports/{member}.md"}
    source = {"root": str(tmp_path), "status": "completed", "latest_run_id": "v1",
              "graph_state": {"run_id": "v1", "target": "比亚迪", "checkpoint_revision": 7,
                              "members": members, "degraded": True}}
    state = MagicMock()
    state.active_turn_id.return_value = None
    state.expert_team_revision_source.side_effect = lambda **kw: deepcopy(source) if kw["session_key"] == "websocket:byd" and kw["run_id"] == "v1" else None
    return ExpertTeamRevisionService(state), source, tmp_path


def payload(**extra):
    return {"run_id": "v1", "checkpoint_revision": 7, "roles": ["risk-assessor"],
            "text": "有息负债补充", "period": "2026H1", "links": [], "files": [], **extra}


def test_prepare_stages_evidence_without_starting_or_resolving_gaps(revision):
    service, source, root = revision
    context = service.context("websocket:byd", "v1")
    assert "超时" in context["roles"][-1]["reason"]
    assert context["base_cached"] is False
    original = deepcopy(source)
    plan = service.prepare("websocket:byd", payload(files=[{
        "name": "../../original.md", "base64": base64.b64encode(b"new evidence").decode(),
    }]))
    assert plan["version"] == 2
    assert plan["selected_roles"] == ["risk-assessor"]
    assert plan["reused_roles"] == list(MEMBER_NODES[:-1])
    material = plan["materials"][0]
    assert material["status"] == "待核验"
    assert (root / material["path"]).read_text() == "new evidence"
    assert not (root.parent / "original.md").exists()
    assert source == original
    assert not (root / material["path"]).parent.joinpath("started").exists()


def test_confirmation_is_session_scoped_and_cannot_start_twice(revision):
    service, _, root = revision
    one = service.prepare("websocket:byd", payload())
    two = service.prepare("websocket:byd", payload())
    with pytest.raises(ValueError):
        service.consume("websocket:other", one["plan_id"])
    with pytest.raises(ValueError, match="项目目录"):
        service.consume("websocket:byd", one["plan_id"], expected_root=root / "other")
    consumed = service.consume("websocket:byd", one["plan_id"], expected_root=root)
    assert consumed["source"]["graph_state"]["members"]["risk-assessor"]["status"] == "failed"
    for plan in [one, two]:
        with pytest.raises(ValueError, match="已提交"):
            service.consume("websocket:byd", plan["plan_id"])
    service.discard("websocket:byd", one["plan_id"])
    assert (root / consumed["supplement_path"]).exists()


@pytest.mark.parametrize("change", ["checkpoint", "artifact", "new_run", "active_turn"])
def test_confirmation_rejects_changed_evidence_or_active_task(revision, change):
    service, source, root = revision
    plan = service.prepare("websocket:byd", payload())
    if change == "checkpoint":
        source["graph_state"]["checkpoint_revision"] += 1
    elif change == "artifact":
        (root / "reports/financial-analyst.md").write_text("corrected")
    elif change == "new_run":
        source["latest_run_id"] = "v2"
    else:
        service.state.active_turn_id.return_value = "active"
    with pytest.raises(ValueError):
        service.consume("websocket:byd", plan["plan_id"])
    assert not (root / f"reports/.team-revisions/{plan['plan_id']}/started").exists()


def test_discard_only_removes_unsubmitted_owned_materials(revision):
    service, _, root = revision
    plan = service.prepare("websocket:byd", payload())
    directory = root / f"reports/.team-revisions/{plan['plan_id']}"
    service.discard("websocket:other", plan["plan_id"])
    assert directory.exists()
    service.discard("websocket:byd", plan["plan_id"])
    assert not directory.exists()
    with pytest.raises(ValueError):
        service.consume("websocket:byd", plan["plan_id"])


@pytest.mark.parametrize("extra", [
    {"roles": []}, {"roles": ["unknown"]}, {"roles": [None]},
    {"checkpoint_revision": 6}, {"text": "x" * 30001},
    {"links": ["file:///etc/passwd"]},
    {"files": [{"name": "script.exe", "base64": "YQ=="}]},
    {"files": [{"name": "file.md", "base64": "not base64"}]},
])
def test_invalid_plan_is_rejected_before_writing(revision, extra):
    service, _, root = revision
    with pytest.raises(ValueError):
        service.prepare("websocket:byd", payload(**extra))
    assert not (root / "reports/.team-revisions").exists()


def test_missing_reused_cache_requires_expanding_scope(revision):
    service, source, root = revision
    (root / "reports/financial-analyst.md").unlink()
    with pytest.raises(ValueError, match="财务分析师"):
        service.prepare("websocket:byd", payload())
    plan = service.prepare("websocket:byd", payload(roles=["risk-assessor", "financial-analyst"]))
    assert plan["selected_roles"] == ["financial-analyst", "risk-assessor"]
    source["graph_state"]["members"]["business-analyst"]["artifact"] = "../../outside.md"
    with pytest.raises(ValueError, match="商业分析师"):
        service.prepare("websocket:byd", payload(roles=["risk-assessor", "financial-analyst"]))

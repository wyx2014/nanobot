import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronRunRecord, CronSchedule
from nanobot.storage.state import StateStore, StateStoreError
from nanobot.webui.schedule_routes import WebUIScheduleRouter, _schedule_from_query, _task_payload


def _json_response(payload: dict) -> Response:
    return Response(200, "OK", Headers(), json.dumps(payload).encode())


def _error_response(status: int, message: str | None) -> Response:
    return Response(status, "Error", Headers(), json.dumps({"error": message}).encode())


def _router(
    service: CronService,
    *,
    purge_session=None,
    state_store: StateStore | None = None,
) -> WebUIScheduleRouter:
    return WebUIScheduleRouter(
        cron_service=service,
        check_api_token=lambda _request: True,
        parse_query=lambda path: {
            key: [value]
            for key, value in (
                part.split("=", 1)
                for part in path.partition("?")[2].split("&")
                if "=" in part
            )
        },
        json_response=_json_response,
        error_response=_error_response,
        logger=MagicMock(),
        state_store=state_store,
        purge_session=purge_session,
    )


def _service_with_run(
    store_path: Path,
    *,
    status: str = "ok",
    session_key: str | None = None,
) -> tuple[CronService, str]:
    service = CronService(store_path)
    created = service.add_job(
        name="history",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        channel="websocket",
        to="direct",
        session_key="websocket:source",
    )
    service._running = True
    store = service._load_store()
    job = next(item for item in store.jobs if item.id == created.id)
    job.state.run_history.append(
        CronRunRecord(
            run_at_ms=int(time.time() * 1000),
            status=status,
            run_id="run-1",
            session_key=session_key,
        )
    )
    service._save_store()
    return service, job.id


def test_monthly_schedule_query_builds_monthly_cron_and_metadata() -> None:
    schedule, enabled, metadata = _schedule_from_query(
        {
            "frequency": ["monthly"],
            "hour": ["9"],
            "minute": ["15"],
            "day_of_month": ["1"],
            "timezone": ["Asia/Shanghai"],
        }
    )

    assert enabled is True
    assert schedule.kind == "cron"
    assert schedule.expr == "15 9 1 * *"
    assert schedule.tz == "Asia/Shanghai"
    assert metadata == {
        "frequency": "monthly",
        "time": {"hour": 9, "minute": 15},
        "dayOfMonth": 1,
    }


def test_monthly_schedule_query_clamps_day_to_valid_cron_range() -> None:
    schedule, _, metadata = _schedule_from_query(
        {"frequency": ["monthly"], "day_of_month": ["50"]}
    )

    assert schedule.expr == "0 9 31 * *"
    assert metadata["dayOfMonth"] == 31


def test_once_schedule_query_builds_at_schedule() -> None:
    schedule, enabled, metadata = _schedule_from_query(
        {
            "frequency": ["once"],
            "at": ["2099-08-30T01:15:00Z"],
            "timezone": ["Asia/Shanghai"],
        }
    )

    assert enabled is True
    assert schedule.kind == "at"
    assert schedule.at_ms == 4091735700000
    assert metadata == {
        "frequency": "once",
        "at": "2099-08-30T01:15:00Z",
        "timezone": "Asia/Shanghai",
    }


def test_once_schedule_requires_an_execution_time() -> None:
    with pytest.raises(ValueError, match="at is required"):
        _schedule_from_query({"frequency": ["once"]})


def test_chat_created_once_job_is_not_serialized_as_daily() -> None:
    job = CronJob(
        id="once-1",
        name="One-time reminder",
        schedule=CronSchedule(kind="at", at_ms=4091735700000),
        payload=CronPayload(message="提醒我提交材料"),
        created_at_ms=1,
        updated_at_ms=1,
    )

    payload = _task_payload(job)

    assert payload["schedule"] == {
        "frequency": "once",
        "at": "2099-08-30T01:15:00Z",
    }


def test_finished_once_job_is_serialized_as_completed() -> None:
    job = CronJob(
        id="once-completed",
        name="Completed reminder",
        enabled=False,
        schedule=CronSchedule(kind="at", at_ms=4091735700000),
        payload=CronPayload(message="提醒我提交材料"),
        state=CronJobState(last_run_at_ms=4091735700000),
        created_at_ms=1,
        updated_at_ms=1,
    )

    assert _task_payload(job)["status"] == "completed"


def test_legacy_run_does_not_fabricate_a_conversation_session() -> None:
    job = CronJob(
        id="legacy-job",
        name="Legacy reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="remind me"),
        state=CronJobState(run_history=[CronRunRecord(
            run_at_ms=1_700_000_000_000,
            status="ok",
            run_id="legacy-run",
        )]),
        created_at_ms=1,
        updated_at_ms=1,
    )

    [run] = _task_payload(job)["runs"]

    assert run["conversationId"] == ""
    assert run["sessionKey"] == ""
    assert run["resultType"] == "none"
    assert run["conversationAvailable"] is False
    assert run["unavailableReason"] == "legacy"


def test_run_with_session_exposes_conversation_availability() -> None:
    job = CronJob(
        id="current-job",
        name="Current task",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="run"),
        state=CronJobState(run_history=[CronRunRecord(
            run_at_ms=1_700_000_000_000,
            status="ok",
            run_id="current-run",
            session_key="cron:current-job:current-run",
        )]),
        created_at_ms=1,
        updated_at_ms=1,
    )

    [run] = _task_payload(job)["runs"]

    assert run["conversationAvailable"] is True
    assert run["resultType"] == "conversation"
    assert "unavailableReason" not in run


def test_reminder_run_never_exposes_an_internal_conversation_session() -> None:
    job = CronJob(
        id="reminder-job",
        name="Simple reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(result_type="none", message="drink water"),
        state=CronJobState(run_history=[CronRunRecord(
            run_at_ms=1_700_000_000_000,
            status="ok",
            run_id="reminder-run",
            session_key="cron:reminder-job:reminder-run",
        )]),
        created_at_ms=1,
        updated_at_ms=1,
    )

    [run] = _task_payload(job)["runs"]

    assert run["conversationId"] == ""
    assert run["sessionKey"] == ""
    assert run["resultType"] == "none"
    assert run["conversationAvailable"] is False
    assert "unavailableReason" not in run


def test_unrecognized_recurring_job_is_serialized_as_custom_not_daily() -> None:
    job = CronJob(
        id="custom-1",
        name="Custom schedule",
        schedule=CronSchedule(kind="cron", expr="*/15 8-18 * * 1-5", tz="Asia/Shanghai"),
        payload=CronPayload(message="检查状态"),
        created_at_ms=1,
        updated_at_ms=1,
    )

    payload = _task_payload(job)

    assert payload["schedule"] == {
        "frequency": "custom",
        "cronExpression": "*/15 8-18 * * 1-5",
        "timezone": "Asia/Shanghai",
    }


async def test_create_schedule_persists_registered_workspace_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_path = tmp_path / "project"
    project_path.mkdir()
    state = StateStore(
        workspace / ".nanobot" / "state.sqlite",
        default_workspace=workspace,
    )
    service = CronService(workspace / "cron" / "jobs.json")
    request = Request(
        "/api/schedule/tasks/create"
        f"?name=weekly&prompt=report&workspace_path={project_path}",
        Headers(),
    )

    response = await _router(service, state_store=state).dispatch(
        request,
        "/api/schedule/tasks/create",
    )

    assert response is not None
    assert response.status_code == 200
    [job] = service.list_jobs(include_disabled=True)
    project = state.get_project(job.payload.project_id or "")
    assert project is not None
    assert job.payload.origin_metadata["workspace_scope"] == {
        "project_path": project.canonical_root_path,
    }


async def test_delete_run_route_removes_completed_record(tmp_path) -> None:
    service, job_id = _service_with_run(tmp_path / "cron" / "jobs.json")
    request = Request(
        f"/api/schedule/runs/delete?task_id={job_id}&run_id=run-1",
        Headers(),
    )

    response = await _router(service).dispatch(request, "/api/schedule/runs/delete")

    assert response is not None
    assert response.status_code == 200
    assert service.get_job(job_id).state.run_history == []


async def test_confirming_completed_one_time_reminder_physically_deletes_job(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    created = service.add_job(
        name="A股开市提醒",
        schedule=CronSchedule(kind="at", at_ms=4_091_735_700_000),
        message="提醒我关注A股开市",
        result_type="none",
        session_key="websocket:source",
        origin_channel="websocket",
        origin_chat_id="source",
    )
    service._running = True
    job = service.get_job(created.id)
    assert job is not None
    job.enabled = False
    job.state.last_run_at_ms = 1_700_000_000_000
    job.state.run_history.append(CronRunRecord(
        run_at_ms=1_700_000_000_000,
        status="ok",
        run_id="reminder-run",
    ))
    service._save_store()
    request = Request(
        f"/api/schedule/runs/viewed?task_id={job.id}&run_id=reminder-run",
        Headers(),
    )

    response = await _router(service).dispatch(request, "/api/schedule/runs/viewed")

    assert response is not None
    assert response.status_code == 200
    assert service.get_job(job.id) is None
    assert json.loads(response.body)["tasks"] == []


async def test_delete_run_route_rejects_running_record(tmp_path) -> None:
    service, job_id = _service_with_run(
        tmp_path / "cron" / "jobs.json",
        status="running",
    )
    request = Request(
        f"/api/schedule/runs/delete?task_id={job_id}&run_id=run-1",
        Headers(),
    )

    response = await _router(service).dispatch(request, "/api/schedule/runs/delete")

    assert response is not None
    assert response.status_code == 409
    assert service.get_job(job_id).state.run_history


async def test_delete_run_route_purges_owned_conversation_before_record(tmp_path) -> None:
    service, job_id = _service_with_run(
        tmp_path / "cron" / "jobs.json",
        session_key="cron:job:run-1",
    )
    purge_session = MagicMock(return_value={"purged": True})
    request = Request(
        f"/api/schedule/runs/delete?task_id={job_id}&run_id=run-1",
        Headers(),
    )

    response = await _router(
        service,
        purge_session=purge_session,
    ).dispatch(request, "/api/schedule/runs/delete")

    assert response is not None
    assert response.status_code == 200
    purge_session.assert_called_once_with("cron:job:run-1")
    assert service.get_job(job_id).state.run_history == []


async def test_delete_run_route_keeps_record_when_conversation_purge_fails(
    tmp_path,
) -> None:
    service, job_id = _service_with_run(
        tmp_path / "cron" / "jobs.json",
        session_key="cron:job:run-1",
    )
    purge_session = MagicMock(side_effect=StateStoreError("session is busy"))
    request = Request(
        f"/api/schedule/runs/delete?task_id={job_id}&run_id=run-1",
        Headers(),
    )

    response = await _router(
        service,
        purge_session=purge_session,
    ).dispatch(request, "/api/schedule/runs/delete")

    assert response is not None
    assert response.status_code == 409
    assert service.get_job(job_id).state.run_history


async def test_delete_legacy_run_does_not_purge_shared_fallback_session(tmp_path) -> None:
    service, job_id = _service_with_run(tmp_path / "cron" / "jobs.json")
    purge_session = MagicMock(return_value={"purged": True})
    request = Request(
        f"/api/schedule/runs/delete?task_id={job_id}&run_id=run-1",
        Headers(),
    )

    response = await _router(
        service,
        purge_session=purge_session,
    ).dispatch(request, "/api/schedule/runs/delete")

    assert response is not None
    assert response.status_code == 200
    purge_session.assert_not_called()

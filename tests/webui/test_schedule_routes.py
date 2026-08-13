import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from nanobot.cron.service import CronService
from nanobot.cron.types import CronRunRecord, CronSchedule
from nanobot.storage.state import StateStoreError
from nanobot.webui.schedule_routes import WebUIScheduleRouter, _schedule_from_query


def _json_response(payload: dict) -> Response:
    return Response(200, "OK", Headers(), json.dumps(payload).encode())


def _error_response(status: int, message: str | None) -> Response:
    return Response(status, "Error", Headers(), json.dumps({"error": message}).encode())


def _router(
    service: CronService,
    *,
    purge_session=None,
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

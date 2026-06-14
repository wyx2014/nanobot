"""HTTP route adapter for WebUI schedule APIs backed by CronService."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronRunRecord, CronSchedule

QueryParams = dict[str, list[str]]

_META_NS = "nanobot_gui"


def _first(query: QueryParams, key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    value = values[0]
    return value if isinstance(value, str) else default


def _int(query: QueryParams, key: str, default: int) -> int:
    try:
        return int(_first(query, key, str(default)))
    except ValueError:
        return default


def _job_meta(job: CronJob) -> dict[str, Any]:
    meta = job.payload.channel_meta if isinstance(job.payload.channel_meta, dict) else {}
    scoped = meta.get(_META_NS)
    return scoped if isinstance(scoped, dict) else {}


def _schedule_from_query(query: QueryParams) -> tuple[CronSchedule, bool, dict[str, Any]]:
    frequency = _first(query, "frequency", "daily")
    hour = max(0, min(23, _int(query, "hour", 9)))
    minute = max(0, min(59, _int(query, "minute", 0)))
    day = max(0, min(6, _int(query, "day_of_week", 1)))
    tz = _first(query, "timezone", "") or None

    meta_schedule: dict[str, Any] = {"frequency": frequency}
    enabled = True

    if frequency == "hourly":
        expr = f"{minute} * * * *"
        meta_schedule["time"] = {"hour": 0, "minute": minute}
    elif frequency == "weekly":
        expr = f"{minute} {hour} * * {day}"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
        meta_schedule["dayOfWeek"] = day
    elif frequency == "weekdays":
        expr = f"{minute} {hour} * * 1-5"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
    elif frequency == "manual":
        # CronService needs a concrete schedule. Keep manual tasks disabled and
        # force-run them through /api/schedule/tasks/run.
        expr = "0 0 1 1 *"
        enabled = False
    else:
        expr = f"{minute} {hour} * * *"
        meta_schedule["frequency"] = "daily"
        meta_schedule["time"] = {"hour": hour, "minute": minute}

    return CronSchedule(kind="cron", expr=expr, tz=tz), enabled, meta_schedule


def _message(prompt: str, skill_name: str) -> str:
    prompt = prompt.strip()
    skill_name = skill_name.strip()
    return f"/{skill_name} {prompt}" if skill_name else prompt


def _run_payload(job: CronJob, run: CronRunRecord) -> dict[str, Any]:
    status = "completed" if run.status == "ok" else "error" if run.status == "error" else "running"
    completed_at = run.run_at_ms + max(0, run.duration_ms or 0)
    return {
        "id": f"{job.id}:{run.run_at_ms}",
        "scheduledTaskId": job.id,
        "conversationId": f"cron:{job.id}",
        "startedAt": run.run_at_ms,
        "completedAt": completed_at,
        "status": status,
        "error": run.error,
    }


def _task_payload(job: CronJob) -> dict[str, Any]:
    meta = _job_meta(job)
    schedule = meta.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {"frequency": "manual" if not job.enabled else "daily"}
    prompt = meta.get("prompt")
    if not isinstance(prompt, str):
        prompt = job.payload.message
    runs = [_run_payload(job, run) for run in reversed(job.state.run_history)]
    return {
        "id": job.id,
        "name": job.name,
        "description": meta.get("description") if isinstance(meta.get("description"), str) else "",
        "prompt": prompt,
        "schedule": schedule,
        "status": "active" if job.enabled else "paused",
        "skillName": meta.get("skillName") if isinstance(meta.get("skillName"), str) else "",
        "workspacePath": meta.get("workspacePath") if isinstance(meta.get("workspacePath"), str) else "",
        "createdAt": job.created_at_ms,
        "updatedAt": job.updated_at_ms,
        "lastRunAt": job.state.last_run_at_ms,
        "nextRunAt": job.state.next_run_at_ms,
        "runs": runs,
        "totalRuns": len(job.state.run_history),
    }


def _payload(cron: CronService) -> dict[str, Any]:
    jobs = [
        job for job in cron.list_jobs(include_disabled=True)
        if job.payload.kind != "system_event"
    ]
    return {
        "tasks": [_task_payload(job) for job in jobs],
        "status": cron.status(),
    }


class WebUIScheduleRouter:
    """Route WebUI schedule HTTP requests behind a transport-neutral boundary."""

    def __init__(
        self,
        *,
        cron_service: CronService | None,
        check_api_token: Callable[[WsRequest], bool],
        parse_query: Callable[[str], QueryParams],
        json_response: Callable[[dict[str, Any]], Response],
        error_response: Callable[[int, str | None], Response],
        logger: Any,
    ) -> None:
        self.cron = cron_service
        self._check_api_token = check_api_token
        self._parse_query = parse_query
        self._json_response = json_response
        self._error_response = error_response
        self.logger = logger

    async def dispatch(self, request: WsRequest, path: str) -> Response | None:
        if not path.startswith("/api/schedule/"):
            return None
        if not self._check_api_token(request):
            return self._error_response(401, "Unauthorized")
        if self.cron is None:
            return self._error_response(503, "cron service unavailable")

        if path == "/api/schedule/tasks":
            return self._json_response(_payload(self.cron))
        if path == "/api/schedule/tasks/create":
            return self._create(request)
        if path == "/api/schedule/tasks/update":
            return self._update(request)
        if path == "/api/schedule/tasks/delete":
            return self._delete(request)
        if path == "/api/schedule/tasks/pause":
            return self._enable(request, False)
        if path == "/api/schedule/tasks/resume":
            return self._enable(request, True)
        if path == "/api/schedule/tasks/run":
            return await self._run(request)
        return self._error_response(404, "schedule route not found")

    def _query(self, request: WsRequest) -> QueryParams:
        return self._parse_query(request.path)

    def _meta(self, query: QueryParams, schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            _META_NS: {
                "description": _first(query, "description"),
                "prompt": _first(query, "prompt"),
                "skillName": _first(query, "skill_name"),
                "workspacePath": _first(query, "workspace_path"),
                "schedule": schedule,
            }
        }

    def _create(self, request: WsRequest) -> Response:
        query = self._query(request)
        name = _first(query, "name").strip()
        prompt = _first(query, "prompt").strip()
        if not name or not prompt:
            return self._error_response(400, "name and prompt are required")
        schedule, enabled, meta_schedule = _schedule_from_query(query)
        skill_name = _first(query, "skill_name")
        try:
            job = self.cron.add_job(
                name=name,
                schedule=schedule,
                message=_message(prompt, skill_name),
                deliver=False,
                channel="websocket",
                to="direct",
                channel_meta=self._meta(query, meta_schedule),
            )
            if not enabled:
                self.cron.enable_job(job.id, False)
        except ValueError as exc:
            return self._error_response(400, str(exc))
        return self._json_response(_payload(self.cron))

    def _update(self, request: WsRequest) -> Response:
        query = self._query(request)
        job_id = _first(query, "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        name = _first(query, "name").strip()
        prompt = _first(query, "prompt").strip()
        if not name or not prompt:
            return self._error_response(400, "name and prompt are required")
        schedule, enabled, meta_schedule = _schedule_from_query(query)
        skill_name = _first(query, "skill_name")
        try:
            result = self.cron.update_job(
                job_id,
                name=name,
                schedule=schedule,
                message=_message(prompt, skill_name),
                channel_meta=self._meta(query, meta_schedule),
            )
        except ValueError as exc:
            return self._error_response(400, str(exc))
        if result == "not_found":
            return self._error_response(404, "task not found")
        if result == "protected":
            return self._error_response(403, "system task cannot be updated")
        self.cron.enable_job(job_id, enabled)
        return self._json_response(_payload(self.cron))

    def _delete(self, request: WsRequest) -> Response:
        job_id = _first(self._query(request), "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        result = self.cron.remove_job(job_id)
        if result == "not_found":
            return self._error_response(404, "task not found")
        if result == "protected":
            return self._error_response(403, "system task cannot be removed")
        return self._json_response(_payload(self.cron))

    def _enable(self, request: WsRequest, enabled: bool) -> Response:
        job_id = _first(self._query(request), "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        job = self.cron.enable_job(job_id, enabled)
        if job is None:
            return self._error_response(404, "task not found")
        return self._json_response(_payload(self.cron))

    async def _run(self, request: WsRequest) -> Response:
        job_id = _first(self._query(request), "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        ok = await self.cron.run_job(job_id, force=True)
        if not ok:
            return self._error_response(404, "task not found")
        return self._json_response(_payload(self.cron))

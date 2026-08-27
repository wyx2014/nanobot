"""HTTP route adapter for WebUI schedule APIs backed by CronService."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronRunRecord, CronSchedule
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY
from nanobot.storage.state import StateStore, StateStoreError

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


def _iso_datetime(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
    return value.replace("+00:00", "Z")


def _once_schedule(query: QueryParams, tz: str | None) -> tuple[CronSchedule, dict[str, Any]]:
    value = _first(query, "at").strip()
    if not value:
        raise ValueError("at is required for a one-time schedule")
    try:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("at must be a valid ISO datetime") from None

    if parsed.tzinfo is None:
        if tz:
            from zoneinfo import ZoneInfo

            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                raise ValueError(f"unknown timezone '{tz}'") from None
        else:
            parsed = parsed.astimezone()

    at_ms = int(parsed.timestamp() * 1000)
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    if at_ms <= now_ms:
        raise ValueError("one-time schedule must be in the future")

    metadata: dict[str, Any] = {
        "frequency": "once",
        "at": _iso_datetime(at_ms),
    }
    if tz:
        metadata["timezone"] = tz
    return CronSchedule(kind="at", at_ms=at_ms), metadata


def _custom_schedule(query: QueryParams, tz: str | None) -> tuple[CronSchedule, dict[str, Any]]:
    cron_expression = _first(query, "cron_expression").strip()
    if cron_expression:
        metadata: dict[str, Any] = {
            "frequency": "custom",
            "cronExpression": cron_expression,
        }
        if tz:
            metadata["timezone"] = tz
        return CronSchedule(kind="cron", expr=cron_expression, tz=tz), metadata

    every_ms_value = _first(query, "every_ms").strip()
    try:
        every_ms = int(every_ms_value)
    except ValueError:
        every_ms = 0
    if every_ms <= 0:
        raise ValueError("custom schedule requires cron_expression or a positive every_ms")
    return CronSchedule(kind="every", every_ms=every_ms), {
        "frequency": "custom",
        "everyMs": every_ms,
    }


def _job_meta(job: CronJob) -> dict[str, Any]:
    meta = job.payload.origin_metadata if isinstance(job.payload.origin_metadata, dict) else {}
    if not meta:
        meta = job.payload.channel_meta if isinstance(job.payload.channel_meta, dict) else {}
    scoped = meta.get(_META_NS)
    return scoped if isinstance(scoped, dict) else {}


def _schedule_from_query(query: QueryParams) -> tuple[CronSchedule, bool, dict[str, Any]]:
    frequency = _first(query, "frequency", "daily")
    hour = max(0, min(23, _int(query, "hour", 9)))
    minute = max(0, min(59, _int(query, "minute", 0)))
    day = max(0, min(6, _int(query, "day_of_week", 1)))
    month_day = max(1, min(31, _int(query, "day_of_month", 1)))
    tz = _first(query, "timezone", "") or None

    if frequency == "once":
        schedule, metadata = _once_schedule(query, tz)
        return schedule, True, metadata
    if frequency == "custom":
        schedule, metadata = _custom_schedule(query, tz)
        return schedule, True, metadata

    meta_schedule: dict[str, Any] = {"frequency": frequency}
    enabled = True

    if frequency == "hourly":
        expr = f"{minute} * * * *"
        meta_schedule["time"] = {"hour": 0, "minute": minute}
    elif frequency == "weekly":
        expr = f"{minute} {hour} * * {day}"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
        meta_schedule["dayOfWeek"] = day
    elif frequency == "monthly":
        expr = f"{minute} {hour} {month_day} * *"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
        meta_schedule["dayOfMonth"] = month_day
    elif frequency == "weekdays":
        expr = f"{minute} {hour} * * 1-5"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
    elif frequency == "manual":
        # CronService needs a concrete schedule. Keep manual tasks disabled and
        # force-run them through /api/schedule/tasks/run.
        expr = "0 0 1 1 *"
        enabled = False
    elif frequency == "daily":
        expr = f"{minute} {hour} * * *"
        meta_schedule["time"] = {"hour": hour, "minute": minute}
    else:
        raise ValueError(f"unsupported schedule frequency '{frequency}'")

    return CronSchedule(kind="cron", expr=expr, tz=tz), enabled, meta_schedule


def _cron_schedule_payload(schedule: CronSchedule) -> dict[str, Any]:
    expression = (schedule.expr or "").strip()
    fields = expression.split()
    timezone = schedule.tz

    def _number(value: str, minimum: int, maximum: int) -> int | None:
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if minimum <= parsed <= maximum else None

    if len(fields) == 5:
        minute_text, hour_text, month_day_text, month_text, week_day_text = fields
        minute = _number(minute_text, 0, 59)
        hour = _number(hour_text, 0, 23)
        month_day = _number(month_day_text, 1, 31)
        week_day = _number(week_day_text, 0, 6)

        if (
            minute is not None
            and hour_text == "*"
            and month_day_text == "*"
            and month_text == "*"
            and week_day_text == "*"
        ):
            payload: dict[str, Any] = {
                "frequency": "hourly",
                "time": {"hour": 0, "minute": minute},
            }
        elif (
            minute is not None
            and hour is not None
            and month_day_text == "*"
            and month_text == "*"
            and week_day_text == "1-5"
        ):
            payload = {
                "frequency": "weekdays",
                "time": {"hour": hour, "minute": minute},
            }
        elif (
            minute is not None
            and hour is not None
            and month_day_text == "*"
            and month_text == "*"
            and week_day_text == "*"
        ):
            payload = {
                "frequency": "daily",
                "time": {"hour": hour, "minute": minute},
            }
        elif (
            minute is not None
            and hour is not None
            and month_day_text == "*"
            and month_text == "*"
            and week_day is not None
        ):
            payload = {
                "frequency": "weekly",
                "time": {"hour": hour, "minute": minute},
                "dayOfWeek": week_day,
            }
        elif (
            minute is not None
            and hour is not None
            and month_day is not None
            and month_text == "*"
            and week_day_text == "*"
        ):
            payload = {
                "frequency": "monthly",
                "time": {"hour": hour, "minute": minute},
                "dayOfMonth": month_day,
            }
        else:
            payload = {"frequency": "custom", "cronExpression": expression}
    else:
        payload = {"frequency": "custom", "cronExpression": expression}

    if timezone:
        payload["timezone"] = timezone
    return payload


def _schedule_payload(job: CronJob, meta: dict[str, Any]) -> dict[str, Any]:
    schedule = job.schedule
    metadata = meta.get("schedule")
    metadata = metadata if isinstance(metadata, dict) else {}

    if schedule.kind == "at" and schedule.at_ms is not None:
        payload: dict[str, Any] = {
            "frequency": "once",
            "at": _iso_datetime(schedule.at_ms),
        }
        timezone = metadata.get("timezone")
        if isinstance(timezone, str) and timezone:
            payload["timezone"] = timezone
        return payload

    if schedule.kind == "every":
        return {
            "frequency": "custom",
            "everyMs": schedule.every_ms,
        }

    if schedule.kind == "cron":
        if metadata.get("frequency") == "manual" and schedule.expr == "0 0 1 1 *":
            return {"frequency": "manual"}
        return _cron_schedule_payload(schedule)

    return {"frequency": "custom"}


def _message(prompt: str, skill_name: str) -> str:
    prompt = prompt.strip()
    skill_name = skill_name.strip()
    return f"/{skill_name} {prompt}" if skill_name else prompt


def _run_payload(job: CronJob, run: CronRunRecord) -> dict[str, Any]:
    status = "running" if run.status == "running" else "error" if run.status == "error" else "completed"
    completed_at = run.run_at_ms + max(0, run.duration_ms or 0)
    session_key = run.session_key or ""
    expects_conversation = job.payload.result_type == "conversation"
    conversation_available = expects_conversation and bool(session_key)
    return {
        "id": run.run_id or f"{job.id}:{run.run_at_ms}",
        "scheduledTaskId": job.id,
        "conversationId": session_key if expects_conversation else "",
        "sessionKey": session_key if expects_conversation else "",
        "resultType": "conversation" if conversation_available else "none",
        "conversationAvailable": conversation_available,
        **({
            "unavailableReason": "missing" if status == "running" else "legacy"
        } if expects_conversation and not conversation_available else {}),
        "runId": run.run_id,
        "startedAt": run.run_at_ms,
        "completedAt": completed_at,
        "status": status,
        "error": run.error,
        "viewedAt": run.viewed_at_ms,
    }


def _task_payload(job: CronJob) -> dict[str, Any]:
    meta = _job_meta(job)
    schedule = _schedule_payload(job, meta)
    prompt = meta.get("prompt")
    if not isinstance(prompt, str):
        prompt = job.payload.message
    runs = [_run_payload(job, run) for run in reversed(job.state.run_history)]
    status = "active" if job.enabled else "paused"
    if job.schedule.kind == "at" and not job.enabled and job.state.last_run_at_ms is not None:
        status = "completed"
    return {
        "id": job.id,
        "name": job.name,
        "description": meta.get("description") if isinstance(meta.get("description"), str) else "",
        "prompt": prompt,
        "schedule": schedule,
        "status": status,
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
        state_store: StateStore | None = None,
        purge_session: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.cron = cron_service
        self._check_api_token = check_api_token
        self._parse_query = parse_query
        self._json_response = json_response
        self._error_response = error_response
        self.logger = logger
        self.state = state_store
        self._purge_session = purge_session

    async def dispatch(self, request: WsRequest, path: str) -> Response | None:
        if not path.startswith("/api/schedule/"):
            return None
        if not self._check_api_token(request):
            return self._error_response(401, "Unauthorized")
        if self.cron is None:
            return self._error_response(503, "cron service unavailable")

        if path == "/api/schedule/tasks":
            self._sync_state()
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
        if path == "/api/schedule/runs/viewed":
            return self._mark_run_viewed(request)
        if path == "/api/schedule/runs/delete":
            return self._delete_run(request)
        return self._error_response(404, "schedule route not found")

    def _query(self, request: WsRequest) -> QueryParams:
        return self._parse_query(request.path)

    def _meta(self, query: QueryParams, schedule: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            _META_NS: {
                "description": _first(query, "description"),
                "prompt": _first(query, "prompt"),
                "skillName": _first(query, "skill_name"),
                "workspacePath": _first(query, "workspace_path"),
                "schedule": schedule,
            }
        }
        if self.state is not None:
            workspace_path = _first(query, "workspace_path").strip()
            project = self.state.ensure_project(
                workspace_path or self.state.default_workspace
            )
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = {
                "project_path": project.canonical_root_path,
            }
        return metadata

    def _project_id(self, query: QueryParams) -> str | None:
        workspace_path = _first(query, "workspace_path").strip()
        if self.state is None:
            return None
        return self.state.ensure_project(
            workspace_path or self.state.default_workspace
        ).id

    def _sync_state(self) -> None:
        if self.state is None or self.cron is None:
            return
        self.state.sync_project_schedules(
            self.cron.list_jobs(include_disabled=True)
        )

    def _create(self, request: WsRequest) -> Response:
        query = self._query(request)
        name = _first(query, "name").strip()
        prompt = _first(query, "prompt").strip()
        if not name or not prompt:
            return self._error_response(400, "name and prompt are required")
        skill_name = _first(query, "skill_name")
        try:
            schedule, enabled, meta_schedule = _schedule_from_query(query)
            job = self.cron.add_job(
                name=name,
                schedule=schedule,
                message=_message(prompt, skill_name),
                deliver=False,
                channel="websocket",
                to="direct",
                origin_metadata=self._meta(query, meta_schedule),
                project_id=self._project_id(query),
            )
            if not enabled:
                self.cron.enable_job(job.id, False)
        except ValueError as exc:
            return self._error_response(400, str(exc))
        self._sync_state()
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
        skill_name = _first(query, "skill_name")
        try:
            schedule, enabled, meta_schedule = _schedule_from_query(query)
            result = self.cron.update_job(
                job_id,
                name=name,
                schedule=schedule,
                message=_message(prompt, skill_name),
                origin_metadata=self._meta(query, meta_schedule),
                project_id=self._project_id(query),
                delete_after_run=False,
            )
        except ValueError as exc:
            return self._error_response(400, str(exc))
        if result == "not_found":
            return self._error_response(404, "task not found")
        if result == "protected":
            return self._error_response(403, "system task cannot be updated")
        self.cron.enable_job(job_id, enabled)
        self._sync_state()
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
        self._sync_state()
        return self._json_response(_payload(self.cron))

    def _enable(self, request: WsRequest, enabled: bool) -> Response:
        job_id = _first(self._query(request), "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        job = self.cron.enable_job(job_id, enabled)
        if job is None:
            return self._error_response(404, "task not found")
        self._sync_state()
        return self._json_response(_payload(self.cron))

    async def _run(self, request: WsRequest) -> Response:
        job_id = _first(self._query(request), "id").strip()
        if not job_id:
            return self._error_response(400, "id is required")
        if self.cron.get_job(job_id) is None:
            return self._error_response(404, "task not found")
        task = asyncio.create_task(self.cron.run_job(job_id, force=True))

        def _log_failure(done: asyncio.Task[bool]) -> None:
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                self.logger.opt(exception=exc).error("schedule task run failed")

        task.add_done_callback(_log_failure)
        await asyncio.sleep(0)
        self._sync_state()
        return self._json_response(_payload(self.cron))

    def _mark_run_viewed(self, request: WsRequest) -> Response:
        query = self._query(request)
        job_id = _first(query, "task_id").strip() or _first(query, "id").strip()
        run_id = _first(query, "run_id").strip()
        if not job_id or not run_id:
            return self._error_response(400, "task_id and run_id are required")
        job = self.cron.get_job(job_id)
        record = self.cron.get_run_record(job_id, run_id)
        if (
            job is not None
            and record is not None
            and record.status != "running"
            and job.schedule.kind == "at"
            and job.payload.result_type == "none"
        ):
            if self.cron.remove_job(job_id) != "removed":
                return self._error_response(404, "run not found")
            self._sync_state()
            return self._json_response(_payload(self.cron))
        if not self.cron.mark_run_viewed(job_id, run_id):
            return self._error_response(404, "run not found")
        return self._json_response(_payload(self.cron))

    def _delete_run(self, request: WsRequest) -> Response:
        query = self._query(request)
        job_id = _first(query, "task_id").strip() or _first(query, "id").strip()
        run_id = _first(query, "run_id").strip()
        if not job_id or not run_id:
            return self._error_response(400, "task_id and run_id are required")
        record = self.cron.get_run_record(job_id, run_id)
        if record is None:
            return self._error_response(404, "run not found")
        if record.status == "running":
            return self._error_response(409, "running record cannot be deleted")

        # Modern cron runs own a dedicated conversation. Permanently purge it
        # before removing the history pointer so a failed cleanup remains
        # visible and retryable. Legacy records without an explicit session
        # key are intentionally not mapped to the old shared cron session.
        session_key = (record.session_key or "").strip()
        if session_key and self._purge_session is not None:
            try:
                cleanup = self._purge_session(session_key)
            except StateStoreError as exc:
                return self._error_response(409, str(exc))
            except Exception as exc:
                self.logger.opt(exception=exc).error(
                    "schedule run conversation purge failed job={} run={}",
                    job_id,
                    run_id,
                )
                return self._error_response(
                    500,
                    "run conversation could not be permanently deleted",
                )
            if cleanup.get("cleanup_pending"):
                self.logger.warning(
                    "schedule run conversation cleanup pending job={} run={} errors={}",
                    job_id,
                    run_id,
                    cleanup.get("cleanup_errors", []),
                )

        result = self.cron.delete_run(job_id, run_id)
        if result == "not_found":
            return self._error_response(404, "run not found")
        if result == "running":
            return self._error_response(409, "running record cannot be deleted")
        return self._json_response(_payload(self.cron))

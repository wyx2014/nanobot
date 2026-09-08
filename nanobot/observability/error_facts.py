"""Bounded technical exception facts; never retain messages, bodies or local variables."""

from __future__ import annotations

import errno
import re
import traceback
from typing import Any


def symbol(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.$<>:-]{1,160}", value) else None


def error_category(code: str | None, status: int | None = None) -> str:
    key = (code or "").upper()
    groups = {
        "network.dns": {"EAI_AGAIN", "EAI_NONAME", "GAIERROR", "ENOTFOUND"},
        "network.tls": {"SSLERROR", "SSLCERTVERIFICATIONERROR", "CERTIFICATEERROR"},
        "network.timeout": {"TIMEOUT", "TIMEOUTERROR", "CONNECTTIMEOUT", "READTIMEOUT", "APITIMEOUTERROR", "ETIMEDOUT"},
        "network.proxy": {"PROXYERROR", "PROXY_CONNECTION_FAILED"},
        "network.connection": {"CONNECTION", "ECONNREFUSED", "ECONNRESET", "EPIPE", "CONNECTERROR", "APICONNECTIONERROR"},
        "resource.missing": {"ENOENT", "FILENOTFOUNDERROR", "MODULENOTFOUNDERROR"},
        "storage.permission": {"EACCES", "EPERM", "PERMISSIONERROR"},
        "storage.full": {"ENOSPC"},
        "storage.busy": {"SQLITE_BUSY", "SQLITE_LOCKED"},
        "provider.quota": {"INSUFFICIENT_QUOTA"},
        "provider.model_missing": {"MODEL_NOT_FOUND"},
        "provider.context_limit": {"CONTEXT_LENGTH_EXCEEDED"},
        "policy.denied": {"POLICY_DENIED", "NETWORK_DENIED", "TOOLBOUNDARYBLOCKED"},
        "cancelled": {"CANCELLEDERROR"},
        "auth.rejected": {"HTTP_401", "AUTHENTICATIONERROR", "INVALID_API_KEY", "TOKEN_EXPIRED"},
    }
    for category, codes in groups.items():
        if key in codes:
            return category
    if status == 401:
        return "auth.rejected"
    if status == 403:
        return "http.forbidden"
    if status == 429 or key in {"RATE_LIMIT", "RATELIMITERROR"}:
        return "http.rate_limit"
    if isinstance(status, int) and 500 <= status <= 599:
        return "http.server"
    if isinstance(status, int) and 400 <= status <= 499:
        return "http.client"
    return "unknown"


def safe_error_facts(exc: BaseException) -> dict[str, Any]:
    chain = []
    seen: set[int] = set()
    current: BaseException | None = exc
    request_id = None
    while current is not None and len(chain) < 5 and id(current) not in seen:
        seen.add(id(current))
        kind = symbol(type(current).__name__) or "Error"
        if request_id is None:
            try:
                request_id = symbol(getattr(current, "request_id", None))
                headers = getattr(getattr(current, "response", None), "headers", None)
                if request_id is None and headers is not None:
                    request_id = symbol(headers.get("x-request-id")) or symbol(headers.get("x-oai-request-id"))
            except Exception:
                pass
        code = symbol(getattr(current, "code", None)) or symbol(getattr(current, "sqlite_errorname", None))
        if not code and isinstance(current, OSError):
            code = errno.errorcode.get(current.errno)
        status = getattr(current, "status_code", None)
        item: dict[str, Any] = {"error_type": kind}
        if code:
            item["error_code"] = code
        if isinstance(status, int) and 100 <= status <= 599:
            item["status_code"] = status
        item["error_category"] = error_category(code or kind, item.get("status_code"))
        chain.append(item)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    frames = [{"module": symbol(frame.filename.replace("\\", "/").rsplit("/", 1)[-1]) or "<module>",
               "function": symbol(frame.name) or "<function>", "line": frame.lineno}
              for frame in traceback.extract_tb(exc.__traceback__, limit=12)]
    classified = next((item["error_category"] for item in reversed(chain)
                       if item["error_category"] != "unknown"), "unknown")
    return {**chain[0], "error_category": classified, "cause_chain": chain, "stack_frames": frames,
            **({"provider_request_id": request_id} if request_id else {})}


def technical_array(key: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    frames = key == "stack_frames"
    result = []
    for item in value[:12 if frames else 5]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for field in (["module", "function"] if frames else ["error_type", "error_code", "error_category"]):
            if text := symbol(item.get(field)):
                row[field] = text
        for field in (["line", "column"] if frames else ["status_code"]):
            number = item.get(field)
            if isinstance(number, int) and 0 <= number < 2**53:
                row[field] = number
        result.append(row)
    return result


def incident_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in ("captured_at", "context_captured_at"):
        stamp = value.get(key)
        if isinstance(stamp, str) and re.fullmatch(r"[0-9T:Z+.-]{1,35}", stamp):
            output[key] = stamp
    for key in ("connection_status", "session_id", "turn_id",
                "trace_id", "runtime_epoch", "turn_status", "stage"):
        if text := symbol(value.get(key)):
            output[key] = text
    for key in ("queue_depth", "dropped", "write_failures", "last_event_seq", "active_operation_count"):
        item = value.get(key)
        if isinstance(item, int) and 0 <= item < 2**53:
            output[key] = item
    return output

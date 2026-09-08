"""Privacy boundary and execution metadata for security audit records."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_LIMIT = 2_000
_VALUE = r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s;&|]+)'''
_SECRET = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|client[_-]?secret|aws_access_key_id|aws_secret_access_key|aws_session_token|private_key)"
_HTTP_AUDIT: ContextVar[dict[str, Any] | None] = ContextVar("security_http_audit", default=None)


@contextmanager
def capture_http_audit():
    activity: dict[str, Any] = {"request_count": 0, "requests": []}
    token = _HTTP_AUDIT.set(activity)
    try:
        yield activity
    finally:
        _HTTP_AUDIT.reset(token)


def audit_http_hooks() -> dict[str, list[Any]]:
    async def on_request(request: Any) -> None:
        activity = _HTTP_AUDIT.get()
        if activity is None:
            return
        activity["request_count"] += 1
        if len(activity["requests"]) < 20:
            item = {"url": redact_security_text(str(request.url)), "method": request.method}
            activity["requests"].append(item)
            request.extensions["nanobot_audit"] = item

    async def on_response(response: Any) -> None:
        item = response.request.extensions.get("nanobot_audit")
        if isinstance(item, dict):
            item["status_code"] = response.status_code

    return {"request": [on_request], "response": [on_response]}


def audit_url(value: str) -> str:
    """Keep a request target, never URL credentials, query values or fragments."""
    try:
        parsed = urlsplit(value)
        if not parsed.hostname:
            return "[UNKNOWN DESTINATION]"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([(key, "[REDACTED]") for key, _ in parse_qsl(parsed.query)])
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "[INVALID URL]"


def redact_security_text(value: str) -> str:
    text = str(value)
    # Sanitize the full input before truncation, including incomplete exported JWTs.
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", "[REDACTED PRIVATE KEY]", text)
    text = re.sub(r"https?://[^\s\"'`;|<>]+", lambda m: audit_url(m[0]), text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s\"';&|]+", "[REDACTED AUTH]", text)
    text = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+", r"\1: [REDACTED]", text)
    text = re.sub(rf"(?i)(\b{_SECRET}\b[\"']?\s*[:=]\s*){_VALUE}", r"\1[REDACTED]", text)
    text = re.sub(rf"(?i)(--?{_SECRET}(?:\s+|=)){_VALUE}", r"\1[REDACTED]", text)
    text = re.sub(rf"(?i)((?:--user|-u)(?:\s+|=)){_VALUE}", r"\1[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,2}", "[REDACTED JWT]", text)
    text = re.sub(r"\b(?:sk[-_]|pk_|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(rf"(?i)((?:--(?:data(?:-raw|-binary|-urlencode)?|json|body|prompt|content)|-d)\s+){_VALUE}", r"\1[PAYLOAD OMITTED]", text)
    text = re.sub(r"<<-?\s*['\"]?\w+['\"]?[^\n]*\n[\s\S]*", "[HEREDOC OMITTED]", text)
    return text if len(text) <= _LIMIT else text[:_LIMIT] + "..."


def redact_security_details(value: Any, *, key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in ("password", "passwd", "token", "secret", "api_key", "apikey", "authorization", "cookie", "private_key", "access_key")):
        return "[REDACTED]"
    if normalized in {"arguments", "params", "payload", "content", "body", "prompt", "messages", "reasoning", "query", "search", "stdin", "stdout", "stderr", "output", "error"}:
        return "[CONTENT OMITTED]"
    if isinstance(value, dict):
        return {str(k): redact_security_details(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_security_details(item, key=key) for item in value[:100]]
    if isinstance(value, str):
        return redact_security_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_security_text(str(value))


class AuditedToolResult(str):
    """Keep the tool's text protocol while exposing trusted execution metadata."""

    def __new__(cls, value: str, *, result: str, **details: Any):
        instance = super().__new__(cls, value)
        instance.audit_result = result
        instance.audit_details = details
        return instance

    def __getnewargs_ex__(self):
        return (str(self),), {"result": self.audit_result, **self.audit_details}


def tool_audit_outcome(tool_name: str, value: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(value, AuditedToolResult):
        return value.audit_result, value.audit_details
    if isinstance(value, str) and value.startswith("Error"):
        if any(marker in value.lower() for marker in (
            "blocked by", "path outside", "outside allowed", "outside the configured workspace",
        )):
            return "blocked", {"error_type": "ToolBoundaryBlocked"}
        return "failed", {"error_type": "ToolError"}
    if tool_name == "web_fetch" and isinstance(value, str):
        try:
            payload = json.loads(value)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("error"):
                blocked = "blocked" in str(payload["error"]).lower()
                return "blocked" if blocked else "failed", {"error_type": "WebFetchError"}
            if isinstance(payload.get("status"), int):
                return "failed" if payload["status"] >= 400 else "succeeded", {"status_code": payload["status"]}
    if tool_name.startswith("mcp_") and isinstance(value, str):
        match = re.match(r"^\(MCP [^\n:)]*?(failed|timed out|cancelled|blocked)", value)
        if match:
            return {"failed": "failed", "timed out": "timed_out", "cancelled": "cancelled", "blocked": "blocked"}[match[1]], {"error_type": "MCPToolError"}
    return "succeeded", {}

"""Local, read-only support checks. No model calls, network probes or process launches."""

from __future__ import annotations

import os
import shutil
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def connectivity_facts(environment: dict[str, str]) -> dict[str, Any]:
    proxies = []
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if key not in environment:
            continue
        raw = environment[key]
        row: dict[str, Any] = {"variable": key, "configured": bool(raw.strip())}
        try:
            parsed = urlsplit(raw if "://" in raw else "http://" + raw)
            scheme = parsed.scheme if parsed.scheme in {"http", "https", "socks5", "socks5h", "socks4"} else "unknown"
            row.update(scheme=scheme, valid=bool(parsed.hostname) and scheme != "unknown",
                       authentication_present=bool(parsed.username or parsed.password),
                       port=parsed.port, loopback=parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        except ValueError:
            row["valid"] = False
        proxies.append(row)
    return {"proxies": proxies, "no_proxy_configured": bool(environment.get("NO_PROXY") or environment.get("no_proxy")),
            "certificate_overrides": {key: bool(environment.get(key)) for key in
                ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")}}


def collect_doctor(log_path: Path, ready: bool | None, mcp_status: str, presentations: Any = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append({"id": "runtime.python", "status": "ok" if sys.version_info >= (3, 11) else "fail",
                   "version": sys.version.split()[0]})
    checks.append({"id": "runtime.gateway", "status": "ok" if ready else "unknown" if ready is None else "fail"})
    checks.append({"id": "runtime.mcp", "status": "ok" if mcp_status in {"ready", "disabled"}
                   else "warning" if mcp_status == "unavailable" else "unknown", "state": mcp_status})
    try:
        free = shutil.disk_usage(log_path.parent).free
        readable = os.access(log_path, os.R_OK)
        writable = os.access(log_path.parent, os.W_OK)
        checks.append({"id": "storage.logs", "status": "warning" if free < 256 * 1024 * 1024 or not readable or not writable else "ok",
                       "free_bytes": free, "readable": readable, "directory_writable_hint": writable})
    except OSError:
        checks.append({"id": "storage.logs", "status": "unknown"})
    for package in ("nanobot-ai", "python-pptx", "Pillow"):
        try:
            checks.append({"id": "dependency." + package, "status": "ok", "version": metadata.version(package)})
        except metadata.PackageNotFoundError:
            checks.append({"id": "dependency." + package, "status": "warning", "reason": "NOT_INSTALLED"})
    if presentations is not None:
        try:
            for template in presentations.diagnostic_availability():
                checks.append({"id": "presentation." + template["id"],
                               "status": "ok" if template["available"] else "warning", "missing": template["missing"]})
        except Exception:
            checks.append({"id": "presentation.resources", "status": "unknown", "reason": "CHECK_FAILED"})
    connectivity = connectivity_facts(dict(os.environ))
    checks.append({"id": "network.configuration", "status": "warning" if any(not row.get("valid")
                   for row in connectivity["proxies"]) else "ok", **connectivity})
    return {"schema_version": 1, "captured_at": int(time.time() * 1000), "mode": "local_read_only",
            "checks": checks, "not_checked": ["remote_reachability", "credential_validity", "font_rendering"],
            "overall_status": "fail" if any(row["status"] == "fail" for row in checks)
            else "warning" if any(row["status"] in {"warning", "unknown"} for row in checks) else "ok"}

"""Best-effort diagnostics for Windows atomic file replacement failures."""

from __future__ import annotations

import ctypes
import locale
import os
import stat
import subprocess
import sys
import threading
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

_ERROR_SUCCESS = 0
_ERROR_MORE_DATA = 234
_CCH_RM_MAX_APP_NAME = 255
_CCH_RM_MAX_SVC_NAME = 63
_CCH_RM_SESSION_KEY = 32
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_ATTRIBUTE_NAMES = {
    0x0001: "readonly",
    0x0002: "hidden",
    0x0004: "system",
    0x0010: "directory",
    0x0020: "archive",
    0x0400: "reparse_point",
    0x0800: "compressed",
    0x1000: "offline",
    0x2000: "not_content_indexed",
    0x4000: "encrypted",
}
_RM_APP_TYPES = {
    0: "unknown",
    1: "main_window",
    2: "other_window",
    3: "service",
    4: "explorer",
    5: "console",
    1000: "critical",
}
_COMMAND_OUTPUT_LIMIT = 12_000


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("low", wintypes.DWORD),
        ("high", wintypes.DWORD),
    ]


class _RmUniqueProcess(ctypes.Structure):
    _fields_ = [
        ("process_id", wintypes.DWORD),
        ("process_start_time", _FileTime),
    ]


class _RmProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", _RmUniqueProcess),
        ("app_name", wintypes.WCHAR * (_CCH_RM_MAX_APP_NAME + 1)),
        ("service_short_name", wintypes.WCHAR * (_CCH_RM_MAX_SVC_NAME + 1)),
        ("application_type", wintypes.UINT),
        ("app_status", wintypes.ULONG),
        ("terminal_session_id", wintypes.DWORD),
        ("restartable", wintypes.BOOL),
    ]


def _bounded(text: str, limit: int = _COMMAND_OUTPUT_LIMIT) -> str:
    value = text.strip()
    return value if len(value) <= limit else value[:limit] + "…[truncated]"


def _decode_output(payload: bytes) -> str:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return payload.decode(encoding, errors="replace")


def _run_read_only_command(argv: list[str], *, timeout_s: float = 3.0) -> dict[str, Any]:
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=timeout_s,
            creationflags=creationflags,
        )
        return {
            "status": "ok",
            "returncode": completed.returncode,
            "stdout": _bounded(_decode_output(completed.stdout)),
            "stderr": _bounded(_decode_output(completed.stderr)),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _path_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "lexists": os.path.lexists(path),
    }
    try:
        info = path.stat()
        snapshot.update(
            {
                "is_file": stat.S_ISREG(info.st_mode),
                "is_dir": stat.S_ISDIR(info.st_mode),
                "mode": oct(stat.S_IMODE(info.st_mode)),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "writable": os.access(path, os.W_OK),
            }
        )
    except OSError as exc:
        snapshot["stat_error"] = f"{type(exc).__name__}: {exc}"
    return snapshot


def _windows_file_attributes(path: Path) -> dict[str, Any]:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attributes = kernel32.GetFileAttributesW
        get_attributes.argtypes = [wintypes.LPCWSTR]
        get_attributes.restype = wintypes.DWORD
        value = int(get_attributes(str(path)))
        if value == _INVALID_FILE_ATTRIBUTES:
            return {"status": "unavailable", "last_error": ctypes.get_last_error()}
        return {
            "status": "ok",
            "value": value,
            "flags": [name for mask, name in _FILE_ATTRIBUTE_NAMES.items() if value & mask],
        }
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}


def _restart_manager_lockers(paths: list[Path]) -> dict[str, Any]:
    existing = [str(path.resolve()) for path in paths if path.exists()]
    if not existing:
        return {"status": "no_existing_resources", "lockers": []}

    session_handle = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
    try:
        restart_manager = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
        start_session = restart_manager.RmStartSession
        start_session.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
        start_session.restype = wintypes.DWORD
        register_resources = restart_manager.RmRegisterResources
        register_resources.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.c_void_p,
        ]
        register_resources.restype = wintypes.DWORD
        get_list = restart_manager.RmGetList
        get_list.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(_RmProcessInfo),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_list.restype = wintypes.DWORD
        end_session = restart_manager.RmEndSession
        end_session.argtypes = [wintypes.DWORD]
        end_session.restype = wintypes.DWORD

        result = int(start_session(ctypes.byref(session_handle), 0, session_key))
        if result != _ERROR_SUCCESS:
            return {"status": "start_failed", "return_code": result, "lockers": []}
        try:
            resource_array = (wintypes.LPCWSTR * len(existing))(*existing)
            result = int(
                register_resources(
                    session_handle,
                    len(existing),
                    resource_array,
                    0,
                    None,
                    0,
                    None,
                )
            )
            if result != _ERROR_SUCCESS:
                return {
                    "status": "register_failed",
                    "return_code": result,
                    "resources": existing,
                    "lockers": [],
                }

            needed = wintypes.UINT(0)
            count = wintypes.UINT(0)
            reboot_reasons = wintypes.DWORD(0)
            result = int(
                get_list(
                    session_handle,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reboot_reasons),
                )
            )
            if result == _ERROR_SUCCESS and needed.value == 0:
                return {
                    "status": "ok",
                    "resources": existing,
                    "reboot_reasons": reboot_reasons.value,
                    "lockers": [],
                }
            if result != _ERROR_MORE_DATA:
                return {
                    "status": "list_size_failed",
                    "return_code": result,
                    "resources": existing,
                    "lockers": [],
                }

            process_info = (_RmProcessInfo * needed.value)()
            count.value = needed.value
            result = int(
                get_list(
                    session_handle,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    process_info,
                    ctypes.byref(reboot_reasons),
                )
            )
            if result != _ERROR_SUCCESS:
                return {
                    "status": "list_failed",
                    "return_code": result,
                    "resources": existing,
                    "lockers": [],
                }
            lockers = []
            for item in process_info[: count.value]:
                lockers.append(
                    {
                        "pid": int(item.process.process_id),
                        "app_name": item.app_name,
                        "service_short_name": item.service_short_name,
                        "application_type": _RM_APP_TYPES.get(
                            int(item.application_type), str(int(item.application_type))
                        ),
                        "app_status": int(item.app_status),
                        "terminal_session_id": int(item.terminal_session_id),
                        "restartable": bool(item.restartable),
                    }
                )
            return {
                "status": "ok",
                "resources": existing,
                "reboot_reasons": reboot_reasons.value,
                "lockers": lockers,
            }
        finally:
            end_session(session_handle)
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}


def _windows_acl_snapshot(path: Path) -> dict[str, Any]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "icacls.exe"
    result = _run_read_only_command([str(executable), str(path)])
    result["queried_path"] = str(path)
    return result


def _recent_defender_events() -> dict[str, Any]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "wevtutil.exe"
    query = (
        "*[System[((EventID=1123 or EventID=1124 or EventID=1127 or EventID=1128) "
        "and TimeCreated[timediff(@SystemTime) <= 120000])]]"
    )
    result = _run_read_only_command(
        [
            str(executable),
            "qe",
            "Microsoft-Windows-Windows Defender/Operational",
            f"/q:{query}",
            "/f:text",
            "/rd:true",
            "/c:8",
        ],
        timeout_s=4.0,
    )
    result["channel"] = "Microsoft-Windows-Windows Defender/Operational"
    result["event_ids"] = [1123, 1124, 1127, 1128]
    result["lookback_seconds"] = 120
    return result


def collect_atomic_replace_diagnostics(
    source: Path,
    target: Path,
    error: BaseException,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect bounded evidence without masking the original replace error."""
    diagnostic: dict[str, Any] = {
        "event": "session_atomic_replace_failed",
        "captured_at": datetime.now().astimezone().isoformat(),
        "platform": sys.platform,
        "process": {
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "thread_name": threading.current_thread().name,
            "executable": sys.executable,
        },
        "error": {
            "type": type(error).__name__,
            "errno": getattr(error, "errno", None),
            "winerror": getattr(error, "winerror", None),
            "strerror": getattr(error, "strerror", None),
            "message": str(error),
        },
        "source": _path_snapshot(source),
        "target": _path_snapshot(target),
        "parent": _path_snapshot(target.parent),
        "context": context or {},
    }
    if sys.platform != "win32":
        return diagnostic

    diagnostic["windows"] = {
        "source_attributes": _windows_file_attributes(source),
        "target_attributes": _windows_file_attributes(target),
        "parent_attributes": _windows_file_attributes(target.parent),
        "restart_manager": _restart_manager_lockers([source, target]),
        "target_acl": _windows_acl_snapshot(target if target.exists() else target.parent),
        "recent_defender_events": _recent_defender_events(),
    }
    return diagnostic

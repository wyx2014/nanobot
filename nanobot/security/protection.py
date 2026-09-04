"""Full-access safety policy, approvals, and audit coordination."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

from nanobot.security.project_context import current_project_context
from nanobot.storage.logs import StructuredLogStore

SecurityDecision = Literal["allow", "require_approval", "block"]
SecurityRisk = Literal["normal", "sensitive", "high", "critical"]
ApprovalCallback = Callable[[dict[str, Any]], Awaitable[None]]

_POLICY_FILE = "security-policy.json"
_EMERGENCY_AUDIT_FILE = "security-emergency.jsonl"
_MAX_COMMAND_PREVIEW = 2_000
_MAX_POLICY_ITEMS = 200
_MAX_POLICY_VALUE = 500
_RETENTION_DAYS = 3 * 365
_RETENTION_MS = _RETENTION_DAYS * 24 * 60 * 60 * 1_000
_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


class SecurityPolicyError(ValueError):
    pass


class AuditUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SecurityAssessment:
    decision: SecurityDecision
    risk: SecurityRisk
    category: str
    action: str
    rule_id: str
    summary: str
    target: str | None = None
    audit_required: bool = True
    mutating: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def grant_key(self) -> str:
        return f"{self.rule_id}:{self.action}"


@dataclass
class AuditHandle:
    event_id: int | None
    started_at: float
    emergency: bool = False
    fallback_payload: dict[str, Any] = field(default_factory=dict)


def _canonical(path: str | Path, workspace: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and workspace is not None:
        value = workspace / value
    return value.resolve(strict=False)


def _compare(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _within(path: Path, root: Path) -> bool:
    candidate = _compare(path)
    base = _compare(root)
    return candidate == base or candidate.startswith(base + os.sep)


def redact_security_text(value: str) -> str:
    """Redact common credentials embedded in otherwise unstructured commands."""
    text = str(value)
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)\S+", r"\1[REDACTED]"),
        (r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)\s*=\s*([^\s;&|]+)", r"\1=[REDACTED]"),
        (r"(?i)(https?://[^\s:/]+:)[^@\s/]+@", r"\1[REDACTED]@"),
        (r"\b(?:sk|pk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    if len(text) > _MAX_COMMAND_PREVIEW:
        text = text[:_MAX_COMMAND_PREVIEW] + "..."
    return text


def redact_security_details(value: Any, *, key: str = "") -> Any:
    """Keep audit metadata useful without persisting credentials or large payloads."""
    normalized_key = key.lower().replace("-", "_")
    if any(marker in normalized_key for marker in ("password", "passwd", "token", "secret", "api_key", "authorization")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key): redact_security_details(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_security_details(item, key=key) for item in value[:100]]
    if isinstance(value, str):
        return redact_security_text(value[:_MAX_COMMAND_PREVIEW])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_security_text(str(value))


class SecurityPolicyStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.path = self.root / ".nanobot" / _POLICY_FILE
        self._lock = threading.RLock()

    @staticmethod
    def _default_approval_candidates(
        home: Path,
        *,
        platform_name: str,
        environ: Mapping[str, str],
    ) -> list[Path]:
        candidates = [home / ".ssh"]
        if platform_name == "win32":
            roaming = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
            local = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
            candidates.extend((
                roaming / "gnupg",
                roaming / "Microsoft" / "Credentials",
                local / "Microsoft" / "Credentials",
                roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup",
            ))
        elif platform_name == "darwin":
            candidates.extend((
                home / ".gnupg",
                home / "Library" / "Keychains",
                home / "Library" / "LaunchAgents",
            ))
        else:
            candidates.extend((
                home / ".gnupg",
                home / ".config" / "autostart",
                home / ".config" / "systemd" / "user",
            ))
        return candidates

    def _default_approval_paths(self) -> list[str]:
        candidates = self._default_approval_candidates(
            Path.home(),
            platform_name=sys.platform,
            environ=os.environ,
        )
        output: list[str] = []
        for path in candidates:
            value = str(path.resolve(strict=False))
            if value not in output:
                output.append(value)
        return output

    def _with_default_approval_paths(self, paths: list[str]) -> list[str]:
        output = self._default_approval_paths()
        output.extend(path for path in paths if path not in output)
        return output

    def defaults(self) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "file_allow_paths": [],
            "approval_paths": self._default_approval_paths(),
            "command_allow_prefixes": [],
            "command_approval_prefixes": [],
            "network_block_all": False,
            "network_allow_domains": [],
            "network_deny_domains": [],
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return self.defaults()
        if not isinstance(raw, dict):
            return self.defaults()
        try:
            normalized = {
                "schema_version": 3,
                # Version 1 exposed trusted paths. They are intentionally
                # ignored so old default exceptions cannot silently return.
                "file_allow_paths": self._normalize_list(raw.get("file_allow_paths")),
                "approval_paths": self._with_default_approval_paths(
                    self._normalize_list(raw.get("approval_paths"))
                ),
                "command_allow_prefixes": self._normalize_text_list(
                    raw.get("command_allow_prefixes"), label="command prefixes"
                ),
                "command_approval_prefixes": self._normalize_text_list(
                    raw.get("command_approval_prefixes"), label="command prefixes"
                ),
                "network_block_all": self._normalize_bool(
                    raw.get("network_block_all", False), label="network_block_all"
                ),
                "network_allow_domains": self._normalize_domains(
                    raw.get("network_allow_domains")
                ),
                "network_deny_domains": self._normalize_domains(
                    raw.get("network_deny_domains")
                ),
            }
            self._validate_allow_paths(normalized["file_allow_paths"])
            self._validate_path_overlaps(
                normalized["file_allow_paths"], normalized["approval_paths"]
            )
            return normalized
        except SecurityPolicyError:
            return self.defaults()

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        file_allow = current["file_allow_paths"]
        if "file_allow_paths" in payload:
            file_allow = self._normalize_list(payload.get("file_allow_paths"))
        approval = current["approval_paths"]
        if "approval_paths" in payload:
            approval = self._with_default_approval_paths(
                self._normalize_list(payload.get("approval_paths"))
            )
        normalized = {
            "schema_version": 3,
            "file_allow_paths": file_allow,
            "approval_paths": approval,
            "command_allow_prefixes": (
                self._normalize_text_list(
                    payload.get("command_allow_prefixes"), label="command prefixes"
                )
                if "command_allow_prefixes" in payload
                else current["command_allow_prefixes"]
            ),
            "command_approval_prefixes": (
                self._normalize_text_list(
                    payload.get("command_approval_prefixes"), label="command prefixes"
                )
                if "command_approval_prefixes" in payload
                else current["command_approval_prefixes"]
            ),
            "network_block_all": (
                self._normalize_bool(
                    payload.get("network_block_all"), label="network_block_all"
                )
                if "network_block_all" in payload
                else current["network_block_all"]
            ),
            "network_allow_domains": (
                self._normalize_domains(payload.get("network_allow_domains"))
                if "network_allow_domains" in payload
                else current["network_allow_domains"]
            ),
            "network_deny_domains": (
                self._normalize_domains(payload.get("network_deny_domains"))
                if "network_deny_domains" in payload
                else current["network_deny_domains"]
            ),
        }
        self._validate_allow_paths(file_allow)
        self._validate_path_overlaps(file_allow, approval)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with self._lock:
            temp.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        return normalized

    def reset(self) -> dict[str, Any]:
        return self.save(self.defaults())

    def payload(self) -> dict[str, Any]:
        policy = self.load()
        defaults = set(self._default_approval_paths())
        return {
            "protection_enabled": True,
            "enforcement_level": "application",
            "access_mode": "full",
            "core_protection_locked": True,
            "file_allow_paths": [
                {"path": path, "source": "user"}
                for path in policy["file_allow_paths"]
            ],
            "approval_paths": [
                {"path": path, "source": "default" if path in defaults else "user"}
                for path in policy["approval_paths"]
            ],
            "command_allow_prefixes": policy["command_allow_prefixes"],
            "command_approval_prefixes": policy["command_approval_prefixes"],
            "network_block_all": policy["network_block_all"],
            "network_allow_domains": policy["network_allow_domains"],
            "network_deny_domains": policy["network_deny_domains"],
            "components": {
                "file": {"enabled": True, "configurable": True},
                "command": {"enabled": True, "configurable": True},
                "network": {"enabled": True, "configurable": True},
                "audit": {
                    "enabled": True,
                    "retention_days": _RETENTION_DAYS,
                    "max_records": 100_000,
                },
            },
            "core_rules": [
                {"id": "core.disk_destroy", "label": "磁盘与分区破坏", "locked": True},
                {"id": "core.root_delete", "label": "根目录与用户目录清空", "locked": True},
                {"id": "core.boot_security", "label": "启动与安全机制破坏", "locked": True},
                {"id": "core.audit_tamper", "label": "安全配置与审计篡改", "locked": True},
            ],
        }

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise SecurityPolicyError("paths must be an array")
        output: list[str] = []
        for item in value:
            raw = item.get("path") if isinstance(item, dict) else item
            if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
                raise SecurityPolicyError("path must be a non-empty string")
            path = str(_canonical(raw))
            if path not in output:
                output.append(path)
        return output

    @staticmethod
    def _normalize_text_list(value: Any, *, label: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise SecurityPolicyError(f"{label} must be an array")
        if len(value) > _MAX_POLICY_ITEMS:
            raise SecurityPolicyError(f"too many {label}")
        output: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or "\0" in item:
                raise SecurityPolicyError(f"{label} must contain non-empty strings")
            normalized = re.sub(r"\s+", " ", item).strip()
            if len(normalized) > _MAX_POLICY_VALUE:
                raise SecurityPolicyError(f"{label} entry is too long")
            if normalized not in output:
                output.append(normalized)
        return output

    @classmethod
    def _normalize_domains(cls, value: Any) -> list[str]:
        entries = cls._normalize_text_list(value, label="domains")
        output: list[str] = []
        for entry in entries:
            candidate = entry.strip().lower().rstrip(".")
            wildcard = candidate.startswith("*.")
            if wildcard:
                candidate = candidate[2:]
            parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
            hostname = (parsed.hostname or "").rstrip(".")
            if not hostname or any(char.isspace() for char in hostname):
                raise SecurityPolicyError(f"invalid domain: {entry}")
            try:
                hostname = hostname.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise SecurityPolicyError(f"invalid domain: {entry}") from exc
            normalized = f"*.{hostname}" if wildcard else hostname
            if normalized not in output:
                output.append(normalized)
        return output

    @staticmethod
    def _normalize_bool(value: Any, *, label: str) -> bool:
        if not isinstance(value, bool):
            raise SecurityPolicyError(f"{label} must be a boolean")
        return value

    def _validate_allow_paths(self, paths: list[str]) -> None:
        home = Path.home().resolve(strict=False)
        forbidden_exact = [home]
        forbidden_trees = [self.root / ".nanobot"]
        if os.name == "nt":
            forbidden_exact.extend(Path(f"{drive}:\\") for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            forbidden_trees.extend(
                Path(value)
                for value in (
                    os.environ.get("SYSTEMROOT"),
                    os.environ.get("ProgramFiles"),
                    os.environ.get("ProgramFiles(x86)"),
                )
                if value
            )
        else:
            forbidden_exact.append(Path("/"))
            forbidden_trees.extend(
                Path(value) for value in ("/etc", "/usr", "/bin", "/sbin", "/System", "/Library")
            )
        for raw in paths:
            path = _canonical(raw)
            if any(_compare(path) == _compare(item.resolve(strict=False)) for item in forbidden_exact):
                raise SecurityPolicyError(f"path is too broad for automatic allow: {path}")
            if any(_within(path, item.resolve(strict=False)) for item in forbidden_trees):
                raise SecurityPolicyError(f"path is protected from automatic allow: {path}")

    @staticmethod
    def _validate_path_overlaps(file_allow: list[str], approval: list[str]) -> None:
        for allow_raw in file_allow:
            allow_path = _canonical(allow_raw)
            for approval_raw in approval:
                approval_path = _canonical(approval_raw)
                if _within(allow_path, approval_path) or _within(approval_path, allow_path):
                    raise SecurityPolicyError(
                        f"automatic allow and approval paths cannot overlap: "
                        f"{allow_path} / {approval_path}"
                    )

class SecurityApprovalBroker:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[asyncio.Future[str], str | None]] = {}

    async def request(
        self,
        payload: dict[str, Any],
        callback: ApprovalCallback,
        *,
        chat_id: str | None,
        timeout_s: float = 300,
    ) -> str:
        approval_id = str(payload["approval_id"])
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[approval_id] = (future, chat_id)
        await callback(payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            return "deny"
        finally:
            self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, decision: str, *, chat_id: str | None) -> bool:
        pending = self._pending.get(approval_id)
        if pending is None:
            return False
        future, expected_chat = pending
        if expected_chat and chat_id != expected_chat:
            return False
        if future.done():
            return False
        future.set_result("allow_turn" if decision == "allow_turn" else "deny")
        return True

    def cancel_chat(self, chat_id: str) -> None:
        for future, expected_chat in list(self._pending.values()):
            if expected_chat == chat_id and not future.done():
                future.set_result("deny")


_HARD_COMMAND_RULES: tuple[tuple[str, str, str], ...] = (
    ("core.disk_destroy", r"(?i)\b(mkfs(?:\.\w+)?|wipefs|diskpart|clear-disk|initialize-disk|format-volume|cipher\s+/w)\b", "禁止修改或擦除磁盘与分区"),
    ("core.disk_destroy", r"(?i)\bdd\b[^|;&]*\bof=(?:/dev/|\\\\\.\\PhysicalDrive)", "禁止直接写入磁盘设备"),
    ("core.disk_destroy", r"(?i)>\s*(?:/dev/(?:sd|nvme)|\\\\\.\\PhysicalDrive)", "禁止覆盖物理磁盘"),
    ("core.root_delete", r"(?i)\brm\b[^\n;&|]*\s-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[^\n;&|]*(?:\s/\s*$|\s/\*|\s~/?\s*$|\s~/\*)", "禁止清空根目录或用户主目录"),
    ("core.root_delete", r"(?i)\brm\b[^\n;&|]*\s-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[^\n;&|]*(?:\$HOME|\$\{HOME\})/?(?:\*|\s*$)", "禁止清空用户主目录"),
    ("core.root_delete", r"(?i)\bfind\s+(?:/|~|\$HOME|\$\{HOME\})\s+[^\n;&|]*-delete\b", "禁止递归清空根目录或用户主目录"),
    ("core.root_delete", r"(?i)\b(?:del|rmdir)\b[^\n;&|]*/[sqf][^\n;&|]*\s+[a-z]:[\\/]?(?:\*\.\*|\*)?\s*$", "禁止清空整个驱动器"),
    ("core.root_delete", r"(?i)\bremove-item\b(?=[^\n;&|]*(?:-recurse|-r\b))(?=[^\n;&|]*(?:-force|-fo\b))[^\n;&|]*(?:[a-z]:[\\/](?:\*|\*\.\*)?|\$env:userprofile[\\/]?(?:\*)?|%userprofile%[\\/]?(?:\*)?)", "禁止清空整个驱动器或用户目录"),
    ("core.boot_security", r"(?i)\b(?:bcdedit|shutdown|reboot|poweroff|stop-computer|restart-computer)\b", "禁止修改启动配置或关闭系统"),
    ("core.process_bomb", r":\s*\(\s*\)\s*\{[^}]*\}\s*;\s*:", "禁止执行进程炸弹"),
)

_HIGH_COMMAND_RULES: tuple[tuple[str, str, str], ...] = (
    ("command.recursive_delete", r"(?i)\b(?:rm\b[^\n;&|]*(?:\s-r|\s-R|--recursive)|del\b[^\n;&|]*/s|rmdir\b[^\n;&|]*/s|remove-item\b[^\n;&|]*(?:-recurse|-r\b)|find\b[^\n;&|]*-delete|xargs\b[^\n;&|]*\brm\b)", "递归或批量删除可能造成数据丢失"),
    ("command.destructive_git", r"(?i)\bgit\s+(?:reset\s+--hard|clean\s+[^\n;&|]*-f|push\s+[^\n;&|]*(?:--force|-f\b)|checkout\s+--\s*\.)", "破坏性 Git 操作可能丢失本地或远程历史"),
    ("command.remote_execute", r"(?i)(?:curl|wget|Invoke-WebRequest)[^\n;&|]*\|\s*(?:sh|bash|zsh|python|powershell|iex)\b", "下载后直接执行远程代码"),
    ("command.obfuscated", r"(?i)\b(?:powershell|pwsh)\b[^\n;&|]*(?:-enc\b|-encodedcommand\b|-executionpolicy\s+bypass)|\b(?:eval|Invoke-Expression|IEX|set-executionpolicy)\b", "动态或编码执行会隐藏真实操作"),
    ("command.privilege", r"(?i)(?:^|[;&|]\s*)\b(?:sudo|runas)\b|\b(?:chmod|chown|takeown|icacls)\b", "权限提升或权限修改需要确认"),
    ("command.system_config", r"(?i)\b(?:reg\s+(?:add|delete)|sc\s+(?:create|delete|stop)|net\s+stop|systemctl\s+(?:stop|disable|mask)|launchctl\s+(?:unload|remove)|schtasks\s+/delete|netsh\b[^\n;&|]*(?:firewall|advfirewall))", "系统服务、注册表或防火墙修改需要确认"),
    ("command.process_kill", r"(?i)\b(?:kill\s+-9|killall|pkill|taskkill\b[^\n;&|]*/f|stop-process\b[^\n;&|]*-force)\b", "强制终止进程需要确认"),
    ("command.irreversible_file", r"(?i)\b(?:truncate|shred)\b", "不可恢复的文件修改需要确认"),
    ("command.package_remove", r"(?i)\b(?:brew|pip|apt(?:-get)?|yum|dnf)\s+(?:uninstall|remove|purge)\b", "卸载软件包需要确认"),
)

_SENSITIVE_PARTS = (
    ".ssh/id_",
    ".aws/credentials",
    ".azure/",
    ".config/gcloud/",
    ".kube/config",
    ".gnupg/",
    ".git-credentials",
    "library/keychains/",
    "appdata/local/microsoft/credentials/",
    "appdata/roaming/microsoft/credentials/",
    "appdata/local/google/chrome/user data/default/login data",
    "appdata/local/microsoft/edge/user data/default/login data",
)


class SecurityService:
    def __init__(self, logs: StructuredLogStore, workspace: Path) -> None:
        self.logs = logs
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.policy = SecurityPolicyStore(self.workspace)
        self.approvals = SecurityApprovalBroker()
        self.emergency_path = logs.path.parent / _EMERGENCY_AUDIT_FILE
        state_path = self.workspace / ".nanobot" / "state.sqlite"
        self._protected_state = tuple(
            path.resolve(strict=False)
            for path in (
                logs.path,
                logs.path.with_name(logs.path.name + "-wal"),
                logs.path.with_name(logs.path.name + "-shm"),
                self.policy.path,
                self.policy.path.with_suffix(".tmp"),
                self.emergency_path,
                state_path,
                state_path.with_name(state_path.name + "-wal"),
                state_path.with_name(state_path.name + "-shm"),
            )
        )
        try:
            self.logs.prune_security_events(
                older_than_ms=(time.time_ns() // 1_000_000) - _RETENTION_MS,
                keep_latest=100_000,
            )
        except Exception:
            pass

    def policy_payload(self) -> dict[str, Any]:
        return self.policy.payload()

    def update_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        saved = self.policy.save(payload)
        self._record_setting_change("security.policy_updated", saved)
        return self.policy.payload()

    def reset_policy(self) -> dict[str, Any]:
        saved = self.policy.reset()
        self._record_setting_change("security.policy_reset", saved)
        return self.policy.payload()

    @contextmanager
    def network_context(self):
        from nanobot.security.network import reset_network_policy, set_network_policy

        policy = self.policy.load()
        token = set_network_policy(
            block_all=policy["network_block_all"],
            allow_domains=policy["network_allow_domains"],
            deny_domains=policy["network_deny_domains"],
        )
        try:
            yield
        finally:
            reset_network_policy(token)

    def _record_setting_change(self, rule_id: str, details: dict[str, Any]) -> None:
        handle = self.begin_audit(
            SecurityAssessment(
                decision="allow",
                risk="normal",
                category="settings",
                action="update",
                rule_id=rule_id,
                summary="安全防护设置已更新",
                details=details,
            ),
            tool_call_id=None,
            tool_name=None,
            session_key=None,
            turn_id=None,
        )
        self.complete_audit(handle, result="succeeded")

    def assess(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        tool: Any | None,
        workspace: Path | None,
    ) -> SecurityAssessment:
        root = (workspace or self.workspace).expanduser().resolve(strict=False)
        if tool_name == "exec":
            return self._assess_command(str(params.get("command") or params.get("cmd") or ""), params, root)
        network_assessment = self._assess_network_tool(tool_name, params)
        if network_assessment is not None:
            return network_assessment
        if tool_name == "write_stdin":
            session_id = str(params.get("session_id") or "").strip()
            return SecurityAssessment(
                "allow", "normal", "command", "write_stdin", "command.session_input",
                "向运行中的命令会话发送输入", target=session_id or None,
                audit_required=True, mutating=True,
            )
        if tool_name == "run_cli_app":
            target = str(params.get("name") or "")
            return SecurityAssessment(
                "allow", "normal", "command", "execute_cli", "command.cli_app",
                f"运行已安装的 CLI 应用 {target}", target=target, mutating=True,
                details={"arguments": params.get("args") or []},
            )
        if tool_name.startswith("mcp_"):
            read_only = bool(getattr(tool, "read_only", False))
            return SecurityAssessment(
                "allow", "normal", "mcp", "call", "mcp.tool_call",
                f"调用 MCP 工具 {tool_name}", target=tool_name,
                audit_required=True, mutating=not read_only,
                details={"arguments": params},
            )

        paths = self._tool_paths(tool_name, params, root)
        mutating = self._tool_mutates(tool_name, tool)
        if tool_name == "apply_patch" and params.get("dry_run") is True:
            mutating = False
        if mutating:
            for path in paths:
                if self._is_protected_state(path):
                    return SecurityAssessment(
                        "block", "critical", "file", "write", "core.audit_tamper",
                        "禁止工具直接修改 nanobot 安全、审计或状态数据库",
                        target=str(path), mutating=True,
                    )
            policy = self.policy.load()
            for path in paths:
                if self._matches(path, policy["approval_paths"]):
                    return SecurityAssessment(
                        "require_approval", "high", "file", "write", "file.protected_path",
                        "目标位于用户设置的强制审批路径", target=str(path), mutating=True,
                    )
                if self._is_system_write_path(path):
                    return SecurityAssessment(
                        "require_approval", "high", "file", "write", "file.system_path",
                        "修改系统目录需要确认", target=str(path), mutating=True,
                    )
            if paths and all(
                self._matches(path, policy["file_allow_paths"]) for path in paths
            ):
                return SecurityAssessment(
                    "allow", "normal", "file", self._file_action(tool_name),
                    "file.user_allowed_path", "目标命中用户设置的自动放行白名单",
                    target=", ".join(str(path) for path in paths[:3]), mutating=True,
                    details={"paths": [str(path) for path in paths]},
                )
            target = ", ".join(str(path) for path in paths[:3]) or None
            action = self._file_action(tool_name)
            summaries = {
                "write": "通过文件工具写入文件",
                "edit": "通过文件工具编辑文件",
                "patch": "通过补丁工具编辑文件",
                "create": "通过文件工具创建文件",
            }
            return SecurityAssessment(
                "allow", "normal", "file", action, "file.normal_write",
                summaries.get(action, "文件操作已通过安全检查"),
                target=target, mutating=True,
                details={"paths": [str(path) for path in paths]},
            )

        if tool_name == "read_file" and paths:
            sensitive = any(self._is_sensitive_path(path) for path in paths)
            return SecurityAssessment(
                "allow", "sensitive" if sensitive else "normal", "file", "read",
                "file.sensitive_read" if sensitive else "file.normal_read",
                "读取敏感文件（仅记录路径元数据）" if sensitive else "通过文件工具读取文件",
                target=", ".join(str(path) for path in paths[:3]),
                audit_required=True, mutating=False,
                details={"paths": [str(path) for path in paths]},
            )
        return SecurityAssessment(
            "allow", "normal", "tool", "read", "tool.normal_read",
            "只读工具调用", audit_required=False, mutating=False,
        )

    def _assess_command(
        self,
        command: str,
        params: dict[str, Any],
        workspace: Path,
    ) -> SecurityAssessment:
        # POSIX shells allow backslash-obfuscated command names (for example
        # ``r\m``). On Windows the same syntax is an ordinary path separator.
        normalized = command if os.name == "nt" else re.sub(r"\\([A-Za-z])", r"\1", command)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        cwd = _canonical(str(params.get("working_dir") or params.get("workdir") or workspace), workspace)
        targets = self._command_targets(normalized, cwd)
        if any(self._is_protected_state(path) for path in targets) or any(
            name.lower() in normalized.lower()
            for name in ("logs.sqlite", _POLICY_FILE, _EMERGENCY_AUDIT_FILE, "state.sqlite")
        ):
            return SecurityAssessment(
                "block", "critical", "command", "execute", "core.audit_tamper",
                "禁止命令直接修改 nanobot 安全、审计或状态数据库",
                target=redact_security_text(command), mutating=True,
            )
        for rule_id, pattern, summary in _HARD_COMMAND_RULES:
            if re.search(pattern, normalized):
                return SecurityAssessment(
                    "block", "critical", "command", "execute", rule_id, summary,
                    target=redact_security_text(command), mutating=True,
                )
        policy = self.policy.load()
        if self._command_may_modify_paths(normalized) and any(
            self._matches(path, policy["approval_paths"]) for path in targets
        ):
            return SecurityAssessment(
                "require_approval", "high", "command", "execute", "file.protected_path",
                "命令将修改用户设置的强制审批路径",
                target=redact_security_text(command), mutating=True,
                details={"paths": [str(path) for path in targets]},
            )
        network_assessment = self._assess_network_command(normalized, policy)
        if network_assessment is not None:
            return network_assessment
        if self._matches_command_prefix(
            normalized, policy["command_approval_prefixes"], any_segment=True
        ):
            return SecurityAssessment(
                "require_approval", "high", "command", "execute", "command.user_approval",
                "命令命中用户设置的询问前缀",
                target=redact_security_text(command), mutating=True,
            )
        if self._matches_command_prefix(normalized, policy["command_allow_prefixes"]):
            return SecurityAssessment(
                "allow", "normal", "command", "execute", "command.user_allowed",
                "命令命中用户设置的放行前缀",
                target=redact_security_text(command), mutating=True,
            )
        for rule_id, pattern, summary in _HIGH_COMMAND_RULES:
            if not re.search(pattern, normalized):
                continue
            if targets and all(
                self._matches(path, policy["file_allow_paths"]) for path in targets
            ):
                return SecurityAssessment(
                    "allow", "normal", "command", "execute", "file.user_allowed_path",
                    "高风险路径操作命中用户设置的自动放行白名单",
                    target=redact_security_text(command), mutating=True,
                    details={
                        "original_rule_id": rule_id,
                        "paths": [str(path) for path in targets],
                    },
                )
            return SecurityAssessment(
                "require_approval", "high", "command", "execute", rule_id, summary,
                target=redact_security_text(command), mutating=True,
                details={"paths": [str(path) for path in targets]},
            )
        return SecurityAssessment(
            "allow", "normal", "command", "execute", "command.normal",
            "命令已通过安全检查", target=redact_security_text(command), mutating=True,
        )

    def _assess_network_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> SecurityAssessment | None:
        if tool_name not in {"web_fetch", "web_search"}:
            return None
        policy = self.policy.load()
        is_search = tool_name == "web_search"
        target = str(
            (params.get("query") if is_search else params.get("url")) or tool_name
        ).strip()
        hosts = self._hosts_from_values(params.get("url", ""))
        blocked = self._network_decision(
            policy,
            hosts,
            target=target or tool_name,
            unknown_network_target=is_search,
        )
        if blocked is not None:
            return blocked
        return SecurityAssessment(
            "allow", "normal", "network", "search" if is_search else "fetch",
            "network.web_search" if is_search else "network.web_fetch",
            "通过 Web 工具执行联网搜索" if is_search else "通过 Web 工具访问网络",
            target=redact_security_text(target or tool_name),
            audit_required=True,
            mutating=False,
            details={"domains": hosts},
        )

    def _assess_network_command(
        self,
        command: str,
        policy: dict[str, Any],
    ) -> SecurityAssessment | None:
        if not re.search(
            r"(?i)\b(?:curl|wget|invoke-webrequest|iwr|start-bitstransfer|ssh|scp|sftp|ftp|nc|ncat|telnet|ping)\b"
            r"|\bgit\s+(?:clone|fetch|pull|push)\b"
            r"|\b(?:npm|pnpm|yarn)\s+(?:install|add|publish)\b"
            r"|\bpip(?:3)?\s+install\b",
            command,
        ):
            return None
        hosts = self._hosts_from_values(command)
        return self._network_decision(
            policy,
            hosts,
            target=redact_security_text(command),
            unknown_network_target=not hosts,
        )

    @staticmethod
    def _matches_command_prefix(
        command: str,
        prefixes: list[str],
        *,
        any_segment: bool = False,
    ) -> bool:
        segments = [
            segment.strip()
            for segment in re.split(r"\s*(?:&&|\|\||[;|\n])\s*", command)
            if segment.strip()
        ]
        if not any_segment and len(segments) != 1:
            return False

        def matches(segment: str) -> bool:
            candidate = segment if os.name != "nt" else segment.lower()
            for raw in prefixes:
                prefix = raw if os.name != "nt" else raw.lower()
                if candidate == prefix or candidate.startswith(prefix + " "):
                    return True
            return False

        return any(matches(segment) for segment in segments)

    @staticmethod
    def _command_may_modify_paths(command: str) -> bool:
        return bool(re.search(
            r"(?i)(?:^|[;&|]\s*)\b(?:rm|del|rmdir|remove-item|truncate|shred|chmod|chown|takeown|icacls|cp|mv|touch|mkdir|install)\b"
            r"|\b(?:set-content|add-content|out-file|copy-item|move-item|new-item|rename-item)\b"
            r"|\bsed\b[^;&|]*\s-i(?:\s|$)"
            r"|(?<!<)>{1,2}",
            command,
        ))

    @classmethod
    def _hosts_from_values(cls, value: Any) -> list[str]:
        values: list[str] = []
        if isinstance(value, dict):
            for child in value.values():
                values.extend(cls._hosts_from_values(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                values.extend(cls._hosts_from_values(child))
        elif isinstance(value, str):
            for url in _URL_RE.findall(value):
                hostname = urlparse(url).hostname
                if hostname:
                    values.append(hostname.lower().rstrip("."))
            for hostname in re.findall(r"(?i)\b[\w.+-]+@([a-z0-9.-]+)(?::|\s|$)", value):
                values.append(hostname.lower().rstrip("."))
            if not values:
                try:
                    tokens = shlex.split(value, posix=os.name != "nt")
                except ValueError:
                    tokens = value.split()
                for token in tokens:
                    raw = token.strip("\"'(),;|&").rstrip("/.")
                    if raw.startswith("-") or not raw:
                        continue
                    parsed = urlparse(raw if "://" in raw else f"//{raw}")
                    hostname = parsed.hostname
                    if hostname and (
                        "." in hostname
                        or hostname.lower() == "localhost"
                        or re.fullmatch(r"\d+(?:\.\d+){3}", hostname)
                    ):
                        values.append(hostname.lower().rstrip("."))
        output: list[str] = []
        for hostname in values:
            if hostname not in output:
                output.append(hostname)
        return output

    @staticmethod
    def _domain_matches(hostname: str, rules: list[str]) -> bool:
        host = hostname.lower().rstrip(".")
        for raw in rules:
            rule = raw.lower().rstrip(".")
            if rule.startswith("*."):
                rule = rule[2:]
                if host.endswith("." + rule):
                    return True
                continue
            if host == rule or host.endswith("." + rule):
                return True
        return False

    def _network_decision(
        self,
        policy: dict[str, Any],
        hosts: list[str],
        *,
        target: str,
        unknown_network_target: bool,
    ) -> SecurityAssessment | None:
        denied = [
            host for host in hosts
            if self._domain_matches(host, policy["network_deny_domains"])
        ]
        if denied:
            return SecurityAssessment(
                "block", "high", "network", "connect", "network.denied_domain",
                "网络目标命中拒绝域名",
                target=redact_security_text(target),
                details={"domains": denied},
            )
        if not policy["network_block_all"]:
            return None
        disallowed = [
            host for host in hosts
            if not self._domain_matches(host, policy["network_allow_domains"])
        ]
        if unknown_network_target or disallowed:
            return SecurityAssessment(
                "block", "high", "network", "connect", "network.block_all",
                "网络访问已默认阻断，且目标不在允许域名中",
                target=redact_security_text(target),
                details={"domains": disallowed},
            )
        return None

    @staticmethod
    def _tool_mutates(tool_name: str, _tool: Any | None) -> bool:
        return tool_name in {
            "write_file", "edit_file", "apply_patch", "create_docx", "create_pdf",
            "create_research_chart",
        }

    @staticmethod
    def _file_action(tool_name: str) -> str:
        if tool_name.startswith("create_"):
            return "create"
        if tool_name == "apply_patch":
            return "patch"
        if tool_name == "edit_file":
            return "edit"
        return "write"

    @staticmethod
    def _tool_paths(tool_name: str, params: dict[str, Any], workspace: Path) -> list[Path]:
        values: list[str] = []
        if tool_name == "apply_patch":
            for edit in params.get("edits") or []:
                if isinstance(edit, dict) and isinstance(edit.get("path"), str):
                    values.append(edit["path"])
        else:
            for key in ("path", "output_path"):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value)
            if tool_name in {"create_docx", "create_pdf"} and not params.get("output_path"):
                source = params.get("source_path")
                if isinstance(source, str) and source:
                    suffix = ".docx" if tool_name == "create_docx" else ".pdf"
                    values.append(str(Path(source).with_suffix(suffix)))
        return [_canonical(value, workspace) for value in values]

    @staticmethod
    def _command_targets(command: str, cwd: Path) -> list[Path]:
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = command.split()
        targets: list[Path] = []
        destructive = False
        for token in tokens:
            lower = token.lower().strip('"\'')
            if lower in {"rm", "del", "rmdir", "remove-item", "truncate", "shred", "chmod", "chown", "takeown", "icacls"}:
                destructive = True
                continue
            if lower in {";", "&&", "||", "|"}:
                destructive = False
                continue
            if not destructive or lower.startswith("-") or lower.startswith("/") and len(lower) <= 3:
                continue
            if any(mark in lower for mark in ("$", "`", "$(", "${")):
                continue
            raw = token.strip('"\'').rstrip(";,|&")
            if raw:
                targets.append(_canonical(raw, cwd))
        absolute_patterns = (
            r"(?<![A-Za-z])([A-Za-z]:[\\/][^\s\"'|><;]+)",
            r"(?:^|[\s>\"'])(/[^\s\"'>;|<]+)",
        )
        for pattern in absolute_patterns:
            for match in re.findall(pattern, command):
                try:
                    targets.append(_canonical(match, cwd))
                except (OSError, ValueError):
                    continue
        unique: list[Path] = []
        for path in targets:
            if all(_compare(path) != _compare(existing) for existing in unique):
                unique.append(path)
        return unique

    def _is_protected_state(self, path: Path) -> bool:
        return any(_compare(path) == _compare(item) for item in self._protected_state)

    @staticmethod
    def _matches(path: Path, roots: list[str]) -> bool:
        return any(_within(path, _canonical(root)) for root in roots)

    @staticmethod
    def _is_sensitive_path(path: Path) -> bool:
        normalized = str(path).replace("\\", "/").lower()
        return any(part in normalized for part in _SENSITIVE_PARTS)

    @staticmethod
    def _is_system_write_path(path: Path) -> bool:
        if os.name == "nt":
            roots = [
                os.environ.get("SYSTEMROOT", r"C:\Windows"),
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            ]
        else:
            roots = ["/etc", "/usr", "/bin", "/sbin", "/System", "/Library"]
        return any(_within(path, _canonical(root)) for root in roots if root)

    async def authorize(
        self,
        assessment: SecurityAssessment,
        *,
        callback: ApprovalCallback | None,
        chat_id: str | None,
        turn_grants: set[str],
        interactive: bool,
        tool_call_id: str,
        tool_name: str,
    ) -> tuple[bool, str]:
        if assessment.decision == "block":
            return False, "blocked"
        if assessment.decision != "require_approval":
            return True, "allowed"
        if assessment.grant_key in turn_grants:
            return True, "approved_for_turn"
        if not interactive or callback is None:
            return False, "blocked_unattended"
        approval_id = f"sap_{uuid.uuid4().hex}"
        payload = {
            "approval_id": approval_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "risk": assessment.risk,
            "rule_id": assessment.rule_id,
            "summary": assessment.summary,
            "target": assessment.target,
            "scope": "turn",
        }
        decision = await self.approvals.request(payload, callback, chat_id=chat_id)
        if decision == "allow_turn":
            turn_grants.add(assessment.grant_key)
            return True, "approved"
        return False, "denied"

    def begin_audit(
        self,
        assessment: SecurityAssessment,
        *,
        tool_call_id: str | None,
        tool_name: str | None,
        session_key: str | None,
        turn_id: str | None,
    ) -> AuditHandle | None:
        if not assessment.audit_required:
            return None
        project = current_project_context()
        details = redact_security_details(assessment.details)
        if not isinstance(details, dict):
            details = {}
        if session_key:
            details["session_key"] = session_key
        payload = {
            "category": assessment.category,
            "action": assessment.action,
            "decision": assessment.decision,
            "risk": assessment.risk,
            "summary": assessment.summary,
            "rule_id": assessment.rule_id,
            "project_id": project.project_id if project is not None else None,
            "session_id": project.session_id if project is not None else None,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "target": assessment.target,
            "details": details,
        }
        started = time.monotonic()
        try:
            event_id = self.logs.begin_security_event(**payload)
            return AuditHandle(event_id=event_id, started_at=started)
        except Exception as exc:
            fallback = {"timestamp": time.time_ns() // 1_000_000, **payload, "result": "pending"}
            if self._write_emergency(fallback):
                return AuditHandle(None, started, emergency=True, fallback_payload=fallback)
            raise AuditUnavailable("security audit storage is unavailable") from exc

    def complete_audit(
        self,
        handle: AuditHandle | None,
        *,
        result: str,
        decision: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if handle is None:
            return
        safe_details = redact_security_details(details or {})
        if not isinstance(safe_details, dict):
            safe_details = {}
        duration_ms = max(0, round((time.monotonic() - handle.started_at) * 1000))
        if handle.event_id is not None:
            try:
                self.logs.complete_security_event(
                    handle.event_id,
                    result=result,
                    decision=decision,
                    duration_ms=duration_ms,
                    details=safe_details,
                )
                return
            except Exception:
                pass
        payload = {
            **handle.fallback_payload,
            "timestamp": time.time_ns() // 1_000_000,
            "result": result,
            "duration_ms": duration_ms,
            **({"decision": decision} if decision else {}),
            **({"completion_details": safe_details} if safe_details else {}),
        }
        self._write_emergency(payload)

    def _write_emergency(self, payload: dict[str, Any]) -> bool:
        try:
            self.emergency_path.parent.mkdir(parents=True, exist_ok=True)
            with self.emergency_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            with suppress(OSError):
                self.emergency_path.chmod(0o600)
            return True
        except OSError:
            return False

_SERVICE_LOCK = threading.RLock()
_SERVICES: dict[str, SecurityService] = {}


def get_security_service(logs: StructuredLogStore, workspace: str | Path) -> SecurityService:
    key = _compare(logs.path.resolve(strict=False))
    with _SERVICE_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = SecurityService(logs, Path(workspace))
            _SERVICES[key] = service
        return service

"""MCP preset helpers for the WebUI settings and message surfaces."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.protocol import app_manifest, compact_dict
from nanobot.config.loader import load_config, resolve_config_env_vars, save_config
from nanobot.config.paths import get_runtime_subdir
from nanobot.config.schema import MCPServerConfig
from nanobot.utils.helpers import ensure_dir

QueryParams = dict[str, list[str]]

_MCP_PRESET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:[^=&]*(?:api[_-]?key|token|secret|password|bearer)[^=&]*)=)[^&#\s]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|token|secret|password|bearer)(?:[=:]|\s+))[^,\s'\"&]+",
    re.IGNORECASE,
)
_MCP_ATTACHMENT_KEYS = (
    "name",
    "display_name",
    "category",
    "transport",
    "logo_url",
    "brand_color",
    "status",
    "configured",
)
_MAX_TEST_TOOLS = 16
_DEFAULT_TEST_TIMEOUT = 20
_DEFAULT_CUSTOM_TIMEOUT = 30
_CUSTOM_ACTIONS = {"custom", "import", "import-cursor", "tools"}
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.78"
JUYUAN_MCP_URL = "https://api.gildata.com/mcp-servers/aidata-assistant-srv-api"
DESKTOP_DEFAULT_MCP_PRESETS = ("juyuan", "playwright")
RETIRED_DESKTOP_MCP_PRESETS = frozenset({
    "aws-docs",
    "brave-search",
    "browserbase",
    "context7",
    "exa",
    "figma",
    "firecrawl",
    "github",
    "microsoft-learn",
    "postman",
    "supabase",
})

McpReload = Callable[[], Awaitable[dict[str, Any]]]


class McpPresetError(Exception):
    """WebUI-facing MCP preset error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class McpPresetField:
    name: str
    label: str
    target: tuple[Literal["env", "url_param", "arg", "header"], str]
    secret: bool = True
    required: bool = True
    env_var: str | None = None
    placeholder: str = ""


@dataclass(frozen=True)
class McpPreset:
    name: str
    display_name: str
    category: str
    description: str
    docs_url: str
    transport: Literal["stdio", "streamableHttp", "sse", "oauth"]
    install_supported: bool
    brand_domain: str
    brand_color: str
    server: MCPServerConfig | None = None
    fields: tuple[McpPresetField, ...] = ()
    requires: str = ""
    note: str = ""


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


MCP_PRESETS: tuple[McpPreset, ...] = (
    McpPreset(
        name="juyuan",
        display_name="聚源金融数据",
        category="finance",
        description="通过聚源金融数据 MCP 查询行情、财务、公告、资金与机构数据。",
        docs_url="https://www.gildata.com/",
        transport="streamableHttp",
        install_supported=True,
        brand_domain="gildata.com",
        brand_color="#B42318",
        requires="聚源 MCP token",
        server=MCPServerConfig(
            type="streamableHttp",
            url=JUYUAN_MCP_URL,
            connect_timeout=10,
            tool_timeout=60,
        ),
        fields=(
            McpPresetField(
                name="juyuan_token",
                label="聚源 MCP token",
                target=("url_param", "token"),
                env_var="JUYUAN_MCP_TOKEN",
                placeholder="请输入聚源 MCP token",
            ),
        ),
        note="首次安装会预置连接器；填写 token 后才会连接并启用工具。",
    ),
    McpPreset(
        name="playwright",
        display_name="Playwright",
        category="browser",
        description="Local browser inspection and automation with Playwright's MCP server.",
        docs_url="https://playwright.dev/docs/getting-started-mcp",
        transport="stdio",
        install_supported=True,
        brand_domain="playwright.dev",
        brand_color="#2EAD33",
        requires="Node.js and npx",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", PLAYWRIGHT_MCP_PACKAGE],
            connect_timeout=15,
            tool_timeout=60,
        ),
    ),
)


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_value(query: QueryParams, key: str) -> str | None:
    raw = _query_first(query, key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _preset_by_name(name: str) -> McpPreset:
    if not name or _MCP_PRESET_NAME_RE.match(name) is None:
        raise McpPresetError("invalid MCP preset name")
    for preset in MCP_PRESETS:
        if preset.name == name:
            return preset
    raise McpPresetError("unknown MCP preset", status=404)


def _preset_by_name_optional(name: str) -> McpPreset | None:
    try:
        return _preset_by_name(name)
    except McpPresetError:
        return None


def _known_preset_names() -> set[str]:
    return {preset.name for preset in MCP_PRESETS}


def _known_mcp_names() -> set[str]:
    names = _known_preset_names()
    with suppress(Exception):
        names.update(load_config().tools.mcp_servers)
    return names


def _clip_ws_string(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def normalize_mcp_preset_mentions(raw: Any) -> list[dict[str, Any]]:
    """Sanitize structured MCP preset mentions sent by the WebUI."""
    if not isinstance(raw, list):
        return []
    known = _known_mcp_names()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        name = _clip_ws_string(item.get("name"), 64)
        if not name or _MCP_PRESET_NAME_RE.match(name) is None:
            continue
        key = name.lower()
        if key in seen or key not in known:
            continue
        seen.add(key)
        row: dict[str, Any] = {"name": key}
        for field_name in _MCP_ATTACHMENT_KEYS[1:]:
            value = item.get(field_name)
            if isinstance(value, bool):
                row[field_name] = value
                continue
            limit = 512 if field_name == "logo_url" else 160
            text = _clip_ws_string(value, limit)
            if text:
                row[field_name] = text
        out.append(row)
    return out


def _clone_server(server: MCPServerConfig) -> MCPServerConfig:
    return MCPServerConfig.model_validate(server.model_dump(mode="json"))


def _with_managed_stdio_cwd(name: str, cfg: MCPServerConfig) -> MCPServerConfig:
    if cfg.command and (cfg.type in (None, "stdio")) and not cfg.cwd:
        cfg.cwd = str(ensure_dir(get_runtime_subdir("mcp") / name))
    return cfg


def _remove_managed_stdio_cwd(name: str, cfg: MCPServerConfig | None) -> bool:
    if cfg is None or not cfg.cwd:
        return False
    cwd = Path(cfg.cwd).expanduser().resolve(strict=False)
    managed = (get_runtime_subdir("mcp") / name).resolve(strict=False)
    if cwd != managed or not cwd.exists():
        return False
    if cwd.is_symlink() or cwd.is_file():
        cwd.unlink()
    else:
        shutil.rmtree(cwd)
    return True


def _url_with_param(url: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _arg_value(args: list[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _with_arg_value(args: list[str], flag: str, value: str) -> list[str]:
    out: list[str] = []
    skip_next = False
    prefix = f"{flag}="
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == flag:
            skip_next = True
            continue
        if item.startswith(prefix):
            continue
        out.append(item)
    out.extend([flag, value])
    return out


def _field_value_from_config(field: McpPresetField, cfg: MCPServerConfig | None) -> str | None:
    if cfg is None:
        return None
    target_kind, target_name = field.target
    if target_kind == "env":
        value = cfg.env.get(target_name)
        return value if value else None
    if target_kind == "header":
        value = cfg.headers.get(target_name)
        return value if value else None
    if target_kind == "arg":
        return _arg_value(list(cfg.args), target_name)
    if target_kind == "url_param" and cfg.url:
        parsed = urllib.parse.urlsplit(cfg.url)
        values = urllib.parse.parse_qs(parsed.query).get(target_name)
        if values:
            return values[0]
    return None


def _field_configured(field: McpPresetField, cfg: MCPServerConfig | None) -> bool:
    value = _field_value_from_config(field, cfg)
    if value:
        return True
    return bool(field.env_var and os.environ.get(field.env_var))


def _field_payload(field: McpPresetField, cfg: MCPServerConfig | None) -> dict[str, Any]:
    return {
        "name": field.name,
        "label": field.label,
        "secret": field.secret,
        "required": field.required,
        "configured": _field_configured(field, cfg),
        "placeholder": field.placeholder,
        "env_var": field.env_var,
    }


def _resolve_field_value(
    field: McpPresetField,
    query: QueryParams,
    existing: MCPServerConfig | None,
) -> str | None:
    provided = _query_value(query, field.name)
    if provided:
        return provided
    current = _field_value_from_config(field, existing)
    if current:
        return current
    if field.env_var and os.environ.get(field.env_var):
        return f"${{{field.env_var}}}"
    return None


def _materialize_server(
    preset: McpPreset,
    query: QueryParams,
    existing: MCPServerConfig | None,
) -> MCPServerConfig:
    if preset.server is None or not preset.install_supported:
        raise McpPresetError(f"{preset.display_name} is not supported yet", status=409)

    cfg = _clone_server(preset.server)
    for field_spec in preset.fields:
        value = _resolve_field_value(field_spec, query, existing)
        if field_spec.required and not value:
            raise McpPresetError(f"missing {field_spec.label}")
        if not value:
            continue
        target_kind, target_name = field_spec.target
        if target_kind == "env":
            cfg.env[target_name] = value
        elif target_kind == "header":
            cfg.headers[target_name] = value
        elif target_kind == "arg":
            cfg.args = _with_arg_value(list(cfg.args), target_name, value)
        elif target_kind == "url_param":
            cfg.url = _url_with_param(cfg.url, target_name, value)
    return _with_managed_stdio_cwd(preset.name, cfg)


def install_desktop_default_mcp_servers(config: Any) -> bool:
    """Install the desktop's first-launch MCP defaults without embedding secrets."""
    changed = False
    for name in DESKTOP_DEFAULT_MCP_PRESETS:
        if name in config.tools.mcp_servers:
            continue
        preset = _preset_by_name(name)
        if name == "juyuan" and not os.environ.get("JUYUAN_MCP_TOKEN"):
            # Keep a credential-free placeholder in config so the connector is
            # installed in the toolbox, but do not make startup connect to an
            # unauthenticated remote endpoint.
            server = MCPServerConfig(
                type="streamableHttp",
                connect_timeout=10,
                tool_timeout=60,
            )
        else:
            server = _materialize_server(preset, {}, None)
        config.tools.mcp_servers[name] = server
        changed = True
    return changed


def prune_retired_desktop_mcp_presets(config: Any) -> bool:
    """Remove only presets formerly shipped by desktop; preserve custom MCPs."""
    removed = RETIRED_DESKTOP_MCP_PRESETS.intersection(config.tools.mcp_servers)
    for name in removed:
        config.tools.mcp_servers.pop(name, None)
    return bool(removed)


def _command_available(command: str) -> bool:
    if not command:
        return False
    if shutil.which(command):
        return True
    path = Path(command).expanduser()
    return path.exists() and path.is_file()


def _config_available(cfg: MCPServerConfig | None) -> bool:
    if cfg is None:
        return False
    if cfg.command:
        return _command_available(cfg.command)
    if cfg.url:
        return True
    return False


def _status_for(preset: McpPreset, cfg: MCPServerConfig | None) -> str:
    if cfg is None:
        return "not_installed" if preset.install_supported else "coming_soon"
    if any(field.required and not _field_configured(field, cfg) for field in preset.fields):
        return "missing_credentials"
    if cfg.command and not _command_available(cfg.command):
        return "missing_dependency"
    return "configured"


def _connection_summary(cfg: MCPServerConfig | None) -> str:
    if cfg is None:
        return ""
    if cfg.command:
        return " ".join([cfg.command, *cfg.args[:2]]).strip()
    if cfg.url:
        parsed = urllib.parse.urlsplit(cfg.url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return ""


def _tool_allowlist(cfg: MCPServerConfig | None) -> list[str]:
    if cfg is None:
        return ["*"]
    return list(cfg.enabled_tools)


def _managed_mcp_path(name: str, cfg: MCPServerConfig | None) -> list[str]:
    if cfg is None or not cfg.command:
        return []
    return [f"runtime:mcp/{name}"]


def _preset_manifest(preset: McpPreset, *, logo_url: str) -> dict[str, Any]:
    server = preset.server
    managed_paths = _managed_mcp_path(preset.name, server)
    field_specs = [
        compact_dict({
            "name": field.name,
            "target": field.target[0],
            "required": field.required,
            "secret": field.secret,
            "env_var": field.env_var,
        })
        for field in preset.fields
    ]
    capabilities = [
        compact_dict({
            "type": "mcp",
            "transport": preset.transport,
            "command": server.command if server and server.command else None,
            "args": list(server.args) if server and server.command else None,
            "url": _connection_summary(server) if server and server.url else None,
            "fields": field_specs,
        })
    ]
    return app_manifest(
        app_id=preset.name,
        display_name=preset.display_name,
        description=preset.description,
        category=preset.category,
        source="mcp-preset",
        docs_url=preset.docs_url,
        logo_url=logo_url,
        brand_color=preset.brand_color,
        capabilities=capabilities,
        install=compact_dict({
            "supported": preset.install_supported,
            "strategy": "config",
            "managed_paths": managed_paths,
            "verification": ["config_present", "dependency_available"],
        }),
        remove=compact_dict({
            "supported": True,
            "strategy": "config",
            "managed_paths": managed_paths,
            "verification": ["config_absent", "managed_paths_absent"] if managed_paths else ["config_absent"],
        }),
        trust={
            "registry": "mcp-presets",
            "level": "builtin",
            "review_status": "builtin_preset",
        },
    )


def _custom_manifest(name: str, cfg: MCPServerConfig) -> dict[str, Any]:
    transport = cfg.type or ("stdio" if cfg.command else "streamableHttp")
    managed_paths: list[str] = []
    return app_manifest(
        app_id=name,
        display_name=name,
        description="Custom MCP server from nanobot config.",
        category="custom",
        source="mcp-custom",
        brand_color="#64748B",
        capabilities=[
            compact_dict({
                "type": "mcp",
                "transport": transport,
                "command": cfg.command or None,
                "url": _connection_summary(cfg) if cfg.url else None,
            })
        ],
        install=compact_dict({
            "supported": True,
            "strategy": "config",
            "managed_paths": managed_paths,
            "verification": ["config_present", "dependency_available"],
        }),
        remove=compact_dict({
            "supported": True,
            "strategy": "config",
            "managed_paths": managed_paths,
            "verification": ["config_absent", "managed_paths_absent"] if managed_paths else ["config_absent"],
        }),
        trust={
            "registry": "user-config",
            "level": "user",
            "review_status": "user_managed",
        },
    )


def _preset_payload(preset: McpPreset, configured_servers: dict[str, MCPServerConfig]) -> dict[str, Any]:
    cfg = configured_servers.get(preset.name)
    status = _status_for(preset, cfg)
    configured = cfg is not None and status not in {"missing_credentials"}
    logo_url = _favicon_url(preset.brand_domain)
    return {
        "name": preset.name,
        "display_name": preset.display_name,
        "category": preset.category,
        "description": preset.description,
        "docs_url": preset.docs_url,
        "transport": preset.transport,
        "requires": preset.requires,
        "note": preset.note,
        "install_supported": preset.install_supported,
        "installed": cfg is not None,
        "configured": configured,
        "available": configured and _config_available(cfg),
        "status": status,
        "logo_url": logo_url,
        "brand_color": preset.brand_color,
        "required_fields": [_field_payload(field, cfg) for field in preset.fields],
        "connection_summary": _connection_summary(cfg),
        "enabled_tools": _tool_allowlist(cfg),
        "source": "preset",
        "manifest": _preset_manifest(preset, logo_url=logo_url),
    }


def _custom_payload(
    name: str,
    cfg: MCPServerConfig,
    *,
    tool_names: list[str] | None = None,
) -> dict[str, Any]:
    transport = cfg.type
    if not transport:
        transport = "stdio" if cfg.command else ("sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp")
    status = "missing_dependency" if cfg.command and not _command_available(cfg.command) else "configured"
    return {
        "name": name,
        "display_name": name,
        "category": "custom",
        "description": "Custom MCP server from nanobot config.",
        "docs_url": "",
        "transport": transport,
        "requires": "",
        "note": "",
        "install_supported": True,
        "installed": True,
        "configured": True,
        "available": _config_available(cfg),
        "status": status,
        "logo_url": None,
        "brand_color": "#64748B",
        "required_fields": [],
        "connection_summary": _connection_summary(cfg),
        "enabled_tools": _tool_allowlist(cfg),
        "tool_names": tool_names or [],
        "source": "custom",
        "manifest": _custom_manifest(name, cfg),
    }


def mcp_presets_payload(
    *,
    last_action: dict[str, Any] | None = None,
    tool_preview: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    config = load_config()
    known = _known_preset_names()
    preset_rows = [
        _preset_payload(preset, config.tools.mcp_servers)
        | ({"tool_names": tool_preview.get(preset.name, [])} if tool_preview and preset.name in tool_preview else {})
        for preset in MCP_PRESETS
    ]
    custom_rows = [
        _custom_payload(name, cfg, tool_names=(tool_preview or {}).get(name))
        for name, cfg in sorted(config.tools.mcp_servers.items())
        if name not in known
    ]
    payload: dict[str, Any] = {
        "presets": [*preset_rows, *custom_rows],
        "installed_count": len(config.tools.mcp_servers),
    }
    if last_action is not None:
        payload["last_action"] = last_action
    return payload


def _display_name_for(name: str, preset: McpPreset | None = None) -> str:
    return preset.display_name if preset is not None else name


def _action_message(action: str, preset: McpPreset, *, ok: bool = True) -> dict[str, Any]:
    verb = {
        "enable": "Enabled",
        "remove": "Removed",
        "test": "Checked",
    }.get(action, "Updated")
    payload: dict[str, Any] = {
        "ok": ok,
        "message": f"{verb} MCP preset for {preset.display_name}.",
    }
    if action == "enable":
        payload["installed"] = True
        payload["verification"] = ["config_present"]
    elif action == "remove":
        payload["removed"] = True
        payload["verification"] = ["config_absent"]
    return payload


def _server_action_message(action: str, name: str, *, ok: bool = True) -> dict[str, Any]:
    verb = {
        "custom": "Saved",
        "import": "Imported",
        "import-cursor": "Imported",
        "tools": "Updated tools for",
        "remove": "Removed",
    }.get(action, "Updated")
    payload: dict[str, Any] = {
        "ok": ok,
        "message": f"{verb} MCP server {name}.",
    }
    if action in {"custom", "import", "import-cursor"}:
        payload["installed"] = True
        payload["verification"] = ["config_present"]
    elif action == "remove":
        payload["removed"] = True
        payload["verification"] = ["config_absent"]
    return payload


def _scrub_test_error(text: str) -> str:
    scrubbed = _SECRET_QUERY_RE.sub(r"\1<redacted>", text.strip())
    scrubbed = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", scrubbed)
    return scrubbed[:400] if scrubbed else "Connection failed."


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _test_timeout(cfg: MCPServerConfig) -> int:
    raw = cfg.tool_timeout or _DEFAULT_TEST_TIMEOUT
    return max(5, min(int(raw), _DEFAULT_TEST_TIMEOUT))


async def _close_mcp_stacks(stacks: Mapping[str, Any]) -> None:
    for stack in stacks.values():
        with suppress(Exception):
            await stack.aclose()


async def mcp_presets_test_action(query: QueryParams) -> dict[str, Any]:
    """Connect to an enabled MCP preset and report its tool surface."""
    from nanobot.agent.tools.mcp import connect_mcp_servers

    name = (_query_first(query, "name") or "").strip()
    if not name:
        raise McpPresetError("missing MCP preset name")
    if _MCP_PRESET_NAME_RE.match(name) is None:
        raise McpPresetError("invalid MCP server name")
    preset = _preset_by_name_optional(name)
    display_name = _display_name_for(name, preset)

    try:
        config = resolve_config_env_vars(load_config())
    except ValueError as exc:
        return mcp_presets_payload(last_action={
            "ok": False,
            "message": _scrub_test_error(str(exc)),
            "error": _scrub_test_error(str(exc)),
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        })

    cfg = config.tools.mcp_servers.get(name)
    if cfg is None:
        raise McpPresetError(f"{display_name} is not enabled", status=404)

    status = _status_for(preset, cfg) if preset is not None else (
        "missing_dependency" if cfg.command and not _command_available(cfg.command) else "configured"
    )
    if status == "missing_credentials":
        last_action = {
            "ok": False,
            "message": f"{display_name} is missing required credentials.",
            "error": "missing credentials",
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        }
        return mcp_presets_payload(last_action=last_action)

    if cfg.command and not _command_available(cfg.command):
        last_action = {
            "ok": False,
            "message": f"{display_name} requires '{cfg.command}' on PATH.",
            "error": "missing dependency",
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        }
        return mcp_presets_payload(last_action=last_action)

    registry = ToolRegistry()
    stacks: dict[str, Any] = {}
    try:
        stacks = await asyncio.wait_for(
            connect_mcp_servers({name: cfg}, registry),
            timeout=_test_timeout(cfg),
        )
        tool_prefix = f"mcp_{name}_"
        tool_names = sorted(name for name in registry.tool_names if name.startswith(tool_prefix))
        ok = name in stacks
        if ok:
            last_action = {
                "ok": True,
                "message": (
                    f"{display_name} connected with {len(tool_names)} tools."
                    if tool_names
                    else f"{display_name} connected, but reported no tools."
                ),
                "tool_count": len(tool_names),
                "tool_names": tool_names[:_MAX_TEST_TOOLS],
                "checked_at": _checked_at(),
            }
        else:
            last_action = {
                "ok": False,
                "message": f"{display_name} did not complete an MCP handshake.",
                "error": "MCP handshake failed",
                "tool_count": 0,
                "tool_names": [],
                "checked_at": _checked_at(),
            }
    except asyncio.TimeoutError:
        last_action = {
            "ok": False,
            "message": f"{display_name} test timed out.",
            "error": "timeout",
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        }
    except Exception as exc:
        error = _scrub_test_error(str(exc))
        last_action = {
            "ok": False,
            "message": f"{display_name} could not connect.",
            "error": error,
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        }
    finally:
        await _close_mcp_stacks(stacks)

    preview = {name: last_action.get("tool_names", [])} if last_action.get("tool_names") else None
    return mcp_presets_payload(last_action=last_action, tool_preview=preview)


def _parse_json_value(raw: str | None, *, fallback: Any) -> Any:
    if raw is None or not raw.strip():
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpPresetError(f"invalid JSON: {exc.msg}") from exc


def _parse_string_list(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return []
    parsed = _parse_json_value(raw, fallback=None)
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return [item for item in parsed if item.strip()]
    if isinstance(parsed, str):
        return shlex.split(parsed)
    raise McpPresetError("expected a JSON string array")


def _parse_string_map(raw: str | None) -> dict[str, str]:
    parsed = _parse_json_value(raw, fallback={})
    if not isinstance(parsed, dict):
        raise McpPresetError("expected a JSON object")
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpPresetError("JSON object values must be strings")
        if key.strip():
            out[key.strip()] = value
    return out


def _parse_enabled_tools(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return ["*"]
    values = _parse_string_list(raw)
    if "*" in values:
        return ["*"]
    return values


def _normalize_transport(value: str | None, *, command: str = "", url: str = "") -> Literal["stdio", "sse", "streamableHttp"]:
    raw = (value or "").strip()
    if not raw:
        if command:
            return "stdio"
        if url.rstrip("/").endswith("/sse"):
            return "sse"
        return "streamableHttp"
    aliases = {
        "stdio": "stdio",
        "sse": "sse",
        "streamableHttp": "streamableHttp",
        "streamable-http": "streamableHttp",
        "streamable_http": "streamableHttp",
        "http": "streamableHttp",
    }
    normalized = aliases.get(raw)
    if normalized is None:
        raise McpPresetError("unsupported MCP transport")
    return normalized  # type: ignore[return-value]


def _validated_server_name(name: str) -> str:
    if not name or _MCP_PRESET_NAME_RE.match(name) is None:
        raise McpPresetError("invalid MCP server name")
    return name.strip().lower()


def _custom_server_from_query(query: QueryParams) -> tuple[str, MCPServerConfig]:
    name = _validated_server_name((_query_first(query, "name") or "").strip())
    command = (_query_first(query, "command") or "").strip()
    url = (_query_first(query, "url") or "").strip()
    transport = _normalize_transport(_query_first(query, "transport"), command=command, url=url)
    if transport == "stdio" and not command:
        raise McpPresetError("stdio MCP servers require a command")
    if transport in {"sse", "streamableHttp"} and not url:
        raise McpPresetError("remote MCP servers require a URL")
    raw_timeout = (_query_first(query, "tool_timeout") or "").strip()
    tool_timeout = _DEFAULT_CUSTOM_TIMEOUT
    if raw_timeout:
        try:
            tool_timeout = max(5, min(int(raw_timeout), 600))
        except ValueError as exc:
            raise McpPresetError("tool_timeout must be an integer") from exc
    cfg = MCPServerConfig(
        type=transport,
        command=command if transport == "stdio" else "",
        args=_parse_string_list(_query_first(query, "args")),
        env=_parse_string_map(_query_first(query, "env")),
        cwd=(_query_first(query, "cwd") or "").strip() if transport == "stdio" else "",
        url=url if transport in {"sse", "streamableHttp"} else "",
        headers=_parse_string_map(_query_first(query, "headers")),
        tool_timeout=tool_timeout,
        enabled_tools=_parse_enabled_tools(_query_first(query, "enabled_tools")),
    )
    return name, cfg


def _mcp_server_config(name: str, raw: Any) -> tuple[str, MCPServerConfig]:
    server_name = _validated_server_name(name)
    if not isinstance(raw, Mapping):
        raise McpPresetError(f"MCP server '{server_name}' must be an object")
    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()
    transport_value = str(raw.get("type", raw.get("transport", "")) or "")
    transport = _normalize_transport(transport_value, command=command, url=url)
    if transport == "stdio" and not command:
        raise McpPresetError(f"MCP server '{server_name}' stdio transport requires a command")
    if transport in {"sse", "streamableHttp"} and not url:
        raise McpPresetError(f"MCP server '{server_name}' remote transport requires a URL")
    args = raw.get("args") or []
    env = raw.get("env") or {}
    headers = raw.get("headers") or {}
    cwd = str(raw.get("cwd") or "").strip()
    enabled_tools = raw.get("enabledTools", raw.get("enabled_tools", ["*"]))
    tool_timeout = raw.get("toolTimeout", raw.get("tool_timeout", _DEFAULT_CUSTOM_TIMEOUT))
    try:
        timeout_int = max(5, min(int(tool_timeout), 600))
    except (TypeError, ValueError):
        timeout_int = _DEFAULT_CUSTOM_TIMEOUT
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise McpPresetError(f"MCP server '{server_name}' args must be a string array")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise McpPresetError(f"MCP server '{server_name}' env must be a string object")
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise McpPresetError(f"MCP server '{server_name}' headers must be a string object")
    if not isinstance(enabled_tools, list) or not all(isinstance(item, str) for item in enabled_tools):
        enabled_tools = ["*"]
    return server_name, MCPServerConfig(
        type=transport,
        command=command if transport == "stdio" else "",
        args=args,
        env=dict(env),
        cwd=cwd if transport == "stdio" else "",
        url=url if transport in {"sse", "streamableHttp"} else "",
        headers=dict(headers),
        tool_timeout=timeout_int,
        enabled_tools=list(enabled_tools),
    )


def _import_mcp_servers(raw_json: str | None) -> dict[str, MCPServerConfig]:
    parsed = _parse_json_value(raw_json, fallback=None)
    if not isinstance(parsed, Mapping):
        raise McpPresetError("MCP config must be a JSON object")
    servers = parsed.get("mcpServers", parsed)
    if not isinstance(servers, Mapping):
        raise McpPresetError("MCP config must contain mcpServers")
    out: dict[str, MCPServerConfig] = {}
    for name, raw_server in servers.items():
        if not isinstance(name, str):
            raise McpPresetError("MCP server names must be strings")
        server_name, cfg = _mcp_server_config(name, raw_server)
        out[server_name] = cfg
    if not out:
        raise McpPresetError("MCP config contains no servers")
    return out


def custom_mcp_action(action: str, query: QueryParams) -> dict[str, Any]:
    config = load_config()
    if action == "custom":
        name, cfg = _custom_server_from_query(query)
        config.tools.mcp_servers[name] = cfg
        save_config(config)
        payload = mcp_presets_payload(last_action=_server_action_message(action, name))
        payload["requires_restart"] = True
        return payload

    if action in {"import", "import-cursor"}:
        servers = _import_mcp_servers(_query_first(query, "config"))
        config.tools.mcp_servers.update(servers)
        save_config(config)
        payload = mcp_presets_payload(last_action={
            "ok": True,
            "message": f"Imported {len(servers)} MCP server(s).",
        })
        payload["requires_restart"] = True
        return payload

    if action == "tools":
        name = _validated_server_name((_query_first(query, "name") or "").strip())
        cfg = config.tools.mcp_servers.get(name)
        if cfg is None:
            raise McpPresetError("unknown MCP server", status=404)
        cfg.enabled_tools = _parse_enabled_tools(_query_first(query, "enabled_tools"))
        config.tools.mcp_servers[name] = cfg
        save_config(config)
        payload = mcp_presets_payload(last_action=_server_action_message(action, name))
        payload["requires_restart"] = True
        return payload

    raise McpPresetError(f"unknown MCP action '{action}'", status=404)


def mcp_presets_action(action: str, query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name:
        raise McpPresetError("missing MCP preset name")
    preset = _preset_by_name_optional(name)

    config = load_config()
    existing = config.tools.mcp_servers.get(name)

    if action == "enable":
        if preset is None:
            raise McpPresetError("unknown MCP preset", status=404)
        config.tools.mcp_servers[preset.name] = _materialize_server(preset, query, existing)
        save_config(config)
        payload = mcp_presets_payload(last_action=_action_message(action, preset))
        payload["requires_restart"] = True
        return payload

    if action == "remove":
        if preset is None and name not in config.tools.mcp_servers:
            raise McpPresetError("unknown MCP server", status=404)
        removed_runtime_files = False
        cleanup_error = ""
        if name in config.tools.mcp_servers:
            existing_cfg = config.tools.mcp_servers[name]
            try:
                removed_runtime_files = _remove_managed_stdio_cwd(name, existing_cfg)
            except OSError as exc:
                cleanup_error = str(exc)
            del config.tools.mcp_servers[name]
            save_config(config)
        last_action = (
            _action_message(action, preset)
            if preset is not None
            else _server_action_message(action, name)
        )
        if removed_runtime_files:
            last_action["message"] = f"{last_action['message']} Removed managed runtime files."
            last_action["managed_paths_removed"] = [f"runtime:mcp/{name}"]
            last_action["verification"] = ["config_absent", "managed_paths_absent"]
        if cleanup_error:
            last_action["ok"] = False
            last_action["message"] = (
                f"{last_action['message']} Could not remove managed runtime files: {cleanup_error}"
            )
            last_action["verification_failed"] = ["managed_paths_absent"]
        payload = mcp_presets_payload(last_action=last_action)
        payload["requires_restart"] = True
        return payload

    if action == "test":
        raise McpPresetError("MCP preset test must run through the async test action", status=500)

    raise McpPresetError(f"unknown MCP preset action '{action}'", status=404)


def attach_mcp_hot_reload_result(
    payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Merge an agent MCP reload acknowledgement into a WebUI settings payload."""
    payload = dict(payload)
    payload["hot_reload"] = result
    payload["requires_restart"] = bool(result.get("requires_restart"))
    last_action = dict(payload.get("last_action") or {})
    base_message = str(last_action.get("message") or "").strip()
    reload_message = str(result.get("message") or "").strip()
    if reload_message:
        last_action["message"] = (
            f"{base_message} {reload_message}" if base_message else reload_message
        )
    if "ok" not in last_action:
        last_action["ok"] = bool(result.get("ok", False))
    payload["last_action"] = last_action
    return payload


async def mcp_presets_settings_action(
    action: str | None,
    query: QueryParams,
    *,
    reload_mcp: McpReload | None = None,
) -> dict[str, Any]:
    """Run a WebUI MCP preset action and hot-reload the agent when config changes."""
    if action is None:
        return mcp_presets_payload()
    if action == "test":
        return await mcp_presets_test_action(query)
    if action in _CUSTOM_ACTIONS:
        payload = await asyncio.to_thread(custom_mcp_action, action, query)
    else:
        payload = await asyncio.to_thread(mcp_presets_action, action, query)
    if reload_mcp is not None:
        payload = attach_mcp_hot_reload_result(payload, await reload_mcp())
    return payload

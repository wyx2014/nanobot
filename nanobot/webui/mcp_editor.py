"""Structured MCP configuration commands for authenticated desktop clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, resolve_config_env_vars, save_config
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.runtime.mcp_diagnostics import capture_connections, record_connection
from nanobot.webui.mcp_presets_api import (
    McpPresetError,
    _known_preset_names,
    _normalize_transport,
    _validated_server_name,
    mcp_presets_action,
    mcp_presets_payload,
)

MAX_CONFIG_BYTES = 1024 * 1024
_CONFIG_LOCK = threading.RLock()
_FIELDS = {
    "command", "args", "env", "cwd", "url", "headers", "type", "transport",
    "displayName", "display_name", "enabled", "disabled", "enabledTools", "enabled_tools",
    "toolTimeout", "tool_timeout", "connectTimeout", "connect_timeout",
}


def _identifier(display: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", display.lower()).strip("-_")[:48]
    return slug or "mcp-" + hashlib.sha256(display.encode()).hexdigest()[:10]


def _string_map(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in value.items()
    ):
        raise McpPresetError(f"{field}: expected key/value text pairs")
    return value


def _timeout(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= maximum:
        raise McpPresetError(f"{field}: expected 1-{maximum} seconds")
    return int(value)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise McpPresetError(f"{field}: expected true or false")
    return value


def _normalize_variables(value: Any) -> Any:
    if isinstance(value, str):
        normalized = re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", r"${\1}", value)
        variables = re.findall(r"\$\{([^}]+)\}", normalized)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) or item in {
            "workspaceFolder", "workspaceFolderBasename", "userHome", "pathSeparator",
        } for item in variables):
            raise McpPresetError("Unsupported path or environment placeholder; replace it with a concrete value")
        return normalized
    if isinstance(value, list):
        return [_normalize_variables(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_variables(item) for key, item in value.items()}
    return value


def _server(raw: Any, display: str) -> MCPServerConfig:
    if not isinstance(raw, dict):
        raise McpPresetError("Server configuration must be a JSON object")
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise McpPresetError("Unsupported configuration fields: " + ", ".join(unknown))
    raw = _normalize_variables(raw)
    command, url = raw.get("command", ""), raw.get("url", "")
    if not isinstance(command, str) or not isinstance(url, str):
        raise McpPresetError("Command and URL must be text")
    transport = _normalize_transport(raw.get("type", raw.get("transport")), command=command, url=url)
    if transport == "stdio" and not command.strip():
        raise McpPresetError("Select a local executable")
    if transport != "stdio":
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise McpPresetError("Enter a valid HTTP or HTTPS MCP endpoint") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise McpPresetError("Enter a valid HTTP or HTTPS MCP endpoint")
        if parsed.username or parsed.password:
            raise McpPresetError("Use authentication headers instead of credentials in the URL")
    args = raw.get("args", [])
    enabled_tools = raw.get("enabledTools", raw.get("enabled_tools", ["*"]))
    for field, value in (("args", args), ("enabledTools", enabled_tools)):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise McpPresetError(f"{field}: expected a text array")
    enabled = _boolean(raw.get("enabled", True), "enabled")
    if "disabled" in raw:
        enabled = not _boolean(raw["disabled"], "disabled")
    label = raw.get("displayName", raw.get("display_name", display))
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise McpPresetError("Display name must contain 1-120 characters")
    cwd = raw.get("cwd", "")
    if not isinstance(cwd, str):
        raise McpPresetError("Working directory must be text")
    return MCPServerConfig(
        display_name=label.strip(), enabled=enabled, type=transport,
        command=command.strip() if transport == "stdio" else "", args=args,
        cwd=cwd.strip() if transport == "stdio" else "",
        url=url.strip() if transport != "stdio" else "",
        env=_string_map(raw.get("env", {}), "env"),
        headers=_string_map(raw.get("headers", {}), "headers"),
        connect_timeout=_timeout(raw.get("connectTimeout", raw.get("connect_timeout", 15)), "connectTimeout", 300),
        tool_timeout=_timeout(raw.get("toolTimeout", raw.get("tool_timeout", 30)), "toolTimeout", 600),
        enabled_tools=enabled_tools,
    )


def _edit_server(values: dict, config: Config) -> tuple[str, MCPServerConfig]:
    original = values.get("original_name")
    existing = config.tools.mcp_servers.get(original) if isinstance(original, str) else None
    if original and existing is None:
        raise McpPresetError("The server was removed. Refresh the list before saving.", status=409)
    display = values.get("display_name", original or "")
    if not isinstance(display, str) or not display.strip():
        raise McpPresetError("Enter a service name")
    name = _validated_server_name(values.get("name") or original or _identifier(display))
    if existing is not None and (not values.get("name") or values["name"] == original):
        name = original
    if original and name != original:
        raise McpPresetError("The internal server identifier cannot be changed during editing")
    if not original and name in _known_preset_names():
        raise McpPresetError("This identifier belongs to a built-in service; choose another identifier", status=409)
    if not original and name in config.tools.mcp_servers:
        raise McpPresetError("A service with this identifier already exists. Edit it or choose another identifier.", status=409)
    raw = existing.model_dump() if existing else {}
    raw.update({key: value for key, value in values.items() if key in _FIELDS})
    raw["display_name"] = display
    for field in ("env", "headers"):
        patch = values.get(field + "_patch", {})
        if not isinstance(patch, dict):
            raise McpPresetError(f"Invalid {field} patch")
        merged = dict(raw.get(field, {}))
        for key, value in patch.items():
            if not isinstance(key, str) or not key.strip() or (value is not None and not isinstance(value, str)):
                raise McpPresetError(f"Invalid {field} entry")
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        raw[field] = merged
    if "transport" in values:
        raw["type"] = values["transport"]
    return name, _server(raw, display)


def _import_entries(values: dict) -> list[tuple[str, str, MCPServerConfig | None, list[str]]]:
    text = values.get("config")
    if not isinstance(text, str) or len(text.encode()) > MAX_CONFIG_BYTES:
        raise McpPresetError("MCP configuration must be text smaller than 1 MiB")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise McpPresetError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        parsed = json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise McpPresetError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(parsed, dict):
        raise McpPresetError("Expected a JSON object containing mcpServers")
    servers = parsed.get("mcpServers", parsed)
    if not isinstance(servers, dict) or not servers or len(servers) > 100:
        raise McpPresetError("Import must contain 1-100 MCP servers")
    entries = []
    used = set()
    for original, raw in servers.items():
        name = _identifier(original)
        errors = []
        cfg = None
        if name in used:
            errors.append("Duplicate internal identifier after normalization; rename this entry")
        used.add(name)
        try:
            cfg = _server(raw, original)
        except (McpPresetError, ValueError) as exc:
            errors.append(str(exc))
        entries.append((original, name, cfg, errors))
    return entries


def _resolved_server(cfg: MCPServerConfig) -> MCPServerConfig:
    temporary = Config()
    temporary.tools.mcp_servers = {"probe": cfg}
    return resolve_config_env_vars(temporary).tools.mcp_servers["probe"]


def _preview(values: dict) -> dict:
    config = load_config()
    rows = []
    for original, name, cfg, errors in _import_entries(values):
        warnings = []
        if cfg:
            try:
                _resolved_server(cfg.model_copy(update={"enabled": True}))
            except ValueError:
                warnings.append("Some referenced environment variables are not set in the gateway")
        rows.append({
            "original_name": original, "name": name, "display_name": cfg.display_name if cfg else original,
            "transport": cfg.type if cfg else "", "enabled": cfg.enabled if cfg else False,
            "conflict": name in config.tools.mcp_servers or name in _known_preset_names(),
            "errors": errors, "warnings": warnings,
        })
    return {**mcp_presets_payload(), "import_preview": rows}


def _mutate(action: str, values: dict) -> list[str]:
    with _CONFIG_LOCK:
        config = load_config()
        previous = {name: cfg.model_dump() for name, cfg in config.tools.mcp_servers.items()}
        if action == "save":
            name, cfg = _edit_server(values, config)
            config.tools.mcp_servers[name] = cfg
            names = [name]
        elif action == "import":
            decisions = values.get("decisions", {})
            if not isinstance(decisions, dict):
                raise McpPresetError("Invalid import decisions")
            names = []
            for original, name, cfg, errors in _import_entries(values):
                decision = decisions.get(original, {})
                if not isinstance(decision, dict):
                    raise McpPresetError("Invalid import decision")
                mode = decision.get("action", "add")
                if mode == "skip":
                    continue
                if mode not in {"add", "replace", "rename"}:
                    raise McpPresetError("Unknown import conflict action")
                if errors or cfg is None:
                    raise McpPresetError(f"{original}: " + "; ".join(errors))
                if mode == "rename":
                    name = _validated_server_name(decision.get("name", ""))
                if name in names:
                    raise McpPresetError("Multiple imported entries have the same identifier", status=409)
                if (name in config.tools.mcp_servers or name in _known_preset_names()) and mode != "replace":
                    raise McpPresetError(f"{name}: choose replace, rename or skip", status=409)
                config.tools.mcp_servers[name] = cfg
                names.append(name)
            if not names:
                raise McpPresetError("No services selected for import")
        elif action == "toggle":
            raw_name = values.get("name", "")
            name = _validated_server_name(raw_name)
            if raw_name in config.tools.mcp_servers:
                name = raw_name
            if name not in config.tools.mcp_servers:
                raise McpPresetError("Server not found", status=404)
            config.tools.mcp_servers[name].enabled = _boolean(values.get("enabled"), "enabled")
            names = [name]
        else:
            raise McpPresetError("Unsupported mutation")
        for name in names:
            cfg = config.tools.mcp_servers[name]
            if cfg.enabled:
                try:
                    _resolved_server(cfg)
                except ValueError as exc:
                    raise McpPresetError(f"{name}: referenced environment variables are missing. Set them in the gateway environment or import with disabled: true.") from exc
        save_config(config)
        for name in names:
            if previous.get(name) != config.tools.mcp_servers[name].model_dump():
                record_connection(name, "pending", "Configuration saved; waiting for connection.", [])
        return names


async def _probe(values: dict) -> dict:
    config = load_config()
    name, cfg = _edit_server(values, config)
    try:
        cfg = _resolved_server(cfg.model_copy(update={"enabled": True, "enabled_tools": ["*"]}))
    except ValueError:
        return {**mcp_presets_payload(), "probe": {"ok": False, "status": "needs_auth", "message": "Referenced environment variables are missing. Set them before connecting.", "tool_names": []}}
    stacks = {}
    with capture_connections() as diagnostics:
        try:
            stacks = await connect_mcp_servers({name: cfg}, ToolRegistry())
            result = diagnostics.get(name, {})
            probe = {**result, "ok": name in stacks}
        finally:
            for stack in stacks.values():
                with suppress(Exception):
                    await stack.aclose()
    return {**mcp_presets_payload(), "probe": probe}


async def mcp_editor_action(action: str, values: dict, reload_mcp) -> dict:
    if not isinstance(action, str) or not isinstance(values, dict):
        raise McpPresetError("MCP action must be text and values must be an object")
    if len(json.dumps(values).encode()) > MAX_CONFIG_BYTES:
        raise McpPresetError("MCP request exceeds 1 MiB")
    if action == "list":
        return mcp_presets_payload()
    if action == "preview-import":
        return await asyncio.to_thread(_preview, values)
    if action == "probe":
        return await _probe(values)
    if action in {"save", "import", "toggle"}:
        names = await asyncio.to_thread(_mutate, action, values)
    elif action in {"remove", "enable", "tools", "reconnect"}:
        raw_name = values.get("name", "")
        name = _validated_server_name(raw_name)
        if raw_name in load_config().tools.mcp_servers:
            name = raw_name
        values = {**values, "name": name}
        names = [name]
        if action == "reconnect" and name not in load_config().tools.mcp_servers:
            raise McpPresetError("Server not found", status=404)
        if action == "tools":
            with _CONFIG_LOCK:
                config = load_config()
                if name not in config.tools.mcp_servers:
                    raise McpPresetError("Server not found", status=404)
                cfg = config.tools.mcp_servers[name]
                raw = cfg.model_dump()
                raw["enabled_tools"] = values.get("enabled_tools", [])
                config.tools.mcp_servers[name] = _server(raw, cfg.display_name or name)
                save_config(config)
        elif action != "reconnect":
            query = {key: [value if isinstance(value, str) else json.dumps(value)] for key, value in values.items()}
            with _CONFIG_LOCK:
                mcp_presets_action(action, query)
    else:
        raise McpPresetError("Unknown MCP management action", status=404)
    result = await reload_mcp(values.get("name") if action == "reconnect" else None)
    payload = mcp_presets_payload()
    payload["hot_reload"] = result
    payload["requires_restart"] = bool(result.get("requires_restart"))
    payload["last_action"] = {
        "ok": bool(result.get("ok")), "saved": action != "reconnect", "names": names,
        "message": result.get("message", "Configuration saved."),
    }
    return payload

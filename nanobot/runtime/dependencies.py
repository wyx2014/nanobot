"""Desktop task runtimes and package defaults. No user-wide pip/npm configuration is written."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from nanobot.config.schema import PackageSourcesConfig
from nanobot.runtime.python_commands import PYTHON_ENV_KEY, python_command_dir
from nanobot.security.network import validate_url_target

_config = PackageSourcesConfig()
_workspace_locks: dict[str, asyncio.Lock] = {}
_checks: dict[str, tuple[float, list[dict[str, str | bool]]]] = {}


def configure_package_sources(config: PackageSourcesConfig) -> None:
    global _config
    if config != _config:
        _checks.clear()
    _config = config.model_copy(deep=True)


def package_sources_config() -> PackageSourcesConfig:
    return _config.model_copy(deep=True)


def normalize_source_url(value: str, *, pypi: bool = False) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid repository URL") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(char.isspace() for char in value)
            or any(char in value for char in '"\'`\\') or port == 0):
        raise ValueError("Repository URL must be HTTP(S), without credentials, query or fragment")
    value = value.rstrip("/") + "/"
    if pypi and not urlsplit(value).path.endswith("/simple/"):
        raise ValueError("Python index URL must end with /simple/")
    return value


def package_source_cidrs(config: PackageSourcesConfig) -> list[str]:
    """Explicitly configured repository IPs use the existing SSRF allowlist mechanism."""
    if not config.enabled:
        return []
    cidrs = []
    for url in (config.npm_registry, config.pypi_index_url):
        try:
            address = ipaddress.ip_address(urlsplit(url).hostname or "")
        except ValueError:
            continue
        if not address.is_loopback and not address.is_link_local and not address.is_unspecified:
            cidrs.append(f"{address}/{address.max_prefixlen}")
    return list(dict.fromkeys(cidrs))


def runtime_bin_dirs() -> list[str]:
    directories = [str(Path(sys.executable).parent)]
    if os.name == "nt":
        directories.append(str(Path(sys.executable).parent / "Scripts"))
    node = os.environ.get("NANOBOT_NODE_BIN", "")
    if node and Path(node).is_file():
        directories.insert(0, str(Path(node).parent))
    return list(dict.fromkeys(directories))


def managed_python_enabled() -> bool:
    return _config.workspace_python or os.environ.get("NANOBOT_DESKTOP_GATEWAY") == "1"


def package_environment(
    base: dict[str, str] | None = None, *, prepare_runtime: bool = True,
) -> dict[str, str]:
    env = dict(base or {})
    if _config.enabled:
        # Environment values override ordinary user/site pip and npm defaults.
        # Explicit command flags and dependency URLs remain visible to the caller.
        for key in list(env):
            if key.lower() in {"npm_config_registry", "pip_index_url", "pip_extra_index_url",
                              "pip_config_file", "pip_trusted_host", "uv_default_index",
                              "uv_index_url", "uv_index", "uv_extra_index_url", "uv_insecure_host"}:
                env.pop(key)
        python_host = urlsplit(_config.pypi_index_url)
        insecure_host = python_host.hostname if python_host.scheme == "http" else ""
        env.update({
            "npm_config_registry": _config.npm_registry,
            "PIP_INDEX_URL": _config.pypi_index_url,
            "PIP_EXTRA_INDEX_URL": "",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_TRUSTED_HOST": insecure_host or "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_DEFAULT_INDEX": _config.pypi_index_url,
            "UV_INDEX_URL": _config.pypi_index_url,
            "UV_INDEX": "",
            "UV_EXTRA_INDEX_URL": "",
            "UV_INSECURE_HOST": insecure_host or "",
        })
    if managed_python_enabled():
        commands = python_command_dir(prepare=prepare_runtime)
        env["PATH"] = os.pathsep.join([
            str(commands), *runtime_bin_dirs(), env.get("PATH", os.environ.get("PATH", "")),
        ])
        env[PYTHON_ENV_KEY] = sys.executable
        env["PYTHON"] = sys.executable
        env["UV_PYTHON"] = sys.executable
        env["UV_PYTHON_DOWNLOADS"] = "never"
    return env


def workspace_python_path(workspace: Path) -> Path:
    return workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


async def _run(argv: list[str], *, env: dict[str, str], timeout: int = 60) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return process.returncode or 0, output.decode("utf-8", errors="replace")[-3000:]


async def prepare_workspace_environment(workspace: Path, base: dict[str, str]) -> dict[str, str]:
    env = package_environment(base)
    if not _config.workspace_python:
        return env
    workspace = workspace.expanduser().resolve()
    venv = workspace / ".venv"
    python = workspace_python_path(workspace)
    async with _workspace_locks.setdefault(str(workspace), asyncio.Lock()):
        if venv.is_symlink():
            raise ValueError("Workspace .venv is a symlink; choose a local virtual environment")
        if not python.is_file():
            if venv.exists():
                raise ValueError("Workspace .venv is incomplete. Repair or rename it before retrying; it was preserved")
            workspace.mkdir(parents=True, exist_ok=True)
            code, output = await _run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv)], env=env,
            )
            if code:
                raise ValueError(f"Python environment creation failed (exit {code}): {output}")
        if not (venv / "pyvenv.cfg").is_file():
            raise ValueError("Workspace .venv has no pyvenv.cfg; it was preserved")
    env["VIRTUAL_ENV"] = str(venv)
    env["PYTHONNOUSERSITE"] = "1"
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    env[PYTHON_ENV_KEY] = str(python)
    env["PYTHON"] = str(python)
    env["UV_PYTHON"] = str(python)
    env["PATH"] = os.pathsep.join([
        str(python_command_dir()), str(python.parent), env.get("PATH", ""),
    ])
    return env


def restore_task_environment(command: str, env: dict[str, str]) -> str:
    """Restore managed defaults after a Unix login profile has run."""
    keys = {"PATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONNOUSERSITE",
            PYTHON_ENV_KEY, "PYTHON", "UV_PYTHON", "UV_PYTHON_DOWNLOADS"}
    if _config.enabled:
        keys.update(key for key in package_environment(prepare_runtime=False) if key.startswith(("PIP_", "UV_", "npm_config_")))
    exports = []
    if _config.enabled:
        exports.append("unset NPM_CONFIG_REGISTRY")
    for key in sorted(keys & env.keys()):
        saved_key = f"NANOBOT_TASK_{key}"
        env[saved_key] = env[key]
        exports.append(f'export {key}="${saved_key}"')
    return "; ".join([*exports, command])


def dependency_status() -> dict:
    env = package_environment(prepare_runtime=False)
    return {
        **_config.model_dump(),
        "python_path": sys.executable,
        "npm_path": shutil.which("npm", path=env.get("PATH")) or "",
        "node_path": shutil.which("node", path=env.get("PATH")) or "",
        "uv_path": shutil.which("uv", path=env.get("PATH")) or "",
    }


async def check_package_sources(*, force: bool = False) -> list[dict[str, str | bool]]:
    if not _config.enabled:
        return []
    key = _config.model_dump_json()
    cached = _checks.get(key)
    if not force and cached and time.monotonic() - cached[0] < 60:
        return cached[1]

    async def probe(name: str, url: str) -> dict[str, str | bool]:
        ok, error = validate_url_target(url)
        if not ok:
            return {"name": name, "ok": False, "code": "network_policy", "message": error}
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    status = response.status_code
            if status == 200:
                return {"name": name, "ok": True, "code": "ready", "message": "Package index reachable"}
            code = "authentication_required" if status in {401, 403} else "package_not_found" if status == 404 else "http_error"
            return {"name": name, "ok": False, "code": code, "message": f"HTTP {status}: {url}"}
        except httpx.HTTPError as exc:
            return {"name": name, "ok": False, "code": "unreachable", "message": f"Repository unavailable: {type(exc).__name__}"}

    checks = await asyncio.gather(
        probe("npm", _config.npm_registry.rstrip("/") + "/docx"),
        probe("pip", _config.pypi_index_url.rstrip("/") + "/python-docx/"),
    )
    _checks[key] = (time.monotonic(), list(checks))
    return list(checks)


def install_command_family(command: str) -> str | None:
    if re.search(r"\b(?:npm|npx)(?:\.cmd)?\s+(?:install|i|ci|add|exec|--yes|-y|@)", command):
        return "npm"
    if re.search(r"\b(?:pip\d*|uv)(?:\.exe)?\s+(?:pip\s+)?(?:install|download|add|sync)\b|\buvx\s+", command):
        return "pip"
    return None


async def check_install_command(command: str, env: dict[str, str]) -> None:
    if not (_config.enabled or _config.workspace_python):
        return
    family = install_command_family(command)
    if not family:
        return
    if family == "npm" and not shutil.which("npm", path=env.get("PATH")):
        raise ValueError("dependency_runtime_missing: npm is unavailable; check the bundled Node/npm runtime")
    if re.search(r"(?:^|\s)--(?:no-index|offline|no-install)(?:\s|$)", command):
        return
    for check in await check_package_sources():
        if check["name"] == family and not check["ok"] and check["code"] != "package_not_found":
            raise ValueError(f"dependency_{check['code']}: {check['message']}. Check dependency settings; do not retry unchanged or switch to a public index")


def dependency_instructions(workspace: Path) -> str:
    if not (_config.enabled or managed_python_enabled()):
        return ""
    python = workspace_python_path(workspace) if _config.workspace_python else Path(sys.executable)
    lines = [
        "## Dependency environment",
        f"Python executable: {python}. Use this interpreter for both '-m pip' and scripts.",
    ]
    if managed_python_enabled():
        lines.append("exec provides python/python3 and pip/pip3 commands bound to this interpreter. Run scripts with python and install packages with python -m pip; do not search for or install a system Python. In Python subprocesses use sys.executable. uv also uses this interpreter without downloading another Python.")
    if _config.workspace_python:
        lines.append("exec prepares and reuses this workspace's .venv automatically before running commands. Bundled packages remain readable; install additions into .venv.")
    if _config.enabled:
        lines.extend([
            f"Package defaults: npm={_config.npm_registry}; Python/uv={_config.pypi_index_url}.",
            "Use the configured defaults, without overriding registry/index flags or switching to public sources. Reuse installed packages. If installation fails, report the package, version, and cause; do not repeat an unchanged failing command.",
            "Pinned external URLs, scoped registries, and browser/binary downloads may need a separate internal artifact source.",
        ])
    status = dependency_status()
    lines.append(f"npm executable: {status['npm_path'] or 'unavailable; check dependency settings before npm/npx tasks'}")
    return "\n".join(lines)


def normalize_python_launcher(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Resolve plain MCP Python launchers without changing explicit interpreter paths."""
    if not managed_python_enabled():
        return command, args
    if command.lower() in {"python", "python3", "python.exe", "python3.exe"}:
        return sys.executable, args
    if command.lower() in {"pip", "pip3", "pip.exe", "pip3.exe"}:
        return sys.executable, ["-m", "pip", *args]
    return command, args

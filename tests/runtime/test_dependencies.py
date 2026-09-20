from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import zipfile
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools import mcp
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config, MCPServerConfig, PackageSourcesConfig
from nanobot.runtime import dependencies as deps
from nanobot.security.network import configure_ssrf_whitelist, validate_url_target
from nanobot.webui.package_sources_api import save_package_sources


@pytest.fixture(autouse=True)
def isolated_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config.json")
    deps.configure_package_sources(PackageSourcesConfig())
    deps._checks.clear()
    yield
    deps.configure_package_sources(PackageSourcesConfig())
    configure_ssrf_whitelist([])


def test_settings_persist_apply_immediately_and_remove_derived_network_permission(tmp_path):
    config = Config()
    config.tools.ssrf_whitelist = ["192.168.20.1/32"]
    save_config(config)
    saved = save_package_sources({"enabled": True, "workspace_python": True})
    assert saved["enabled"]
    assert load_config().tools.package_sources.workspace_python
    assert deps.package_environment()["PIP_INDEX_URL"].endswith("officialPypi/simple/")
    assert validate_url_target(saved["pypi_index_url"])[0]
    assert not validate_url_target("http://10.94.211.67/")[0]
    save_package_sources({"enabled": False})
    assert "PIP_INDEX_URL" not in deps.package_environment()
    assert not validate_url_target(saved["pypi_index_url"])[0]
    assert load_config().tools.ssrf_whitelist == ["192.168.20.1/32"]


@pytest.mark.parametrize("values", [
    {"enabled": "false"}, {"something": True},
    {"pypi_index_url": "https://example.com/repository/python/"},
    {"npm_registry": "http://user:secret@example.com/"},
    {"npm_registry": "file:///tmp/packages"},
    {"npm_registry": "http://example.com/\nother"},
])
def test_settings_reject_invalid_values_without_saving(values):
    with pytest.raises(ValueError):
        save_package_sources(values)
    assert not load_config().tools.package_sources.enabled


def test_source_defaults_override_ambient_public_indexes_without_secret_inheritance(monkeypatch):
    monkeypatch.setenv("CAIHUI_MCP_API_KEY", "must-not-leak")
    deps.configure_package_sources(PackageSourcesConfig(enabled=True))
    env = deps.package_environment({"NPM_CONFIG_REGISTRY": "https://public.example/", "PIP_EXTRA_INDEX_URL": "https://extra.example/"})
    assert env["npm_config_registry"].endswith("/npm_mirror/")
    assert "NPM_CONFIG_REGISTRY" not in env
    assert env["PIP_EXTRA_INDEX_URL"] == ""
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["PIP_TRUSTED_HOST"] == "10.94.211.66"
    assert env["UV_DEFAULT_INDEX"] == env["PIP_INDEX_URL"]
    assert "CAIHUI_MCP_API_KEY" not in env
    deps.configure_package_sources(PackageSourcesConfig(enabled=True, pypi_index_url="https://packages.example/simple/"))
    assert deps.package_environment()["PIP_TRUSTED_HOST"] == ""
    assert deps.package_environment()["UV_INSECURE_HOST"] == ""


@pytest.mark.skipif(os.name == "nt", reason="Unix login profiles")
def test_task_overrides_are_restored_after_shell_profile_exports():
    deps.configure_package_sources(PackageSourcesConfig(enabled=True))
    env = deps.package_environment(dict(os.environ))
    command = deps.restore_task_environment('printf "%s" "$PIP_INDEX_URL"', env)
    result = subprocess.run(["/bin/sh", "-c", 'export PIP_INDEX_URL=https://wrong.example/; ' + command], env=env, capture_output=True, text=True, check=True)
    assert result.stdout == deps.package_sources_config().pypi_index_url


@pytest.mark.asyncio
async def test_exec_makes_workspace_python_available_and_reuses_it(tmp_path, monkeypatch):
    deps.configure_package_sources(PackageSourcesConfig(enabled=True, workspace_python=True))
    monkeypatch.setenv("NANOBOT_SECRET_TOKEN", "must-not-leak")
    tool = ExecTool(working_dir=str(tmp_path))
    code = 'import os,sys,json; print(json.dumps([sys.prefix,os.getenv("PIP_INDEX_URL"),os.getenv("NANOBOT_SECRET_TOKEN")]))'
    quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
    result = await tool.execute(command=quote(["python", "-c", code]))
    assert "Exit code: 0" in result, result
    assert "must-not-leak" not in result
    assert "officialPypi/simple/" in result
    assert ".venv" in result
    config = tmp_path / ".venv" / "pyvenv.cfg"
    modified = config.stat().st_mtime_ns
    assert "Exit code: 0" in await tool.execute(command="python -m pip --version")
    assert config.stat().st_mtime_ns == modified


@pytest.mark.asyncio
async def test_preserves_incomplete_or_external_workspace_environment(tmp_path):
    deps.configure_package_sources(PackageSourcesConfig(workspace_python=True))
    (tmp_path / ".venv").mkdir()
    marker = tmp_path / ".venv" / "user-data"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="incomplete"):
        await deps.prepare_workspace_environment(tmp_path, {})
    assert marker.read_text() == "keep"


@pytest.mark.asyncio
async def test_failed_preflight_is_actionable_and_does_not_launch_install(monkeypatch, tmp_path):
    deps.configure_package_sources(PackageSourcesConfig(enabled=True))
    monkeypatch.setattr(deps, "check_package_sources", AsyncMock(return_value=[{
        "name": "pip", "ok": False, "code": "unreachable", "message": "Repository unavailable",
    }]))
    spawn = AsyncMock()
    monkeypatch.setattr(ExecTool, "_spawn", spawn)
    result = await ExecTool(working_dir=str(tmp_path)).execute(command="python -m pip install demo")
    assert "dependency_unreachable" in result
    assert "do not retry unchanged" in result
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,command", [(False, "python -m pip install demo"), (True, "python -m pip install --no-index demo")])
async def test_disabled_or_offline_install_does_not_probe_repository(monkeypatch, enabled, command):
    deps.configure_package_sources(PackageSourcesConfig(enabled=enabled))
    probe = AsyncMock()
    monkeypatch.setattr(deps, "check_package_sources", probe)
    await deps.check_install_command(command, {})
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_stdio_gets_same_package_defaults_and_keeps_connector_env(monkeypatch):
    deps.configure_package_sources(PackageSourcesConfig(enabled=True))
    captured = []

    @asynccontextmanager
    async def transport(params):
        captured.append(params)
        yield object(), object()

    @asynccontextmanager
    async def session(*args):
        yield SimpleNamespace(initialize=AsyncMock(), list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))

    monkeypatch.setattr(mcp, "_load_mcp_client_runtime_async", AsyncMock(return_value=(session, SimpleNamespace, None, transport, None)))
    stacks = await mcp.connect_mcp_servers({"demo": MCPServerConfig(command="demo", env={"DEMO_TOKEN": "connector-only"})}, ToolRegistry())
    try:
        assert captured[0].env["npm_config_registry"].endswith("/npm_mirror/")
        assert captured[0].env["UV_DEFAULT_INDEX"].endswith("/officialPypi/simple/")
        assert captured[0].env["DEMO_TOKEN"] == "connector-only"
    finally:
        for stack in stacks.values():
            await stack.aclose()


def test_model_instructions_use_the_active_workspace_and_runtime(tmp_path):
    deps.configure_package_sources(PackageSourcesConfig(enabled=True, workspace_python=True))
    project = tmp_path / "project with spaces"
    prompt = ContextBuilder(tmp_path)._get_identity(workspace=project)
    assert str(deps.workspace_python_path(project)) in prompt
    assert "npm_mirror" in prompt
    assert "do not repeat an unchanged failing command" in prompt
    deps.configure_package_sources(PackageSourcesConfig())
    assert "Dependency environment" not in ContextBuilder(tmp_path)._get_identity()


@pytest.mark.asyncio
async def test_install_from_local_index_and_import_in_next_exec(tmp_path):
    wheel = tmp_path / "nb_env_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("nb_env_fixture.py", 'VALUE = "local-index-success"\n')
        archive.writestr("nb_env_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: nb-env-fixture\nVersion: 1.0\n")
        archive.writestr("nb_env_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("nb_env_fixture-1.0.dist-info/RECORD", "")
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path.endswith(".whl"):
                body, content_type = wheel.read_bytes(), "application/octet-stream"
            else:
                body = f'<a href="/{wheel.name}">{wheel.name}</a>'.encode()
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    deps.configure_package_sources(PackageSourcesConfig(enabled=True, workspace_python=True, npm_registry=base + "/npm/", pypi_index_url=base + "/simple/"))
    configure_ssrf_whitelist(["127.0.0.1/32"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace))
    try:
        result = await tool.execute(command="python -m pip install --no-deps --no-cache-dir nb-env-fixture==1.0", timeout=60)
        assert "Exit code: 0" in result, result
        code = 'import nb_env_fixture; print(nb_env_fixture.VALUE)'
        quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
        result = await tool.execute(command=quote(["python", "-c", code]))
        assert "local-index-success" in result, result
        assert "/simple/nb-env-fixture/" in visited
        assert any(path.endswith(".whl") for path in visited)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
async def test_npm_install_node_and_npx_share_the_bundled_runtime_and_local_registry(tmp_path, monkeypatch):
    node = os.environ.get("NANOBOT_NODE_BIN") or shutil.which("node")
    if not node:
        pytest.skip("Node runtime unavailable")
    monkeypatch.setenv("NANOBOT_NODE_BIN", node)
    monkeypatch.setenv("NANOBOT_DESKTOP_GATEWAY", "1")
    name = "nb-npm-env-fixture"
    package = {"name": name, "version": "1.0.0", "main": "index.js", "bin": {name: "cli.js"}}
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for filename, content in {
            "package.json": json.dumps(package),
            "index.js": 'module.exports = "local-npm-success";',
            "cli.js": '#!/usr/bin/env node\nconsole.log("local-npx-success");\n',
        }.items():
            encoded = content.encode()
            info = tarfile.TarInfo("package/" + filename)
            info.size = len(encoded)
            info.mode = 0o755 if filename == "cli.js" else 0o644
            archive.addfile(info, io.BytesIO(encoded))
    tarball = data.getvalue()
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path == f"/npm/{name}":
                version = {**package, "dist": {
                    "tarball": f"http://127.0.0.1:{self.server.server_port}/fixture.tgz",
                    "shasum": hashlib.sha1(tarball).hexdigest(),
                }}
                body = json.dumps({"name": name, "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": version}}).encode()
            elif self.path == "/fixture.tgz":
                body = tarball
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if self.path.endswith(".tgz") else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    deps.configure_package_sources(PackageSourcesConfig(enabled=True, npm_registry=base + "/npm/", pypi_index_url=base + "/simple/"))
    configure_ssrf_whitelist(["127.0.0.1/32"])
    workspace = tmp_path / "npm workspace"
    workspace.mkdir()
    tool = ExecTool(working_dir=str(workspace))
    quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
    try:
        result = await tool.execute(command=f"npm install --ignore-scripts --no-audit --fund=false --cache .npm-cache {name}@1.0.0", login=False, timeout=60)
        assert "Exit code: 0" in result, result
        result = await tool.execute(command=quote(["node", "-e", f"console.log(require('{name}'))"]), login=False)
        assert "local-npm-success" in result, result
        result = await tool.execute(command=f"npx --no-install --cache .npm-cache {name}", login=False)
        assert "local-npx-success" in result, result
        assert f"/npm/{name}" in visited
        assert "/fixture.tgz" in visited
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)

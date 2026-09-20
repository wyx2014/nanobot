"""Python entrypoints work without a system Python, regardless of package-source toggles."""

import json
import os
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools import mcp
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import MCPServerConfig, PackageSourcesConfig
from nanobot.runtime import dependencies as deps
from nanobot.runtime.python_commands import PYTHON_ENV_KEY, python_command_dir


@pytest.fixture(autouse=True)
def desktop_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config" / "config.json")
    monkeypatch.setenv("NANOBOT_DESKTOP_GATEWAY", "1")
    # Keep only OS utilities on PATH; do not rely on the developer's installed Python.
    system_path = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32") if os.name == "nt" else "/usr/bin:/bin"
    monkeypatch.setenv("PATH", system_path)
    deps.configure_package_sources(PackageSourcesConfig())
    yield
    deps.configure_package_sources(PackageSourcesConfig())


def quote(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("launcher", ["python", "python3", "pip", "pip3"])
async def test_default_launchers_use_gateway_python_without_system_python(tmp_path, launcher):
    tool = ExecTool(working_dir=str(tmp_path))
    if launcher.startswith("pip"):
        result = await tool.execute(f"{launcher} --version", login=False)
        expected = subprocess.check_output([sys.executable, "-m", "pip", "--version"], text=True).strip()
    else:
        result = await tool.execute(quote([launcher, "-c", "import sys; print(sys.executable)"]), login=False)
        expected = sys.executable
    assert "Exit code: 0" in result, result
    assert expected in result, result
    assert not (tmp_path / ".venv").exists()


@pytest.mark.asyncio
async def test_both_python_names_and_pip_share_the_workspace_environment(tmp_path):
    deps.configure_package_sources(PackageSourcesConfig(workspace_python=True))
    tool = ExecTool(working_dir=str(tmp_path))
    for name in ("python", "python3"):
        result = await tool.execute(quote([name, "-c", "import sys; print(sys.prefix)"]), login=False)
        assert str(tmp_path / ".venv") in result, result
        assert "Exit code: 0" in result, result
    environment = await deps.prepare_workspace_environment(tmp_path, {})
    assert environment[PYTHON_ENV_KEY] == str(deps.workspace_python_path(tmp_path))
    assert environment["UV_PYTHON"] == environment[PYTHON_ENV_KEY]
    assert environment["PYTHON"] == environment[PYTHON_ENV_KEY]
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert "Exit code: 0" in await tool.execute("pip3 --version", login=False)


@pytest.mark.skipif(os.name == "nt", reason="Unix shell-profile and PATH ordering")
@pytest.mark.asyncio
async def test_runtime_wins_over_a_different_python_on_configured_path(tmp_path):
    other = tmp_path / "other-python"
    other.mkdir()
    launcher = other / "python"
    launcher.write_text("#!/bin/sh\nprintf wrong-python")
    launcher.chmod(0o755)
    tool = ExecTool(working_dir=str(tmp_path), path_prepend=str(other))
    result = await tool.execute(quote(["python", "-c", "import sys; print(sys.executable)"]), login=False)
    assert sys.executable in result, result
    assert "wrong-python" not in result
    env = deps.package_environment({"PATH": "/usr/bin:/bin"})
    command = deps.restore_task_environment("python --version", env)
    result = subprocess.run(
        ["/bin/sh", "-c", 'export PATH=/usr/bin:/bin NANOBOT_PYTHON_EXECUTABLE=/missing; ' + command],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_long_running_exec_uses_the_same_default_python(tmp_path):
    result = await ExecTool(working_dir=str(tmp_path)).execute(
        quote(["python3", "-c", "import sys; print(sys.executable)"]),
        login=False, yield_time_ms=3000,
    )
    assert sys.executable in result, result
    assert "Exit code: 0" in result, result


def test_model_keeps_python_guidance_with_both_settings_off(tmp_path):
    prompt = deps.dependency_instructions(tmp_path)
    assert sys.executable in prompt
    assert "python -m pip" in prompt
    assert "Package defaults:" not in prompt
    # Merely describing the runtime must not write command files or a workspace venv.
    assert not python_command_dir(prepare=False).exists()


@pytest.mark.asyncio
async def test_mcp_uses_exact_embedded_python_and_preserves_explicit_interpreters(monkeypatch):
    captured = []

    @asynccontextmanager
    async def transport(params):
        captured.append(params)
        yield object(), object()

    @asynccontextmanager
    async def session(*args):
        yield SimpleNamespace(initialize=AsyncMock(), list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))

    monkeypatch.setattr(mcp, "_load_mcp_client_runtime_async", AsyncMock(return_value=(session, SimpleNamespace, None, transport, None)))
    stacks = await mcp.connect_mcp_servers({"demo": MCPServerConfig(command="python3", args=["server.py"])}, ToolRegistry())
    try:
        assert captured[0].command == sys.executable
        assert captured[0].args == ["server.py"]
        assert captured[0].env[PYTHON_ENV_KEY] == sys.executable
        assert deps.normalize_python_launcher("/custom/python", ["server.py"]) == ("/custom/python", ["server.py"])
    finally:
        for stack in stacks.values():
            await stack.aclose()


@pytest.mark.asyncio
async def test_independent_process_environments_do_not_change_each_others_python(tmp_path):
    first = deps.package_environment()
    deps.configure_package_sources(PackageSourcesConfig(workspace_python=True))
    second = await deps.prepare_workspace_environment(tmp_path / "another project", {})
    command = quote(["python", "-c", "import json,sys; print(json.dumps(sys.prefix))"])
    first_result = await ExecTool._spawn(command, str(tmp_path), first, login=False)
    second_result = await ExecTool._spawn(command, str(tmp_path), second, login=False)
    stdout1, stderr1 = await first_result.communicate()
    stdout2, stderr2 = await second_result.communicate()
    assert first_result.returncode == 0, stderr1
    assert second_result.returncode == 0, stderr2
    assert Path(json.loads(stdout1)) == Path(sys.prefix)
    assert Path(json.loads(stdout2)) == tmp_path / "another project" / ".venv"

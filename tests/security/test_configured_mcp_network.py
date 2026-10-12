"""Configured MCP origins work across tool guards without granting whole networks."""

import socket

import pytest

from nanobot.agent.tools.shell import ExecTool, ExecToolConfig
from nanobot.config import loader
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.security import network


@pytest.fixture(autouse=True)
def isolated_network(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    monkeypatch.setattr(network, "_allowed_networks", [])
    monkeypatch.setattr(network, "_configured_mcp_origins", frozenset())

    def resolve(host, *_args):
        address = "10.20.30.40" if host.endswith(".corp") else host
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 0))]

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)


def save_server(url, **kwargs):
    config = Config()
    config.tools.mcp_servers = {"docs": MCPServerConfig(url=url, **kwargs)}
    loader.save_config(config)
    loader.load_config()
    return config


@pytest.mark.parametrize("host", [
    "10.20.30.40", "172.20.1.2", "192.168.1.2", "100.64.1.2", "127.0.0.1",
    "[fd00::123]", "[::1]", "[::ffff:10.20.30.40]", "docs.corp",
])
def test_saved_mcp_origin_passes_exec_and_web_guards(host, tmp_path):
    endpoint = f"http://{host}:32300/api/proxy/mcp?api_key=test-secret"
    save_server(endpoint)
    command = f'python -c "import requests; requests.get(\'{endpoint}\')"'
    tool = ExecTool(ExecToolConfig())
    assert tool._guard_command(command, str(tmp_path)) is None
    assert network.validate_url_target(endpoint) == (True, "")
    assert network.validate_resolved_url(endpoint) == (True, "")
    assert "test-secret" not in repr(network._configured_mcp_origins)


@pytest.mark.parametrize("target", [
    "http://other.corp:32300/mcp", "http://docs.corp:32301/mcp",
    "https://docs.corp:32300/mcp", "http://10.20.30.40:32300/mcp",
])
def test_saved_service_does_not_grant_other_origins(target):
    save_server("http://docs.corp:32300/mcp")
    assert network.contains_internal_url(f"curl {target}")
    assert not network.validate_resolved_url(target)[0]


@pytest.mark.parametrize("host", [
    "169.254.169.254", "100.100.100.200", "[fd00:ec2::254]",
    "[::ffff:169.254.169.254]", "[fe80::1]", "0.0.0.0",
])
def test_saved_service_keeps_metadata_and_link_local_blocked(host):
    endpoint = f"http://{host}/mcp"
    save_server(endpoint)
    assert network.contains_internal_url(f"curl {endpoint}")
    assert not network.validate_resolved_url(endpoint)[0]


@pytest.mark.parametrize("kwargs", [
    {"enabled": False}, {"type": "stdio", "command": "python"}, {"command": "python"},
])
def test_disabled_and_stdio_servers_do_not_grant_http_access(kwargs):
    endpoint = "http://docs.corp/mcp"
    save_server(endpoint, **kwargs)
    assert network.contains_internal_url(f"curl {endpoint}")


def test_save_change_disable_remove_and_empty_load_revoke_old_origins():
    old, new = "http://old.corp/mcp", "http://new.corp/mcp"
    config = save_server(old)
    config.tools.mcp_servers["docs"].url = new
    loader.save_config(config)
    assert network.contains_internal_url(f"curl {old}")
    assert not network.contains_internal_url(f"curl {new}")
    config.tools.mcp_servers["docs"].enabled = False
    loader.save_config(config)
    assert network.contains_internal_url(f"curl {new}")
    config.tools.mcp_servers["docs"].enabled = True
    loader.save_config(config)
    assert not network.contains_internal_url(f"curl {new}")
    config.tools.mcp_servers.clear()
    loader.save_config(config)
    assert network.contains_internal_url(f"curl {new}")
    save_server(old)
    loader.get_config_path().unlink()
    loader.load_config()
    assert network.contains_internal_url(f"curl {old}")


def test_environment_reference_grants_resolved_origin_only(monkeypatch):
    monkeypatch.setenv("MCP_TEST_ENDPOINT", "http://docs.corp:8000/mcp")
    save_server("${MCP_TEST_ENDPOINT}")
    assert not network.contains_internal_url("curl http://docs.corp:8000/mcp")
    monkeypatch.delenv("MCP_TEST_ENDPOINT")
    loader.load_config()
    assert network.contains_internal_url("curl http://docs.corp:8000/mcp")


@pytest.mark.parametrize("block_all,deny", [(True, []), (False, ["docs.corp"])])
def test_domain_policy_still_overrides_saved_services(block_all, deny):
    endpoint = "http://docs.corp/mcp"
    save_server(endpoint)
    token = network.set_network_policy(block_all=block_all, allow_domains=[], deny_domains=deny)
    try:
        assert network.contains_internal_url(f"curl {endpoint}")
        assert not network.validate_resolved_url(endpoint)[0]
    finally:
        network.reset_network_policy(token)


def test_mcp_transport_exception_cannot_redirect_to_other_saved_service():
    endpoint = "http://docs.corp/mcp"
    save_server(endpoint)
    assert not network.validate_url_target(
        endpoint, configured_service_url="http://other.corp/mcp",
    )[0]

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.config.loader import load_config
from nanobot.config.schema import PackageSourcesConfig
from nanobot.runtime.dependencies import configure_package_sources
from nanobot.security.network import configure_ssrf_whitelist
from nanobot.webui.http_utils import http_json_response, parse_query
from nanobot.webui.settings_routes import WebUISettingsRouter


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config.json")
    reload = AsyncMock(return_value={"ok": True, "requires_restart": False})
    monkeypatch.setattr("nanobot.webui.settings_routes.request_mcp_reload", reload)
    route = WebUISettingsRouter(
        bus=SimpleNamespace(), logger=logger,
        check_api_token=lambda request: request.headers.get("Authorization") == "Bearer test",
        parse_query=parse_query, json_response=http_json_response,
        error_response=lambda code, message: http_json_response({"error": message}, status=code),
        runtime_surface="native", runtime_capabilities={},
    )
    yield route, reload
    configure_package_sources(PackageSourcesConfig())
    configure_ssrf_whitelist([])


@pytest.mark.asyncio
async def test_auth_is_required_before_package_settings_can_change(router):
    route, reload = router
    path = "/api/settings/package-sources/save"
    response = await route.dispatch(Request(path, Headers({"X-Nanobot-Package-Sources": '{"enabled":true}'})), path)
    assert response.status_code == 401
    assert not load_config().tools.package_sources.enabled
    reload.assert_not_called()


@pytest.mark.asyncio
async def test_save_persists_and_reloads_stdio_mcp(router):
    route, reload = router
    path = "/api/settings/package-sources/save"
    response = await route.dispatch(Request(path, Headers({"Authorization": "Bearer test", "X-Nanobot-Package-Sources": '{"enabled":true,"workspace_python":true}'})), path)
    assert response.status_code == 200
    assert json.loads(response.body)["enabled"]
    assert load_config().tools.package_sources.workspace_python
    reload.assert_awaited_once_with(route.bus, server_name="*")


@pytest.mark.asyncio
async def test_reload_failure_keeps_saved_config_and_requests_restart(router):
    route, reload = router
    reload.return_value = {"ok": False, "requires_restart": True}
    path = "/api/settings/package-sources/save"
    response = await route.dispatch(Request(path, Headers({"Authorization": "Bearer test", "X-Nanobot-Package-Sources": '{"enabled":true}'})), path)
    assert response.status_code == 200
    assert json.loads(response.body)["requires_restart"]
    assert load_config().tools.package_sources.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected", [False, True])
async def test_saved_settings_report_partial_or_unexpected_reload_failure(router, unexpected):
    route, reload = router
    if unexpected:
        reload.side_effect = RuntimeError("bus stopped")
    else:
        reload.return_value = {"ok": False, "requires_restart": False, "message": "demo did not connect"}
    path = "/api/settings/package-sources/save"
    response = await route.dispatch(Request(path, Headers({"Authorization": "Bearer test", "X-Nanobot-Package-Sources": '{"enabled":true}'})), path)
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["mcp_reload_error"]
    assert payload["requires_restart"] is unexpected
    assert load_config().tools.package_sources.enabled


@pytest.mark.asyncio
async def test_invalid_save_does_not_reconnect_mcp(router):
    route, reload = router
    path = "/api/settings/package-sources/save"
    response = await route.dispatch(Request(path, Headers({"Authorization": "Bearer test", "X-Nanobot-Package-Sources": '{"pypi_index_url":"http://repo/"}'})), path)
    assert response.status_code == 400
    reload.assert_not_called()

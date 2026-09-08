import json

from nanobot.observability.doctor import collect_doctor, connectivity_facts


def test_connectivity_reports_configuration_without_proxy_credentials_or_addresses():
    result = connectivity_facts({
        "HTTPS_PROXY": "https://private-user:private-password@internal.example:443?secret=private",
        "ALL_PROXY": "socks5://localhost:1080", "NO_PROXY": "private.internal",
        "SSL_CERT_FILE": "/Users/private/cert.pem", "OPENAI_API_KEY": "private-api-key",
    })
    assert result["proxies"][0] == {"variable": "HTTPS_PROXY", "configured": True, "scheme": "https",
                                     "valid": True, "authentication_present": True, "port": 443, "loopback": False}
    assert result["proxies"][1]["loopback"] is True
    assert result["certificate_overrides"]["SSL_CERT_FILE"] is True
    assert "private" not in json.dumps(result)
    assert "internal.example" not in json.dumps(result)


def test_invalid_proxy_port_and_optional_presentation_failure_do_not_break_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://private:password@host:invalid")

    class Presentations:
        def diagnostic_availability(self):
            raise OSError("private source path")

    result = collect_doctor(tmp_path / "logs.sqlite", False, "unavailable", Presentations())
    assert result["overall_status"] == "fail"
    checks = {row["id"]: row for row in result["checks"]}
    assert checks["presentation.resources"]["status"] == "unknown"
    assert checks["network.configuration"]["status"] == "warning"
    assert "private" not in json.dumps(result)
    assert "credential_validity" in result["not_checked"]

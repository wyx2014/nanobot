from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.image_extract import (
    IMAGE_EXTRACT_API_KEY_ENV,
    IMAGE_EXTRACT_API_URL_ENV,
    IMAGE_EXTRACT_MODEL_ENV,
    ImageExtractionClient,
    ImageExtractTool,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
    return path


def test_client_sends_openai_compatible_multimodal_request(tmp_path: Path) -> None:
    client = ImageExtractionClient(
        api_url="http://vision.example/v1/chat/completions",
        api_key="secret",
        model="qwen-vl",
    )
    image = _png(tmp_path / "invoice.png")

    with patch(
        "nanobot.agent.tools.image_extract.urllib.request.urlopen",
        return_value=_Response({"choices": [{"message": {"content": "金额：100.00"}}]}),
    ) as urlopen:
        result = client.extract(image, "提取金额", 2048)

    assert result == "金额：100.00"
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert request.get_header("Authorization") == "Bearer secret"
    assert payload["model"] == "qwen-vl"
    assert payload["max_tokens"] == 2048
    assert payload["messages"][0]["content"][0]["text"] == "提取金额"
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.asyncio
async def test_tool_extracts_workspace_image_without_shell_python(tmp_path: Path) -> None:
    image = _png(tmp_path / "receipt.png")
    client = ImageExtractionClient(api_url="http://vision.example", api_key="key", model="vl")
    tool = ImageExtractTool(
        workspace=tmp_path,
        restrict_to_workspace=True,
        client=client,
    )

    with patch.object(client, "extract", return_value="识别结果") as extract:
        result = await tool.execute(image_path="receipt.png")

    assert result == "识别结果"
    extract.assert_called_once_with(image, "提取图中的全部内容", 4096)


@pytest.mark.asyncio
async def test_tool_rejects_path_outside_restricted_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _png(tmp_path / "outside.png")
    client = ImageExtractionClient(api_url="http://vision.example", api_key="key", model="vl")
    tool = ImageExtractTool(
        workspace=workspace,
        restrict_to_workspace=True,
        client=client,
    )

    result = await tool.execute(image_path=str(outside))

    assert result.startswith("Error:")
    assert "outside allowed directory" in result


def test_tool_availability_requires_managed_service_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (IMAGE_EXTRACT_API_URL_ENV, IMAGE_EXTRACT_API_KEY_ENV, IMAGE_EXTRACT_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    assert ImageExtractTool.enabled(None) is False

    monkeypatch.setenv(IMAGE_EXTRACT_API_URL_ENV, "http://vision.example")
    monkeypatch.setenv(IMAGE_EXTRACT_API_KEY_ENV, "key")
    monkeypatch.setenv(IMAGE_EXTRACT_MODEL_ENV, "qwen-vl")
    assert ImageExtractTool.enabled(None) is True

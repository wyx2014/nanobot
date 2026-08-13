from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.agent.tool_scope import build_webui_turn_tools, is_plain_social_turn
from nanobot.agent.tools.registry import ToolRegistry


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        tool = MagicMock()
        tool.name = name
        registry.register(tool)
    return registry


@pytest.mark.parametrize(
    "content",
    [
        "你好",
        "你好呀",
        "你好晚上好 今天心情如何",
        "您好，我的朋友",
        "Hello there!",
        "谢谢",
    ],
)
def test_plain_social_turn_accepts_only_self_contained_social_messages(content: str) -> None:
    assert is_plain_social_turn(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "你好，帮我分析长江电力",
        "你好，比亚迪",
        "你好，读取一下 report.pdf",
        "谢谢，再帮我搜索今天的新闻",
        "比亚迪",
    ],
)
def test_plain_social_turn_rejects_messages_with_a_real_task(content: str) -> None:
    assert is_plain_social_turn(content) is False


def test_brand_new_plain_greeting_has_no_tool_contracts() -> None:
    source = _registry(
        "read_file",
        "web_search",
        "my",
        "mcp_juyuan_company_financials",
    )

    scoped = build_webui_turn_tools(
        source,
        content="你好",
        media=[],
        metadata={"webui": True},
        has_history=False,
    )

    assert scoped.tool_names == []
    assert set(source.tool_names) == {
        "read_file",
        "web_search",
        "my",
        "mcp_juyuan_company_financials",
    }


def test_ordinary_webui_turn_excludes_unattached_mcp_tools() -> None:
    source = _registry(
        "read_file",
        "web_search",
        "mcp_juyuan_company_financials",
        "mcp_anysearch_search",
    )

    scoped = build_webui_turn_tools(
        source,
        content="帮我整理一下这份材料",
        media=[],
        metadata={"webui": True},
        has_history=False,
    )

    assert set(scoped.tool_names) == {"read_file", "web_search"}


def test_explicit_mcp_attachment_restores_only_that_server_tools() -> None:
    source = _registry(
        "web_search",
        "mcp_juyuan_company_financials",
        "mcp_anysearch_search",
        "mcp_playwright_browser_snapshot",
    )

    scoped = build_webui_turn_tools(
        source,
        content="查询公司财务数据",
        media=[],
        metadata={
            "webui": True,
            "mcp_presets": [{"name": "juyuan", "configured": True}],
        },
        has_history=False,
    )

    assert set(scoped.tool_names) == {
        "web_search",
        "mcp_juyuan_company_financials",
    }


def test_attachment_prevents_greeting_fast_path() -> None:
    source = _registry("read_file")

    scoped = build_webui_turn_tools(
        source,
        content="你好",
        media=["/tmp/report.pdf"],
        metadata={"webui": True},
        has_history=False,
    )

    assert scoped.tool_names == ["read_file"]

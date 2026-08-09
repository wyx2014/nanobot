"""Tests for structured-finance source fallback and loop protection."""

from __future__ import annotations

from nanobot.utils.runtime import (
    external_lookup_signature,
    mark_structured_finance_source_failed,
    repeated_external_lookup_error,
    structured_finance_result_failed,
    structured_finance_source,
)


def _ifind_call(query: str) -> dict[str, str]:
    return {
        "command": (
            "cd /workspace/skills/ifind-finance-data && "
            "node scripts/call-node.js stock get_stock_info "
            f"""'{{"query":"{query}"}}'"""
        )
    }


def test_structured_finance_source_detects_ifind_exec_and_juyuan_mcp() -> None:
    assert structured_finance_source("exec", _ifind_call("新易盛 300502.SZ")) == "ifind"
    assert structured_finance_source(
        "mcp_juyuan_AShareLiveQuote",
        {"query": "新易盛 300502.SZ"},
    ) == "juyuan"
    assert structured_finance_source("exec", {"command": "npm test"}) is None


def test_structured_finance_source_detects_all_team_bound_mcp_sources() -> None:
    assert structured_finance_source(
        "mcp_hexin-ifind-ds-stock-mcp_quote",
        {"query": "工商银行"},
    ) == "ifind"
    assert structured_finance_source(
        "mcp_caihui_mcp_company_financials",
        {"query": "工商银行"},
    ) == "caihui"
    assert structured_finance_source(
        "mcp_anysearch_search",
        {"query": "工商银行 年报"},
    ) == "anysearch"


def test_repeated_ifind_lookup_disables_ifind_for_different_queries() -> None:
    counts: dict[str, int] = {}
    first = _ifind_call("新易盛 300502.SZ PE PB")

    assert repeated_external_lookup_error("exec", first, counts) is None
    assert repeated_external_lookup_error("exec", first, counts) is None
    blocked = repeated_external_lookup_error("exec", first, counts)
    different = repeated_external_lookup_error(
        "exec",
        _ifind_call("天孚通信 300394.SZ PE PB"),
        counts,
    )

    assert blocked is not None
    assert "mcp_juyuan_" in blocked
    assert different is not None
    assert "Stop calling iFinD immediately" in different


def test_hard_ifind_failure_disables_follow_up_ifind_calls() -> None:
    counts: dict[str, int] = {}
    mark_structured_finance_source_failed(counts, "ifind")

    blocked = repeated_external_lookup_error(
        "exec",
        _ifind_call("新易盛 300502.SZ 财务"),
        counts,
    )

    assert blocked is not None
    assert "mcp_juyuan_" in blocked


def test_three_core_failures_route_to_anysearch_then_duckduckgo() -> None:
    counts: dict[str, int] = {}
    for source in ("ifind", "juyuan", "caihui"):
        mark_structured_finance_source_failed(counts, source)

    anysearch_instruction = repeated_external_lookup_error(
        "mcp_juyuan_company_financials",
        {"query": "missing field"},
        counts,
    )

    assert anysearch_instruction is not None
    assert "mcp_anysearch_" in anysearch_instruction

    mark_structured_finance_source_failed(counts, "anysearch")
    duckduckgo_instruction = repeated_external_lookup_error(
        "mcp_anysearch_search",
        {"query": "missing field"},
        counts,
    )

    assert duckduckgo_instruction is not None
    assert "provider=duckduckgo" in duckduckgo_instruction


def test_rotating_ifind_queries_hit_a_total_run_budget() -> None:
    counts: dict[str, int] = {}

    for index in range(8):
        assert repeated_external_lookup_error(
            "exec",
            _ifind_call(f"可比公司 {index} 财务估值"),
            counts,
        ) is None

    blocked = repeated_external_lookup_error(
        "exec",
        _ifind_call("第九家可比公司 财务估值"),
        counts,
    )

    assert blocked is not None
    assert "Stop calling iFinD immediately" in blocked
    assert "mcp_juyuan_" in blocked


def test_ifind_inner_failure_is_detected_even_when_outer_call_succeeded() -> None:
    payload = (
        '{"ok":true,"status_code":200,"data":{"result":{"content":'
        '[{"text":"call failed: status 429"}]}}}'
    )

    assert structured_finance_result_failed("ifind", payload) is True
    assert structured_finance_result_failed(
        "ifind",
        '{"ok":true,"status_code":200,"data":{"answer":"valid rows"}}',
    ) is False


def test_structured_finance_signature_is_stable_for_argument_order() -> None:
    left = external_lookup_signature(
        "mcp_juyuan_AShareLiveQuote",
        {"query": "新易盛", "limit": 5},
    )
    right = external_lookup_signature(
        "mcp_juyuan_AShareLiveQuote",
        {"limit": 5, "query": "新易盛"},
    )

    assert left == right


def test_repeated_browser_navigation_to_same_url_is_blocked() -> None:
    counts: dict[str, int] = {}
    arguments = {"url": "https://xueqiu.com/k?q=%E5%8F%AF%E8%BD%AC%E5%80%BA"}

    assert repeated_external_lookup_error("navigate", arguments, counts) is None
    assert repeated_external_lookup_error("navigate", arguments, counts) is None
    blocked = repeated_external_lookup_error("navigate", arguments, counts)

    assert blocked is not None
    assert "meaningfully different source" in blocked

from pathlib import Path

import yaml

from nanobot.webui.expert_team_materials import material_guidance


def guidance(**bodies):
    return material_guidance("中国太平", {key.replace("_", "-"): ("hash", body) for key, body in bodies.items()})


BASE = """# 中国太平（00966.HK）基础数据包
数据截止日期：2026-06-30（中报） / 2025-12-31（年报）

## 证券身份
| 字段 | 内容 | 来源 |
| --- | --- | --- |
| 证券全称 | 中国太平保险控股有限公司 | 公司公告 |
| 股票代码 | 00966.HK | 公司公告 |
| 上市市场 | 香港联合交易所主板 | 公司公告 |

## 数据缺口
| 字段 | 状态 | 影响 |
| --- | --- | --- |
| 境外财险 COR | 缺失 | 承保盈利待核验 |
| 有息负债 | 缺失 | 偿债能力待核验 |
"""


def test_concrete_role_gaps_replace_repeated_connector_failures_and_resolved_rows():
    result = guidance(data_package=BASE, business_analyst="""# 商业分析师
## 数据缺口与置信度声明
| 缺失/问题字段 | 问题描述 | 影响 | 处理方式 |
| --- | --- | --- | --- |
| iFinD数据 | 缺失 | 本轮超限停用 | 无 |
| iFinD数据 | 缺失 | 本轮超限停用 | 无 |
| A 股工具误匹配 | 中国太保（601601.SH） | 缺失 | 无 |
| 境外财险 COR | 未按地区拆解 | 无法评估各地区承保盈利 | 待核验 |
| 境外财险 COR | 缺失 | 无法评估各地区承保盈利 | 待核验 |
| 再保险分部利润 | 未详细拆解 | 业务深度不足 | 已通过公告补充数据 |
| 管理层履历 | 未系统获取 | 管理层分析不足 | 后续由 risk-assessor 补充 |
| 销售毛利率 | 缺失 | 无 | 待核验 |
## 来源冲突矩阵
| 字段 | 来源一 | 来源二 | 冲突说明 |
| --- | --- | --- | --- |
| 上市日期 | 2000-06-29 | 2000-06-29 | 一致 |
""")
    role = result["roles"]["business-analyst"]
    assert [item["title"] for item in role["material_requests"]] == ["境外财险 COR"]
    item = role["material_requests"][0]
    assert item["status"] == "reported"
    assert "2026-06-30" in item["period"]
    assert "承保盈利" in item["purpose"]
    assert "页码" in item["source"]
    assert "00966.HK" in role["research_prompt"]
    for unwanted in ("601601", "iFinD", "超限", "risk-assessor", "有息负债", "上市日期"):
        assert unwanted not in role["research_prompt"]


def test_source_outage_alone_returns_insurance_recommendations_not_confirmed_gaps():
    result = guidance(data_package=BASE.split("## 数据缺口")[0], business_analyst="""# 数据缺口
| 数据 | 状态 | 说明 |
| --- | --- | --- |
| iFinD数据 | 缺失 | 本轮超限停用 |
""")
    items = result["roles"]["business-analyst"]["material_requests"]
    assert items and all(item["status"] == "suggested" for item in items)
    assert "寿险新业务与渠道质量" in [item["title"] for item in items]
    assert all("尚未确认缺失" in item["basis"] for item in items)


def test_newer_role_evidence_does_not_revive_old_report_gaps():
    result = guidance(data_package=BASE, business_analyst="""# 商业分析师
境外财险 COR：已通过公司中报验证。

## 待核验
| 字段 | 状态 | 影响 |
| --- | --- | --- |
| 太平财险市场份额 | 尚未核验 | 竞争地位待核验 |
""")
    items = result["roles"]["business-analyst"]["material_requests"]
    assert [item["title"] for item in items] == ["太平财险市场份额"]


def test_missing_role_uses_only_relevant_shared_fields():
    result = guidance(data_package=BASE, risk_assessor="runtime deadline exceeded")
    items = result["roles"]["risk-assessor"]["material_requests"]
    assert [item["title"] for item in items] == ["有息负债"]


def test_numbered_tables_and_partially_resolved_fields_keep_concrete_requests():
    result = guidance(data_package=BASE, business_analyst="""# 商业分析师
## 待核验
| 序号 | 数据项 | 期间 | 问题描述 |
| --- | --- | --- | --- |
| 1 | 新业务价值 | 2025 年报 | 总量已核验，但渠道拆分仍待核验 |
| 2 | 境外财险 COR | 2026H1 | 未获取，已通过公告补充 |
| 3 | 境外财险 COR 精确值 | 2026H1 | 已获取整体数据并按地区拆解，已更新 |
""")
    items = result["roles"]["business-analyst"]["material_requests"]
    assert [item["title"] for item in items] == ["新业务价值"]
    assert items[0]["period"] == "2025 年报"


def test_same_metric_with_a_reported_percentage_is_one_request():
    result = guidance(data_package=BASE, business_analyst="""# 商业分析师
## 来源冲突
| 字段 | 状态 |
| --- | --- |
| 太平财险市场份额 | 未直接核验 |
## 数据缺口
| 缺失/问题字段 | 状态 | 影响 |
| --- | --- | --- |
| 太平财险市场份额 1.8% | 待核验 | 无法确认市场份额趋势 |
""")
    items = result["roles"]["business-analyst"]["material_requests"]
    assert len(items) == 1
    assert items[0]["title"] == "太平财险市场份额 1.8%"


def test_period_metadata_does_not_include_adjacent_source_and_quality_lines():
    result = guidance(data_package="""# 中国太平基础数据包
**数据截止日期**：2026-06-30（中报） / 2025-12-31（年报）\x20\x20
**数据来源层级**：Juyuan 港股系列 > 财汇 Caihui\x20\x20
**信息丰富度**：B级，部分字段缺失
""")
    assert result["research_period"] == "2026-06-30（中报） / 2025-12-31（年报）"
    for role in result["roles"].values():
        assert "Juyuan" not in role["research_prompt"]
        assert "信息丰富度" not in role["research_prompt"]


def test_portable_prompt_uses_real_framework_and_omits_local_runtime(monkeypatch, tmp_path):
    team = tmp_path / "asset-research-team"
    source = team / "source"
    (source / "skills").mkdir(parents=True)
    (team / "team.yaml").write_text(yaml.safe_dump({
        "id": "asset-research-team", "source_root": "source", "members": [
            {"id": "financial-analyst", "name": "财务分析师", "framework": "巴菲特视角"},
        ],
    }), encoding="utf-8")
    (source / "skills/investment-team.md").write_text("""### 第三步
#### 任务2：财务与估值分析
- subject: `分析公司`
- description 包含：
  1. 检查利润含金量与资本回报
  2. 金融严谨性验证（必须使用Bash调用工具，禁止心算）
     - `python3 tools/financial_rigor.py verify-market-cap`
#### 任务3：行业与竞争分析
1. 不应导出其他角色的要求
""", encoding="utf-8")
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(tmp_path))
    result = guidance(data_package=BASE, financial_analyst="""# 财务分析师
## 待核验
| 字段 | 状态 | 影响 |
| --- | --- | --- |
| 每股收益 EPS | 未返回 | 无法核验估值 |
## 财务摘要
| 字段 | 数据 | 来源 |
| --- | --- | --- |
| 营业收入 | 100 亿港元 | 公司年报 |
| 股本 | 100 亿股 | /Users/private/reports/data.md |
""")
    role = result["roles"]["financial-analyst"]
    prompt = role["research_prompt"]
    assert role["framework_source"] == "团队角色研究要求"
    assert "检查利润含金量与资本回报" in prompt
    assert "公式和输入" in prompt
    assert "营业收入 | 100 亿港元" in prompt
    for unwanted in ("/Users/", "data.md", "financial_rigor", "Bash", "subject:", "其他角色的要求"):
        assert unwanted not in prompt


def test_generic_subject_does_not_guess_a_stock_code(monkeypatch):
    monkeypatch.delenv("NANOBOT_EXPERT_TEAMS_DIR", raising=False)
    result = material_guidance("比亚迪", {"data-package": ("", "工具误匹配中国太保（601601.SH）")})
    assert "未提供可确认" in result["security_identity"]
    assert "601601" not in result["roles"]["risk-assessor"]["research_prompt"]
    assert "角色通用研究框架" == result["roles"]["risk-assessor"]["framework_source"]


def test_bundled_framework_contract_when_gui_checkout_available(monkeypatch):
    resources = Path(__file__).resolve().parents[3] / "nanobot-gui/resources/expert-teams"
    if not resources.is_dir():
        return
    monkeypatch.setenv("NANOBOT_EXPERT_TEAMS_DIR", str(resources))
    roles = guidance(data_package=BASE)["roles"]
    assert "飞轮" in roles["business-analyst"]["framework_prompt"]
    assert "自由现金流" in roles["financial-analyst"]["framework_prompt"]
    assert "产业链" in roles["industry-researcher"]["framework_prompt"]
    assert "资本配置" in roles["risk-assessor"]["framework_prompt"]

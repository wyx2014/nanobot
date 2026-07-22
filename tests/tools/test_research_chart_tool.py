from __future__ import annotations

import pytest
from PIL import Image

from nanobot.agent.tools.research_chart import CreateResearchChartTool


@pytest.mark.asyncio
async def test_create_research_chart_from_source_backed_data(tmp_path):
    output = tmp_path / "reports" / "assets" / "revenue.png"
    result = await CreateResearchChartTool(workspace=tmp_path).execute(
        output_path=str(output),
        chart_type="line",
        title="收入与利润趋势",
        data={
            "categories": ["2023", "2024", "2025"],
            "series": [
                {"name": "营收", "values": [100.0, 120.0, 132.0]},
                {"name": "净利润", "values": [12.0, 15.0, 18.0]},
            ],
        },
        unit="亿元",
        source="公司年报",
    )

    assert isinstance(result, dict)
    assert "Research chart created successfully" in result["text"]
    assert output.exists()
    assert Image.open(output).size == (1600, 900)
    assert result["files"][0]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_research_chart_rejects_misaligned_series(tmp_path):
    result = await CreateResearchChartTool(workspace=tmp_path).execute(
        output_path="reports/assets/bad.png",
        chart_type="bar",
        title="无效图表",
        data={"categories": ["A", "B"], "series": [{"name": "指标", "values": [1]}]},
        source="测试",
    )

    assert result.startswith("Error: render_failed:")

"""Deterministic, source-backed charts for research report artifacts."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.filesystem import FileToolsConfig, _FsTool
from nanobot.agent.tools.schema import ArraySchema, NumberSchema, ObjectSchema, StringSchema, tool_parameters_schema


_CHART_TYPES = ("line", "bar", "stacked_bar")
_PALETTE = ("#3156D3", "#058C78", "#7650BE", "#D97757", "#C17A16")
_WIDTH = 1600
_HEIGHT = 900


@tool_parameters(
    tool_parameters_schema(
        output_path=StringSchema(
            "PNG output path, relative to the current project. Put report figures under reports/assets/.",
            min_length=1,
        ),
        chart_type=StringSchema(
            "Chart type: line for time series, bar for comparison, or stacked_bar for composition.",
            enum=_CHART_TYPES,
        ),
        title=StringSchema("Specific chart title that states the data relationship.", min_length=1, max_length=120),
        data=ObjectSchema(
            categories=ArraySchema(StringSchema("Category or period label."), min_items=2, max_items=20),
            series=ArraySchema(
                ObjectSchema(
                    name=StringSchema("Series label.", min_length=1, max_length=80),
                    values=ArraySchema(NumberSchema(description="Observed numeric value."), min_items=2, max_items=20),
                    required=["name", "values"],
                ),
                min_items=1,
                max_items=5,
            ),
            required=["categories", "series"],
            additional_properties=False,
        ),
        unit=StringSchema("Optional displayed unit, for example 亿元、% or 万吨.", max_length=40, nullable=True),
        source=StringSchema("Required concise source label for the chart footer.", min_length=1, max_length=240),
        required=["output_path", "chart_type", "title", "data", "source"],
    )
)
class CreateResearchChartTool(_FsTool):
    """Create a polished local PNG from research data without arbitrary code execution."""

    _scopes = {"core", "subagent"}
    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @property
    def name(self) -> str:
        return "create_research_chart"

    @property
    def description(self) -> str:
        return (
            "Create a source-backed local PNG chart for a research report. Use only figures already collected "
            "from cited sources; never estimate or fabricate values merely to make a chart. After success, insert "
            "the returned relative PNG path into Markdown as ![caption](assets/file.png)."
        )

    async def execute(
        self,
        output_path: str | None = None,
        chart_type: str | None = None,
        title: str | None = None,
        data: dict[str, Any] | None = None,
        unit: str | None = None,
        source: str | None = None,
        **kwargs: Any,
    ) -> str | dict[str, Any]:
        if not output_path or not chart_type or not title or not data or not source:
            return "Error: render_failed: output_path, chart_type, title, data, and source are required"
        if chart_type not in _CHART_TYPES:
            return f"Error: render_failed: unsupported chart_type {chart_type!r}"
        try:
            output = self._resolve_write(output_path)
            if output.suffix.lower() != ".png":
                return "Error: render_failed: output_path must end in .png"
            categories, series = _normalize_chart_data(data)
            await asyncio.to_thread(_render_chart, output, chart_type, title, categories, series, unit or "", source)
        except ModuleNotFoundError as exc:
            return f"Error: dependency_missing: missing dependency: {exc.name}"
        except Exception as exc:
            return f"Error: render_failed: {exc}"

        size = output.stat().st_size
        markdown_reference = f"assets/{output.name}" if output.parent.name == "assets" else output.name
        return {
            "text": (
                "Research chart created successfully\n"
                f"chart_path: {output}\n"
                f"markdown_reference: ![{title}]({markdown_reference})\n"
                f"file_size: {size}"
            ),
            "files": [{
                "path": str(output),
                "name": output.name,
                "mime_type": "image/png",
                "size": size,
            }],
        }


def _normalize_chart_data(data: dict[str, Any]) -> tuple[list[str], list[tuple[str, list[float]]]]:
    categories_raw = data.get("categories")
    series_raw = data.get("series")
    if not isinstance(categories_raw, list) or not 2 <= len(categories_raw) <= 20:
        raise ValueError("data.categories must contain 2 to 20 labels")
    if not isinstance(series_raw, list) or not 1 <= len(series_raw) <= 5:
        raise ValueError("data.series must contain 1 to 5 series")
    categories = [str(item).strip() for item in categories_raw]
    if any(not item or len(item) > 50 for item in categories):
        raise ValueError("each category must be a non-empty label of at most 50 characters")

    series: list[tuple[str, list[float]]] = []
    for item in series_raw:
        if not isinstance(item, dict):
            raise ValueError("each series must be an object")
        name = str(item.get("name", "")).strip()
        values_raw = item.get("values")
        if not name or len(name) > 80 or not isinstance(values_raw, list) or len(values_raw) != len(categories):
            raise ValueError("each series needs a name and one value for every category")
        values = [float(value) for value in values_raw]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("chart values must be finite numbers")
        series.append((name, values))
    return categories, series


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _format_value(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _draw_text(draw: Any, xy: tuple[float, float], value: str, font: Any, fill: str, *, anchor: str | None = None) -> None:
    draw.text(xy, value, font=font, fill=fill, anchor=anchor)


def _render_chart(
    output: Path,
    chart_type: str,
    title: str,
    categories: list[str],
    series: list[tuple[str, list[float]]],
    unit: str,
    source: str,
) -> None:
    from PIL import Image, ImageDraw

    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (_WIDTH, _HEIGHT), "#F6F8FC")
    draw = ImageDraw.Draw(image)
    title_font = _font(42, bold=True)
    body_font = _font(23)
    small_font = _font(19)
    value_font = _font(18, bold=True)

    draw.rounded_rectangle((36, 32, _WIDTH - 36, _HEIGHT - 32), radius=28, fill="#FFFFFF", outline="#DFE5F1", width=2)
    _draw_text(draw, (88, 84), title, title_font, "#172033")
    if unit:
        _draw_text(draw, (88, 142), f"单位：{unit}", body_font, "#687083")

    left, top, right, bottom = 150, 220, 1480, 680
    draw.rounded_rectangle((left - 22, top - 12, right + 14, bottom + 10), radius=18, fill="#FBFCFF")
    if chart_type == "stacked_bar":
        totals = [sum(values[index] for _name, values in series) for index in range(len(categories))]
        minimum, maximum = 0.0, max(totals) if totals else 1.0
    else:
        values = [value for _name, points in series for value in points]
        minimum, maximum = min(values), max(values)
        if chart_type == "bar":
            minimum = min(0.0, minimum)
            maximum = max(0.0, maximum)
    if math.isclose(minimum, maximum):
        minimum -= 1.0
        maximum += 1.0
    padding = (maximum - minimum) * 0.12
    lower = minimum - padding if chart_type == "line" else min(0.0, minimum - padding)
    upper = maximum + padding
    scale = (bottom - top) / (upper - lower)

    for step in range(6):
        value = lower + (upper - lower) * step / 5
        y = bottom - (value - lower) * scale
        draw.line((left, y, right, y), fill="#E7ECF5", width=2)
        _draw_text(draw, (left - 22, y), _format_value(value), small_font, "#7A8498", anchor="rm")
    axis_y = bottom - (0 - lower) * scale
    if top <= axis_y <= bottom:
        draw.line((left, axis_y, right, axis_y), fill="#B9C4D7", width=2)

    count = len(categories)
    step_width = (right - left) / count
    x_positions = [left + step_width * (index + 0.5) for index in range(count)]
    for x, label in zip(x_positions, categories):
        _draw_text(draw, (x, bottom + 38), label, small_font, "#687083", anchor="mt")

    if chart_type == "line":
        for series_index, (_name, values) in enumerate(series):
            color = _PALETTE[series_index]
            points = [(x_positions[index], bottom - (value - lower) * scale) for index, value in enumerate(values)]
            draw.line(points, fill=color, width=6, joint="curve")
            for point in points:
                draw.ellipse((point[0] - 7, point[1] - 7, point[0] + 7, point[1] + 7), fill="#FFFFFF", outline=color, width=4)
    else:
        group_width = step_width * 0.70
        if chart_type == "bar":
            bar_width = group_width / len(series)
            for category_index, x in enumerate(x_positions):
                for series_index, (_name, values) in enumerate(series):
                    value = values[category_index]
                    baseline = bottom - (0 - lower) * scale
                    y = bottom - (value - lower) * scale
                    x0 = x - group_width / 2 + bar_width * series_index + 4
                    x1 = x0 + bar_width - 8
                    draw.rounded_rectangle((x0, min(y, baseline), x1, max(y, baseline)), radius=7, fill=_PALETTE[series_index])
        else:
            for category_index, x in enumerate(x_positions):
                y = bottom - (0 - lower) * scale
                for series_index, (_name, values) in enumerate(series):
                    next_y = y - values[category_index] * scale
                    draw.rounded_rectangle((x - group_width / 2, next_y, x + group_width / 2, y), radius=5, fill=_PALETTE[series_index])
                    y = next_y

    legend_x = 92
    for index, (name, _values) in enumerate(series):
        x = legend_x + index * 238
        draw.rounded_rectangle((x, 748, x + 20, 768), radius=5, fill=_PALETTE[index])
        _draw_text(draw, (x + 32, 758), name, body_font, "#3C475E", anchor="lm")
    _draw_text(draw, (88, 824), f"来源：{source}", small_font, "#7A8498")
    _draw_text(draw, (_WIDTH - 88, 824), "TPARUYI · RESEARCH FIGURE", small_font, "#8A96AD", anchor="ra")
    image.save(output, format="PNG", optimize=True)

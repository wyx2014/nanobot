# Kimi Themes: Local PPTX Profile v1

Use the selected `reference/design_system/<category>/<theme>/design.md` for visual
direction. Use this document as the authoritative source format and export contract.
This is a local adapter for the MIT-licensed open-kimi-ppt design themes. It does not
use the Kimi website, its editor, browser automation, uploads or remote fonts.

Write `deck.pptd`, `pages/*.page` and local media in the document project. Then call
`export_presentation` with the bound document ID. The gateway produces editable
`presentation.pptx` and, when a local preview renderer is installed, `preview.pdf`.
Never run upstream export scripts or install dependencies. Do not edit `.source`.

## Design and Quality

- Follow the selected theme's signature typography, palette, density and composition.
  Themes guide page design; they are not fixed company slide masters.
- Start with three representative pages when sample-first is selected: cover,
  evidence/chart page and a conclusion or action page. Do not create three covers.
- Use assertion titles, real evidence and deliberate visual hierarchy. Use local
  photos where the selected theme calls for photography. Keep charts and tables native.
- Do not use a preview screenshot as a slide background or rasterize an entire page.
- All coordinates are in points. The default canvas is 960 x 540 (16:9); 1 PPTD px
  equals 1 PowerPoint point. Scale theme measurements consistently to your canvas.
- Plan text boxes to fit their text: at least fontSize * lineHeight per line and
  enough width for Chinese glyphs. Avoid long paragraphs; split evidence across pages.
  Export checks canvas bounds but cannot guarantee font-dependent text fit.
- No fabricated business data. Label synthetic examples explicitly as sample data.
- Review the exported preview before declaring design complete. If PDF preview is
  unavailable, disclose that the PPTX still needs a visual check in PowerPoint/WPS.

## Source Structure

YAML or JSON objects; no YAML anchors/aliases. Maximum 100 pages, 500 elements/page,
2 MiB per source file, 25 MiB per media file and 100 MiB of total input reads.
Paths use forward slashes, relative to the project (including image `src` in pages).
No absolute paths, `..`, symlinks, `.source` media, URLs or data URIs.

```yaml
version: v2
title: Annual Review
size: [960, 540]
theme:
  colors: {title: '#00295F', accent: '#00ACEE', secondary: '#0064BC'}
  textStyles:
    title: {fontSize: 32, fontFamily: Microsoft YaHei, bold: true, color: '$title'}
  tableStyles:
    evidence:
      cellStyle: {fontSize: 16, border: {color: '#D5D6D9', width: 0.5}}
      firstRowStyle: {fill: {type: solid, color: '$title'}, color: '#FFFFFF', bold: true}
pages: [pages/cover.page, pages/evidence.page, pages/actions.page]
```

Allowed deck fields: `version` (must be `v2`), `title`, `size`, `theme`, `pages`.
Allowed theme groups: `colors`, `textStyles`, `tableStyles`. Use `$name` references.
Colors are quoted `#RRGGBB` or `#RRGGBBAA`. Font names refer to installed system fonts;
fonts are not embedded. Use a Chinese font installed on the user's target computer.

```yaml
pageType: content
background: {type: solid, color: '#FFFFFF'}
notes: Speaker notes
elements:
  - elementId: heading
    elementType: text
    bounds: [48, 40, 864, 80]
    content:
      style: '$title'
      text: '<p>Evidence supports <strong>the next investment</strong></p>'
```

Allowed page fields: `pageType`, `background`, `notes`, `elements` (back to front).
Every element has unique nonempty `elementId`, `elementType`, `bounds: [x,y,w,h]`.
Bounds must be inside the canvas with positive width and height. Optional `rotation`
(-360..360) works on text, shapes, lines and images; native tables/charts cannot rotate.
Optional `opacity` (0..1) is supported on text, shapes, lines and images only.
Unknown fields/types are rejected with a page and element error, never silently dropped.

## Text, Shapes, Lines and Images

- Text: `content` accepts `text`, `style`, `fontSize`, `fontFamily`, `color`, `bold`,
  `italic`, `underline`, `lineHeight` (default 1.15), `lineHeightPx`, `wrap` and
  `align: [left|center|right|justify, top|middle|bottom]`. Text style definitions
  support the same font properties. Plain text supports newlines.
- Rich text supports `p`, `span`, `strong`/`b`, `em`/`i`, `u`, `br`. Inline CSS is
  limited to `color`, `font-size` (px/pt), `font-family`, `font-weight` (400/700 or
  normal/bold), `font-style` (normal/italic). No arbitrary HTML or CSS.
- Shapes: `shapeName` is an OOXML preset name such as `rect`, `roundRect`, `ellipse`,
  `triangle`, `diamond`, `chevron`, `rightArrow`, `star5`. Optional `fill`, `border`,
  `adjustments` (preset adjustment values in 0..100000). No custom SVG paths.
- Fills: `{type: solid, color: '#RRGGBB'}` or `{type: gradient, gradientType: linear,
  angle: 0, stops: [{position: 0, color: '#FFFFFF'}, {position: 1, color: '#EEEEEE'}]}`.
  Use 2-10 ordered stops. Omitted shape fill is transparent. No image/radial fills;
  use a separate local image element behind text instead.
- Borders: `{color: '#000000', width: 1, style: solid|dash|dot|dashDot}`. Omitted
  shape border is invisible.
- Lines: `viewBox: [width,height]`, `points: 'x1,y1 x2,y2'`, `border`, optional
  `arrow: [null, arrow]` (also triangle, diamond, oval). Endpoints are scaled into
  bounds. A horizontal line still uses positive bounds height (e.g. 1).
- Images: `src: media/photo.jpg`, optional `fit: {mode: cover|contain|fill}`.
  PNG/JPEG preferred. Images embed in the PPTX; no remote image fetching occurs.

## Editable Tables

```yaml
elementId: metrics
elementType: table
bounds: [48, 160, 864, 240]
style: '$evidence'
columnWidths: [0.5, 0.25, 0.25]
rows:
  - [{text: Metric}, {text: '2025'}, {text: '2026'}]
  - [{text: Revenue}, {text: '82.5'}, {text: '96.3'}]
  - [{text: Margin}, {text: '12%'}, {text: '16%'}]
```

Use equal-length rows of cell objects (1-100 rows, 1-30 columns). Optional
`columnWidths` and `rowHeights` are positive proportions summing to 1.
Cells accept `text`, `textStyle` ($textStyles reference), the font properties above,
`align`, `fill`, `border`. A cell border may be one object or
`[top, right, bottom, left]` with null entries for invisible sides.
Theme table styles support `cellStyle`, `firstRowStyle`, `lastRowStyle`,
`firstColumnStyle`, `lastColumnStyle`, alternating `bodyStyles` and `rowOverColumn`.
No merged cells, rowSpan or colSpan. Use separate native shapes for complex matrices.

## Editable Charts

```yaml
elementId: revenue
elementType: chart
bounds: [48, 160, 600, 290]
data:
  cols: [quarter, revenue]
  rows: [[Q1, 12], [Q2, 18], [Q3, 22], [Q4, 28]]
series:
  - type: bar
    name: Revenue
    encode: {x: quarter, y: revenue}
    fill: '$accent'
legend: false
dataLabels: {show: true, content: value, numberFormat: '0'}
xAxis: {type: category, gridLine: false}
yAxis: {type: value, min: 0, gridLine: {color: '#E1E5EA', width: 0.5}}
```

- Types: `bar`, `line`, `area`, `pie`. All series in a chart must share one type
  and category column; 1-20 series and 1-500 data rows. No mixed/secondary axes.
- For bar/line/area, each series supports `type`, `name`, `encode`, `fill`, `border`,
  `stack` (omitted, `value` or `percent`; same mode across all series).
- Horizontal bars: `yAxis: {type: category}` and `encode: {y: categoryColumn, x: valueColumn}`.
- Pie: one series, `encode: {category: categoryColumn, value: valueColumn}`.
  Optional `innerRadius` (0 for pie, 0.1..0.9 for doughnut), `startAngle` (0..360),
  `fill` (color/gradient or list of colors), `border`. No axes or stack.
- Chart `legend` is a boolean or `{show, position: top|bottom|left|right}` plus
  fontSize/fontFamily/color/bold/italic. Default legend position is bottom.
- `dataLabels`: `{show, content: value|category|percentage, numberFormat}` plus
  fontSize/fontFamily/color/bold/italic. Percentage labels are for pie only.
- `xAxis`/`yAxis`: `{type: category|value, show, min, max, title, label, gridLine, axisLine}`.
  min/max apply only to value axes. `label` is a boolean or a font style plus
  `numberFormat`; `gridLine` and `axisLine` are booleans or border objects.
- Chart/axis `title`: string or text content object. Chart also accepts `fontFamily`,
  frame `fill` and `border`. Set series colors explicitly to the chosen design palette.

Animations, icons, SVG paths, shadows, merged cells, custom fonts, advanced chart
types (waterfall, scatter, heatmap, etc.) and other upstream PPTD features are not
part of this profile. Build a requested diagram from supported native shapes/lines,
or use a project-local image for a complex illustration. Preserve the chosen theme.

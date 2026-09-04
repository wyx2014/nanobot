# CorporateDeck Format

CorporateDeck is a YAML or JSON intermediate format. The model chooses semantic exhibits; the
renderer owns coordinates, fonts, brand colors, masters and OOXML. Version 1 remains backward
compatible with the original nine slide types.

## Document Fields

```yaml
version: 1
title: 2026年度经营分析
organization: 太平资产管理有限公司
author: 战略运营中心
date: 2026年8月31日
footer: 内部资料
show_page_numbers: true
slides: []
```

Only `version` and `slides` are required. Media paths are relative to the deck file and cannot leave
the project directory. A deck may contain at most one `cover` and one `closing`.

## Core Slide Types

### Cover And Closing

```yaml
- type: cover
  title: 信息科技部（2026年度重点项目跟踪）
  organization: 太平资产管理有限公司
  date: 2026年8月31日
- type: closing
```

These retain the original company prototype slides.

### Title And Body

```yaml
- type: title-body
  title: 本期核心结论
  lead: 重点项目总体按计划推进
  body: 可选的一段简短说明。
  bullets: [三项工程完成阶段验收, 下一阶段关注数据治理质量]
```

Use this only when prose is the clearest main exhibit.

### Section

```yaml
- type: section
  title: 一、经营情况回顾
  subtitle: 从规模、质量和效率三个维度展开
  image: media/business-review.jpg  # optional
  image_fit: cover
  image_focus_x: 0.65
```

### Two Column And Comparison

```yaml
- type: two-column
  title: 本期进展与下期计划
  left: {title: 本期已完成, bullets: [完成需求评审, 核心模块上线]}
  right: {title: 下期重点, bullets: [完成数据迁移, 开展用户培训]}
- type: comparison
  title: 方案对比
  left: {title: 方案A, body: 复用现有系统, bullets: [周期短, 成本低]}
  right: {title: 方案B, body: 建设统一平台, bullets: [扩展性好, 初期投入高]}
```

## Image Slide Types

### Image And Text

```yaml
- type: image-text
  title: 项目建设现场
  image: media/project.jpg
  image_position: right
  image_fit: cover           # cover or contain
  image_focus_x: 0.5         # 0 keeps the left edge, 1 keeps the right edge
  image_focus_y: 0.35        # 0 keeps the top edge, 1 keeps the bottom edge
  body: 现场设备已完成联调
  bullets: [一期建设完成, 关键设备投入运行]
  caption: 图：项目建设现场
  source: 内部项目影像库，2026-08
```

### Image Grid

Use two to six related images. Three images use one large image plus two supporting images.

```yaml
- type: image-grid
  title: 从实验室原型到真实环境部署
  source: 官方档案与项目资料，访问日期 2026-08-31
  images:
    - path: media/prototype.jpg
      caption: 早期实验室原型
      fit: cover
      focus_x: 0.4
    - path: media/field-test.jpg
      caption: 户外测试
    - path: media/deployment.jpg
      caption: 生产部署
```

### Full Image

```yaml
- type: full-image
  title: 总体建设蓝图
  image: media/blueprint.png
  image_fit: contain
  caption: 图：总体架构与建设路径
  source: 信息科技部总体设计，V3.2
```

## Editable Visual Slide Types

### Metrics

```yaml
- type: metrics
  title: 核心经营指标
  metrics:
    - {label: 营业收入, value: 12.6亿元, change: 同比 +18.2%}
    - {label: 综合成本率, value: 96.3%, change: 同比下降 1.1pct}
```

### Timeline

Use three to eight events. `highlight` uses the company green accent.

```yaml
- type: timeline
  title: Agent能力经历四个关键阶段
  lead: 每次跃迁都来自新的知识、学习或交互范式
  events:
    - {period: 1950s, title: 概念萌芽, description: 图灵测试提出机器智能问题}
    - {period: 1980s, title: 专家系统, description: 规则与知识工程规模应用}
    - {period: 2010s, title: 深度强化学习, description: 感知和决策开始端到端学习}
    - {period: 2020s, title: 大模型Agent, description: 推理、工具和记忆统一, highlight: true}
```

### Process

Use three to six ordered stages.

```yaml
- type: process
  title: 项目交付采用五阶段闭环
  steps:
    - {title: 需求澄清, description: 明确目标、边界与验收口径}
    - {title: 方案评审, description: 确认架构、风险与资源}
    - {title: 开发验证, description: 完成功能和质量门禁}
    - {title: 灰度上线, description: 小范围验证并保留回滚点}
    - {title: 运营复盘, description: 跟踪指标与问题闭环, highlight: true}
```

### Native Table

`highlight_rows` uses one-based data-row indexes. Tables remain editable.

```yaml
- type: table
  title: 三类方案在关键维度上差异明显
  lead: 推荐方案B，前提是迁移窗口不少于三个月
  columns: [维度, 方案A, 方案B, 方案C]
  column_widths: [1.4, 1, 1, 1]
  rows:
    - [建设周期, 2个月, 4个月, 6个月]
    - [扩展能力, 一般, 强, 强]
    - [迁移风险, 低, 中, 高]
  highlight_rows: [2]
  source: 项目可研报告，2026-08
```

### Native Chart

Charts remain editable and require a real source. Supported types are `column`, `bar`, `line`,
`pie` and `doughnut`; pie and doughnut accept exactly one series.

```yaml
- type: chart
  title: 服务调用量增长快于基础设施成本
  chart_type: line
  categories: [2026Q1, 2026Q2, 2026Q3, 2026Q4]
  series:
    - {name: 调用量, values: [120, 168, 224, 310]}
    - {name: 基础设施成本, values: [100, 118, 139, 162]}
  unit: 指数（2026Q1=100）
  body: 规模效应开始显现
  bullets: [调用量同比增长158%, 单位调用成本持续下降]
  source: 平台监控与财务台账，统计截至2026-08-31
```

Never invent chart values. Use `timeline`, `process`, qualitative comparison or explicit placeholders
when verified numeric data is unavailable.

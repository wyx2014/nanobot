# nanobot Skills

This directory contains built-in skills that extend nanobot's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

When skills reference large local documentation or logs, prefer nanobot's built-in
`grep` tool to narrow the search space before loading full files.
Use `grep(output_mode="count")` / `files_with_matches` for broad searches first,
use `head_limit` / `offset` to page through large result sets,
and `grep(glob="*.md")` to filter by file name pattern.

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `clawhub` | 从如意Hub搜索并安装技能 |
| `skill-creator` | Create new skills |
| `long-goal` | Sustained objectives: `long_task`, `complete_goal`, idempotent goals, modular project work, early research |
| `corporate-ppt` | 使用内置公司模板生成可编辑、可校验的 PowerPoint 演示文稿 |
| `image-extract` | 使用托管多模态服务识别图片文字或提取指定字段 |
| `portfolio-analysis` | 持仓结构体检、两期持仓变动复盘及可复核的分析报告 |
| `office-documents` | 工作材料撰写润色、会议纪要与待办、文档阅读总结 |

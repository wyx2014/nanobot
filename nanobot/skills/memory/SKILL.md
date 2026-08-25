---
name: memory
description: 由 Dream 维护的全局用户画像与会话候选记忆系统。
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Managed by Dream.** Do NOT edit.
- `memory/MEMORY.md` — Read-only legacy/restore compatibility; it is not normal chat context and Dream cannot write it. Do NOT edit.
- `memory/history.jsonl` — append-only Dream input. It contains unreviewed direct-user profile candidates and legacy archive entries; candidates are not confirmed memory and are not injected into normal chat context.

Project facts, decisions, generated research, file paths, and workflow state belong in project files
or the current session summary, not in the global user profile.

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with `cursor`, `timestamp`,
`content`, and optional source metadata such as `kind`, `source`, and `session_key`.

Entries tagged `[source: user-profile-candidate]` are staging evidence. Do not treat a candidate as a
known user fact until Dream has curated it into `USER.md` or `SOUL.md`. Expert-team candidates are
also tagged `[source: expert-team]`; team roles, rubrics, report formats, delegation mechanics, and
project facts must not be generalized into global memory.

Slash commands and structured interactive-prompt answers are workflow control, not profile evidence.

- For broad searches, start with `grep(..., path="memory", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.jsonl", output_mode="content", case_insensitive=true)`

## Important

- **Do NOT edit SOUL.md, USER.md, or MEMORY.md.** They are automatically managed by Dream.
- If you notice outdated information, it will be corrected when Dream runs next.
- Keep project-specific continuity in project files and session history; do not promote it to global memory.
- Users can view Dream's activity with the `/dream-log` command.

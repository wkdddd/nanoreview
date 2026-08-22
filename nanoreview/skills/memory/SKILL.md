---
name: memory
description: Session memory and user-editable personalization files.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style.
- `USER.md` — User profile and preferences.
- `sessions/*.jsonl` — per-session conversation history, metadata, and compact summaries.

## Session Memory

Session memory is scoped to one chat/session. It is used to continue the current conversation or review, not to create cross-session long-term facts.

- For broad searches, start with `grep(..., path="sessions", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="sessions", glob="*.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02T10:00", path="sessions", glob="*.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="sessions", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="sessions", glob="*.jsonl", output_mode="content", case_insensitive=true)`

## Important

- Do not write durable cross-session memory.
- Treat `SOUL.md` and `USER.md` as user-editable personalization files.
- Compact summaries live in session metadata and must stay scoped to that session.

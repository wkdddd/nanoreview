---
name: repo-reader
description: Used when users request repository comprehension or code navigation for a locally available repository; systematically inspects structure, entry points, source code, tests, and call chains with bounded local file tools.
---

# repo-reader

## When to Use

Use this skill when the user wants repository-level understanding or code navigation:
- The repository is already available locally (cloned by the user, or the current workspace).
- The user asks to look at, analyze, navigate, or understand a code repository.
- The user asks "帮我看看这个仓库", "分析一下这个项目", or similar phrases.
- The user wants to understand a code repository before changing it.

Do not use this skill when:
- The user asks a narrow question about files already in the current workspace; read the known files directly.
- The user only needs online documentation, API references, or external facts; use `web_search` and `web_fetch`.
- The user only asks for a small code edit in a known area; inspect the relevant local files directly.
- The user requests a structured code review; use the `nanoreview review --action diff` workflow instead of this skill.
- The user provides a remote repository URL that is not on disk: review input is local-only, so ask the user to clone it (or fetch specific files with `web_fetch`) before reviewing.

## Workflow

1. Inspect top-level files and directories.
2. Read README and project config files.
3. Identify entry points for CLI, API, backend, frontend, package exports, or services.
4. Identify core modules and supporting modules.
5. Find tests that show expected behavior.
6. Summarize the main call chain.
7. Recommend a learning path and low-risk practice tasks.

## Repository Access

- Inspect the repository with `list_dir`, focused `grep`, and bounded `read_file` calls. Start with the directory tree and key project files, then follow imports, call sites, and tests.
- Keep reads narrow and avoid repeatedly scanning the same file or the whole repository. Reuse content already in context and request a different range only when needed.
- Repository understanding is local-only. If the target is not on disk, ask the user to provide a local checkout or use `web_fetch` for specific remote files.
- Do not run `git clone` or `gh repo clone` as part of this skill.

## Rules

- Do not modify code unless the user explicitly asks.
- Prefer facts from files over assumptions.
- Do not summarize a repository from README alone.
- Do not summarize a repository from search results or isolated snippets alone.
- Read source files that establish the directory map, entry points, and call chain.
- Keep explanations beginner-friendly when the user is learning.
- Mention uncertainty clearly.
- Keep this skill exploratory and read-only. Do not create a structured finding report or claim that a diff review was completed.
- If the repository is not available locally yet, ask the user to clone it (or fetch specific files with `web_fetch`) before applying this workflow.

## Output

Respond with:

1. Project type
2. Directory map
3. Entry points
4. Core call chain
5. Extension points
6. Learning path
7. Beginner practice tasks

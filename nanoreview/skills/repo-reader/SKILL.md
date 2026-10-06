---
name: repo-reader
description: Used when users request full repository comprehension, analysis, or code review of a locally available repository; systematically inspects repository structure, entry points, source code, tests, and call chains using local files and local_review.
---

# repo-reader

## When to Use

Use this skill when the user wants repository-level understanding:
- The repository is already available locally (cloned by the user, or the current workspace).
- The user asks to look at, analyze, review, or understand a code repository.
- The user asks "帮我看看这个仓库", "分析一下这个项目", or similar phrases.
- The user wants to understand a code repository before changing it.

Do not use this skill when:
- The user asks a narrow question about files already in the current workspace; use `local_review(review_query="...")` or read the known files directly.
- The user only needs online documentation, API references, or external facts; use `web_search` and `web_fetch`.
- The user only asks for a small code edit in a known area; inspect the relevant local files directly.
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

- Inspect the repository files directly, and use `local_review(review_query="...")` to find relevant code when the important files are not obvious.
- Review input is local-only: there is no remote repository reader. If the target is not on disk, ask the user to clone it first, or fetch individual files with `web_fetch`.
- Do not run `git clone` or `gh repo clone` for review access unless the user explicitly asks outside the review workflow.

## Rules

- Do not modify code unless the user explicitly asks.
- Prefer facts from files over assumptions.
- Do not summarize a repository from README alone.
- Do not summarize a repository from review snippets alone.
- Read source files that establish the directory map, entry points, and call chain.
- Keep explanations beginner-friendly when the user is learning.
- Mention uncertainty clearly.
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

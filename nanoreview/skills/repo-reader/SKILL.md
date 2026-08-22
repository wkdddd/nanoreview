---
name: repo-reader
description: Used when users provide GitHub/GitLab repo links or request full repository comprehension, analysis, or code review; systematically inspects repository structure, entry points, source code, tests, and call chains using local files and local_review/github_review where appropriate.
---

# repo-reader

## When to Use

Use this skill when the user wants repository-level understanding:
- The user provides a GitHub, GitLab, or other git hosting URL, such as `https://github.com/user/repo`.
- The user asks to look at, analyze, review, or understand a code repository.
- The user asks "帮我看看这个仓库", "分析一下这个项目", or similar phrases.
- The user wants to understand a code repository before changing it.

Do not use this skill when:
- The user asks a narrow question about files already in the current workspace; use `local_review(review_query="...")` or read the known files directly.
- The user only needs online documentation, API references, or external facts; use `web_search` and `web_fetch`.
- The user only asks for a small code edit in a known area; inspect the relevant local files directly.

## Workflow

1. Inspect top-level files and directories.
2. Read README and project config files.
3. Identify entry points for CLI, API, backend, frontend, package exports, or services.
4. Identify core modules and supporting modules.
5. Find tests that show expected behavior.
6. Summarize the main call chain.
7. Recommend a learning path and low-risk practice tasks.

## Repository Access

- For the current local workspace, inspect files directly and use `local_review(review_query="...")` to find relevant code when the important files are not obvious.
- For GitHub repositories, start with read-only API inspection:
  - `github_review(action="meta", target_repo="owner/repo")`
  - `github_review(action="tree", target_repo="owner/repo", tree_pattern="*.py", tree_limit=500)`
  - `github_review(action="file", target_repo="owner/repo", repo_path="README.md")`
- If deep full-repository analysis is needed, use `github_review(action="repo", target_repo="owner/repo")`; it stores remote snapshots only in the fixed workspace `.nanoreview/review_github` cache.
- GitHub mode reads metadata, tree entries, and file contents through the GitHub API; it does not perform RAG over the remote repository.
- Do not run `git clone` or `gh repo clone` for review access unless the user explicitly asks outside the review workflow.

## Rules

- Do not modify code unless the user explicitly asks.
- Prefer facts from files over assumptions.
- Do not summarize a repository from README alone.
- Do not summarize a repository from review snippets alone.
- Read source files that establish the directory map, entry points, and call chain.
- Keep explanations beginner-friendly when the user is learning.
- Mention uncertainty clearly.
- If the repository URL has not been fetched yet, first use `github_review` to access the needed source files before applying this workflow.

## Output

Respond with:

1. Project type
2. Directory map
3. Entry points
4. Core call chain
5. Extension points
6. Learning path
7. Beginner practice tasks

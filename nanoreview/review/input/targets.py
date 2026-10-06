"""Extract and normalize review targets before plan construction."""
from __future__ import annotations

import re


def infer_review_target_type(target: str | None) -> str | None:
    """Local paths are the only supported review target."""
    if not target:
        return None
    return "local"


def extract_review_target(text: str) -> tuple[str, str] | None:
    local_match = re.search(r"(?i)(?:review|code\s*review|审查|评审)\s+([^\s，。；;\r\n]+)", text)
    if local_match:
        target = local_match.group(1).strip(" `\"'")
        if target:
            return target, target
    path_match = re.search(
        r"(?P<path>(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|/)[^\s`\"'，。；;]+)",
        text,
    )
    if path_match:
        target = path_match.group("path").rstrip(".,;:!?)）】]")
        if target:
            return target, target
    stripped = text.strip().strip(" `\"'")
    if stripped and not re.search(r"\s", stripped):
        if re.match(r"^[A-Za-z]:[\\/]", stripped) or stripped.startswith(("/", "./", "../", "~")):
            return stripped, stripped
    return None

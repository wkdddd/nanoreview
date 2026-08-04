"""Sanitization helpers for text persisted outside the current process."""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_BEARER_TOKEN = re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/=]+)", re.IGNORECASE)
_API_KEY_OPENAI = re.compile(r"sk-[A-Za-z0-9._-]{20,}")
_GITHUB_TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")
_SLACK_TOKEN = re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"
    r".*?"
    r"-----END (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----",
    re.DOTALL,
)
_URL_PASSWORD = re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|authorization|access[_-]?token|"
    r"refresh[_-]?token|token|secret|password)\b\s*(?:=|:)\s*)"
    r"(?P<quote>['\"]?)(?P<bearer>Bearer\s+)?(?P<value>[^'\"\s,}\]]+)"
    r"(?P=quote)"
)
_DSML_PATTERNS = [
    re.compile(r"<\|[^|]*\|>"),
    re.compile(r"</?think(?:ing)?>", re.IGNORECASE),
    re.compile(r"</?reflection>", re.IGNORECASE),
]


def sanitize_persisted_log_text(text: str) -> str:
    """Remove control markers and common credentials before persisting text."""
    if not text:
        return text
    for pattern in _DSML_PATTERNS:
        text = pattern.sub("", text)
    text = _BEARER_TOKEN.sub(rf"\1{REDACTED}", text)
    text = _API_KEY_OPENAI.sub(REDACTED, text)
    text = _GITHUB_TOKEN.sub(REDACTED, text)
    text = _SLACK_TOKEN.sub(REDACTED, text)
    text = _PRIVATE_KEY_BLOCK.sub("***REDACTED PRIVATE KEY***", text)
    text = _URL_PASSWORD.sub(rf"\1{REDACTED}\3", text)
    return _SENSITIVE_ASSIGNMENT.sub(
        rf"\g<prefix>\g<quote>\g<bearer>{REDACTED}\g<quote>", text
    )

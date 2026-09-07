from __future__ import annotations

import json

import pytest

from nanoreview.agent.tools.review_submit import SubmitReviewFindingsTool, review_submit

_DETAILS = {
    "trust_boundary": "Public request reaches the query layer",
    "attack_preconditions": "Attacker controls the parameter",
    "attack_path": "request -> handler -> database",
}


def test_review_submit_normalizes_valid_findings() -> None:
    result = review_submit([{
        "severity": "HIGH",
        "file": " src/app.py ",
        "line": 3,
        "title": " Issue ",
        "evidence": " line3 ",
        "impact": " bad ",
        "recommendation": " fix ",
        "details": _DETAILS,
    }])

    assert result.submitted is True
    assert result.errors == []
    assert result.findings == [{
        "severity": "high",
        "file": "src/app.py",
        "line": 3,
        "title": "Issue",
        "evidence": "line3",
        "impact": "bad",
        "recommendation": "fix",
        "details": _DETAILS,
    }]


def test_review_submit_accepts_empty_findings() -> None:
    result = review_submit([])

    assert result.submitted is True
    assert result.findings == []


@pytest.mark.parametrize(
    ("finding", "message"),
    [
        ({"severity": "bogus"}, "missing required fields"),
        ({
            "severity": "bogus",
            "file": "src/app.py",
            "line": 1,
            "title": "Issue",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
            "details": _DETAILS,
        }, "severity must be one of"),
        ({
            "severity": "high",
            "file": "src/app.py",
            "line": 0,
            "title": "Issue",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
            "details": _DETAILS,
        }, "line must be >= 1"),
        ({
            "severity": "high",
            "file": "src/app.py",
            "line": 1,
            "title": "Issue",
            "evidence": "line1",
            "impact": "bad",
            "recommendation": "fix",
            "details": "not an object",
        }, "details must be an object"),
    ],
)
def test_review_submit_rejects_invalid_findings(
    finding: dict[str, object],
    message: str,
) -> None:
    result = review_submit([finding])

    assert result.submitted is False
    assert result.findings == []
    assert message in result.errors[0]


@pytest.mark.asyncio
async def test_review_submit_tool_returns_canonical_json() -> None:
    tool = SubmitReviewFindingsTool()

    raw = await tool.execute(findings=[{
        "severity": "HIGH",
        "file": "src/app.py",
        "line": None,
        "title": "中文标题",
        "evidence": "证据",
        "impact": "影响",
        "recommendation": "修复",
        "details": _DETAILS,
    }])

    data = json.loads(raw)
    assert data["submitted"] is True
    assert data["findings"][0]["severity"] == "high"
    assert data["findings"][0]["line"] is None
    assert data["findings"][0]["title"] == "中文标题"
    assert data["findings"][0]["details"] == _DETAILS

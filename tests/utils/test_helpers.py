"""Tests for the tokenizer-free prompt estimate used by review budgeting."""
from __future__ import annotations

from nanoreview.utils.helpers import estimate_text_tokens_upper_bound


def test_empty_text_costs_nothing() -> None:
    assert estimate_text_tokens_upper_bound("") == 0


def test_ascii_text_is_estimated_at_three_characters_per_token() -> None:
    assert estimate_text_tokens_upper_bound("a" * 300) == 100
    # Short remainders still cost a token instead of rounding to zero.
    assert estimate_text_tokens_upper_bound("ab") == 1


def test_non_ascii_text_is_counted_per_character() -> None:
    """A flat chars/4 would under-count CJK and let an over-window request pass.

    The review pre-flight uses this fallback only when no tokenizer is
    available, so it has to stay an upper bound: Chinese prose is close to one
    token per character, not four characters per token.
    """
    text = "数据库连接池已耗尽" * 10

    assert estimate_text_tokens_upper_bound(text) == len(text)
    assert estimate_text_tokens_upper_bound(text) > len(text) // 4


def test_mixed_text_counts_each_side_separately() -> None:
    text = "abcdefgh" + "中文中文"  # 8 ASCII + 4 CJK

    assert estimate_text_tokens_upper_bound(text) == 3 + 4

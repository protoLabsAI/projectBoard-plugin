"""Failure-classification tests — the retry-policy table.

Pure function, so these are exhaustive and cheap: each category's patterns, the
ordering (first match wins), and the terminal fallback for anything unknown.
"""

from __future__ import annotations

import pytest

from project_board.failures import TERMINAL, classify


@pytest.mark.parametrize(
    "msg,category,retryable",
    [
        # rate-limit / capacity
        ("coder dispatch failed: 429 rate limit exceeded", "rate_limit", True),
        ("Error: you are being rate-limited, slow down", "rate_limit", True),
        ("model overloaded, try later", "rate_limit", True),
        # transient infra
        ("git push failed: connection reset by peer", "transient", True),
        ("git fetch origin main timed out after 60s", "transient", True),
        ("gh pr create failed: 503 service unavailable", "transient", True),
        ("could not resolve host github.com", "transient", True),
        # merge / rebase
        ("git push failed: ! [rejected] (non-fast-forward)", "merge_conflict", True),
        ("merge failed: CONFLICT in foo.py", "merge_conflict", True),
        # auth — NOT retryable
        ("gh pr create failed: 403 forbidden — bad credential", "auth", False),
        ("not authorized: permission denied", "auth", False),
        # capability / unknown → terminal
        ("coder produced no commits vs base — nothing to PR", "terminal", False),
        ("some weird unexpected explosion", "terminal", False),
    ],
)
def test_classify_categories(msg, category, retryable):
    p = classify(msg)
    assert p.category == category
    assert p.retryable is retryable


def test_unknown_falls_back_to_terminal():
    p = classify("???")
    assert p is TERMINAL
    assert p.retryable is False and p.max_attempts == 1


def test_empty_message_is_terminal():
    assert classify("").category == "terminal"


def test_first_match_wins():
    # both "429" (rate_limit) and "connection" (transient) present → rate_limit first.
    assert classify("429 too many requests on connection").category == "rate_limit"


def test_retryable_policies_carry_a_real_budget():
    for msg in ("429 rate limit", "connection reset", "merge conflict"):
        p = classify(msg)
        assert p.retryable and p.max_attempts >= 2 and p.base_delay_s >= 0

"""Failure classification — map a coder/infra error to a retry policy.

The loop used to treat every failure the same: Blocked, or (with a ladder) climb a
model tier. But a rate limit or a transient git/network error is not the feature's
fault, and a stronger model won't fix it — it should be retried with backoff, not
permanently blocked. This is a small, ordered regex table (a lean distillation of
protoMaker's failure-classifier, ~14 categories → the handful that matter for a
single-board loop) returning whether an error is retryable, how long to back off,
and the attempt cap. Pure + deterministic — no I/O, trivially testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    """How the loop should respond to a failure."""

    category: str
    retryable: bool
    base_delay_s: float
    max_attempts: int  # total dispatch attempts (1 = no retry)


# Ordered: first match wins. Patterns are matched case-insensitively against the
# error message. Only genuinely transient/infra classes are retryable — a stronger
# model can't clear a rate limit, and a re-dispatch off the latest base can clear a
# merge conflict, but an auth error or an unknown failure needs a human.
_RULES: tuple[tuple[str, Policy], ...] = (
    (
        r"rate.?limit|\b429\b|quota|overloaded|too many requests|capacity",
        Policy("rate_limit", True, 60.0, 5),
    ),
    (
        r"timed out|timeout|connection|network|temporarily|econnreset|reset by peer"
        r"|could not resolve|unavailable|\b50[234]\b",
        Policy("transient", True, 15.0, 3),
    ),
    (
        r"conflict|cannot be merged|merge failed|non-fast-forward|\brebase\b",
        Policy("merge_conflict", True, 5.0, 2),
    ),
    (
        r"\bauth\b|permission|forbidden|\b401\b|\b403\b|credential|not authorized",
        Policy("auth", False, 0.0, 1),
    ),
)

# Anything unmatched (incl. "no commits"/no-diff, which the escalation ladder owns)
# → terminal: block, don't retry.
TERMINAL = Policy("terminal", False, 0.0, 1)


def classify(error: str) -> Policy:
    """Classify an error message → a retry :class:`Policy`. Unknown → ``TERMINAL``."""
    text = (error or "").lower()
    for pattern, policy in _RULES:
        if re.search(pattern, text):
            return policy
    return TERMINAL

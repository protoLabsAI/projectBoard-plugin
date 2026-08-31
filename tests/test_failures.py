"""Failure-classification tests — the retry-policy table.

Pure function, so these are exhaustive and cheap: each category's patterns, the
ordering (first match wins), and the terminal fallback for anything unknown.
"""

from __future__ import annotations

import pytest

from project_board.failures import (
    PRE_MODEL_DISPATCH_CLASS,
    TERMINAL,
    classify,
    is_pre_model_dispatch_failure,
)


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


# ── pre-model dispatch / infrastructure classification (#339) ────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        # the C1 seam-style dispatch failure — the whole bug: a kwarg mismatch below the
        # seam, normalised to `coder dispatch failed: …`.
        "coder dispatch failed: dispatch_tapped() got an unexpected keyword argument 'tool_callback'",
        "coder dispatch failed: TypeError in the adapter",
        # a missing / unresolved delegate.
        "coder dispatch failed: no such delegate 'proto-smart'",
        "delegate 'proto' not found",
        # an adapter / session refusal.
        "coder dispatch failed: adapter refused the session",
        "session refused: over the concurrent limit",
        # a non-TappedResult reply the seam refuses.
        "coder dispatch failed: dispatch_tapped returned NoneType, expected a TappedResult",
        # a timeout BEFORE the first token (no model activity).
        "coder timed out after 1800s",
    ],
)
def test_pre_model_dispatch_failure_blocks_when_model_never_reached(msg):
    # No lifecycle evidence the model ran ⇒ a recognised seam signature is pre-model.
    assert is_pre_model_dispatch_failure(msg, model_reached=False) is True


@pytest.mark.parametrize(
    "msg",
    [
        # the SAME seam signatures — but the model reached first token, so the failure
        # is model-reachable no matter the message: stay on the ladder.
        "coder dispatch failed: dispatch_tapped() got an unexpected keyword argument 'x'",
        "coder timed out after 1800s",
    ],
)
def test_model_reached_is_never_pre_model(msg):
    assert is_pre_model_dispatch_failure(msg, model_reached=True) is False


@pytest.mark.parametrize(
    "msg",
    [
        # build-gate failures PROVE the model produced diffs — model-reachable even if the
        # monitor lost its lifecycle evidence (model_reached=False). These must NOT match,
        # so the ladder still climbs on a genuine model-capability failure.
        "goal verification failed: missing tests for the new behavior",
        "requirements unresolved: r3 still open",
        "coder.solve exhausted after 12 generation(s) (rung=tree): assertion X keeps failing",
        "coder produced no commits vs base — nothing to PR",
    ],
)
def test_model_reachable_failures_are_not_pre_model_even_without_evidence(msg):
    assert is_pre_model_dispatch_failure(msg, model_reached=False) is False


def test_pre_model_class_is_distinct_from_every_classify_category():
    # The dispatch-infra class is deliberately NOT a classify() category — it needs the
    # loop's lifecycle evidence, and it drives notify-not-heal + a tier reset on unblock.
    seen = {classify(m).category for m in ("429", "connection reset", "conflict", "forbidden", "???")}
    assert PRE_MODEL_DISPATCH_CLASS == "dispatch-infra"
    assert PRE_MODEL_DISPATCH_CLASS not in seen


def test_pre_model_empty_message_is_not_pre_model():
    # An empty message carries no seam signature — nothing to triage as infra.
    assert is_pre_model_dispatch_failure("", model_reached=False) is False

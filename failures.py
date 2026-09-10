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
        # The provider can't serve its model AT ALL (#420): the model was retired, the
        # account's plan refuses it, or the client is too old to speak it. Not a quota
        # (it won't reset), not a capability ceiling (a stronger model won't bring it
        # back), not card-terminal (a sibling provider can do the work) — so it is not
        # retryable, and the loop rotates past it within the rung instead. First, and
        # deliberately narrow: the full refusal phrasings, so an incidental "not found"
        # or "unavailable" elsewhere never lands here, and a dead-model message that
        # happens to contain "unavailable" or "capacity" is never backed off and retried.
        r"does not exist or you do not have access"
        r"|\bmodel_not_found\b"
        r"|model is not supported when using"
        r"|model requires a newer version of",
        Policy("provider_unavailable", False, 0.0, 1),
    ),
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


def classify(error: str, *, provider_rules: bool = True) -> Policy:
    """Classify an error message → a retry :class:`Policy`. Unknown → ``TERMINAL``.

    ``provider_rules=False`` skips the ``provider_unavailable`` rule, for text that is not
    a coder dispatch failure: a reviewer's gap or a `gh` error that happens to quote a
    refusal phrase says nothing about the coder's provider, and must fall through to the
    class it would otherwise have (a `502 … timeout` is still transient)."""
    text = (error or "").lower()
    for pattern, policy in _RULES:
        if not provider_rules and policy.category == "provider_unavailable":
            continue
        if re.search(pattern, text):
            return policy
    return TERMINAL


# The `blocked-class:` of a card the loop PARKED because its fresh builds keep timing out
# (#378): too wide to build in one dispatch, with a split handed to the board's own agent.
# Like `dispatch-infra` below it is the loop's own class, not one of `classify()`'s: the
# blocked sweep never re-runs it (it would only time out again, racing its own split), and
# an operator unblock resets the card's timeout count, so a deliberate retry — after
# raising `coder_timeout_s`, say — gets a real attempt instead of re-parking at once.
TOO_WIDE_CLASS = "too-wide"


# ── pre-model dispatch / infrastructure failures (#339) ──────────────────────────
# The `blocked-class:` a pre-model dispatch/infra failure carries. It is deliberately
# NOT one of `classify()`'s categories: it can't be decided from the message alone
# (it needs the loop's dispatch-lifecycle evidence too), and it drives two behaviours
# the message-classes don't — the operator is NOTIFIED rather than auto-healed (it is
# absent from the loop's self-healing set), and an operator unblock RESETS the card's
# escalation tier so a host/adapter incident never leaves a `tier:` label the next
# genuine build inherits.
PRE_MODEL_DISPATCH_CLASS = "dispatch-infra"

# Dispatch-seam / pre-first-token infrastructure signatures — a failure raised BELOW
# the model call: the C1 tapped-seam contract (a kwarg mismatch, a non-TappedResult
# reply), a missing / unresolved / unknown delegate, an adapter or session refusing
# the call, or a timeout before the model produced a first token. `coder_seam`
# normalises every below-seam throw to a `coder dispatch failed: …` WorktreeError, so
# that prefix alone catches the common case; the rest are belt-and-braces for the
# other pre-model shapes named in ADR 0064's dispatch contract.
_PRE_MODEL_DISPATCH = re.compile(
    r"coder dispatch failed"
    r"|unexpected keyword argument"
    r"|dispatch_tapped"
    r"|\bdelegate\b"
    r"|\badapter\b"
    r"|not callable|object is not|takes no|positional argument"
    r"|session (?:refused|rejected|unavailable|not available|limit)"
    r"|timed out|timeout",
    re.IGNORECASE,
)


def is_pre_model_dispatch_failure(error: str, *, model_reached: bool) -> bool:
    """Did a coding failure occur BEFORE the model could influence the result?

    The escalation ladder is a model-CAPABILITY policy, not a generic exception
    retry (ADR 0064): only a model-reachable, execution-grounded failure justifies a
    stronger coding tier. A failure in the dispatch seam / adapter, a missing
    delegate, or a timeout before the first token is a HOST-infrastructure incident —
    a stronger model cannot clear it, and escalating on one burned the whole ladder
    in seconds and stamped a bogus ``tier:`` label onto the card that misrouted its
    next real build (bd-cwpv). Such a failure must block DIRECTLY for triage.

    ``model_reached`` is the loop's dispatch-lifecycle evidence: any tool call,
    thought, streamed answer, or token usage recorded for the attempt. If the model
    reached first token the failure is model-reachable no matter the message — this
    returns ``False`` (stay on the ladder). Otherwise a recognised dispatch-seam
    signature is pre-model → ``True`` (block, no tier climb).

    Message-gated on purpose: a build-gate failure (goal-verify, requirements
    unresolved, ``solve()`` exhausted) proves the model produced diffs, so it never
    matches here even if the monitor lost its lifecycle evidence — only a genuine
    seam / adapter / delegate / timeout signature qualifies. The loop's own fail-safe
    (an unreadable monitor snapshot ⇒ ``model_reached=False``) then routes an
    ambiguous dispatch failure to a block rather than an expensive climb."""
    if model_reached:
        return False
    return bool(_PRE_MODEL_DISPATCH.search(error or ""))

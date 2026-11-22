"""#776: a review run that fails on a provider rate limit is a wait, not a broken gate.

The gate blocks the card at once as ``rate-limit`` with the reset recorded, spends none
of ``review_run_max``, and the blocked sweep returns the card to REVIEW after the reset.
Any other failed step keeps the old path: ``review_run_max`` attempts, then ``terminal``.
"""

import sys
import types
from datetime import datetime, timezone

import pytest

from project_board import worktree
from project_board.failures import classify
from project_board.loop import BoardLoop
from project_board.loop._common import _UNBLOCK_RETRY_MAX
from project_board.loop.drive import _held_summary, _recorded_reset
from project_board.loop.reconcile import (
    _REVIEW_RATE_LIMIT_BACKOFF_S,
    _REVIEW_STEP_ERROR_CAP,
    _failed_step_excerpts,
    _review_rate_limit_wait,
)
from test_loop import _GateStore, _inject_fake_findings

NOW = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc).timestamp()
PR = "https://github.com/o/r/pull/9"
RATE_LIMITED = (
    "Error: step 'find_crossfile' raised AnthropicRateLimitError: Error code: 429 - "
    "{'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'This request would exceed "
    "your organization rate limit'}}"
)
BROKEN = "Error: step 'find_crossfile' raised ValueError: boom"
CLEAN = "clean.\n```json\n[]\n```"


class _Store(_GateStore):
    """One in-review card with an open PR, classified the way the real store does it:
    ``flag_blocked`` takes the caller's category, else ``failures.classify(reason)``."""

    _SUBSTATES = ("review-pending", "changes-requested", "review-clean")

    def __init__(self):
        super().__init__()
        self.labels = ["in-review"]
        self.blocked_class = ""
        self.blocked_reason = ""
        self.cleared = []

    def _row(self):
        blocked = self.state == "blocked"
        return {
            "id": "bd-1",
            "title": "A card",
            "repo": "/repo",
            "pr_url": PR,
            "board_state": self.state,
            "bead_status": "in_progress",
            "blocked": blocked,
            "blocked_class": self.blocked_class if blocked else "",
            "blocked_reason": self.blocked_reason if blocked else "",
            "labels": list(self.labels) + (["blocked"] if blocked else []),
        }

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.labels = [l for l in self.labels if l not in self._SUBSTATES]
        if label:
            self.labels.append(label)
        return super().set_review_substate(fid, label, note)

    def flag_blocked(self, fid, reason, category="", *, automatic=False):
        self.state = "blocked"
        self.blocked_reason = reason
        self.blocked_class = (str(category or "").strip() or classify(reason).category).lower().replace("_", "-")
        return super().flag_blocked(fid, reason, category, automatic=automatic)

    def clear_blocked(self, fid):
        self.cleared.append(fid)
        self.state = "in_review" if "in-review" in self.labels else "ready"
        self.blocked_class = ""
        return {"id": fid}

    def requeue(self, fid):
        self.labels = [l for l in self.labels if l != "in-review"]
        return super().requeue(fid)

    def record_budget(self, fid, kind, n):
        self.labels = [l for l in self.labels if not l.startswith(f"budget:{kind}:")] + [f"budget:{kind}:{n}"]

    def record_notified(self, fid, kind="blocked"):
        return {"id": fid}

    def list_features(self, state=None, **_kw):
        return [self._row()] if state == self.state else []

    def get_feature(self, fid):
        return self._row()


def _env(monkeypatch, results, cfg=None):
    """A review-gate loop over the REAL ``_run_review_workflow``: the host's workflow
    runner is faked and returns the next of ``results`` on each call (the last repeats)."""
    _inject_fake_findings(monkeypatch)
    calls = []

    async def _runner(name, inputs):
        calls.append(inputs)
        return results[min(len(calls), len(results)) - 1]

    rt = types.ModuleType("runtime")
    rt_state = types.ModuleType("runtime.state")
    rt_state.STATE = types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(sys.modules, "runtime", rt)
    monkeypatch.setitem(sys.modules, "runtime.state", rt_state)

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(url, *, cwd="."):
        return "a" * 40

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW)
    store = _Store()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    # merge_poll off: the reconcile exercises only the review-gate resume edge.
    loop = BoardLoop({"review_gate": True, "merge_poll": False, **(cfg or {})})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)  # no reviewer fallback
    monkeypatch.setattr(loop, "_notify_operator", lambda *a, **k: None)
    return loop, store, calls


def _failed(text, sid="find_crossfile"):
    return {"output": CLEAN, "steps": {sid: text}, "failed": [sid]}


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


async def test_rate_limited_review_blocks_as_rate_limit_on_the_first_attempt(monkeypatch):
    loop, store, calls = _env(monkeypatch, [_failed(RATE_LIMITED)])
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert len(calls) == 1
    assert store.state == "blocked" and store.blocked_class == "rate-limit"
    # The reason says what failed (the step's own error), and when the wait ends.
    assert "find_crossfile" in store.blocked_reason
    assert "AnthropicRateLimitError: Error code: 429" in store.blocked_reason
    assert store.blocked_reason.endswith(f"; resets-at:{_iso(NOW + _REVIEW_RATE_LIMIT_BACKOFF_S)}")
    assert _recorded_reset(store.blocked_reason) == NOW + _REVIEW_RATE_LIMIT_BACKOFF_S
    # None of review_run_max was spent, and review-pending is kept through the block (#181).
    assert loop._review_run_failures.get("bd-1", 0) == 0
    assert not any(l.startswith("budget:review-run:") for l in store.labels)
    assert "review-pending" in store.labels
    assert ("requeue", "bd-1") not in store.calls


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("retry-after: 120", NOW + 120),
        ("resets at 2026-11-15T16:30:00Z", NOW + 1800),
        ("resets in 2 hours", NOW + 7200),
        ("no hint at all", NOW + _REVIEW_RATE_LIMIT_BACKOFF_S),
        # A reset that has already passed would unblock at once into the same limit.
        ("resets at 2026-11-15T15:00:00Z", NOW + _REVIEW_RATE_LIMIT_BACKOFF_S),
    ],
)
async def test_recorded_reset_is_the_providers_else_the_fixed_backoff(monkeypatch, hint, expected):
    loop, store, _ = _env(monkeypatch, [_failed(f"{RATE_LIMITED}; {hint}")])
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.blocked_class == "rate-limit"
    assert _recorded_reset(store.blocked_reason) == expected
    assert store.blocked_reason.endswith(f"; resets-at:{_iso(expected)}")


@pytest.mark.parametrize(
    "text",
    [
        "Error: step 'verify' raised RuntimeError: {'type': 'usage_limit_reached'}",
        "Error: step 'verify' raised RuntimeError: You've hit your session limit · resets 1:30pm",
        "Error: step 'verify' raised RuntimeError: 429 Too Many Requests",
        "Error: step 'verify' raised RuntimeError: You've hit your usage limit",
    ],
)
async def test_other_provider_limit_wordings_are_rate_limits_too(monkeypatch, text):
    loop, store, calls = _env(monkeypatch, [_failed(text, sid="verify")])
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert len(calls) == 1 and store.blocked_class == "rate-limit"
    assert "resets-at:" in store.blocked_reason


async def test_sweep_waits_for_the_reset_then_returns_the_card_to_review(monkeypatch):
    loop, store, calls = _env(monkeypatch, [_failed(f"{RATE_LIMITED}; retry-after: 120"), {"output": CLEAN}])
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.state == "blocked" and store.blocked_class == "rate-limit"

    # Before the reset: still blocked, no retry spent, and a blocked card is not reviewed.
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 119)
    await loop._recover_blocked(store)
    await loop._reconcile_prs()
    assert store.state == "blocked" and store.cleared == [] and len(calls) == 1

    # After it: unblocked back into REVIEW with review-pending — never requeued to a coder.
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 121)
    await loop._recover_blocked(store)
    assert store.cleared == ["bd-1"]
    assert store.state == "in_review"
    assert "review-pending" in store.labels and "review-clean" not in store.labels
    assert ("requeue", "bd-1") not in store.calls
    # The wait spent none of the card's auto-retries.
    assert not any(l.startswith("budget:unblock-retry:") for l in store.labels)

    # The next reconcile re-arms the gate; only a review that RAN moves it off pending.
    await loop._reconcile_prs()
    assert len(calls) == 2
    assert store.state == "in_review" and "review-clean" in store.labels
    assert ("requeue", "bd-1") not in store.calls


@pytest.mark.parametrize(
    "error",
    [
        BROKEN,
        "Error: step 'find_crossfile' raised ValueError: insufficient capacity for batch",
        "Error: step 'find_crossfile' raised ValueError: overloaded local queue",
        "Error: step 'find_crossfile' raised ValueError: disk quota exceeded",
        "Error: step 'find_crossfile' raised OSError: connection reset by peer",
        "Error: step 'find_crossfile' raised ValueError: " + "x" * 400 + " capacity",
    ],
)
async def test_non_rate_limit_failure_still_retries_then_blocks_terminal(monkeypatch, error):
    loop, store, calls = _env(monkeypatch, [_failed(error)])
    assert loop.review_run_max == 3
    for attempt in (1, 2):
        await loop._review_gate(store, "bd-1", PR, "/repo")
        assert store.state == "in_review" and loop._review_run_failures["bd-1"] == attempt
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert len(calls) == 3
    assert store.state == "blocked" and store.blocked_class == "terminal"
    assert store.blocked_reason.startswith("review gate could not complete after 3 attempt(s)")
    assert "had failed step(s): find_crossfile" in store.blocked_reason
    assert error[:_REVIEW_STEP_ERROR_CAP] in store.blocked_reason
    assert "resets-at:" not in store.blocked_reason
    assert "review-pending" in store.labels
    # Terminal: the sweep leaves it for a human.
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 86400)
    await loop._recover_blocked(store)
    assert store.state == "blocked" and store.cleared == []


async def test_a_limit_that_outlasts_many_windows_never_strands_the_card(monkeypatch):
    """The provider keeps limiting the reviewer through five fallback windows, on a card
    whose two auto-retries are ALREADY spent. Every reset still returns the card to
    review with no retry spent, and the review that finally runs clears it."""
    loop, store, calls = _env(monkeypatch, [_failed(RATE_LIMITED)] * 5 + [{"output": CLEAN}])
    store.labels.append(f"budget:unblock-retry:{_UNBLOCK_RETRY_MAX}")
    await loop._review_gate(store, "bd-1", PR, "/repo")
    now = NOW
    for window in range(1, 6):
        assert store.state == "blocked" and store.blocked_class == "rate-limit"
        assert _recorded_reset(store.blocked_reason) == now + _REVIEW_RATE_LIMIT_BACKOFF_S
        await loop._recover_blocked(store)  # the reset has not passed: it waits
        assert store.state == "blocked" and len(store.cleared) == window - 1
        now += _REVIEW_RATE_LIMIT_BACKOFF_S + 1
        monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda now=now: now)
        await loop._recover_blocked(store)
        assert store.state == "in_review" and len(store.cleared) == window
        assert "review-pending" in store.labels
        await loop._reconcile_prs()
        assert len(calls) == window + 1
    assert store.state == "in_review" and "review-clean" in store.labels
    assert ("requeue", "bd-1") not in store.calls
    assert f"budget:unblock-retry:{_UNBLOCK_RETRY_MAX}" in store.labels  # untouched
    assert loop._review_run_failures.get("bd-1", 0) == 0


def test_only_a_gate_rate_limit_block_with_a_recorded_reset_is_an_unbudgeted_wait():
    row = {"labels": ["in-review", "review-pending", "blocked"], "pr_url": PR, "blocked_class": "rate-limit"}
    wait = f"review gate rate-limited — x — retried by itself after the reset: {PR}; resets-at:{_iso(NOW)}"
    assert _review_rate_limit_wait(row, wait)
    assert _review_rate_limit_wait(row, "blocked: " + wait)
    # No recorded reset, a coder's block, another class, or a card not in review: budgeted.
    assert not _review_rate_limit_wait(row, wait.split(";")[0])
    assert not _review_rate_limit_wait(row, f"rate_limit: 429; resets-at:{_iso(NOW)}")
    assert not _review_rate_limit_wait(
        row, f"review gate could not complete after 3 attempt(s) — rate limit; resets-at:{_iso(NOW)}"
    )
    assert not _review_rate_limit_wait({**row, "blocked_class": "transient"}, wait)
    assert not _review_rate_limit_wait({**row, "labels": ["blocked"]}, wait)
    assert not _review_rate_limit_wait({**row, "pr_url": ""}, wait)


async def test_other_self_healing_blocks_keep_the_retry_budget(monkeypatch):
    """The unbudgeted path is the review wait alone. A review-gate block of another
    self-healing class still spends a retry, and stops at the cap."""
    loop, store, _ = _env(monkeypatch, [_failed(RATE_LIMITED)])
    store.labels.append("review-pending")
    reason = f"review gate could not complete after 3 attempt(s) — connection reset — needs operator attention: {PR}"
    store.flag_blocked("bd-1", reason, "transient")
    await loop._recover_blocked(store)
    assert store.cleared == ["bd-1"] and store.state == "in_review"
    assert "budget:unblock-retry:1" in store.labels
    # A coder's rate-limit block with its retries spent is escalated, not cleared.
    loop, store, _ = _env(monkeypatch, [_failed(RATE_LIMITED)])
    await loop._budget_set(store, "bd-1", "unblock-retry", _UNBLOCK_RETRY_MAX)
    store.labels = [l for l in store.labels if l != "in-review"]
    store.flag_blocked("bd-1", f"rate_limit: 429; resets-at:{_iso(NOW - 1)}", "rate_limit")
    await loop._recover_blocked(store)
    assert store.state == "blocked" and store.cleared == []


def test_held_summary_promises_the_retry_for_a_review_wait_with_spent_retries():
    reason = f"review gate rate-limited — x — retried by itself after the reset: {PR}; resets-at:{_iso(NOW)}"
    row = {
        "id": "bd-1",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": "rate-limit",
        "blocked_reason": reason,
        "labels": ["in-review", "review-pending", "blocked"],
        "pr_url": PR,
        "budgets": {"unblock-retry": _UNBLOCK_RETRY_MAX},
    }
    assert _held_summary([row])["blocked:rate-limit"]["next"].startswith("none — the health sweep retries")
    coder = {**row, "labels": ["blocked"], "blocked_reason": "rate_limit: 429"}
    assert "a human decides" in _held_summary([coder])["blocked:rate-limit"]["next"]


async def test_coder_rate_limit_block_is_still_requeued(monkeypatch):
    """Only a block the review gate set is returned to review: a rate-limited CODER
    dispatch (no in-review, no review-gate reason) keeps the clear + requeue path."""
    loop, store, _ = _env(monkeypatch, [_failed(RATE_LIMITED)])
    store.labels = []
    store.flag_blocked("bd-1", f"rate_limit: 429; resets-at:{_iso(NOW - 1)}", "rate_limit")
    await loop._recover_blocked(store)
    assert store.cleared == ["bd-1"] and ("requeue", "bd-1") in store.calls


def test_failed_step_excerpt_is_capped_and_keeps_what_the_gate_acts_on():
    long = "Error: step 'a' raised RuntimeError: " + "x" * 1000
    out = _failed_step_excerpts(["a"], {"a": long}, now=NOW)
    assert out.startswith(" — a: Error: step 'a' raised RuntimeError: xxx")
    assert len(out) < _REVIEW_STEP_ERROR_CAP + 20 and "x" * 400 not in out
    # A rate limit and its reset time past the cap are still named after the excerpt.
    late = long + " 429 rate_limit_error; resets at 2026-11-15T16:30:00Z"
    out = _failed_step_excerpts(["a"], {"a": late}, now=NOW)
    assert classify(out).category == "rate_limit"
    assert f"resets at {_iso(NOW + 1800)}" in out
    # Several failed steps each get their own excerpt; a step with no text adds none.
    out = _failed_step_excerpts(["a", "b", "c"], {"a": "Error: one", "b": None, "c": "Error: three"}, now=NOW)
    assert out == " — a: Error: one | c: Error: three"
    assert _failed_step_excerpts(["a"], {}, now=NOW) == ""
    assert _failed_step_excerpts(["a"], None, now=NOW) == ""

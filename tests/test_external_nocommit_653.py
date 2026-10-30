"""A no-commit fix round after an EXTERNAL review bounce blocks once for the operator (#653 slice B).

Incident bd-jq56.6 / protoAgent#4581: an external QA-panel bounce triggered three fix rounds,
and the coder correctly made no commit on each one — it had judged the finding out of scope.
Each round climbed the attempt ladder and burned a tier, when a stronger model would decline the
same finding. Slice B stops the ladder: a ``fix_round_unchanged`` NoChangesError whose card carries
this card's ``ext-review-bounced:<sha>`` stamp for the head the round resumed is the coder
DECLINING, so the drive blocks ONCE for the operator (owner operator, ``terminal``) — no attempt
counted, no tier climb, no retry. Internal-gate and CI-fix no-commit rounds carry no such stamp
for the current head and keep today's escalation ladder.

The operator then rules the finding out of scope with ``board_unblock_feature --override-review``:
``unblock_side_effects`` records an "operator ruled out of scope" audit comment and LEAVES the
``ext-review-bounced:<sha>`` stamp on the head, so reconcile's panel edge keeps HOLDING that head
instead of dispatching another fix round for it.

The drive cases reuse ``tests/test_loop.py``'s mocked drive tier; the unblock + reconcile cases
use a fake store, mirroring ``tests/test_unblock_override_512.py`` and ``tests/test_external_scope_653.py``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from project_board import coder_seam, worktree
import project_board.loop as loop_mod
from project_board.loop import BoardLoop
from project_board.loop._common import (
    EXTERNAL_FINDING_DECLINED_REASON,
    _external_bounce_head,
    _is_external_finding_block,
)
from project_board.loop.reconcile import ReconcileMixin
from project_board.store import (
    LABEL_EXTERNAL_REVIEW_BOUNCED_PREFIX,
    LABEL_IN_REVIEW,
    SHORT_SHA_LEN,
)

from test_loop import FEATURE, _EscalatingStore

HEAD = "a" * 40  # the full PR head a fix round resumes
SHORT = HEAD[:SHORT_SHA_LEN]
STAMP = f"{LABEL_EXTERNAL_REVIEW_BOUNCED_PREFIX}{SHORT}"
PR_URL = "https://github.com/protoLabsAI/protoAgent/pull/4581"
DECLINE = (
    "I examined the external reviewer's finding at src/graph/config.py:418.\n\n"
    "## Summary\n\n"
    "Declining: the flagged behavior is pre-existing and out of scope for this card; "
    "changing it would alter an unrelated contract, so I made no commit."
)


def _fix_round_feature(*, labels):
    """A card on a fix round: it owns an open PR and carries whatever labels the test sets."""
    return {**FEATURE, "pr_url": PR_URL, "labels": list(labels)}


async def _run_fix_round(monkeypatch, *, feature, dispatch, open_pr, store, head=HEAD, unchanged=HEAD):
    """Drive ONE fix round over ``feature`` with the worktree tier mocked. ``head`` is what the
    resume checkout records as ``round_start``; ``unchanged`` is what ``fix_round_unchanged``
    reports (equal to ``head`` ⇒ the round produced no commit, raising NoChangesError)."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _checkout_head_sha(path):
        return head

    async def _fix_round_unchanged(path, start_sha):
        return unchanged

    async def _discard(repo, wt, branch="", *, base="", why=""):
        return (True, None)  # a clean tree reaps with nothing to save

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "checkout_head_sha", _checkout_head_sha)
    monkeypatch.setattr(worktree, "fix_round_unchanged", _fix_round_unchanged)
    monkeypatch.setattr(worktree, "discard_worktree", _discard)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on  # a real ladder is available in every drive case below
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(feature)
    return loop


# ── r1: an external-bounce no-commit round blocks ONCE, no climb, no retry ────────────


async def test_external_bounce_no_commit_blocks_once_for_the_operator(monkeypatch):
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        return DECLINE

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("a declined no-commit round must block before any PR push")

    store = _EscalatingStore(tiers=["smart", "top"])  # a ladder IS available…
    loop = await _run_fix_round(
        monkeypatch,
        feature=_fix_round_feature(labels=[LABEL_IN_REVIEW, STAMP, "blocked"]),
        dispatch=_dispatch,
        open_pr=_open_pr,
        store=store,
    )

    # ONE dispatch, and the ladder was NEVER consulted — no tier climb, no attempt counted.
    assert len(dispatches) == 1
    assert store.escalated == []
    assert [c for c in store.calls if c[0] == "record_attempt"] == []
    # Blocked once, owner operator (terminal), with the exact reason + the PR url + the coder's reason.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    _name, fid, reason, category = blocked[0]
    assert fid == "bd-1" and category == "terminal"
    assert reason.startswith(EXTERNAL_FINDING_DECLINED_REASON)
    assert PR_URL in reason
    assert "coder:" in reason
    assert "Declining: the flagged behavior is pre-existing" in reason
    assert loop._inflight == {}  # the tree was reaped and the slot released


async def test_external_bounce_no_commit_block_omits_coder_reason_when_reply_is_empty(monkeypatch):
    """ "…plus the coder's reason when present": an empty reply leaves the base reason alone."""

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return ""  # the coder said nothing

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("still no PR")

    store = _EscalatingStore(tiers=["smart"])
    await _run_fix_round(
        monkeypatch,
        feature=_fix_round_feature(labels=[LABEL_IN_REVIEW, STAMP]),
        dispatch=_dispatch,
        open_pr=_open_pr,
        store=store,
    )
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    assert reason.startswith(EXTERNAL_FINDING_DECLINED_REASON) and PR_URL in reason
    assert "coder:" not in reason  # nothing to quote
    assert store.escalated == []


# ── r2: internal-gate / CI-fix no-commit rounds keep today's ladder (no matching stamp) ─


@pytest.mark.parametrize(
    "labels",
    [
        pytest.param([LABEL_IN_REVIEW], id="internal-gate-or-ci-fix (no ext stamp)"),
        pytest.param(
            [LABEL_IN_REVIEW, f"{LABEL_EXTERNAL_REVIEW_BOUNCED_PREFIX}{'b' * SHORT_SHA_LEN}"],
            id="stale ext stamp for a DIFFERENT head",
        ),
    ],
)
async def test_no_commit_round_without_a_matching_stamp_still_climbs_the_ladder(monkeypatch, labels):
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # Real tool activity in the gen buffer — the coder executed, just shipped nothing.
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "a.py"}})
        return "I read the files but made no edits"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("the no-commit round opens no PR")

    store = _EscalatingStore(tiers=["smart"])  # fast → smart (one climb), then the top blocks
    loop = await _run_fix_round(
        monkeypatch,
        feature=_fix_round_feature(labels=labels),
        dispatch=_dispatch,
        open_pr=_open_pr,
        store=store,
    )

    # The ladder was CLIMBED (fast → smart, then the top), exactly as before #653 — the whole
    # point of r2. Only once the ladder is exhausted does it block, and NOT as the #653 operator
    # hold: the reason is the ordinary capability one, never "external finding the coder declined".
    assert [e[0] for e in store.escalated] == ["bd-1", "bd-1"]
    assert len(dispatches) == 2
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    assert not reason.startswith(EXTERNAL_FINDING_DECLINED_REASON)
    assert "fix round produced no commit" in reason  # the plain capability no-change block
    assert loop._inflight == {}


# ── r3: the operator rules the finding out of scope via --override-review ─────────────


class _FakeStore:
    """Records every write the unblock helper makes, so a test can prove it posts ONLY the
    out-of-scope audit comment and never touches the review substate / budget / labels."""

    def __init__(self):
        self.comments = []
        self.substate = []
        self.budgets_cleared = []

    def comment(self, fid, text):
        self.comments.append((fid, text))

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.substate.append((fid, label, note, head_sha))
        return {"id": fid}

    def clear_budgets(self, fid, kinds=None):
        self.budgets_cleared.append((fid, kinds))
        return {"id": fid}


def _declined_block(fid="bd-1", labels=None):
    """A card the drive terminal-blocked because the coder declined an external finding."""
    return {
        "id": fid,
        "board_state": "blocked",
        "labels": labels if labels is not None else [LABEL_IN_REVIEW, STAMP, "blocked", "blocked-class:terminal"],
        "pr_url": PR_URL,
        "blocked_reason": f"{EXTERNAL_FINDING_DECLINED_REASON} — {PR_URL}\n\ncoder: out of scope",
        "project": "",
    }


def _trap_gh(monkeypatch):
    """The out-of-scope override returns BEFORE the helper reads the live PR head, so gh is
    never shelled. Any call fails the test loudly."""
    calls = []

    def _boom(*args, cwd=".", timeout=30.0):
        calls.append(args)
        raise AssertionError("the out-of-scope override must not read the live head via gh")

    monkeypatch.setattr(worktree, "_gh_sync", _boom)
    return calls


def test_predicate_and_head_reader_identify_the_declined_block():
    f = _declined_block()
    assert _is_external_finding_block(f) is True
    assert _external_bounce_head(f) == SHORT
    # not blocked any more (a stale reason comment), or a different reason → not our block
    assert _is_external_finding_block({**f, "board_state": "in_review", "labels": [LABEL_IN_REVIEW]}) is False
    assert _is_external_finding_block({**f, "blocked_reason": "parked by hand"}) is False
    assert _external_bounce_head({"labels": [LABEL_IN_REVIEW]}) == ""


def test_unblock_override_rules_external_finding_out_of_scope(monkeypatch):
    gh = _trap_gh(monkeypatch)
    store = _FakeStore()

    out = loop_mod.unblock_side_effects("bd-1", _declined_block(), store, override_review=True, cwd="/repo")

    assert out == {"review": "out-of-scope", "head": SHORT}
    # the audit comment names the override and the exact head the finding was at
    assert store.comments == [("bd-1", f"operator ruled out of scope: external finding at head {SHORT}")]
    # it is NOT a review-gate block: no substate, no budget reset, and no gh read
    assert store.substate == [] and store.budgets_cleared == [] and gh == []


def test_unblock_without_override_leaves_the_declined_block_to_today_s_path(monkeypatch):
    """A PLAIN unblock of such a card is today's unchanged path — the coder is asked to try
    again — so the helper posts no out-of-scope comment (it is not a review-gate block either)."""
    gh = _trap_gh(monkeypatch)
    store = _FakeStore()

    out = loop_mod.unblock_side_effects("bd-1", _declined_block(), store, cwd="/repo")

    assert out == {"review": "n/a"}
    assert store.comments == [] and store.substate == [] and store.budgets_cleared == [] and gh == []


def test_unblock_override_ignores_a_non_declined_block(monkeypatch):
    """Override on a card whose reason is NOT the #653 marker does nothing here (it falls
    through to the review-gate machinery, which this card is not either → n/a)."""
    gh = _trap_gh(monkeypatch)
    store = _FakeStore()
    hand = _declined_block()
    hand["blocked_reason"] = "parked by hand — waiting on the platform team"

    out = loop_mod.unblock_side_effects("bd-1", hand, store, override_review=True, cwd="/repo")

    assert out == {"review": "n/a"}
    assert store.comments == [] and gh == []


# ── r3 (cont.): the stamp stays, so reconcile keeps HOLDING the head — no re-dispatch ──


async def test_a_stamped_head_holds_in_reconcile_and_never_requeues():
    """The out-of-scope override leaves ``ext-review-bounced:<sha>`` on the head. Reconcile's
    external-bounce tail HOLDS a head that already carries this card's stamp instead of
    dispatching another fix round for it — the mechanism #653 relies on (confirmed here)."""
    loop = ReconcileMixin()
    loop._note_external_hold = Mock()
    store = Mock()
    feature = {"id": "bd-1", "board_state": "in_review", "labels": [LABEL_IN_REVIEW, STAMP], "pr_url": PR_URL}

    await loop._record_external_bounce(
        store,
        feature,
        PR_URL,
        ".",
        head=HEAD,
        rendered="findings",
        signals="QA panel FAIL",
        reject_label="external review FAILED",
        already_bounced_hold=f"already bounced for {SHORT}, needs a human: {PR_URL}",
    )

    # Held once for this head; no requeue, no budget spend, no new bounce comment.
    loop._note_external_hold.assert_called_once()
    store.requeue.assert_not_called()
    store.record_review_bounce.assert_not_called()


def test_external_finding_reason_is_the_exact_operator_marker():
    """The reason string the drive writes and the unblock reads must stay in lockstep — a
    drift would silently route the override past the out-of-scope branch."""
    assert EXTERNAL_FINDING_DECLINED_REASON == "needs operator: external finding the coder declined"

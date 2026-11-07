"""A no-commit block carries what the coder said; a leading `BLOCKED-ON-<WORD>:` is a design refusal (#667).

When a coder deliberately refuses — a card tells it to end with ``BLOCKED-ON-<WORD>: <why>``
and open no PR because the spec is unbuildable as written — the board used to record only
``coder produced no commits vs base — nothing to PR``. ``NoChangesError`` is raised with no
sight of the reply, so a principled refusal looked identical to an empty run and the ladder
retried it into the same wall. This fix reads the coder's reply in the NoChangesError handler:

- a ``BLOCKED-ON-<WORD>:`` line (``^\\s*BLOCKED-ON-[A-Z][A-Z-]*:``, multiline) is a DELIBERATE
  refusal — block ONCE under the ``design`` class, no tier climb, no same-tier retry, reason =
  the marker line plus the rest of its paragraph; friction summary ``coder refused: BLOCKED-ON-<WORD>``
  at minor severity;
- any OTHER reply folds a bounded, whitespace-collapsed TAIL (≤ 1500 chars, the gate-red idiom)
  into the block reason, and the drive otherwise blocks/escalates exactly as today;
- an EMPTY reply changes nothing — today's no-commit message stands verbatim.

The drive cases reuse ``tests/test_loop.py``'s mocked drive tier (as ``tests/test_external_nocommit_653.py``
does); the friction case mirrors ``tests/test_host_friction.py``'s ledger fixture.
"""

from __future__ import annotations

import sys

import pytest

from project_board import coder_seam, host_friction, worktree
from project_board.failures import DESIGN_BLOCK_CLASS
from project_board.loop import BoardLoop
from project_board.loop._common import (
    _blocked_on_label,
    _coder_refusal,
    _coder_reply_tail,
    _collapse_ws,
)

from test_loop import FEATURE, _EscalatingStore

FID = "bd-1"
PR_URL = "https://github.com/protoLabsAI/protoAgent/pull/667"
HEAD = "a" * 40
REFUSAL_LINE = "BLOCKED-ON-SPEC: §3 kit class .foo missing"
NO_COMMIT_MSG = "coder produced no commits vs base — nothing to PR"  # worktree.open_pr's message


# ── helpers ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch):
    """A stand-in for ``graph.sdk.record_friction`` (mirrors tests/test_host_friction.py)."""

    class _Ledger:
        def __init__(self):
            self.rows = []

        def __call__(self, kind, summary, detail="", severity="minor", *, source):
            self.rows.append({"summary": summary, "severity": severity})
            return "abcd1234"

    led = _Ledger()
    monkeypatch.setattr(sys.modules["graph.sdk"], "record_friction", led, raising=False)
    host_friction.reset()
    yield led
    host_friction.reset()


async def _run_no_commit_drive(monkeypatch, *, reply, store, pr_url=None, labels=None):
    """Drive ONE no-commit attempt with the worktree tier mocked. The coder returns ``reply``
    and commits nothing: a FRESH build (no ``pr_url``) reaches ``open_pr``, which raises the
    canonical NoChangesError; a FIX ROUND (``pr_url`` set) stops at ``fix_round_unchanged``
    with the round's head unchanged. Returns ``(loop, dispatches)``."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    dispatches: list[str] = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # Real tool activity in the gen buffer: the coder executed, it just shipped nothing —
        # so the empty-result (#198) same-tier retry never fires and the no-commit paths
        # under test are exactly what the drive takes.
        coder_seam.progress_tool(FID, 1, {"phase": "start", "name": "Read", "id": "t1", "input": {}})
        return reply

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _checkout_head_sha(path):
        return HEAD

    async def _fix_round_unchanged(path, start_sha):
        return HEAD  # a fix round ended on the head it resumed → NoChangesError

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.NoChangesError(NO_COMMIT_MSG)

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
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    feature = {**FEATURE}
    if pr_url is not None:
        feature["pr_url"] = pr_url
    if labels is not None:
        feature["labels"] = list(labels)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on  # a real ladder is available in every drive case below
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(feature)
    return loop, dispatches


# ── the helpers in loop/_common.py ──────────────────────────────────────────────────────


def test_coder_refusal_extracts_the_marker_paragraph():
    reply = (
        "Looked at the spec against the kit.\n\n"
        "BLOCKED-ON-SPEC: §3 kit class .foo missing\n"
        "and acceptance criterion 2 contradicts criterion 5.\n\n"
        "## Summary\nI made no commit."
    )
    out = _coder_refusal(reply)
    assert out.startswith("BLOCKED-ON-SPEC: §3 kit class .foo missing")
    assert "contradicts criterion 5" in out  # the rest of the paragraph rides along
    assert "## Summary" not in out  # …but it stops at the blank line
    assert "Looked at the spec" not in out  # …and never reaches back before the marker


def test_coder_refusal_matches_a_trailing_marker_line():
    """The acceptance scenario: text ENDING with the marker, no following paragraph."""
    reply = "I cannot build this as written.\n\n" + REFUSAL_LINE
    assert _coder_refusal(reply) == REFUSAL_LINE


def test_coder_refusal_is_empty_without_an_uppercase_marker():
    assert _coder_refusal("just prose, no marker at all") == ""
    assert _coder_refusal("") == ""
    assert _coder_refusal("blocked-on-spec: lower-case is not the marker") == ""


def test_coder_refusal_is_bounded():
    reply = "BLOCKED-ON-SPEC: " + ("x" * 5000)
    assert len(_coder_refusal(reply, limit=1500)) == 1500


def test_blocked_on_label_isolates_the_token():
    assert _blocked_on_label("BLOCKED-ON-SPEC: whatever") == "BLOCKED-ON-SPEC"
    assert _blocked_on_label("BLOCKED-ON-DEP-GRAPH: cycle") == "BLOCKED-ON-DEP-GRAPH"
    assert _blocked_on_label("no token here") == "BLOCKED-ON"  # a stable fallback


def test_coder_reply_tail_keeps_a_bounded_end():
    assert _coder_reply_tail("") == ""
    assert _coder_reply_tail("   \n\t ") == ""
    short = "a short reply"
    assert _coder_reply_tail(short) == short  # under the limit: unchanged
    long = "HEADSTART " + ("z " * 2000) + "ENDMARKER"
    out = _coder_reply_tail(long, limit=1500)
    assert len(out) <= 1500
    assert out.startswith("…")  # the START is elided…
    assert out.endswith("ENDMARKER")  # …the END — where the coder says why it stopped — is kept
    assert "HEADSTART" not in out


def test_collapse_ws_flattens_every_run():
    assert _collapse_ws("a\n\n  b\t c\n") == "a b c"
    assert _collapse_ws("") == "" and _collapse_ws(None) == ""


# ── r1: a deliberate BLOCKED-ON refusal blocks ONCE as `design`, no climb, no retry ──────


async def test_refusal_blocks_once_as_design_no_climb_no_retry(monkeypatch):
    reply = (
        "I analyzed the card against the kit and the acceptance criteria.\n\n"
        "The criteria reference a kit class that does not exist, so this is unbuildable as "
        "written and I made no commit.\n\n" + REFUSAL_LINE
    )
    store = _EscalatingStore(tiers=["smart", "top"])  # a ladder IS available…
    loop, dispatches = await _run_no_commit_drive(monkeypatch, reply=reply, store=store)

    # ONE dispatch; the ladder was NEVER consulted; no attempt counted.
    assert len(dispatches) == 1
    assert store.escalated == []
    assert [c for c in store.calls if c[0] == "record_attempt"] == []
    # Blocked exactly once, under the `design` class, the reason quoting the marker line.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    _name, fid, reason, category = blocked[0]
    assert fid == FID
    assert category == DESIGN_BLOCK_CLASS == "design"
    assert REFUSAL_LINE in reason
    assert "coder refused" in reason
    assert loop._inflight == {}  # the tree was reaped and the slot released


async def test_refusal_is_detected_on_a_fix_round_too(monkeypatch):
    """The refusal path is independent of the raise site: a fix-round NoChangesError
    (``fix_round_unchanged``) routes to the same `design` block."""
    reply = "Resumed the PR branch; the review ask is unbuildable.\n\n" + REFUSAL_LINE
    store = _EscalatingStore(tiers=["smart"])
    loop, dispatches = await _run_no_commit_drive(monkeypatch, reply=reply, store=store, pr_url=PR_URL)

    assert len(dispatches) == 1 and store.escalated == []
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and blocked[0][3] == "design"
    assert REFUSAL_LINE in blocked[0][2]


# ── r2: an ordinary no-commit reply blocks/escalates as today, with a BOUNDED tail ───────


async def test_ordinary_no_commit_reply_folds_a_bounded_tail_and_still_climbs(monkeypatch):
    end_marker = "END-OF-CODER-REPLY-UNIQUE-TAIL"
    reply = "I read the files but made no edits. " + ("filler " * 500) + end_marker  # > 1500 chars
    store = _EscalatingStore(tiers=["smart"])  # fast → smart (one climb), then the top blocks
    loop, dispatches = await _run_no_commit_drive(monkeypatch, reply=reply, store=store)

    # The ladder was climbed exactly as before #667 — an ordinary empty build, not a refusal:
    # fast → smart (one climb), then the top escalate returns None and it blocks.
    assert [e[0] for e in store.escalated] == [FID, FID]
    assert len(dispatches) == 2
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    # Today's message is preserved AND the coder's final-message tail now rides along…
    assert NO_COMMIT_MSG in reason
    assert "Coder's final message (tail):" in reason
    assert end_marker in reason  # the END of the reply is what the tail keeps
    # …and the tail is BOUNDED (≤ 1500 chars, the gate-red idiom), with its start elided.
    tail = reason.split("Coder's final message (tail): ", 1)[1]
    assert len(tail) <= 1500
    assert tail.startswith("…")
    assert blocked[0][3] != "design"  # NOT a refusal — the ordinary capability class


# ── r3: an EMPTY reply keeps today's no-commit message verbatim (no tail appended) ───────


async def test_empty_reply_keeps_the_message_verbatim(monkeypatch):
    store = _EscalatingStore(tiers=[])  # no ladder → blocks on the first no-commit result
    loop, dispatches = await _run_no_commit_drive(monkeypatch, reply="", store=store)

    assert len(dispatches) == 1
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    assert NO_COMMIT_MSG in reason  # today's message, unchanged
    assert "Coder's final message (tail):" not in reason  # nothing folded in
    assert blocked[0][3] != "design"


# ── r1 (friction): the refusal names itself at minor severity, not an empty run ──────────


def test_refusal_friction_summary_names_the_block_at_minor_severity(ledger):
    lp = BoardLoop({})
    exc = worktree.NoChangesError(NO_COMMIT_MSG)
    lp._friction_drive_failure(FID, exc, {"id": FID}, base="epic/x", branch="feat/bd-1", refusal=REFUSAL_LINE)
    (row,) = ledger.rows
    assert row == {"summary": "coder refused: BLOCKED-ON-SPEC", "severity": "minor"}


def test_without_a_refusal_a_no_commit_is_the_ordinary_empty_run_summary(ledger):
    lp = BoardLoop({})
    exc = worktree.NoChangesError(NO_COMMIT_MSG)
    lp._friction_drive_failure(FID, exc, {"id": FID}, base="epic/x", branch="feat/bd-1")
    (row,) = ledger.rows
    assert row == {
        "summary": "coder produced no commits vs base (nothing to open a PR from)",
        "severity": "minor",
    }

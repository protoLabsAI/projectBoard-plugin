"""#715: a coding card whose coder DECLINED every requirement built nothing — the
all-declined ledger passes the disposition gate (`declined` is a closed status) yet has
no delivery behind it. On protoAgent#4953 the coder declined all six requirements,
shipped one docs file, and the PR still said ``Fixes #4725``: review passed and
auto-merge would have closed the issue with nothing built.

The fix adds ``_all_items_declined`` beside ``_all_items_disposed`` and reads it at the
drive's publication seam:
  - an all-declined CODING card is blocked owner=operator (terminal, never auto-cleared),
    no PR opens, and the worktree is kept so the prerequisite report survives;
  - a PARTIAL delivery (at least one ``done``) publishes exactly as before;
  - the PR opener stamps ``Refs #N`` (never ``Fixes #N``) for an all-declined ledger, so
    an operator who later unblocks or salvages the card references rather than closes the
    issue;
  - tasks (non-coding cards) are unaffected.
"""

from __future__ import annotations

from project_board import worktree
from project_board.loop import BoardLoop, _all_items_declined, _inject_source_issue_line
from project_board.store import MANUAL_BLOCK_CLASS
from project_board.store import _all_items_declined as _store_all_items_declined
from test_loop import FEATURE, FakeLoopStore, _drive_with


# ── r1: the predicate ────────────────────────────────────────────────────────────


def test_all_items_declined_true_only_when_every_item_declined():
    declined_a = {"id": "r1", "text": "a", "status": "declined"}
    declined_b = {"id": "r2", "text": "b", "status": "declined"}
    done = {"id": "r3", "text": "c", "status": "done"}
    open_item = {"id": "r4", "text": "d", "status": "open"}
    # all declined → True
    assert _store_all_items_declined([declined_a, declined_b]) is True
    # one done alongside a decline → False (a partial delivery is a real delivery)
    assert _store_all_items_declined([done, declined_a]) is False
    # an open item alongside declines → False
    assert _store_all_items_declined([declined_a, open_item]) is False
    # empty ledger → False (no ledger → nothing to judge all-declined)
    assert _store_all_items_declined([]) is False
    assert _store_all_items_declined(None) is False


def test_all_items_declined_is_reexported_through_the_loop():
    """The drive reads it via the loop kernel namespace, same as ``_all_items_disposed``."""
    assert _all_items_declined is _store_all_items_declined
    assert _all_items_declined([{"id": "r1", "status": "declined"}]) is True


# ── r2: an all-declined coding card is blocked, no PR, worktree kept ──────────────


async def _dispatch_all_declined(c, wt, prompt, *, timeout=None, env_passthrough=()):
    return (
        "## Requirements\n\n"
        "- r1: declined — depends on an upstream API that is not shipped yet\n"
        "- r2: declined — out of scope for this card\n\n"
        "## Summary\n\nNothing could be built; the prerequisites are missing."
    )


async def test_all_declined_coding_card_is_blocked_no_pr_worktree_kept(monkeypatch):
    """Fails on current main: today the all-declined ledger is 'disposed', so the card
    opens a PR and reaches open_review. With the fix it is blocked owner=operator, no PR
    opens, and the worktree is not reaped."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True, **_kw):
        opened.append((wt, branch))
        return "https://example/pr/declined"

    feature = {
        **FEATURE,
        "id": "bd-1",
        "source_issue": "#4725",
        "requirements": [
            {"id": "r1", "text": "build the core change", "status": "open"},
            {"id": "r2", "text": "build the follow-on", "status": "open"},
        ],
    }
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch_all_declined, feature=feature)

    assert opened == []  # NO ready PR opened
    assert "open_review" not in store.names()  # never reached in_review
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked, "an all-declined coding card must be blocked"
    _, fid, reason, category = blocked[0]
    assert fid == "bd-1"
    assert category == MANUAL_BLOCK_CLASS  # owner=operator → terminal, never auto-cleared
    assert "all requirements declined" in reason
    # the per-item decline reasons ride the block so the operator sees why
    assert "upstream API" in reason and "out of scope" in reason
    # the worktree is KEPT — not reaped or removed — so the prerequisite report survives
    assert store.reaps == [] and store.removes == []


# ── r3: a partial delivery (one done, rest declined) publishes as today ───────────


async def _dispatch_partial(c, wt, prompt, *, timeout=None, env_passthrough=()):
    return (
        "## Requirements\n\n"
        "- r1: done — implemented the core change\n"
        "- r2: declined — out of scope for this card\n\n"
        "## Summary\n\nShipped r1; r2 is out of scope."
    )


async def test_one_done_rest_declined_still_opens_its_pr(monkeypatch):
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True, **_kw):
        opened.append((wt, branch))
        return "https://example/pr/partial"

    feature = {
        **FEATURE,
        "id": "bd-1",
        "requirements": [
            {"id": "r1", "text": "build the core change", "status": "open"},
            {"id": "r2", "text": "build the follow-on", "status": "open"},
        ],
    }
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch_partial, feature=feature)

    assert len(opened) == 1  # PR opened exactly as today
    assert ("open_review", "bd-1", "https://example/pr/partial") in store.calls
    assert "flag_blocked" not in store.names()  # a real delivery is never held by #715


# ── r4: the PR opener references, never closes, an all-declined card's issue ──────


def _sibling_store(source_issue="#4725"):
    store = FakeLoopStore()
    store.features = [{"id": "bd-1", "board_state": "in_progress", "source_issue": source_issue}]
    return store


def _loop_with_store(monkeypatch, store, *, slug="acme/widgets"):
    loop = BoardLoop({})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _slug(*, cwd):
        return slug

    monkeypatch.setattr(worktree, "repo_slug", _slug)
    return loop


async def test_with_source_issue_ref_refs_for_all_declined_card(monkeypatch):
    """An all-declined card (sole open slice of its issue) gets ``Refs #4725``, not
    ``Fixes #4725`` — the wiring that protects a later operator unblock/salvage."""
    loop = _loop_with_store(monkeypatch, _sibling_store())
    feature = {
        "id": "bd-1",
        "source_issue": "#4725",
        "requirements": [
            {"id": "r1", "status": "declined"},
            {"id": "r2", "status": "declined"},
        ],
    }
    body, refs_only = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert body == "## Summary\n\nRefs #4725"
    assert "Fixes #4725" not in body
    assert refs_only is True


async def test_with_source_issue_ref_fixes_when_a_requirement_is_done(monkeypatch):
    """Contrast: one ``done`` item means it is NOT all-declined, so a sole-open slice
    still gets ``Fixes #4725`` — the all-declined guard is specific to no-delivery cards."""
    loop = _loop_with_store(monkeypatch, _sibling_store())
    feature = {
        "id": "bd-1",
        "source_issue": "#4725",
        "requirements": [
            {"id": "r1", "status": "done"},
            {"id": "r2", "status": "declined"},
        ],
    }
    body, refs_only = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert body == "## Summary\n\nFixes #4725"
    assert refs_only is False


def test_inject_source_issue_line_refs_only_for_all_declined_ledger():
    """The helper itself, driven by the all-declined predicate, yields ``Refs #N``."""
    ledger = [{"id": "r1", "status": "declined"}, {"id": "r2", "status": "declined"}]
    refs_only = _all_items_declined(ledger)
    out = _inject_source_issue_line("## Summary\n\nnothing built", "", 4725, "acme/widgets", refs_only=refs_only)
    assert "Refs #4725" in out
    assert "Fixes #4725" not in out


# ── r5: a task (non-coding card) is unaffected by the all-declined gate ───────────


async def test_task_with_all_items_declined_is_unaffected(monkeypatch):
    """The gate is CODING-only (``issue_type != LABEL_TASK``): a task-typed card with an
    all-declined ledger is NOT blocked by #715 — it proceeds past the seam untouched."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True, **_kw):
        opened.append((wt, branch))
        return "https://example/pr/task"

    feature = {
        **FEATURE,
        "id": "bd-1",
        "issue_type": "task",
        "requirements": [
            {"id": "r1", "text": "a", "status": "open"},
            {"id": "r2", "text": "b", "status": "open"},
        ],
    }
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch_all_declined, feature=feature)

    # The #715 all-declined block never fires for a task: nothing is blocked for it.
    all_declined_blocks = [c for c in store.calls if c[0] == "flag_blocked" and "all requirements declined" in c[2]]
    assert all_declined_blocks == []
    assert len(opened) == 1  # the task-typed card passed the seam and published as before

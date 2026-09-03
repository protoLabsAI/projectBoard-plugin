"""Board lifecycle tools (#112): cancel / requeue / block / unblock.

The PM agent could create and promote work but had no verb to redirect, block, unblock,
or retire it — even though the store methods (``cancel_feature`` / ``requeue`` /
``flag_blocked`` / ``clear_blocked``) and their HTTP routes already existed. These tests
pin the four new tool wrappers: each de-quotes its free-text ``reason``, calls through to
its store method, and returns the ``{id, state}`` JSON echo (or an ``Error: …`` string on
a bad id).

The store path is exercised with the shared ``make_board`` fixture (a fake ``br``): a
recording ``_run`` captures the exact args each store method emits, and ``get_feature`` is
patched to supply the projection the method reads back — the same tool-level wiring
``test_board_updates.py`` uses.
"""

from __future__ import annotations

import json

import pytest

import project_board as pb


class _RecordingBr:
    """A fake ``_run`` that records every call and returns an inert value, so the exact
    ``br`` args a store method emits can be asserted. The feature projection is supplied
    separately via a patched ``get_feature`` (these lifecycle methods don't read state out
    of ``_run``; they act, then re-read)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, want_json=False):
        self.calls.append(args)
        return [] if want_json else ""

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


def _get_tool(name, cfg=None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


def _wire(make_board, monkeypatch, state):
    """Build a board whose ``_run`` records calls and whose ``get_feature`` returns
    ``state``, and point ``get_store`` at it so the tool wrappers call through."""
    br = _RecordingBr()
    board = make_board(br)
    monkeypatch.setattr(board, "get_feature", lambda fid: state)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    return board, br


# ── registration: all four verbs are exposed as agent tools ─────────────────────────


def test_lifecycle_tools_are_registered():
    names = {t.name for t in pb._board_tools({})}
    assert {
        "board_cancel_feature",
        "board_mark_done",
        "board_requeue_feature",
        "board_requeue_ci_fix",
        "board_block_feature",
        "board_unblock_feature",
    } <= names


# ── board_cancel_feature → store.cancel_feature ─────────────────────────────────────


def test_board_cancel_feature_tags_cancelled_and_closes_with_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    out = json.loads(_get_tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": "duplicate"}))

    # #211: the reply also says what the cancel did beyond the board edge — no PR to
    # close, no drive in flight here.
    assert out == {"id": "bd-1", "state": "cancelled", "pr_closed": False, "pr_detail": "", "drive_cancelled": False}
    (update,) = br.cmds("update")
    assert update == ("update", "bd-1", "--add-label", "cancelled", "--assignee", "")
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled: duplicate")


def test_board_cancel_feature_without_reason_closes_bare(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    _get_tool("board_cancel_feature").invoke({"feature_id": "bd-1"})

    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled")


def test_board_cancel_feature_strips_wrapping_quotes_from_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    _get_tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": '"bad decomposition"'})

    # the wrapping quotes are peeled before the reason reaches the audit trail.
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled: bad decomposition")


# ── board_mark_done → store.mark_done (the manual Done edge, #228) ──────────────────


def test_board_mark_done_closes_the_feature_with_a_done_reason(make_board, monkeypatch):
    """The wrapper calls through to store.mark_done: the bead is closed with an auditable
    `done: <reason>` and the projection reads `done` (get_feature returns the in-flight
    source first — what mark_done validates — then the closed projection it echoes back)."""
    br = _RecordingBr()
    board = make_board(br)
    calls = {"n": 0}

    def _gf(fid):
        calls["n"] += 1
        return {"id": fid, "board_state": "in_progress" if calls["n"] == 1 else "done", "labels": []}

    monkeypatch.setattr(board, "get_feature", _gf)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(_get_tool("board_mark_done").invoke({"feature_id": "bd-1", "reason": "shipped off-board"}))

    assert out == {"id": "bd-1", "state": "done"}
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "done: shipped off-board")


def test_board_mark_done_strips_wrapping_quotes_from_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "in_review", "labels": []})

    _get_tool("board_mark_done").invoke({"feature_id": "bd-1", "reason": '"shipped off-board"'})

    # the wrapping quotes are peeled before the reason reaches the audit trail.
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "done: shipped off-board")


# ── board_requeue_feature → store.requeue ───────────────────────────────────────────


def test_board_requeue_feature_returns_to_ready_and_drops_in_review(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "ready"})

    out = json.loads(_get_tool("board_requeue_feature").invoke({"feature_id": "bd-1"}))

    assert out == {"id": "bd-1", "state": "ready"}
    (update,) = br.cmds("update")
    # back to ready + in-review dropped + assignee cleared, so the puller can re-claim it.
    assert update == (
        "update",
        "bd-1",
        "--status",
        "open",
        "--assignee",
        "",
        "--add-label",
        "ready",
        "--remove-label",
        "in-review",
    )


def test_board_requeue_feature_bare_path_carries_no_findings(make_board, monkeypatch):
    """The empty-findings path stays a PLAIN re-dispatch: no review-bounce comment, and
    nothing queued for the loop's next dispatch prompt — only the requeue update fires
    (the coder re-runs against the ORIGINAL prompt). Asserts the EFFECT, not just the
    return: the feedback bridge must be untouched so a bare retry can't masquerade as a
    fix round."""
    import project_board.loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()  # isolate the cross-instance bridge (test_api convention)
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "ready"})

    out = json.loads(_get_tool("board_requeue_feature").invoke({"feature_id": "bd-1"}))

    assert out == {"id": "bd-1", "state": "ready"}
    assert loop_mod._PENDING_FEEDBACK == {}  # nothing queued on the bare path
    assert br.cmds("comments") == []  # no review-bounce comment recorded
    (update,) = br.cmds("update")  # only the requeue write fired
    assert update[:2] == ("update", "bd-1")


def test_board_requeue_feature_with_findings_carries_them_to_next_dispatch(make_board, monkeypatch):
    """WITH findings the tool runs the fix round — mirrors POST /features/{fid}/review:
    record_review_bounce → queue_review_feedback → requeue. Asserts the EFFECT that the
    review flagged as missing before: the findings are queued on the loop's feedback
    bridge so the NEXT dispatch prompt LEADS with them (not silently dropped)."""
    import project_board.loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()
    # record_review_bounce enforces in_review — the state an adverse review lands from.
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "in_review"})

    _get_tool("board_requeue_feature").invoke(
        {"feature_id": "bd-1", "findings": "the retry loop never terminates on a 500"}
    )

    # EFFECT 1: queue_review_feedback populated the bridge for this fid (the whole point
    # of #112 — without it the coder re-runs blind and reproduces the rejected output).
    assert "bd-1" in loop_mod._PENDING_FEEDBACK
    assert "the retry loop never terminates on a 500" in loop_mod._PENDING_FEEDBACK["bd-1"]
    # EFFECT 2: a DISTINCT review-bounce comment is recorded on the bead.
    (comment,) = br.cmds("comments")
    assert comment == (
        "comments",
        "add",
        "bd-1",
        "review requested changes: the retry loop never terminates on a 500",
    )
    # EFFECT 3: the feature is requeued onto the same open PR (ready + in-review dropped).
    (update,) = br.cmds("update")
    assert update == (
        "update",
        "bd-1",
        "--status",
        "open",
        "--assignee",
        "",
        "--add-label",
        "ready",
        "--remove-label",
        "in-review",
    )


def test_board_requeue_feature_strips_wrapping_quotes_from_findings(make_board, monkeypatch):
    import project_board.loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "in_review"})

    _get_tool("board_requeue_feature").invoke({"feature_id": "bd-1", "findings": '"missing a null guard"'})

    # the wrapping quotes are peeled before the findings reach the bead comment + bridge.
    (comment,) = br.cmds("comments")
    assert comment == ("comments", "add", "bd-1", "review requested changes: missing a null guard")
    assert "missing a null guard" in loop_mod._PENDING_FEEDBACK["bd-1"]


def test_board_requeue_feature_with_findings_from_non_in_review_is_an_error(make_board, monkeypatch):
    """record_review_bounce enforces in_review; the tool must surface that as an Error
    string (not blow up the turn) and queue NOTHING — a fix round can't start from a
    state an adverse review never lands in."""
    import project_board.loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "in_progress"})

    out = _get_tool("board_requeue_feature").invoke({"feature_id": "bd-1", "findings": "x"})

    assert out.startswith("Error: ")
    assert loop_mod._PENDING_FEEDBACK == {}  # bounce rejected before anything was queued
    assert br.cmds("update") == []  # and nothing was requeued


# ── board_block_feature → store.flag_blocked ────────────────────────────────────────


def test_board_block_feature_flags_blocked_and_comments_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "blocked"})

    out = json.loads(_get_tool("board_block_feature").invoke({"feature_id": "bd-1", "reason": "waiting on bd-0"}))

    assert out == {"id": "bd-1", "state": "blocked"}
    (update,) = br.cmds("update")
    assert "--add-label" in update and "blocked" in update
    assert "--assignee" in update and "" in update  # a coding feature is unassigned with the block
    # the failure class rides alongside so the sweep can tell a self-healing block from
    # one that needs a human — "waiting on bd-0" matches nothing, so it is terminal.
    assert "blocked-class:terminal" in update
    # the reason rides through as an auditable comment on the bead.
    (comment,) = br.cmds("comments")
    assert comment == ("comments", "add", "bd-1", "blocked: waiting on bd-0")


def test_board_block_feature_strips_wrapping_quotes_from_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "blocked"})

    _get_tool("board_block_feature").invoke({"feature_id": "bd-1", "reason": '"waiting on bd-0"'})

    (comment,) = br.cmds("comments")
    assert comment == ("comments", "add", "bd-1", "blocked: waiting on bd-0")


# ── board_unblock_feature → store.clear_blocked ─────────────────────────────────────


def test_board_unblock_feature_removes_the_blocked_label(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "ready"})

    out = json.loads(_get_tool("board_unblock_feature").invoke({"feature_id": "bd-1"}))

    assert out == {"id": "bd-1", "state": "ready"}
    (update,) = br.cmds("update")
    assert update[:2] == ("update", "bd-1")
    assert "--remove-label" in update and "blocked" in update


# ── the shared error path: a bad id surfaces as an Error string, not an exception ───


@pytest.mark.parametrize(
    "name,args",
    [
        ("board_cancel_feature", {"feature_id": "bd-x"}),
        ("board_mark_done", {"feature_id": "bd-x"}),
        ("board_requeue_feature", {"feature_id": "bd-x"}),
        ("board_block_feature", {"feature_id": "bd-x", "reason": "r"}),
        ("board_unblock_feature", {"feature_id": "bd-x"}),
    ],
)
def test_lifecycle_tools_return_error_string_for_unknown_feature(make_board, monkeypatch, name, args):
    # get_feature → None makes the store's `_require(fid)` raise BoardError; the tool must
    # catch it and hand back an "Error: …" string rather than blowing up the agent turn.
    br = _RecordingBr()
    board = make_board(br)
    monkeypatch.setattr(board, "get_feature", lambda fid: None)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = _get_tool(name).invoke(args)

    assert out.startswith("Error: unknown feature")
    assert br.cmds("update") == []  # nothing was written for a non-existent feature


# ── depends_on-aware dedup: a card built on an in-flight (in_review + open PR) feature ──
#
# Title matching can't see this ("work based on work that hasn't landed"): the new card
# has a legitimately different title, so it slips past the title guard and stacks a second
# card subordinate to an in-flight PR. _open_duplicate now also inspects the new card's
# depends_on and steers the author to board_requeue_feature (the fix-round verb, #112).


def test_open_duplicate_flags_depends_on_in_review_with_open_pr():
    features = [{"id": "bd-1", "title": "Base", "board_state": "in_review", "pr_url": "https://github.com/o/r/pull/9"}]
    dup = pb._open_duplicate(features, "A brand new title", ["bd-1"])
    assert dup is not None
    assert dup["id"] == "bd-1"
    assert dup["_dup_reason"] == "depends_on_in_review"  # tagged so the caller picks the requeue message


def test_open_duplicate_ignores_depends_on_in_review_without_a_pr():
    # in_review but no open PR → not the "based on in-flight work" pattern; let it through.
    features = [{"id": "bd-1", "title": "Base", "board_state": "in_review", "pr_url": ""}]
    assert pb._open_duplicate(features, "A brand new title", ["bd-1"]) is None


def test_open_duplicate_ignores_depends_on_not_in_review():
    # an open blocker in any other state (here in_progress) is a normal dependency, not a dup.
    features = [{"id": "bd-1", "title": "Base", "board_state": "in_progress", "pr_url": "https://gh/pr/9"}]
    assert pb._open_duplicate(features, "A brand new title", ["bd-1"]) is None


def test_open_duplicate_title_match_still_wins_and_is_untagged():
    # backward-compat: a plain same-title dup returns the raw feature with no _dup_reason,
    # even when depends_on is also supplied.
    features = [{"id": "bd-1", "title": "Same title", "board_state": "backlog"}]
    dup = pb._open_duplicate(features, "same   title", ["bd-2"])
    assert dup["id"] == "bd-1"
    assert "_dup_reason" not in dup


class _BoardWithDeps:
    """A store whose ``list_features`` returns a fixed board projection and whose
    ``create_feature`` records what it stacked — enough to exercise
    board_create_feature's depends_on dedup decision without a real br CLI."""

    def __init__(self, board):
        self._board = board
        self.created: list[dict] = []

    def list_features(self, state=None):
        return list(self._board)

    def create_feature(self, title, **kw):
        f = {"id": f"bd-new{len(self.created) + 1}", "title": title, "board_state": "backlog", **kw}
        self.created.append(f)
        return f


def test_board_create_feature_depends_on_in_review_pr_steers_to_requeue(monkeypatch):
    board = [{"id": "bd-1", "title": "Foundation", "board_state": "in_review", "pr_url": "https://gh/acme/pull/7"}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = _get_tool("board_create_feature").invoke(
        {"title": "Fix a bug in the foundation", "spec": "s", "depends_on": "bd-1"}
    )

    assert "Skipped" in out
    assert "bd-1" in out  # names the in-flight blocker
    assert "board_requeue_feature" in out and "findings=" in out  # the correct verb
    assert fake.created == []  # the subordinate card was NOT stacked


def test_board_create_feature_depends_on_in_review_force_stacks_anyway(monkeypatch):
    board = [{"id": "bd-1", "title": "Foundation", "board_state": "in_review", "pr_url": "https://gh/acme/pull/7"}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = _get_tool("board_create_feature").invoke(
        {"title": "Fix a bug in the foundation", "depends_on": "bd-1", "force": True}
    )

    assert json.loads(out)["id"] == "bd-new1"  # force bypasses the guard
    assert len(fake.created) == 1


def test_board_create_feature_depends_on_in_review_without_pr_is_allowed(monkeypatch):
    # in_review but no open PR yet → the guard doesn't fire; the card is created normally.
    board = [{"id": "bd-1", "title": "Foundation", "board_state": "in_review", "pr_url": ""}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = _get_tool("board_create_feature").invoke({"title": "New downstream work", "depends_on": "bd-1"})

    assert json.loads(out)["id"] == "bd-new1"
    assert len(fake.created) == 1


def test_board_create_feature_depends_on_open_but_not_in_review_is_allowed(monkeypatch):
    # a normal open dependency (backlog) is legitimate — depends_on dedup only fires on
    # the in_review-with-open-PR pattern.
    board = [{"id": "bd-1", "title": "Foundation", "board_state": "backlog", "pr_url": ""}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = _get_tool("board_create_feature").invoke({"title": "New downstream work", "depends_on": "bd-1"})

    assert json.loads(out)["id"] == "bd-new1"
    assert len(fake.created) == 1


# ── no tool may raise BoardError through the graph ──────────────────────────────
# An escaped BoardError propagates through the LangChain tool layer and FAILS the
# whole turn — every tool call the model already completed in it is discarded
# (observed live 2026-08-21: two turns died this way on an unbound board, one after
# 21 successful research calls). The board's failure mode must be an `Error: …`
# tool RESULT the model can adapt to. This sweep pins the contract for EVERY
# registered tool so the next unguarded one cannot ship.

_MINIMAL_ARGS = {
    "board_create_epic": {"title": "t"},
    "board_create_feature": {"title": "t"},
    "board_create_task": {"title": "t"},
    "board_update_feature": {"feature_id": "bd-1", "title": "t"},
    "board_get_feature": {"feature_id": "bd-1"},
    "board_mark_ready": {"feature_id": "bd-1"},
    "board_cancel_feature": {"feature_id": "bd-1"},
    "board_mark_done": {"feature_id": "bd-1"},
    "board_deliver": {"feature_id": "bd-1"},
    "board_verify": {"feature_id": "bd-1"},
    "board_requeue_feature": {"feature_id": "bd-1"},
    "board_requeue_ci_fix": {"feature_id": "bd-1", "ci_failure": "ci failed"},
    "board_block_feature": {"feature_id": "bd-1", "reason": "r"},
    "board_unblock_feature": {"feature_id": "bd-1"},
    "board_reset_merged_verify_budget": {"feature_id": "bd-1"},
    "board_list": {},
    "board_retro": {},
}


class _UnusableStore:
    """Every store access raises the unbound-board BoardError — the exact failure a
    fresh desktop member hits before `project_board.repo` is bound."""

    def __getattr__(self, name):
        def _raise(*a, **k):
            raise pb.store.BoardError("repo '.' has no beads workspace and `br init` failed — set project_board.repo")

        return _raise


def test_every_tool_returns_boarderror_as_result_never_raises(monkeypatch):
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _UnusableStore())
    tools = pb._board_tools({})
    missing = set(_MINIMAL_ARGS) - {t.name for t in tools}
    assert not missing, f"sweep args reference unknown tools: {missing}"

    for t in tools:
        if t.coroutine is not None:
            # Async tools (board_register_project) don't go through the beads store —
            # they work the HOST config seam and build their own `Error: …` strings
            # (the consent-gate refusal is one). The store sweep is a sync contract.
            continue
        assert t.name in _MINIMAL_ARGS, (
            f"new tool {t.name!r} has no minimal-args entry — add one so the no-raise contract covers it"
        )
        out = t.invoke(_MINIMAL_ARGS[t.name])
        assert isinstance(out, str) and out.startswith("Error:"), (
            f"{t.name} must return the BoardError as an `Error: …` result, got: {out!r:.120}"
        )


# ── #211: a cancel closes the card's open PR + stops its in-flight drive ────────────


def test_board_cancel_feature_closes_an_open_pr_best_effort(make_board, monkeypatch):
    """Cancel during the CI/review bounce: the card has a pr_url — close it with a
    comment pointing at the card. A gh failure is logged, never an Error reply."""
    from project_board import worktree

    closed = []
    monkeypatch.setattr(
        worktree,
        "close_pr_sync",
        lambda url, *, comment, cwd=".", timeout=60: closed.append((url, comment)) or (True, ""),
    )
    _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled", "pr_url": "https://x/pr/4"})
    out = json.loads(_get_tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": "scope cut"}))
    assert out["pr_closed"] is True and out["drive_cancelled"] is False
    assert closed == [("https://x/pr/4", "cancelled by operator — see card bd-1")]

    monkeypatch.setattr(worktree, "close_pr_sync", lambda *a, **k: (False, "gh: not found"))
    _wire(make_board, monkeypatch, {"id": "bd-2", "board_state": "cancelled", "pr_url": "https://x/pr/5"})
    out = json.loads(_get_tool("board_cancel_feature").invoke({"feature_id": "bd-2"}))
    assert out["state"] == "cancelled" and out["pr_closed"] is False  # cancel landed; close didn't — said so


def test_board_cancel_feature_closes_the_pr_under_the_features_project_repo(make_board, monkeypatch):
    """#262: a cancel on a project-B card runs its `gh` PR close in B's checkout —
    resolved from the card's `project:<name>` label through the shared route/tool
    resolver (api.repo_for_feature, the loop's `_repo_for` order), not the
    board-default repo the tool's store_kw carries."""
    from project_board import worktree

    closed = []
    monkeypatch.setattr(
        worktree,
        "close_pr_sync",
        lambda url, *, comment, cwd=".", timeout=60: closed.append((url, cwd)) or (True, ""),
    )
    _wire(
        make_board,
        monkeypatch,
        {"id": "bd-1", "board_state": "cancelled", "pr_url": "https://x/pr/4", "project": "beta"},
    )
    cfg = {
        "repo": "/default",
        "projects": {"alpha": {"repo": "/alpha"}, "beta": {"repo": "/beta"}},
        "default_project": "alpha",
    }
    out = json.loads(_get_tool("board_cancel_feature", cfg).invoke({"feature_id": "bd-1", "reason": "scope cut"}))
    assert out["pr_closed"] is True
    assert closed == [("https://x/pr/4", "/beta")]


def test_board_cancel_feature_unlabeled_card_closes_under_the_default_project_repo(make_board, monkeypatch):
    """Back-compat: a pre-#90 card (no project label) keeps closing its PR under the
    default project's repo — the resolver changes nothing for single-repo boards."""
    from project_board import worktree

    closed = []
    monkeypatch.setattr(
        worktree,
        "close_pr_sync",
        lambda url, *, comment, cwd=".", timeout=60: closed.append(cwd) or (True, ""),
    )
    _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled", "pr_url": "https://x/pr/4"})
    cfg = {
        "repo": "/default",
        "projects": {"alpha": {"repo": "/alpha"}, "beta": {"repo": "/beta"}},
        "default_project": "alpha",
    }
    json.loads(_get_tool("board_cancel_feature", cfg).invoke({"feature_id": "bd-1"}))
    assert closed == ["/alpha"]


# ── task tools (#217): board_create_task / board_deliver / board_verify ──────────────
#
# A task-type bead rides the same rails as a coding feature (ready → claim → in_progress →
# in_review) but ships a DELIVERABLE instead of a PR: board_create_task mints it, board_deliver
# is its open_pr→open_review edge, board_verify its Done edge. These pin the three tool
# wrappers — registration, the happy paths, the same dedup as board_create_feature, and the
# task-only/wrong-state refusals surfacing as `Error: …` strings.


def test_task_tools_are_registered():
    names = {t.name for t in pb._board_tools({})}
    assert {"board_create_task", "board_deliver", "board_verify"} <= names


# ── board_create_task → store.create_feature(issue_type="task") ─────────────────────


def test_board_create_task_mints_a_task_bead_without_files(monkeypatch):
    """A task is minted with issue_type=task and carries NO files_to_modify (a task
    doesn't touch repo files) — the reply echoes {id, title}."""
    fake = _BoardWithDeps([])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = json.loads(
        _get_tool("board_create_task").invoke(
            {"title": "Write the triage doc", "spec": "s", "acceptance_criteria": "a", "assignee": "quinn"}
        )
    )

    assert out["id"] == "bd-new1" and out["title"] == "Write the triage doc"
    (created,) = fake.created  # exactly one bead stacked
    assert created["issue_type"] == "task"  # minted as a task, not a feature
    assert created["assignee"] == "quinn"  # pre-assigned
    assert "files_to_modify" not in created  # a task never carries files_to_modify


def test_board_create_task_refuses_a_duplicate_title(monkeypatch):
    """Same dedup as board_create_feature: a same-title OPEN card is refused (title
    match is normalized — trim/lowercase/collapse-whitespace) and nothing is stacked."""
    board = [{"id": "bd-1", "title": "Write the triage doc", "board_state": "backlog"}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = _get_tool("board_create_task").invoke({"title": "write   the   triage doc", "spec": "s"})

    assert "Skipped" in out and "bd-1" in out
    assert fake.created == []  # the duplicate was NOT stacked


def test_board_create_task_force_bypasses_dedup(monkeypatch):
    board = [{"id": "bd-1", "title": "Write the triage doc", "board_state": "backlog"}]
    fake = _BoardWithDeps(board)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    out = json.loads(_get_tool("board_create_task").invoke({"title": "Write the triage doc", "force": True}))

    assert out["id"] == "bd-new1"  # force bypasses the guard
    assert fake.created[0]["issue_type"] == "task"


# ── board_deliver → store.record_delivery ; board_verify → store.record_verification ──


class _TaskEdgeStore:
    """A store stub exposing just the task edges the deliver/verify wrappers call, so the
    tool wiring (strip quotes → call through positionally → {id, state} echo) is tested
    without the real br store. Set ``error`` to make the edge raise the task-only/wrong-state
    BoardError the wrapper must surface as an `Error: …` string."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def _edge(self, name, fid, *rest):
        self.calls.append((name, fid, *rest))
        if self.error:
            raise pb.store.BoardError(self.error)
        return self.reply

    def record_delivery(self, fid, text="", ref=""):
        return self._edge("record_delivery", fid, text, ref)

    def record_verification(self, fid, approved=True, feedback="", by=""):
        # `by` (#316 S3c) rides at a fixed 4th slot so a call that omits it (the empty
        # tool default) records "" here — the same shape the store sees before resolving
        # it to its own actor. The verifier-identity tests below assert this slot.
        return self._edge("record_verification", fid, approved, feedback, by)


def test_board_deliver_records_delivery_and_returns_in_review(monkeypatch):
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "in_review"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(
        _get_tool("board_deliver").invoke(
            {"feature_id": "bd-t", "text": "triage report at docs/triage.md", "ref": "docs/triage.md"}
        )
    )

    assert out == {"id": "bd-t", "state": "in_review"}
    assert board.calls == [("record_delivery", "bd-t", "triage report at docs/triage.md", "docs/triage.md")]


def test_board_deliver_strips_wrapping_quotes(monkeypatch):
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "in_review"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    _get_tool("board_deliver").invoke({"feature_id": "bd-t", "text": '"the deliverable"', "ref": '"docs/x.md"'})

    # the wrapping quotes are peeled before text/ref reach the store.
    assert board.calls == [("record_delivery", "bd-t", "the deliverable", "docs/x.md")]


def test_board_deliver_surfaces_a_task_only_or_wrong_state_error(monkeypatch):
    board = _TaskEdgeStore(error="record_delivery is task-only — issue_type 'feature' enters review via open_review")
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = _get_tool("board_deliver").invoke({"feature_id": "bd-1", "text": "x"})

    assert out.startswith("Error: ") and "task-only" in out


def test_board_verify_approved_closes_the_task_done(monkeypatch):
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "done"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(_get_tool("board_verify").invoke({"feature_id": "bd-t", "approved": True}))

    assert out == {"id": "bd-t", "state": "done"}
    # omitting `by` forwards the empty tool default (the store resolves it to its actor).
    assert board.calls == [("record_verification", "bd-t", True, "", "")]


def test_board_verify_rejected_requeues_to_ready_with_feedback(monkeypatch):
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "ready"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(
        _get_tool("board_verify").invoke(
            {"feature_id": "bd-t", "approved": False, "feedback": '"misses the Q3 numbers"'}
        )
    )

    assert out == {"id": "bd-t", "state": "ready"}
    # approved flag threads through, and the wrapping quotes are peeled off the feedback;
    # `by` rides its empty default here (this rejection path omits it).
    assert board.calls == [("record_verification", "bd-t", False, "misses the Q3 numbers", "")]


def test_board_verify_surfaces_a_task_only_or_wrong_state_error(monkeypatch):
    board = _TaskEdgeStore(error="record_verification is task-only — issue_type 'feature' closes via record_merge")
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = _get_tool("board_verify").invoke({"feature_id": "bd-1", "approved": True})

    assert out.startswith("Error: ") and "task-only" in out


# ── #316 S3c: board_verify forwards the verifier identity `by` to record_verification ──
#
# The store's Done edge (bd-hksj) already accepts `by` and writes it into the `verified:
# <by>` close reason (flagging, never blocking, a self-approval). These pin the TOOL seam:
# an explicit `by` forwards UNCHANGED, and an omitted `by` forwards the empty tool default
# so the store — not the tool — resolves it to its actor (it must NOT substitute the HTTP
# API's `operator` default, which is a deliberately different caller).


def test_board_verify_forwards_explicit_by_to_record_verification(monkeypatch):
    """An explicit verifier identity threads through the tool to the store's Done edge,
    unchanged — the store writes it into the `verified: <by>` close reason."""
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "done"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(_get_tool("board_verify").invoke({"feature_id": "bd-t", "approved": True, "by": "quinn"}))

    assert out == {"id": "bd-t", "state": "done"}
    # `by` arrives at the store verbatim (4th slot), not dropped and not rewritten.
    assert board.calls == [("record_verification", "bd-t", True, "", "quinn")]


def test_board_verify_omitting_by_forwards_the_empty_store_actor_default(monkeypatch):
    """Omitting `by` forwards the empty TOOL default so the STORE resolves it to its own
    actor (this tool IS the agent). It must NOT substitute the HTTP API's `operator`
    default — the tool never invents a verifier the caller didn't name."""
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "done"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    json.loads(_get_tool("board_verify").invoke({"feature_id": "bd-t", "approved": True}))

    (call,) = board.calls
    assert call == ("record_verification", "bd-t", True, "", "")  # empty tool default, not "operator"


def test_board_verify_does_not_strip_wrapping_quotes_from_by(monkeypatch):
    """`by` forwards UNCHANGED — unlike `feedback`, it is NOT de-quoted. This pins the
    deliberate asymmetry: the store compares identity casefolded but preserves the caller's
    text, so the tool must hand the value through verbatim."""
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "done"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    _get_tool("board_verify").invoke({"feature_id": "bd-t", "approved": True, "by": '"quinn"'})

    # feedback would be peeled; `by` is not — it reaches the store with its quotes intact.
    assert board.calls == [("record_verification", "bd-t", True, "", '"quinn"')]


def test_board_verify_forwards_by_on_the_rejection_path_too(monkeypatch):
    """`by` threads through regardless of the verdict — a rejection still carries the
    verifier identity to the store (which records the feedback comment and requeues)."""
    board = _TaskEdgeStore(reply={"id": "bd-t", "board_state": "ready"})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = json.loads(
        _get_tool("board_verify").invoke(
            {"feature_id": "bd-t", "approved": False, "feedback": "misses the Q3 numbers", "by": "quinn"}
        )
    )

    assert out == {"id": "bd-t", "state": "ready"}
    assert board.calls == [("record_verification", "bd-t", False, "misses the Q3 numbers", "quinn")]

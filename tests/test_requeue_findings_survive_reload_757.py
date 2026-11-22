"""#757 (last slice): a resume-repair pass after a reload cannot send a card to review
while its requeued fix-round findings are still undelivered.

Live shape (mythxengine br-n31.10): ``board_requeue_feature(findings=…)``, then a plugin
reload. Boot recovery saw the PR plus a retained integration checkout and reset the card
"to resume repair"; the drive resumed that checkout with an integration-only prompt, the
coder merged main in, and the card went to ``in_review`` with none of the findings.
"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_board import loop as loop_mod
from project_board import store as store_mod
from project_board import worktree
from project_board.loop import BoardLoop
from tests.test_conflict_recovery_contract import git, history, sibling  # noqa: F401 — fixture
from tests.test_loop import FEATURE, FakeLoopStore, _drive_with, _RecoverStore

FINDING = "X: guard the null branch in the parser"
PR = "https://example/pr/7"


def bounce(text=FINDING):
    return f"{store_mod.REVIEW_BOUNCE_PREFIX}: {text}"


class _CommentStore(FakeLoopStore):
    """A drive store with the durable bounce history slice 1 reads and writes."""

    def __init__(self, comments=()):
        super().__init__()
        self.history = list(comments)

    def feature_comments(self, fid):
        return list(self.history)

    def record_fix_feedback_delivery(self, fid, receipt):
        self.calls.append(("receipt", fid))
        self.history.append(receipt)


class _RecoverWithComments(_RecoverStore):
    def __init__(self, feature, comments=()):
        super().__init__([feature])
        self._feature = feature
        self.history = list(comments)

    def get_feature(self, fid):
        return {**self._feature, "board_state": "in_progress"}

    def feature_comments(self, fid):
        return list(self.history)


@pytest.fixture(autouse=True)
def _clean_queue():
    loop_mod._PENDING_FEEDBACK.clear()
    yield
    loop_mod._PENDING_FEEDBACK.clear()


# ── r2: the resume-repair log line names the waiting fix round ───────────────────


@pytest.mark.parametrize("queued", ["durable", "memory", None])
async def test_reconcile_resume_repair_log_says_feedback_pending(monkeypatch, caplog, queued):
    feature = {**FEATURE, "pr_url": PR}
    store = _RecoverWithComments(feature, [bounce()] if queued == "durable" else [])
    if queued == "memory":
        loop_mod.queue_review_feedback(feature["id"], FINDING)
    board = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(board, "_store", lambda: store)
    monkeypatch.setattr(board, "_repo_for", lambda _: "/repo")
    monkeypatch.setattr(worktree, "pending_integration", AsyncMock(return_value=("/wt/x", "feat/x")))
    lookup = AsyncMock(return_value=PR)
    monkeypatch.setattr(worktree, "pr_url_for_branch", lookup)
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await board._reconcile_orphan(feature["id"])
    assert store.calls == [("requeue", feature["id"])]
    lookup.assert_not_awaited()
    line = next(r.getMessage() for r in caplog.records if "resume repair" in r.getMessage())
    assert ("fix-round feedback pending" in line) is bool(queued)


async def test_reconcile_log_survives_an_unreadable_history(monkeypatch, caplog):
    feature = {**FEATURE, "pr_url": PR}
    store = _RecoverWithComments(feature)

    def boom(fid):
        raise store_mod.BoardError("br unavailable")

    store.feature_comments = boom
    board = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(board, "_store", lambda: store)
    monkeypatch.setattr(board, "_repo_for", lambda _: "/repo")
    monkeypatch.setattr(worktree, "pending_integration", AsyncMock(return_value=("/wt/x", "feat/x")))
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await board._reconcile_orphan(feature["id"])
    assert store.calls == [("requeue", feature["id"])]
    assert any("resume repair)" in r.getMessage() for r in caplog.records)


# ── r1 / r3: the drive that resumes the retained integration ─────────────────────


async def _retained_integration(repo):
    """An open PR whose retained checkout holds a finished, unpublished base merge."""
    sibling(repo, "a")
    feature = {**FEATURE, "pr_url": PR}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
    await worktree.finish_integration(path, branch, "main")
    assert await worktree.pending_integration(str(repo), ".worktrees", feature["id"], feature["title"])
    return feature, path, branch


async def _reload_then_drive(monkeypatch, repo, feature, path, store, *, on_dispatch=None):
    """Boot recovery's reset, then the drive, recording every prompt and the hand-off."""
    recovery = _RecoverWithComments(feature, store.history)
    restarted = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(restarted, "_store", lambda: recovery)
    monkeypatch.setattr(restarted, "_repo_for", lambda _: str(repo))
    monkeypatch.setattr(worktree, "pr_url_for_branch", AsyncMock(return_value=PR))
    await restarted._reconcile_orphan(feature["id"])
    assert recovery.calls == [("requeue", feature["id"])]

    timeline = []

    async def publish(wt, branch, **kwargs):
        git(wt, "push", "origin", branch)
        timeline.append("open_pr")
        return PR

    original_open_review = store.open_review

    def open_review(fid, *, pr_url):
        timeline.append("open_review")
        return original_open_review(fid, pr_url=pr_url)

    store.open_review = open_review

    async def dispatch(coder, resumed_path, prompt, *args, **kwargs):
        assert resumed_path == path  # the same retained tree, branch and PR every pass
        timeline.append(("dispatch", prompt))
        if on_dispatch is not None:
            on_dispatch(len([t for t in timeline if isinstance(t, tuple)]), resumed_path)
        return "## Summary\nRound done"

    def seed(lp):
        monkeypatch.setattr(lp, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(lp, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(lp, "_verify_goal", AsyncMock(return_value=None))
        monkeypatch.setattr(lp, "_empty_result_signals", lambda _: (True, "end_turn"))
        monkeypatch.setattr(lp, "_resolve_delegate", lambda name, expect: name)
        lp.goal_verify = True

    monkeypatch.setattr("project_board.loop.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("project_board.loop.drive._next_rung_cursor", lambda: 0)
    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=AsyncMock(side_effect=publish),
        gate=AsyncMock(return_value=None),
        cfg={"coder": "proto"},
        store=store,
    )
    return timeline, store


def _prompts_before_review(timeline):
    stop = timeline.index("open_review") if "open_review" in timeline else len(timeline)
    return [t[1] for t in timeline[:stop] if isinstance(t, tuple)]


async def test_resumed_integration_dispatch_carries_requeued_findings(history, monkeypatch):  # noqa: F811
    """r1: the reload-resumed repair prompt leads with the requeued findings, and the card
    reaches in_review only after that prompt was dispatched."""
    repo = history
    feature, path, _ = await _retained_integration(repo)
    store = _CommentStore([bounce()])
    timeline, store = await _reload_then_drive(monkeypatch, repo, feature, path, store)
    assert "open_review" in timeline
    before = _prompts_before_review(timeline)
    assert any(FINDING in p for p in before), "card reached in_review with no prompt carrying the findings"
    carrying = next(p for p in before if FINDING in p)
    # The findings lead; the board's integration block rides after them.
    assert "## Current board-prepared base integration" in carrying
    assert carrying.index(FINDING) < carrying.index("## Current board-prepared base integration")
    assert not loop_mod.has_undelivered_fix_feedback(store, feature["id"])


async def test_findings_queued_during_the_repair_pass_get_a_second_dispatch(history, monkeypatch):  # noqa: F811
    """r1 (second form): findings that land while the integration-only pass runs do not
    ride its hand-off — the same PR gets a second dispatch carrying them first."""
    repo = history
    feature, path, _ = await _retained_integration(repo)
    store = _CommentStore()

    def queue_mid_pass(n, wt):
        if n == 1:
            store.history.append(bounce())
        else:
            (Path(wt) / "b").write_text("feature coverage\nnull branch guarded\n")

    timeline, store = await _reload_then_drive(monkeypatch, repo, feature, path, store, on_dispatch=queue_mid_pass)
    prompts = [t[1] for t in timeline if isinstance(t, tuple)]
    assert len(prompts) == 2
    assert FINDING not in prompts[0]
    assert FINDING in prompts[1]
    assert timeline.index("open_review") > timeline.index(("dispatch", prompts[1]))
    assert timeline.count("open_review") == 1
    assert not loop_mod.has_undelivered_fix_feedback(store, feature["id"])


async def test_resume_repair_without_feedback_goes_to_review_as_today(history, monkeypatch):  # noqa: F811
    """r3: no queued feedback — one resumed repair dispatch, then in_review."""
    repo = history
    feature, path, _ = await _retained_integration(repo)
    store = _CommentStore()
    timeline, store = await _reload_then_drive(monkeypatch, repo, feature, path, store)
    prompts = [t[1] for t in timeline if isinstance(t, tuple)]
    assert len(prompts) == 1
    assert "## Current board-prepared base integration" in prompts[0]
    assert timeline[-1] == "open_review"
    assert ("receipt", feature["id"]) not in store.calls


async def test_undeliverable_findings_block_instead_of_reaching_review(monkeypatch):
    """The backstop is bounded: findings that never read back as delivered stop the card
    for triage rather than looping or handing an unfixed PR to review."""

    class _NoReceipt(_CommentStore):
        def record_fix_feedback_delivery(self, fid, receipt):
            self.calls.append(("receipt", fid))  # write accepted, never visible in history

    store = _NoReceipt([bounce()])
    feature = {**FEATURE, "pr_url": PR}
    prompts = []

    async def dispatch(coder, wt, prompt, *a, **k):
        prompts.append(prompt)
        return "## Summary\nRound done"

    async def open_pr(*a, **k):
        return PR

    monkeypatch.setattr(worktree, "recovery_context", AsyncMock(return_value={}))
    monkeypatch.setattr(worktree, "pending_integration", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree, "checkout_head_sha", AsyncMock(return_value=""))
    monkeypatch.setattr(worktree, "tree_exists", lambda wt: True)

    def seed(lp):
        monkeypatch.setattr(lp, "_verify_goal", AsyncMock(return_value=None))
        monkeypatch.setattr(lp, "_prepare_tree", AsyncMock(return_value=""))
        monkeypatch.setattr(lp, "_run_fixups", AsyncMock(return_value=None))
        monkeypatch.setattr(lp, "_empty_result_signals", lambda _: (True, "end_turn"))

    _, store = await _drive_with(
        monkeypatch, feature=feature, dispatch=dispatch, open_pr=open_pr, seed=seed, store=store
    )
    assert "open_review" not in store.names()
    assert any(c[0] == "flag_blocked" and "fix-round findings could not be delivered" in c[2] for c in store.calls)
    assert len(prompts) == 1 + loop_mod.drive._FIX_FEEDBACK_REDISPATCH_MAX


def test_delivered_brief_is_carried_by_the_drive_not_replayed(monkeypatch):
    """The first build hands its delivered brief to the drive; a rebuild that carries it
    keeps the findings, and a build that does not (a later drive) never replays them."""
    store = _CommentStore([bounce()])
    monkeypatch.setattr(loop_mod, "get_store", lambda **kw: store)
    feature = {**FEATURE, "pr_url": PR}
    board = BoardLoop({"coder": "proto"})
    assert FINDING in board._build_prompt(feature)
    carried = board._delivered_fix_briefs.pop(feature["id"])
    assert FINDING in carried
    rebuilt = board._build_prompt(feature, carried_feedback=carried)
    assert rebuilt.count(FINDING) == 1
    assert feature["id"] not in board._delivered_fix_briefs  # nothing new was delivered
    assert FINDING not in board._build_prompt(feature)

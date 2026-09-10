"""A card moved while its drive runs: the move stands, the work is kept (#398).

Live (protoEngineer, 2026-09-07): `bd-p8ft` went TERMINAL with an open PR and no new
cause. The loop's own CI-fix round was building it when the PM called
`board_requeue_ci_fix`. The tool accepted, because a card mid-round has exactly the
in_progress + open-PR shape it accepts. The requeue moved the card to `ready`; the round
finished, `open_review` refused (`expects in_progress, got 'ready'`), and the drive's
catch-all blocked the card terminally on that message.

Two halves, both here:

- **The trigger is refused.** The requeue verbs and routes decline a card the loop is
  still working, with a message saying to wait or cancel.
- **The drive stands aside from a card it no longer owns** — held, marked done, cancelled,
  or moved by any path the guard does not see. It asks before every edge that would
  change the card (block, retry, publish, hand-off). It then records a PR it opened on
  the card, keeps unpushed work where it is, resets its fix budgets and leaves one
  best-effort comment. The move stands.

The store is REAL `br`: every refusal and state the drive reads is the real board's. The
coder, git and GitHub seams are faked — they are not what moves the card.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
import project_board.loop as loop_mod
from project_board import api, worktree
from project_board import failures as failures_mod
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)
pytestmark = requires_br

PR_OLD = "https://github.com/protoLabsAI/protoAgent/pull/3364"
PR_NEW = "https://github.com/protoLabsAI/protoAgent/pull/3370"


class _Round:
    """One drive on a real board, whose coder waits to be released — so a test can act on
    the card while the round is still running. ``with_pr`` makes it a fix round on an
    existing PR; without, it is the card's first build."""

    def __init__(self, tmp_path, monkeypatch, *, with_pr=True, open_pr_exc=None):
        self.board = BeadsBoard(repo=str(tmp_path), actor="test")
        (tmp_path / "target.py").write_text("x = 1\n")
        self.fid = self.board.create_feature(
            "fix(a2a): record continuity entries", spec="s", files_to_modify=["target.py"]
        )["id"]
        args = ["update", self.fid, "--add-label", "ready"] + (["--external-ref", PR_OLD] if with_pr else [])
        self.board._run(*args)  # setup, not under test
        monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: self.board)
        monkeypatch.setattr(store_mod, "get_store", lambda **_kw: self.board)
        monkeypatch.setattr(api, "get_store", lambda **_kw: self.board)
        loop_mod._PENDING_FEEDBACK.clear()
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.creates, self.opened, self.removed, self.closed = [], [], [], []
        self.dispatches = 0
        self.wt = str(tmp_path / "wt")
        pr = PR_OLD if with_pr else PR_NEW

        async def _create(repo, base, fid, root, title="", **kw):
            self.creates.append(kw.get("resume"))
            return (self.wt, "feat/" + fid)

        async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
            self.dispatches += 1
            self.started.set()
            await self.release.wait()  # the coder is still working
            return "## Summary\n\n- propagated the originating session\n"

        async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
            if open_pr_exc is not None:
                raise open_pr_exc
            self.opened.append(pr)
            return pr

        async def _remove(repo, wt, branch=""):
            self.removed.append(wt)
            return True

        async def _close(pr_url, *, comment, cwd="."):
            self.closed.append(pr_url)
            return True, ""

        async def _no_pr(branch, *, cwd="."):
            return ""

        monkeypatch.setattr(worktree, "create_worktree", _create)
        monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
        monkeypatch.setattr(worktree, "open_pr", _open_pr)
        monkeypatch.setattr(worktree, "remove_worktree", _remove)
        monkeypatch.setattr(worktree, "close_pr", _close)
        monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
        self.loop = BoardLoop({"coder": "proto", "repo": str(tmp_path)})
        monkeypatch.setattr(self.loop, "_resolve_delegate", lambda name, expect: object())

    async def start(self):
        assert await self.loop._spawn_ready()  # the loop claims the card and starts its round
        self.drive = next(iter(self.loop._drives))
        await asyncio.wait_for(self.started.wait(), 10)
        assert self.board.get_feature(self.fid)["board_state"] == "in_progress"

    async def finish(self):
        self.release.set()
        await asyncio.wait_for(self.drive, 30)
        return self.board.get_feature(self.fid)

    def notes(self):
        return self.board.feature_comments(self.fid)


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


# ── the trigger: a requeue under a live round is refused ─────────────────────────────


async def test_the_requeue_verbs_refuse_a_card_under_its_live_round(tmp_path, monkeypatch):
    r = _Round(tmp_path, monkeypatch)
    await r.start()

    ci = await asyncio.to_thread(
        _tool("board_requeue_ci_fix").invoke, {"feature_id": r.fid, "ci_failure": "CodeRabbit: drops session_id"}
    )
    bare = await asyncio.to_thread(_tool("board_requeue_feature").invoke, {"feature_id": r.fid})

    for reply in (ci, bare):
        assert reply.startswith(f"Error: {r.fid} can't be requeued while a coder drive is still building it")
        assert "board_cancel_feature" in reply  # says what to do instead
    assert r.board.get_feature(r.fid)["board_state"] == "in_progress"  # nothing moved
    assert loop_mod._PENDING_FEEDBACK == {}  # and nothing was queued for a round that isn't coming

    f = await r.finish()  # the round ends on its own — into review — and the guard lifts
    assert f["board_state"] == "in_review"
    assert json.loads(_tool("board_requeue_feature").invoke({"feature_id": r.fid}))["state"] == "ready"


async def test_the_requeue_routes_refuse_a_card_under_its_live_round(tmp_path, monkeypatch):
    r = _Round(tmp_path, monkeypatch)
    await r.start()
    secret = "s3cret"
    app = FastAPI()
    app.include_router(api.build_router({"webhook_secret": secret}), prefix="/plugins/project_board")
    client = TestClient(app)

    def _post(path, body):
        raw = json.dumps(body, separators=(",", ":")).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return client.post(
            f"/plugins/project_board/features/{r.fid}/{path}", content=raw, headers={"X-Hub-Signature-256": sig}
        )

    for path, body in (("ci", {"passed": False, "reason": "lint"}), ("review", {"findings": "rename it"})):
        resp = _post(path, body)
        assert resp.status_code == 400 and "can't be requeued while" in resp.text, (path, resp.text)
    assert r.board.get_feature(r.fid)["board_state"] == "in_progress"
    await r.finish()


# ── the drive: a card moved under it keeps its move AND its work ─────────────────────


async def test_a_first_build_moved_to_ready_keeps_its_pr_and_the_next_round_resumes_it(tmp_path, monkeypatch):
    """Any mover the guard can't see — here the store's own requeue. Before the fix the
    build opened a PR the card never recorded, so the next round rebuilt off base
    (`resume=False`) and force-pushed over the branch that PR is on."""
    r = _Round(tmp_path, monkeypatch, with_pr=False)
    await r.start()
    await asyncio.to_thread(r.board.requeue, r.fid)

    f = await r.finish()

    assert r.opened == [PR_NEW]
    assert f["board_state"] == "ready" and not f["blocked"]
    assert f["pr_url"] == PR_NEW  # recorded on the card, state untouched
    assert any("moved to ready" in n and PR_NEW in n and "resumes from it" in n for n in r.notes())
    redriven: list[str] = []

    async def _record(feature):
        redriven.append(feature.get("pr_url"))

    monkeypatch.setattr(r.loop, "_drive", _record)
    assert await r.loop._spawn_ready()
    await asyncio.gather(*r.loop._drives)
    assert redriven == [PR_NEW]  # the next round is claimed WITH the PR → create_worktree(resume=True)


async def test_a_card_held_under_its_round_gets_no_pr_and_keeps_its_hold_and_work(tmp_path, monkeypatch):
    r = _Round(tmp_path, monkeypatch, with_pr=False)
    await r.start()
    await asyncio.to_thread(r.board.flag_blocked, r.fid, "hold: legal review pending", "terminal")

    f = await r.finish()

    assert r.opened == []  # no PR for a card someone is holding
    assert f["blocked"] and f["blocked_class"] == "terminal" and f["blocked_reason"] == "hold: legal review pending"
    assert r.removed == []  # the unpushed work stays where it is
    assert any("opened no PR" in n and r.wt in n for n in r.notes())


async def test_a_card_marked_done_under_its_round_gets_no_pr(tmp_path, monkeypatch):
    """Before the fix the build published anyway: an open PR for a done card, tracked by
    nothing."""
    r = _Round(tmp_path, monkeypatch, with_pr=False)
    await r.start()
    await asyncio.to_thread(_tool("board_mark_done").invoke, {"feature_id": r.fid, "reason": "shipped in #1234"})

    f = await r.finish()

    assert r.opened == [] and f["board_state"] == "done"


async def test_a_failing_round_on_a_moved_card_is_not_blocked(tmp_path, monkeypatch):
    """Every block site asks first, not just the hand-off: a requeued card whose round then
    failed to publish was still blocked terminal on the round's error."""
    r = _Round(
        tmp_path, monkeypatch, open_pr_exc=worktree.WorktreeError("gh pr create failed: Head sha can't be blank")
    )
    await r.start()
    await asyncio.to_thread(r.board.requeue, r.fid)

    f = await r.finish()

    assert f["board_state"] == "ready" and not f["blocked"]
    assert any("Head sha can't be blank" in n and "neither blocked nor retried" in n for n in r.notes())


async def test_a_held_card_is_not_retried_or_reblocked_after_a_transient_failure(tmp_path, monkeypatch):
    """Before the fix: a human's hold placed mid-round, then a transient publish error.
    The coder was re-dispatched three times on the held card, which was then re-blocked
    `transient`, and the blocked sweep auto-cleared it: the hold was gone."""
    fast = tuple(
        (pat, failures_mod.Policy(p.category, p.retryable, 0.0, p.max_attempts)) for pat, p in failures_mod._RULES
    )
    monkeypatch.setattr(failures_mod, "_RULES", fast)  # no 15s backoff in a test
    r = _Round(tmp_path, monkeypatch, open_pr_exc=worktree.WorktreeError("git push failed: Connection reset by peer"))
    await r.start()
    await asyncio.to_thread(r.board.flag_blocked, r.fid, "hold: do not ship until the vendor signs", "terminal")

    f = await r.finish()
    await r.loop._recover_blocked(r.board)

    assert r.dispatches == 1
    g = r.board.get_feature(r.fid)
    assert g["blocked"] and g["blocked_class"] == "terminal"
    assert g["blocked_reason"] == f["blocked_reason"] == "hold: do not ship until the vendor signs"


async def test_the_stand_aside_note_failing_never_turns_into_a_block(tmp_path, monkeypatch):
    r = _Round(tmp_path, monkeypatch)
    await r.start()
    await asyncio.to_thread(r.board.flag_blocked, r.fid, "hold: vendor API access is pending", "terminal")
    real = r.board.comment

    def _comment(fid, text):
        # Every comment the DRIVE writes from here fails — whatever its wording — while the
        # human's own `blocked:` record still lands.
        if not text.startswith("blocked:"):
            raise BoardError("`br comments` failed: database is locked")
        return real(fid, text)

    monkeypatch.setattr(r.board, "comment", _comment)

    f = await r.finish()

    assert f["blocked_reason"] == "hold: vendor API access is pending"


async def test_an_unreadable_card_after_a_refused_hand_off_is_read_from_the_refusal(tmp_path, monkeypatch):
    """The move lands in the last moment before the hand-off, and the board then cannot be
    read. Before the fix an unreadable card counted as still the drive's, so the refusal
    was blocked on: the original incident."""
    r = _Round(tmp_path, monkeypatch)
    await r.start()
    real_get, real_open = r.board.get_feature, r.board.open_review
    refused = {"yes": False, "failed_read": False}

    def _open(fid, **kw):
        r.board.requeue(fid)  # the move, between the drive's last check and its write
        try:
            return real_open(fid, **kw)
        except BoardError:
            refused["yes"] = True
            raise

    def _get(fid):
        if refused["yes"] and not refused["failed_read"]:
            refused["failed_read"] = True
            raise BoardError("`br show` timed out after 30s and was killed")
        return real_get(fid)

    monkeypatch.setattr(r.board, "open_review", _open)
    monkeypatch.setattr(r.board, "get_feature", _get)

    f = await r.finish()

    assert refused == {"yes": True, "failed_read": True}
    assert f["board_state"] == "ready" and not f["blocked"]


async def test_standing_aside_resets_the_drives_fix_budgets(tmp_path, monkeypatch):
    """Whoever moved the card decides the next round, and it starts fresh — not with the
    attempts an interrupted build had spent."""
    r = _Round(tmp_path, monkeypatch)
    r.board.record_budget(r.fid, "gate-fix", 2)
    r.board.record_budget(r.fid, "req-fix", 1)
    await r.start()
    await asyncio.to_thread(r.board.requeue, r.fid)

    f = await r.finish()

    assert "gate-fix" not in f["budgets"] and "req-fix" not in f["budgets"]


async def test_a_refusal_while_the_card_is_still_in_progress_still_blocks(tmp_path, monkeypatch):
    """The carve-out is for a card that MOVED. A hand-off refused while the card is still
    the drive's (in_progress) is a real failure and blocks exactly as before — here the
    real board refuses a coding card entering review with no PR."""
    r = _Round(tmp_path, monkeypatch, with_pr=False)
    r.opened = []

    async def _no_url(wt, branch, *, base, title, body, promote_draft=True):
        return ""

    monkeypatch.setattr(worktree, "open_pr", _no_url)
    await r.start()

    f = await r.finish()

    assert f["blocked"] and f["blocked_class"] == "terminal"
    assert f["blocked_reason"].startswith("open_review requires a pr_url")


# ── the store seam and the ownership read ────────────────────────────────────────────


@pytest.mark.parametrize("state", ["ready", "blocked", "done"])
def test_record_pr_url_records_the_pr_without_moving_the_card(tmp_path, state):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature("A card", spec="s")["id"]
    if state == "ready":
        board._run("update", fid, "--add-label", "ready")
    elif state == "blocked":
        board.flag_blocked(fid, "hold: waiting on legal", "terminal")
    else:
        board._run("close", fid, "-r", "done: shipped by hand")

    f = board.record_pr_url(fid, PR_NEW)

    assert f["pr_url"] == PR_NEW and f["board_state"] == state


def test_moved_under_drive_fails_toward_not_overwriting():
    class _S:
        def __init__(self, state):
            self.state = state

        def get_feature(self, fid):
            if self.state is None:
                raise BoardError("`br show` timed out")
            return {"id": fid, "board_state": self.state}

    moved = BoardLoop._moved_under_drive
    assert moved(_S("in_progress"), "bd-1") == ""  # still the drive's
    assert moved(_S("ready"), "bd-1") == "ready"
    assert moved(_S("blocked"), "bd-1") == "blocked"  # a hold on an in_progress card
    assert moved(_S("cancelled"), "bd-1") == "cancelled"  # returned; the caller routes it to the cancel edge
    # unreadable: the refusal's own words, else `unknown` — never "still ours"
    assert moved(_S(None), "bd-1", "open_review expects in_progress, got 'ready'") == "ready"
    assert moved(_S(None), "bd-1") == "unknown"
    assert moved(object(), "bd-1") == ""  # a store with no read at all (a stub) keeps the old path

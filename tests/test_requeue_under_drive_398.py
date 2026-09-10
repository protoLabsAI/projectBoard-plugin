"""A card moved while its drive runs is not blocked when the drive hands off (#398).

Live (protoEngineer, 2026-09-07): `bd-p8ft` went TERMINAL with an open PR and no new
cause. The loop's own CI-fix round was building it (in_progress, PR attached) when the PM
called `board_requeue_ci_fix` for another round — a call the tool accepts, because a card
mid-round has exactly the in_progress + open-PR shape of a card parked by a CI bounce. The
requeue moved it to `ready`; the round finished, `open_review` refused (`expects
in_progress, got 'ready'`), and the drive's catch-all blocked the card terminally on that
message. The bead comment is the evidence: `blocked: open_review expects in_progress, got
'ready'` at 07:12:30Z, seven minutes after the requeue.

The store is REAL `br` here: the refusal the drive hits is the real board's, raised by
the real state the requeue left behind. The coder, git and GitHub seams are faked — they
are not what moved the card.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

import project_board as pb
import project_board.loop as loop_mod
from project_board import store as store_mod
from project_board import worktree
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)

PR = "https://github.com/protoLabsAI/protoAgent/pull/3364"


class _Round:
    """A drive of one CI-fix round on a real board, whose coder waits to be released —
    so a test can act on the card while the round is still running, as the PM did."""

    def __init__(self, tmp_path, monkeypatch, *, pr_opened=PR):
        self.board = BeadsBoard(repo=str(tmp_path), actor="test")
        (tmp_path / "target.py").write_text("x = 1\n")
        self.fid = self.board.create_feature(
            "fix(a2a): record continuity entries by session", spec="s", files_to_modify=["target.py"]
        )["id"]
        # A card on a fix round: ready, with its open PR attached (setup, not under test).
        self.board._run("update", self.fid, "--add-label", "ready", "--external-ref", PR)
        monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: self.board)
        monkeypatch.setattr(store_mod, "get_store", lambda **_kw: self.board)
        loop_mod._PENDING_FEEDBACK.clear()
        self.started, self.release = asyncio.Event(), asyncio.Event()

        async def _create(repo, base, fid, root, title="", **_kw):
            return (str(tmp_path / "wt"), "feat/" + fid)

        async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
            self.started.set()
            await self.release.wait()  # the coder is still working
            return "## Summary\n\n- propagated the originating session\n"

        async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
            return pr_opened

        async def _remove(repo, wt, branch=""):
            return True

        monkeypatch.setattr(worktree, "create_worktree", _create)
        monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
        monkeypatch.setattr(worktree, "open_pr", _open_pr)
        monkeypatch.setattr(worktree, "remove_worktree", _remove)
        self.loop = BoardLoop({"coder": "proto", "repo": str(tmp_path)})
        monkeypatch.setattr(self.loop, "_resolve_delegate", lambda name, expect: object())

    async def start(self):
        assert await self.loop._spawn_ready()  # the loop claims the card and starts its round
        self.drive = next(iter(self.loop._drives))
        await asyncio.wait_for(self.started.wait(), 10)
        assert self.board.get_feature(self.fid)["board_state"] == "in_progress"

    async def finish(self):
        self.release.set()
        await asyncio.wait_for(self.drive, 10)
        return self.board.get_feature(self.fid)


@requires_br
async def test_a_card_requeued_under_its_live_round_is_not_blocked_when_the_round_ends(tmp_path, monkeypatch):
    r = _Round(tmp_path, monkeypatch)
    await r.start()

    # The PM's call, through the real tool, while the loop's round is still running.
    tool = {t.name: t for t in pb._board_tools({})}["board_requeue_ci_fix"]
    reply = await asyncio.to_thread(
        tool.invoke, {"feature_id": r.fid, "ci_failure": "CodeRabbit: _dispatch_traced() drops session_id"}
    )
    assert json.loads(reply)["state"] == "ready"

    f = await r.finish()

    # The requeue stands. Before the fix: blocked, class terminal, reason
    # "open_review expects in_progress, got 'ready'".
    assert f["board_state"] == "ready" and not f["blocked"]
    assert f["blocked_class"] == "" and f["blocked_reason"] == ""
    assert f["pr_url"] == PR
    assert any("moved to ready" in c and PR in c for c in r.board.feature_comments(r.fid))
    assert r.loop._inflight == {}  # the slot is free
    # …and the round the PM asked for still happens, with the PM's feedback: the next
    # scan claims the card again, and the feedback is still queued for its prompt.
    redriven: list[str] = []

    async def _record(feature):
        redriven.append(feature["id"])

    monkeypatch.setattr(r.loop, "_drive", _record)
    assert await r.loop._spawn_ready()
    await asyncio.gather(*r.loop._drives)
    assert redriven == [r.fid]
    assert "drops session_id" in loop_mod._PENDING_FEEDBACK.get(r.fid, "")


@requires_br
async def test_a_card_blocked_under_its_live_round_keeps_the_block_it_was_given(tmp_path, monkeypatch):
    """The same hand-off, after a human BLOCKED the card mid-round. Before the fix the
    drive's catch-all re-blocked it, and its `blocked:` comment — the latest one, so the
    one the board shows — replaced the human's reason with the hand-off's refusal."""
    r = _Round(tmp_path, monkeypatch)
    await r.start()

    await asyncio.to_thread(r.board.flag_blocked, r.fid, "hold: vendor API access is pending", "terminal")

    f = await r.finish()

    assert f["blocked"] and f["blocked_class"] == "terminal"
    assert f["blocked_reason"] == "hold: vendor API access is pending"
    assert r.loop._inflight == {}


@requires_br
async def test_a_refusal_while_the_card_is_still_in_progress_still_blocks(tmp_path, monkeypatch):
    """The carve-out is for a card that MOVED. A hand-off refused while the card is still
    this drive's (in_progress) is a real failure, and blocks exactly as before — here the
    real board refuses a coding card entering review with no PR."""
    r = _Round(tmp_path, monkeypatch, pr_opened="")
    await r.start()

    f = await r.finish()

    assert f["blocked"] and f["blocked_class"] == "terminal"
    assert f["blocked_reason"].startswith("open_review requires a pr_url")


def test_moved_under_drive_reads_only_a_real_move():
    class _S:
        def __init__(self, state):
            self.state = state

        def get_feature(self, fid):
            if self.state is None:
                raise BoardError("br unavailable")
            return {"id": fid, "board_state": self.state}

    moved = BoardLoop._moved_under_drive
    assert moved(_S("ready"), "bd-1") == "ready"
    assert moved(_S("blocked"), "bd-1") == "blocked"
    assert moved(_S("in_progress"), "bd-1") == ""  # still the drive's
    assert moved(_S("cancelled"), "bd-1") == ""  # the cancel edge owns it (it closes the PR)
    assert moved(_S(None), "bd-1") == ""  # unreadable → the old path, never a guess

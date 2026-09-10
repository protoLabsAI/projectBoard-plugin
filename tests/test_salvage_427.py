"""Publish a stranded card's worktree without a coder (#427).

bd-ezs7's coder finished 170 lines and died before publishing them. Recovering that work
needed a CODER — every publication step was only reachable by dispatching one — and that
day the coder delegate was down for an unrelated reason, so finished work sat behind an
outage that had nothing to do with it. The operator salvage runs the board's own tail
instead: commit, the pre-PR gate, push, PR, in_review.

Real git throughout: a bare origin plus a clone, the tree's commit and the push to that
origin are real. Only `gh` is faked, at the `_gh` seam — opening a real PR needs a
disposable GitHub repo (see test_external_seams.py's EXEMPT seams). The board store is a
small state machine: it is not the seam under test here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.store import BoardError


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Origin:
    base = "main"

    def __init__(self, tmp_path: Path):
        self.origin = str(tmp_path / "origin.git")
        seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, seed)
        _identity(seed)
        Path(seed, "README.md").write_text("base\n")
        _git("-C", seed, "add", "-A")
        _git("-C", seed, "commit", "-m", "base commit")
        _git("-C", seed, "remote", "add", "origin", self.origin)
        _git("-C", seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)

    def pushed(self) -> list[str]:
        """The feature branches that reached the origin."""
        out = _git("-C", self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/feat/")
        return [line for line in out.splitlines() if line]

    def show(self, ref: str, rel: str) -> str:
        return _git("-C", self.origin, "show", f"{ref}:{rel}") + "\n"


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


class _Gh:
    """`gh`, faked: `pr create` succeeds and records its arguments; nothing else is used."""

    url = "https://github.com/o/r/pull/9"

    def __init__(self):
        self.creates: list[tuple] = []

    async def __call__(self, *args, cwd, timeout=60):
        if args[:2] == ("pr", "create"):
            self.creates.append(args)
            return 0, self.url + "\n", ""
        return 1, "", f"unexpected gh call {args}"


class _Board:
    """The card's lifecycle, as the store keeps it: a `blocked` flag over an underlying state."""

    def __init__(self, feature: dict, *, blocked: bool = True, under: str = "in_progress"):
        self.feature, self.blocked, self.under = dict(feature), blocked, under
        self.calls: list[tuple] = []

    def _view(self) -> dict:
        return {**self.feature, "blocked": self.blocked, "board_state": "blocked" if self.blocked else self.under}

    def get_feature(self, fid):
        return self._view()

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))
        self.blocked = False
        return self._view()

    def claim(self, fid, assignee=""):
        self.calls.append(("claim", fid))
        if self.blocked or self.under != "ready":
            return None
        self.under = "in_progress"
        return self._view()

    def open_review(self, fid, *, pr_url=""):
        if self._view()["board_state"] != "in_progress":
            raise BoardError(f"open_review expects in_progress, got {self._view()['board_state']!r}")
        self.calls.append(("open_review", fid, pr_url))
        self.under = "in_review"
        return self._view()

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.calls.append(("review", fid, label))

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


_TITLE = "Make the poll timeout progress-based"


def _card(origin: _Origin) -> dict:
    return {"id": "bd-7", "title": _TITLE, "repo": origin.clone, "base_branch": origin.base, "spec": "s"}


async def _stranded_candidate(origin: _Origin, cid: str = "bd-7.g1", marker: str = "g1") -> str:
    """A candidate whose coder finished and died: edits never committed, never pushed."""
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, cid)
    Path(wt, "README.md").write_text(f"finished work ({marker})\n")
    Path(wt, "adapters.py").write_text(f"POLL = '{marker}'\n")
    return wt


def _loop(monkeypatch, board: _Board, *, gate: str = "true", review_gate: bool = False) -> tuple[BoardLoop, _Gh]:
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    gh = _Gh()
    monkeypatch.setattr(worktree, "_gh", gh)
    return BoardLoop({"coder": "proto", "local_gate_cmd": gate, "review_gate": review_gate}), gh


_CANON = f"feat/bd-7-{worktree.slugify(_TITLE)}"


async def test_salvage_publishes_a_stranded_candidate_without_a_coder(origin, monkeypatch):
    """The bd-ezs7 shape: a finished `.g1`, the card requeued and then blocked."""
    cand = await _stranded_candidate(origin)
    board = _Board(_card(origin), blocked=True, under="ready")
    loop, gh = _loop(monkeypatch, board, review_gate=True)

    rec = await loop.salvage("bd-7")

    assert rec["outcome"] == "published", rec
    assert rec["pr_url"] == _Gh.url and rec["draft"] is False
    # The work reached the origin on the card's own branch — committed, pushed, real.
    assert origin.pushed() == [_CANON]
    assert origin.show(_CANON, "README.md") == "finished work (g1)\n"
    assert origin.show(_CANON, "adapters.py") == "POLL = 'g1'\n"
    assert len(gh.creates) == 1 and "--draft" not in gh.creates[0]
    # …through the candidate's promotion to the canonical name, as a drive's would go.
    assert not os.path.exists(cand) and rec["worktree"].endswith(_CANON.replace("feat/", "feat-"))
    # The card moved to review, gated, with a trail — and let go of its reservation.
    assert board.verbs()[:3] == ["clear_blocked", "claim", "open_review"]
    assert ("review", "bd-7", "review-pending") in board.calls
    assert any(c[0] == "comment" and _Gh.url in c[2] for c in board.calls)
    assert loop._inflight_files == {}


async def test_a_red_gate_publishes_nothing_and_hands_back_its_output(origin, monkeypatch):
    cand = await _stranded_candidate(origin)
    board = _Board(_card(origin))
    loop, gh = _loop(monkeypatch, board, gate="echo 'FAILED tests/test_poll.py::test_it - boom'; exit 1")

    rec = await loop.salvage("bd-7")

    assert rec["outcome"] == "gate-red" and "FAILED tests/test_poll.py::test_it" in rec["gate_output"]
    assert origin.pushed() == [] and gh.creates == [] and board.calls == []
    # Refused where it stood: the tree was not even promoted.
    assert Path(cand, "README.md").read_text() == "finished work (g1)\n"
    assert loop._inflight_files == {}


async def test_force_opens_a_red_gate_as_a_draft_that_carries_the_output(origin, monkeypatch):
    await _stranded_candidate(origin)
    board = _Board(_card(origin))
    loop, gh = _loop(monkeypatch, board, gate="echo 'FAILED tests/test_poll.py::test_it - boom'; exit 1")

    rec = await loop.salvage("bd-7", force=True)

    assert rec["outcome"] == "published" and rec["draft"] is True
    ((*create,),) = gh.creates
    assert "--draft" in create
    body = create[create.index("--body") + 1]
    assert "FAILED tests/test_poll.py::test_it" in body and "no coder was dispatched" in body
    assert origin.pushed() == [_CANON] and "open_review" in board.verbs()


async def test_salvage_refuses_while_a_drive_or_another_salvage_owns_the_card(origin, monkeypatch):
    cand = await _stranded_candidate(origin)
    board = _Board(_card(origin))
    loop, gh = _loop(monkeypatch, board, gate="sleep 1")

    loop._inflight_files["bd-7"] = {("", "README.md")}  # a live drive's claim
    held = await loop.salvage("bd-7")
    assert held["outcome"] == "refused" and "live drive" in held["detail"]
    assert Path(cand, "README.md").read_text() == "finished work (g1)\n" and origin.pushed() == []
    del loop._inflight_files["bd-7"]

    first, second = await asyncio.gather(loop.salvage("bd-7"), loop.salvage("bd-7"))
    assert (first["outcome"], second["outcome"]) == ("published", "refused")
    assert len(gh.creates) == 1 and loop._inflight_files == {}


async def test_salvage_refuses_a_card_with_nothing_to_publish(origin, monkeypatch):
    await worktree.create_worktree(origin.clone, origin.base, "bd-7", title=_TITLE)  # a clean canonical tree
    board = _Board(_card(origin))
    loop, gh = _loop(monkeypatch, board)

    rec = await loop.salvage("bd-7")

    assert rec["outcome"] == "refused" and "has changes vs main" in rec["detail"]
    assert gh.creates == [] and board.calls == []


async def test_salvage_refuses_a_card_that_is_not_stranded(origin, monkeypatch):
    await _stranded_candidate(origin)
    loop, gh = _loop(monkeypatch, _Board(_card(origin), blocked=False, under="ready"))

    rec = await loop.salvage("bd-7")

    assert rec["outcome"] == "refused" and "block it first" in rec["detail"] and gh.creates == []


async def test_several_trees_with_work_need_the_operator_to_pick_one(origin, monkeypatch):
    await _stranded_candidate(origin, "bd-7.g1", "g1")
    await _stranded_candidate(origin, "bd-7.g2", "g2")
    board = _Board(_card(origin))
    loop, gh = _loop(monkeypatch, board)

    ambiguous = await loop.salvage("bd-7")
    assert ambiguous["outcome"] == "refused" and "feat-bd-7.g1" in ambiguous["detail"]
    assert "feat-bd-7.g2" in ambiguous["detail"] and gh.creates == []

    picked = await loop.salvage("bd-7", tree="feat-bd-7.g2")
    assert picked["outcome"] == "published"
    assert origin.show(_CANON, "adapters.py") == "POLL = 'g2'\n"


async def test_commits_ahead_counts_what_a_pr_would_carry(origin):
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    assert await worktree.commits_ahead(wt, origin.base) == 0
    Path(wt, "x.py").write_text("x = 1\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-m", "one")
    assert await worktree.commits_ahead(wt, origin.base) == 1


# ── the surfaces: route and tool, over the running loop ──────────────────────────────


async def test_the_route_and_the_tool_carry_the_loops_record(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board import _board_tools, api

    records = {
        "bd-ok": {"outcome": "published", "pr_url": _Gh.url},
        "bd-red": {"outcome": "gate-red", "gate_output": "FAILED"},
        "bd-none": {"outcome": "not-found"},
    }
    asked: list[tuple] = []

    async def _request(fid, *, force=False, tree=""):
        asked.append((fid, force, tree))
        return {"feature_id": fid, **records[fid]}

    monkeypatch.setattr("project_board.loop.request_salvage", _request)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    client = TestClient(app)
    base = "/api/plugins/project_board/features"
    ok = client.post(f"{base}/bd-ok/salvage", json={"force": True, "tree": "feat-bd-ok.g1"})
    assert ok.status_code == 200 and ok.json()["pr_url"] == _Gh.url
    assert client.post(f"{base}/bd-red/salvage", json={}).status_code == 409
    assert client.post(f"{base}/bd-none/salvage").status_code == 404
    assert asked[0] == ("bd-ok", True, "feat-bd-ok.g1") and asked[1] == ("bd-red", False, "")

    (tool,) = [t for t in _board_tools({}) if t.name == "board_salvage_feature"]
    out = json.loads(await tool.ainvoke({"feature_id": "bd-red", "force": False}))
    assert out["outcome"] == "gate-red"


async def test_with_no_loop_running_there_is_nothing_to_salvage_with(monkeypatch):
    from project_board.loop import request_salvage

    monkeypatch.setattr("project_board.loop.live_loop", lambda: None)
    rec = await request_salvage("bd-7")
    assert rec["outcome"] == "loop-not-running"

"""#538: an OracleTimeout block KEEPS the card's worktree so board_salvage_feature can
publish green work.

#459 blocks a card ``oracle-timeout`` when the acceptance oracle (not the candidates)
runs out its budget — a timeout says nothing about the code, so the build it judged is
often green (bd-b4pr: a dependency bump whose build and tests passed). The drive used to
``_discard_tree`` that worktree, and ``board_salvage_feature`` — which only publishes from
a LIVE worktree — then refused with "no worktree of <id> has changes vs main". Now the tree
is KEPT (the same stance the closed-source-issue guard takes, protoAgent#3832), a comment
names the path and the salvage, and neither the health sweep's reaper nor its blocked sweep
touches the tree while the card stays blocked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_board import coder_seam, worktree
from project_board.failures import ORACLE_TIMEOUT_CLASS
from project_board.loop import BoardLoop
from project_board.loop._common import _SELF_HEALING_BLOCKS
from test_loop import FEATURE, FakeLoopStore, _drive_with

posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises real git worktrees")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> Path:
    """A real git repo with one commit on ``main`` and a pinned repo-local identity."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", str(path))
    for k, v in (("user.email", "board-test@localhost"), ("user.name", "Board Test"), ("commit.gpgsign", "false")):
        _git("-C", str(path), "config", k, v)
    (path / "README.md").write_text("base\n")
    _git("-C", str(path), "add", "-A")
    _git("-C", str(path), "commit", "-m", "base")
    return path


def _feature_worktree(repo: str, fid: str, *, root: str = ".worktrees", content: str = "dep==1.2.4\n") -> str:
    """A real ``feat-<fid>`` worktree the card owns, on ``feat/<fid>``, one commit ahead of
    main — exactly what a green solve candidate the oracle timed out on would leave."""
    os.makedirs(os.path.join(repo, root), exist_ok=True)
    wt = os.path.join(repo, root, f"feat-{fid}")
    _git("-C", repo, "worktree", "add", "-b", f"feat/{fid}", wt, "main")
    Path(wt, "requirements.txt").write_text(content)
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-m", "chore: bump dependency")
    return wt


def _oracle_timeout(**kw) -> None:
    raise coder_seam.OracleTimeout(
        "acceptance oracle cannot finish: it ran out the 300s coder_solve_test_timeout_s on "
        f"`{kw.get('test_cmd', 'gate')}` in 2 separate rounds of candidates — an oracle/spec problem, "
        "not the code.",
        test_cmd=kw.get("test_cmd", "gate"),
        timeout=300,
    )


_SOLVE_CFG = {"coder": "proto", "local_gate_cmd": "python scripts/gate.py"}


# ── r1: the drive keeps the worktree and names the salvage ────────────────────────────


@posix_only
async def test_oracle_timeout_keeps_the_worktree_and_points_at_salvage(tmp_path, monkeypatch):
    """After an OracleTimeout the card is blocked ``oracle-timeout``, its worktree is still
    on disk (never discarded to a ``stranded/…`` branch), and a comment names the path and
    board_salvage_feature."""
    repo = str(_repo(tmp_path / "repo"))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())  # solve is importable

    async def _dispatch(**kw):
        # The solve offload built a (green) candidate, then the oracle timed out on it — and,
        # as the REAL dispatch now does (#538), kept that tree and handed it up on the
        # exception's `.kept`, so the drive names EXACTLY it (never a scan). The real path is
        # exercised end-to-end in tests/test_oracle_timeout_keep_tree.py.
        wt = _feature_worktree(kw["repo"], kw["fid"], root=kw["root"])
        try:
            _oracle_timeout(**kw)
        except coder_seam.OracleTimeout as exc:
            exc.kept = (wt, f"feat/{kw['fid']}")
            raise

    monkeypatch.setattr(coder_seam, "dispatch", _dispatch)

    async def _no_pr(*_a, **_k):
        raise AssertionError("no PR for an unverified build")

    feature = {**FEATURE, "repo": repo, "base_branch": "main", "acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}
    loop, store = await _drive_with(monkeypatch, open_pr=_no_pr, cfg=_SOLVE_CFG, feature=feature)

    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][3] == ORACLE_TIMEOUT_CLASS

    kept = os.path.join(repo, ".worktrees", "feat-bd-1")
    assert os.path.isdir(kept), "the worktree must be kept, not discarded"
    # _discard_tree would have moved the work to a stranded/… branch; it must not run.
    assert "stranded/" not in _git("-C", repo, "branch", "--list", "stranded/*")

    comments = [c[2] for c in store.calls if c[0] == "comment"]
    salvage_notes = [t for t in comments if "board_salvage_feature" in t]
    assert len(salvage_notes) == 1
    note = salvage_notes[0]
    assert os.path.abspath(kept) in note and "salvage" in note.lower()


# ── r2: salvage finds the kept tree instead of refusing ───────────────────────────────


class _SalvageStore(FakeLoopStore):
    """A store whose blocked card salvage can take back to review."""

    def __init__(self, card):
        super().__init__()
        self._card = dict(card)

    def get_feature(self, fid):
        return dict(self._card)

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))
        self._card = {**self._card, "blocked": False, "board_state": "in_progress"}
        return dict(self._card)


@posix_only
async def test_salvage_finds_the_kept_tree(tmp_path, monkeypatch):
    """Given the kept tree, board_salvage_feature publishes it — it does NOT refuse with
    "no worktree of <id> has changes"."""
    repo = str(_repo(tmp_path / "repo"))
    wt = _feature_worktree(repo, "bd-1")

    card = {
        "id": "bd-1",
        "title": "",  # keep the canonical name feat-bd-1 so promote is a no-op
        "repo": repo,
        "base_branch": "main",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": ORACLE_TIMEOUT_CLASS,
    }
    store = _SalvageStore(card)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _open_pr(wt_, branch, *, base, title, body, promote_draft=True, **_k):
        return "https://example/pr/538"

    monkeypatch.setattr(worktree, "open_pr", _open_pr)

    loop = BoardLoop({"coder": "proto"})
    result = await loop.salvage("bd-1")

    assert "no worktree" not in result["detail"], result
    assert result["outcome"] == "published", result
    assert os.path.abspath(wt) == os.path.abspath(result["worktree"])
    assert result["pr_url"] == "https://example/pr/538"
    assert ("open_review", "bd-1", "https://example/pr/538") in store.calls


# ── r3: the reaper and the blocked sweep both leave the tree alone ────────────────────


class _BlockedStore(FakeLoopStore):
    def __init__(self, card):
        super().__init__()
        self._card = dict(card)
        self.features = [dict(card)]

    def get_feature(self, fid):
        return dict(self._card)

    def clear_blocked(self, fid):  # must NOT be called for a non-self-healing block
        self.calls.append(("clear_blocked", fid))
        return dict(self._card)

    def requeue(self, fid):  # must NOT be called either
        self.calls.append(("requeue", fid))
        return dict(self._card)


def _blocked_card(repo: str) -> dict:
    return {
        "id": "bd-1",
        "title": "bump a dep",
        "repo": repo,
        "base_branch": "main",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": ORACLE_TIMEOUT_CLASS,
        "blocked_reason": "acceptance oracle cannot finish: it ran out the 300s budget",
    }


@posix_only
async def test_the_health_sweep_reaper_keeps_a_blocked_cards_tree(tmp_path, monkeypatch):
    repo = str(_repo(tmp_path / "repo"))
    kept = _feature_worktree(repo, "bd-1")

    reaped: list[str] = []

    async def _reap(repo_, root, fid):
        reaped.append(fid)
        return True

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coder": "proto"})
    store = _BlockedStore(_blocked_card(repo))
    await loop._sweep_worktrees(store, repo)

    assert reaped == [], "the reaper reaps only done/cancelled/gone trees, never a blocked one"
    assert os.path.isdir(kept)


@posix_only
async def test_the_blocked_sweep_leaves_the_tree_and_keeps_the_card_blocked(tmp_path, monkeypatch):
    repo = str(_repo(tmp_path / "repo"))
    kept = _feature_worktree(repo, "bd-1")

    assert ORACLE_TIMEOUT_CLASS not in _SELF_HEALING_BLOCKS  # the sweep never auto-clears it

    notified: list[str] = []
    monkeypatch.setattr(BoardLoop, "_notify_operator", lambda self, fid, text, **_k: notified.append(fid))

    loop = BoardLoop({"coder": "proto"})
    store = _BlockedStore(_blocked_card(repo))
    await loop._recover_blocked(store)

    assert notified == ["bd-1"]  # escalated to a human, once
    assert not any(c[0] in ("clear_blocked", "requeue") for c in store.calls)  # stays blocked
    assert os.path.isdir(kept)


# ── r4: every OTHER block class still discards the tree ───────────────────────────────


async def test_a_non_oracle_solve_failure_still_discards_the_tree(monkeypatch):
    """A plain SolveExhausted (not OracleTimeout) is a capability failure and keeps its
    current behaviour: it blocks and ``_discard_tree``s the worktree."""
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _exhausted(**kw):
        raise coder_seam.SolveExhausted(
            "coder.solve exhausted after 3 generation(s) (rung=greedy): no candidate passed"
        )

    monkeypatch.setattr(coder_seam, "dispatch", _exhausted)

    async def _no_pr(*_a, **_k):
        raise AssertionError("no PR for an unverified build")

    discarded: list[str] = []

    async def _discard(store, fid, repo, wt, branch, *, base="", comment=True):
        discarded.append(fid)
        return ""

    _, store = await _drive_with(
        monkeypatch,
        open_pr=_no_pr,
        cfg=_SOLVE_CFG,
        seed=lambda lp: setattr(lp, "_discard_tree", _discard),
    )

    assert discarded == ["bd-1"]  # other classes keep discarding
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][3] != ORACLE_TIMEOUT_CLASS

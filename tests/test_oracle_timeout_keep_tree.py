"""#538 (follow-up to #545): an OracleTimeout really KEEPS the card's worktree.

PR #545 tried to keep the oracle-timed-out build's worktree for ``board_salvage_feature``,
but its tests mocked ``coder_seam.dispatch`` wholesale, so they never ran its ``except``
block. In production that block reaps EVERY candidate worktree the moment ``solve()``
raises — and ``OracleTimeout`` (a ``SolveExhausted``) is raised from inside ``solve()`` —
so by the time the drive's oracle-timeout handler ran, no tree was left to keep. Salvage
then still refused with "no worktree of <id> has changes vs main" (the bd-b4pr failure).

These tests drive the REAL ``coder_seam.dispatch`` (with ``solve`` injected only as the
sanctioned test seam — the ``except`` block itself runs for real) over REAL git worktrees,
so they fail if that reaping regresses. They assert that an ``OracleTimeout`` keeps exactly
one candidate (and its branch) on disk, that ``feature_worktrees`` finds it, that salvage
publishes it, that a NON-timeout failure still reaps everything, that the comment names
only the kept tree (never a stale ``feat-<id>`` an earlier run left), and that the health
sweep leaves a blocked card's candidate alone while cancel/salvage still clean it up.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from project_board import coder_seam, worktree
from project_board.failures import ORACLE_TIMEOUT_CLASS
from project_board.loop import BoardLoop
from test_loop import FakeLoopStore

posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises real git worktrees")


# ── real git repo + a coder that leaves a real diff ───────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> str:
    """A real git repo with one commit on ``main`` and a pinned repo-local identity."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", str(path))
    for k, v in (("user.email", "board-test@localhost"), ("user.name", "Board Test"), ("commit.gpgsign", "false")):
        _git("-C", str(path), "config", k, v)
    (path / "README.md").write_text("base\n")
    _git("-C", str(path), "add", "-A")
    _git("-C", str(path), "commit", "-m", "base")
    os.makedirs(os.path.join(str(path), ".worktrees"), exist_ok=True)
    return str(path)


def _branch_exists(repo: str, branch: str) -> bool:
    return subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch]).returncode == 0


# Fakes standing in for plugins.coder.solve (a separate plugin, absent here) — the SAME
# shapes test_coder_seam.py injects, so dispatch() runs its real orchestration against them.


@dataclass
class _FakeVerdict:
    passed: bool
    total: int = 0
    failed: int = 0
    failing: list = field(default_factory=list)
    output: str = ""

    def feedback(self) -> str:
        return "" if self.passed else f"{self.failed}/{self.total} failing: {self.output}"


@dataclass
class _FakeResult:
    solution: str | None
    passed: bool | None
    rung: str
    gens_spent: int
    candidates_tried: int
    verdict: _FakeVerdict | None = None
    note: str = ""


class _FakeBudget:
    def __init__(self, total):
        self.total = total


def _fake_coder(monkeypatch) -> list[str]:
    """Stub the (external) coder so each ``generate`` leaves a real, salvageable diff in its
    candidate worktree. Returns the list of candidate worktree paths it was dispatched into."""
    seen: list[str] = []

    async def _tapped(coder, wt, prompt, *, fid=None, gen=0, tier="", timeout=None, env_passthrough=(), **_kw):
        seen.append(wt)
        Path(wt, "impl.py").write_text(f"# candidate {gen}: oracle-timed-out but green\nVALUE = {gen}\n")
        return f"## Summary\n\n- implemented the thing in candidate {gen}"

    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", _tapped)
    return seen


def _oracle_timeout() -> coder_seam.OracleTimeout:
    return coder_seam.OracleTimeout(
        "acceptance oracle cannot finish: it ran out the 300s coder_solve_test_timeout_s on `gate` in 2 "
        "separate rounds of candidates — an oracle/spec problem, not the code.",
        test_cmd="gate",
        timeout=300,
    )


async def _dispatch_timing_out(repo: str, fid: str, *, root: str = ".worktrees"):
    """Run the REAL ``coder_seam.dispatch`` whose ``solve`` generates two candidates and
    then raises ``OracleTimeout`` — exactly what the real ladder surfaces when ``verify``
    trips its timeout breaker (``solve()`` has no try/except around verify, so it propagates
    straight into dispatch's own except block)."""

    async def _solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)  # candidate g1: a real worktree with a real diff
        await generate(task, feedback="sibling timed out")  # candidate g2
        raise _oracle_timeout()  # verify's OracleTimeout, propagated by solve()

    return await coder_seam.dispatch(
        task="do the thing",
        coder=object(),
        repo=repo,
        base="main",
        root=root,
        fid=fid,
        dispatch_timeout=None,
        test_cmd="gate",
        test_timeout=300,
        budget=6,
        k=2,
        tree_depth=2,
        _solve=_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )


# ── r1 / r6: the real dispatch keeps ONE candidate (+ its branch); feature_worktrees sees it ──


@posix_only
async def test_real_dispatch_keeps_one_candidate_on_oracle_timeout(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = _fake_coder(monkeypatch)

    with pytest.raises(coder_seam.OracleTimeout) as ei:
        await _dispatch_timing_out(repo, "bd-1")
    exc = ei.value

    # dispatch() handed the kept tree up on the exception — the drive reads THIS, not a scan.
    assert exc.kept is not None, "OracleTimeout must carry the kept (path, branch)"
    kept_path, kept_branch = exc.kept

    survivors = [p for p in created if os.path.isdir(p)]
    assert len(survivors) == 1, "exactly one candidate worktree survives an OracleTimeout"
    assert os.path.abspath(kept_path) == os.path.abspath(survivors[0])
    reaped = [p for p in created if p not in survivors]
    assert reaped and all(not os.path.isdir(p) for p in reaped), "every OTHER candidate is reaped"

    # The kept branch lives (the reaper/branch-sweep skip a branch a live worktree holds)…
    assert _branch_exists(repo, kept_branch), "the kept candidate's branch must survive too"
    # …and feature_worktrees — the trees a salvage publishes from — returns it.
    owned = worktree.feature_worktrees(repo, ".worktrees", "bd-1")
    assert any(os.path.abspath(p) == os.path.abspath(kept_path) for p, _b in owned)


# ── r2 / r6: board_salvage_feature publishes the kept tree instead of refusing ────────


class _SalvageStore(FakeLoopStore):
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
async def test_salvage_publishes_the_kept_candidate(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _fake_coder(monkeypatch)

    with pytest.raises(coder_seam.OracleTimeout):
        await _dispatch_timing_out(repo, "bd-1")

    card = {
        "id": "bd-1",
        "title": "",  # keep the canonical name feat-bd-1 so promote is a plain rename
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

    loop = BoardLoop({"coder": "proto"})  # no gate configured → the salvage gate is a no-op
    result = await loop.salvage("bd-1")

    assert "no worktree" not in result["detail"], result
    assert result["outcome"] == "published", result
    assert result["pr_url"] == "https://example/pr/538"
    assert ("open_review", "bd-1", "https://example/pr/538") in store.calls


# ── r4 / r6: a NON-oracle failure through the real dispatch still reaps every candidate ──


@posix_only
async def test_real_dispatch_reaps_every_candidate_on_a_non_oracle_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = _fake_coder(monkeypatch)

    async def _solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)
        await generate(task, feedback="prior failure")
        # A plain capability failure (NOT OracleTimeout): the ladder exhausted its budget.
        raise coder_seam.SolveExhausted("coder.solve exhausted after 2 generation(s) (rung=best-partial)")

    with pytest.raises(coder_seam.SolveExhausted) as ei:
        await coder_seam.dispatch(
            task="t",
            coder=object(),
            repo=repo,
            base="main",
            root=".worktrees",
            fid="bd-2",
            dispatch_timeout=None,
            test_cmd="gate",
            test_timeout=300,
            budget=6,
            k=2,
            tree_depth=2,
            _solve=_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
    # A non-OracleTimeout SolveExhausted carries no kept tree, and nothing survives on disk.
    assert getattr(ei.value, "kept", None) is None
    assert created and all(not os.path.isdir(p) for p in created), "every candidate reaped, as before"
    assert not worktree.feature_worktrees(repo, ".worktrees", "bd-2")


# ── r3: the comment names ONLY the kept tree, never a stale feat-<id> from an earlier run ──


def _stale_canonical(repo: str, fid: str) -> str:
    """A bare ``feat-<fid>`` worktree an EARLIER run left on disk — the stale tree #538 warns
    about: ``feature_worktrees`` would scan it and wrongly report it as this run's green build."""
    wt = os.path.join(repo, ".worktrees", f"feat-{fid}")
    _git("-C", repo, "worktree", "add", "-b", f"feat/{fid}", wt, "main")
    return os.path.abspath(wt)


def _kept_candidate(repo: str, fid: str) -> tuple[str, str]:
    wt = os.path.join(repo, ".worktrees", f"feat-{fid}.g1")
    branch = f"feat/{fid}.g1"
    _git("-C", repo, "worktree", "add", "-b", branch, wt, "main")
    Path(wt, "impl.py").write_text("VALUE = 1\n")
    return os.path.abspath(wt), branch


@posix_only
async def test_keep_tree_comment_names_only_the_kept_tree_not_a_stale_one(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    stale = _stale_canonical(repo, "bd-1")  # present on disk, from an earlier run
    kept_path, kept_branch = _kept_candidate(repo, "bd-1")

    store = FakeLoopStore()
    loop = BoardLoop({"coder": "proto"})
    # The drive passes exactly what dispatch() kept (exc.kept) as (wt, branch).
    await loop._keep_tree_for_salvage(store, "bd-1", repo, kept_path, kept_branch, base="main")

    notes = [c[2] for c in store.calls if c[0] == "comment"]
    assert len(notes) == 1
    note = notes[0]
    assert kept_path in note and "(branch feat/bd-1.g1)" in note
    # The stale canonical must NOT be named — not as a path listing, not by its branch.
    assert "(branch feat/bd-1)" not in note, f"stale {stale} named as the green build: {note}"


@posix_only
async def test_keep_tree_comment_never_scans_for_a_stale_tree_when_nothing_was_kept(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    stale = _stale_canonical(repo, "bd-1")  # on disk, but NOT this run's work

    store = FakeLoopStore()
    loop = BoardLoop({"coder": "proto"})
    # dispatch() kept nothing (exc.kept is None → the drive passes None): the comment must say
    # so, and must NOT fall back to scanning feature_worktrees (which would surface the stale).
    await loop._keep_tree_for_salvage(store, "bd-1", repo, None, None, base="main")

    notes = [c[2] for c in store.calls if c[0] == "comment"]
    assert len(notes) == 1
    note = notes[0]
    assert "survived to keep" in note
    assert stale not in note, f"a stale tree was scanned and named: {note}"


# ── r5: the sweep leaves a blocked card's kept candidate; cancel/salvage still clean it ──


class _BlockedStore(FakeLoopStore):
    def __init__(self, card):
        super().__init__()
        self._card = dict(card)
        self.features = [dict(card)]

    def get_feature(self, fid):
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
        "blocked_reason": "acceptance oracle cannot finish",
    }


@posix_only
async def test_health_sweep_leaves_a_blocked_cards_kept_candidate(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    kept_path, _b = _kept_candidate(repo, "bd-1")  # the kept tree is a CANDIDATE (feat-bd-1.g1)

    reaped: list[str] = []

    async def _reap(repo_, root, fid):
        reaped.append(fid)
        return True

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coder": "proto"})
    await loop._sweep_worktrees(_BlockedStore(_blocked_card(repo)), repo)

    assert reaped == [], "a blocked card's candidate tree is never reaped by the health sweep"
    assert os.path.isdir(kept_path)


@posix_only
async def test_cancel_or_salvage_cleanup_reaps_the_kept_candidate(tmp_path, monkeypatch):
    """Cancel (and a salvage's promote) go through ``reap_feature_worktree`` — which must take
    the kept CANDIDATE tree, not just a canonical one (#175), so the keep is not a leak."""
    repo = _repo(tmp_path / "repo")
    kept_path, kept_branch = _kept_candidate(repo, "bd-1")

    await worktree.reap_feature_worktree(repo, ".worktrees", "bd-1")

    assert not os.path.isdir(kept_path), "cancel's reap must remove the kept candidate tree"

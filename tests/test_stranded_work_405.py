"""A worktree holding work that exists nowhere else is never destroyed automatically (#405, #400).

The incident (bd-ezs7, 2026-09-06): a `coder.solve` candidate finished — 170 lines across
three files, uncommitted in `feat-bd-ezs7.g1` — and then its drive went silent (#423's
unbounded `proc.wait()` in the acceptance tests). The operator requeued the card. The only
reason the implementation survived is that the silent drive still held the card's file
claim, so nothing re-dispatched it: a re-dispatch runs `create_worktree`, whose "clean a
prior run's leftovers" step is `git worktree remove --force` plus `git branch -D` — on
exactly the directory holding the only copy of that work.

Every test here runs REAL git against a bare origin plus a clone. The defect is what git
does to a dirty tree under `--force`, which a faked `_git` cannot show — and this repo has
shipped mock-validated fixes that did nothing before (see test_external_seams.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import pytest

from project_board import worktree
from project_board.loop import BoardLoop


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _git_rc(*args: str) -> int:
    return subprocess.run(["git", *args], capture_output=True, text=True).returncode


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Origin:
    """A bare origin and a clone of it, so `origin/<base>` resolves exactly as in production.
    Identities are repo-local; nothing escapes ``tmp_path``."""

    base = "main"

    def __init__(self, tmp_path: Path):
        self.origin = str(tmp_path / "origin.git")
        seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, seed)
        _identity(seed)
        # `node_modules/` (trailing slash) is the common spelling — and it does NOT match
        # the node_modules SYMLINK create_worktree links into every tree, which git then
        # reports as untracked. The droppings test depends on that trap being present.
        Path(seed, ".gitignore").write_text("node_modules/\n")
        Path(seed, "README.md").write_text("base\n")
        _git("-C", seed, "add", "-A")
        _git("-C", seed, "commit", "-m", "base commit")
        _git("-C", seed, "remote", "add", "origin", self.origin)
        _git("-C", seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)
        self.base_sha = _git("-C", self.clone, "rev-parse", f"origin/{self.base}")


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


def _strand(wt: str, kind: str) -> tuple[str, str]:
    """Leave behind the kind of work a coder that died before promotion leaves: returns
    (path relative to the tree, its content)."""
    if kind == "tracked":  # an edit to a file the base already has
        Path(wt, "README.md").write_text("the coder's edit\n")
        return "README.md", "the coder's edit\n"
    if kind == "staged":  # a new file, added to the index
        Path(wt, "adapters.py").write_text("POLL = 'progress-based'\n")
        _git("-C", wt, "add", "adapters.py")
        return "adapters.py", "POLL = 'progress-based'\n"
    Path(wt, "changelog.d").mkdir()  # untracked: never added at all
    Path(wt, "changelog.d", "3360.fixed.md").write_text("- **Fixed (#3360).** poll timeout\n")
    return "changelog.d/3360.fixed.md", "- **Fixed (#3360).** poll timeout\n"


async def _attempt(coro):
    """Run a worktree call that SHOULD refuse, returning the exception (or None) — so the
    test can check what survived on disk before it looks at how the call ended."""
    try:
        await coro
    except worktree.WorktreeError as exc:
        return exc
    return None


# ── re-dispatch: create_worktree over a stranded tree ────────────────────────────────


@pytest.mark.parametrize("kind", ["tracked", "staged", "untracked"])
async def test_a_redispatch_will_not_rebuild_over_uncommitted_work(origin, kind):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(wt, kind)

    refused = await _attempt(worktree.create_worktree(origin.clone, origin.base, "bd-7.g1"))

    assert Path(wt, rel).is_file() and Path(wt, rel).read_text() == text, (
        f"the re-dispatch destroyed the stranded {kind} change"
    )
    if kind == "staged":
        assert _git("-C", wt, "diff", "--cached", "--name-only") == rel, "the index was reset"
    assert isinstance(refused, worktree.StrandedWorkError)
    assert [t.path for t in refused.trees] == [wt]
    # The refusal names what it protected, so the block it becomes is actionable.
    assert wt in str(refused) and rel in str(refused) and branch in str(refused)


async def test_a_candidate_whose_coder_committed_is_not_rebuilt_over(origin):
    """The brief says edit-only, but a coder with a shell can commit — and `branch -D`
    loses a commit exactly as surely as `worktree remove --force` loses a file."""
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    Path(wt, "adapters.py").write_text("POLL = 1\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-m", "coder's own commit")
    sha = _git("-C", wt, "rev-parse", "HEAD")

    refused = await _attempt(worktree.create_worktree(origin.clone, origin.base, "bd-7.g1"))

    assert _git("-C", origin.clone, "rev-parse", f"refs/heads/{branch}") == sha, "the coder's commit was dropped"
    assert isinstance(refused, worktree.StrandedWorkError)
    assert "1 commit" in str(refused)


async def test_the_boards_own_droppings_are_not_work(origin):
    """The coder's session scratch and the node_modules symlink the board links in are
    the board's own — counting them would strand EVERY tree the board ever built."""
    os.makedirs(os.path.join(origin.clone, "node_modules", "left-pad"))  # linked into each tree
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert os.path.islink(os.path.join(wt, "node_modules"))
    assert _git("-C", wt, "status", "--porcelain") == "?? node_modules", "precondition: git sees the link"
    for scratch in (".proto", ".cursor"):
        Path(wt, scratch).mkdir()
        Path(wt, scratch, "session-notes.md").write_text("the coder's private notes\n")

    assert await worktree.unpublished_work(wt, branch=branch) == ""
    rebuilt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert rebuilt == wt and not Path(wt, ".proto").exists()  # reaped and rebuilt, as before


# ── the other two destructive edges: promotion and the by-id reap ────────────────────


async def test_promotion_will_not_overwrite_a_stranded_canonical_tree(origin):
    canon, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    Path(canon, "README.md").write_text("an earlier drive's finished work\n")
    cand, cand_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g3")
    Path(cand, "winner.py").write_text("x = 1\n")

    refused = await _attempt(worktree.promote_worktree(origin.clone, cand, cand_branch, "bd-7"))

    assert Path(canon, "README.md").read_text() == "an earlier drive's finished work\n"
    assert Path(cand, "winner.py").is_file(), "the winner must not be half-moved either"
    assert isinstance(refused, worktree.StrandedWorkError)
    assert [t.path for t in refused.trees] == [canon]


async def test_the_reap_keeps_a_tree_holding_work_and_still_reaps_the_clean_ones(origin, caplog):
    """The terminal edges and the health sweep reap by feature id. A clean stale
    candidate still goes; one holding work stays, and the log says where it is."""
    canon, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    clean, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g2")
    dirty, dirty_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(dirty, "untracked")

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await worktree.reap_feature_worktree(origin.clone, ".worktrees", "bd-7")

    assert not os.path.exists(canon) and not os.path.exists(clean)
    assert Path(dirty, rel).read_text() == text, "the reap destroyed the stranded candidate"
    assert _git_rc("-C", origin.clone, "rev-parse", "--verify", f"refs/heads/{dirty_branch}") == 0
    assert dirty in caplog.text and rel in caplog.text


# ── the explicit override: once the operator has dealt with the work, it proceeds ────


@pytest.mark.parametrize("how", ["rescue", "discard"])
async def test_once_the_operator_has_dealt_with_the_work_the_rebuild_proceeds(origin, how):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(wt, "untracked")
    refused = await _attempt(worktree.create_worktree(origin.clone, origin.base, "bd-7.g1"))
    assert Path(wt, rel).is_file(), "the work was destroyed before anyone could look at it"
    assert isinstance(refused, worktree.StrandedWorkError)

    if how == "rescue":  # the recovery the block names: keep it on a branch of your own
        _git("-C", wt, "switch", "-c", "rescue/bd-7")
        _git("-C", wt, "add", "-A")
        _git("-C", wt, "commit", "-m", "rescued")
    else:  # the discard it names, for work the operator has looked at and does not want
        _git("-C", origin.clone, "worktree", "remove", "--force", wt)
        _git("-C", origin.clone, "branch", "-D", branch)

    rebuilt, rebuilt_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert (rebuilt, rebuilt_branch) == (wt, branch)
    assert _git("-C", rebuilt, "rev-parse", "HEAD") == origin.base_sha  # a fresh tree off base
    if how == "rescue":
        assert _git("-C", origin.clone, "show", f"rescue/bd-7:{rel}") + "\n" == text


# ── the loop: a stranded card blocks and says where the work is ──────────────────────


class _Store:
    """The few store verbs a drive reaches before (and, on the old code, after) the
    stranded check. Records the transitions the test asserts on."""

    def __init__(self, feature: dict):
        self.feature = feature
        self.calls: list[tuple] = []

    def current_tier(self, fid):
        return ""

    def get_feature(self, fid):
        return dict(self.feature, board_state="in_progress")

    def list_features(self, state=None, include_archived=False):
        return []

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", fid, reason, category))
        return {"id": fid}

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        return {"id": fid}

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))

    def record_budget(self, fid, kind, n):
        pass

    def clear_budgets(self, fid, kinds=None):
        pass


def _card(origin: _Origin) -> dict:
    return {
        "id": "bd-7",
        "title": "Make the poll timeout progress-based",
        "repo": origin.clone,
        "base_branch": origin.base,
        "spec": "do it",
        "acceptance_criteria": "",  # no oracle → the single-dispatch path, not coder.solve
        "files_to_modify": ["README.md"],
    }


def _loop_for(monkeypatch, store: _Store, *, dispatch, open_pr) -> BoardLoop:
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)  # the untapped fallback a host-free run reaches
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": ""})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    return loop


async def test_a_card_with_stranded_work_blocks_instead_of_rebuilding(origin, monkeypatch):
    card = _card(origin)
    # The dead drive's tree, exactly where this card's next build would go.
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7", title=card["title"])
    Path(wt, "README.md").write_text("finished, never published\n")
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        return "reply"

    async def _open_pr(tree, branch, **_kw):
        return "https://github.com/o/r/pull/1"

    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    assert Path(wt, "README.md").read_text() == "finished, never published\n", "the drive built over the work"
    assert dispatched == [], "a coder was dispatched to rebuild a card whose work is stranded"
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1, store.calls
    _verb, fid, reason, category = blocks[0]
    assert fid == "bd-7" and category == "stranded-work"
    # Actionable where the operator reads it: the path, what is in it, and what to do.
    assert wt in reason and "README.md" in reason and "unblock" in reason


async def test_a_drive_still_discards_its_own_failed_attempt(origin, monkeypatch):
    """REGRESSION GUARD, not a red test: the drive's own earlier attempt is not someone
    else's stranded work. A transient push failure after the coder wrote its files used to
    be retried on a fresh tree — `create_worktree` wiped the old one implicitly. It still
    must be, or every retry would block the card as stranded on its own leftovers."""
    card = _card(origin)
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        Path(tree, "README.md").write_text(f"attempt {len(dispatched)}\n")
        return "reply"

    pushes: list[str] = []

    async def _open_pr(tree, branch, **_kw):
        pushes.append(tree)
        if len(pushes) == 1:  # before anything was committed — the tree is still dirty
            raise worktree.WorktreeError("git push failed: Connection reset by peer")
        return "https://github.com/o/r/pull/1"

    real_sleep = asyncio.sleep

    async def _no_backoff(_delay, *args, **kwargs):
        await real_sleep(0)  # skip the transient backoff, still yield like a sleep

    monkeypatch.setattr(asyncio, "sleep", _no_backoff)
    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    assert len(dispatched) == 2 and ("open_review", "bd-7", "https://github.com/o/r/pull/1") in store.calls
    assert not [c for c in store.calls if c[0] == "flag_blocked"]
    assert Path(dispatched[-1], "README.md").read_text() == "attempt 2\n"


async def test_shutdown_leaves_an_interrupted_tree_that_holds_work(origin):
    """A restart mid-drive used to reap the in-flight tree — a finished implementation
    waiting on its gate went with it. It stays now; the card's next dispatch blocks on
    it, naming the path. A clean in-flight tree is still reaped."""
    busy, busy_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7", title="x")
    rel, text = _strand(busy, "tracked")
    idle, idle_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-8", title="y")
    loop = BoardLoop({"coder": "proto"})
    loop._inflight = {"bd-7": (origin.clone, busy, busy_branch), "bd-8": (origin.clone, idle, idle_branch)}

    await loop.stop()

    assert Path(busy, rel).read_text() == text, "shutdown reaped an in-flight tree holding work"
    assert not os.path.exists(idle)

"""A worktree holding work that exists nowhere else is never destroyed without saving it (#405, #400).

The incident (bd-ezs7, 2026-09-06): a `coder.solve` candidate finished — 170 lines across
three files, uncommitted in `feat-bd-ezs7.g1` — and then its drive went silent (#423's
unbounded `proc.wait()` in the acceptance tests). The operator requeued the card. The only
reason the implementation survived is that the silent drive still held the card's file
claim, so nothing re-dispatched it: a re-dispatch runs `create_worktree`, whose "clean a
prior run's leftovers" step is `git worktree remove --force` plus `git branch -D` — on
exactly the directory holding the only copy of that work.

Now every edge that would remove such a tree first saves its work onto a NEW branch,
`stranded/<tree id>/<UTC stamp>`, proves the branch holds it, and only then lets the tree
go. It stops the card only when that save fails.

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
        # reports as untracked. The droppings tests depend on that trap being present.
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

    def stranded(self, tree_id: str = "") -> list[str]:
        """The preservation branches that exist — for one tree id, or all of them."""
        pattern = f"refs/heads/stranded/{tree_id}/*" if tree_id else "refs/heads/stranded/"
        out = _git("-C", self.clone, "for-each-ref", "--format=%(refname:short)", pattern)
        return [line for line in out.splitlines() if line]


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


def _show(origin: _Origin, ref: str, rel: str) -> str:
    return _git("-C", origin.clone, "show", f"{ref}:{rel}") + "\n"


async def _attempt(coro):
    """Run a worktree call that may refuse, returning the exception (or None) — so the test
    can check what survived on disk before it looks at how the call ended."""
    try:
        await coro
    except worktree.WorktreeError as exc:
        return exc
    return None


# ── re-dispatch: create_worktree over a stranded tree ────────────────────────────────


@pytest.mark.parametrize("kind", ["tracked", "staged", "untracked"])
async def test_a_redispatch_saves_uncommitted_work_before_it_rebuilds(origin, kind):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(wt, kind)

    rebuilt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")

    saved = origin.stranded("bd-7.g1")
    assert len(saved) == 1, f"the {kind} change was destroyed, not saved: no stranded/ branch"
    assert _show(origin, saved[0], rel) == text
    # Saved as one commit on top of the tree's own HEAD, so a cherry-pick replays exactly it.
    assert _git("-C", origin.clone, "rev-parse", f"{saved[0]}~1") == origin.base_sha
    # …and only then was the tree rebuilt, fresh off base, as a re-dispatch always has.
    assert rebuilt == wt and _git("-C", wt, "rev-parse", "HEAD") == origin.base_sha
    assert _git("-C", wt, "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude)node_modules") == ""


async def test_a_candidate_whose_coder_committed_keeps_its_commit(origin):
    """The brief says edit-only, but a coder with a shell can commit — and `branch -D`
    loses a commit exactly as surely as `worktree remove --force` loses a file."""
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    Path(wt, "adapters.py").write_text("POLL = 1\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-m", "coder's own commit")
    sha = _git("-C", wt, "rev-parse", "HEAD")

    await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")

    saved = origin.stranded("bd-7.g1")
    assert len(saved) == 1, "the coder's commit was dropped with its branch"
    assert _git("-C", origin.clone, "rev-parse", saved[0]) == sha  # the branch IS its commit — nothing new


async def test_the_boards_own_droppings_are_neither_work_nor_saved(origin):
    """The coder's session scratch and the node_modules symlink the board links in are
    the board's own. Counting them would strand EVERY tree; saving them would carry them."""
    os.makedirs(os.path.join(origin.clone, "node_modules", "left-pad"))  # linked into each tree
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert os.path.islink(os.path.join(wt, "node_modules"))
    assert _git("-C", wt, "status", "--porcelain") == "?? node_modules", "precondition: git sees the link"
    for scratch in (".proto", ".cursor"):
        Path(wt, scratch).mkdir()
        Path(wt, scratch, "session-notes.md").write_text("the coder's private notes\n")

    assert await worktree.unpublished_work(wt, branch=branch) == ""
    await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert origin.stranded() == [], "a tree holding only the board's droppings was 'saved'"

    # Beside real work, the droppings still stay out of what is saved.
    for scratch in (".proto", ".cursor"):
        Path(wt, scratch).mkdir()
        Path(wt, scratch, "session-notes.md").write_text("notes\n")
    rel, _text = _strand(wt, "untracked")
    await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    (saved,) = origin.stranded("bd-7.g1")
    carried = _git("-C", origin.clone, "ls-tree", "-r", "--name-only", saved).splitlines()
    assert rel in carried
    assert not [p for p in carried if p.startswith((".proto", ".cursor")) or p.endswith("node_modules")]


async def test_an_existing_branch_is_never_overwritten_and_unsaved_work_is_kept(origin, monkeypatch):
    """Saving goes to a NEW branch or not at all. When it cannot (the name is taken), the
    tree is kept exactly as it is and the call refuses — work that could not be saved is
    never destroyed. Once the way is clear, the same call saves it and proceeds."""
    monkeypatch.setattr(worktree, "_stamp", lambda: "20260906T232550Z", raising=False)  # red-checkable
    taken = "stranded/bd-7.g1/20260906T232550Z"
    _git("-C", origin.clone, "branch", taken, origin.base_sha)
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(wt, "untracked")

    refused = await _attempt(worktree.create_worktree(origin.clone, origin.base, "bd-7.g1"))

    assert Path(wt, rel).is_file() and Path(wt, rel).read_text() == text, "unsaved work was destroyed"
    assert _git("-C", origin.clone, "rev-parse", taken) == origin.base_sha, "an existing branch was overwritten"
    assert isinstance(refused, worktree.StrandedWorkError)
    assert wt in str(refused) and "saving it to a branch failed" in str(refused)

    _git("-C", origin.clone, "branch", "-D", taken)  # the operator clears the way
    await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    assert _show(origin, taken, rel) == text and not Path(wt, rel).exists()


# ── the other edges: promotion and the by-id reap ────────────────────────────────────


async def test_promotion_saves_a_stranded_canonical_tree_then_promotes(origin):
    canon, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    Path(canon, "README.md").write_text("an earlier drive's finished work\n")
    cand, cand_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g3")
    Path(cand, "winner.py").write_text("x = 1\n")

    promoted, _ = await worktree.promote_worktree(origin.clone, cand, cand_branch, "bd-7")

    (saved,) = origin.stranded("bd-7") or [None]
    assert saved, "promotion destroyed the stranded canonical tree"
    assert _show(origin, saved, "README.md") == "an earlier drive's finished work\n"
    assert promoted == canon and Path(canon, "winner.py").is_file()


async def test_the_reap_saves_a_tree_holding_work_and_still_reaps_everything(origin, caplog):
    """The terminal edges and the health sweep reap by feature id. A clean stale tree
    goes as before; one holding work goes too — once its work is on a branch the log names."""
    canon, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    clean, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g2")
    dirty, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7.g1")
    rel, text = _strand(dirty, "untracked")

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await worktree.reap_feature_worktree(origin.clone, ".worktrees", "bd-7")

    assert not os.path.exists(canon) and not os.path.exists(clean) and not os.path.exists(dirty)
    saved = origin.stranded("bd-7.g1")
    assert len(saved) == 1, "the reap destroyed the stranded candidate"
    assert _show(origin, saved[0], rel) == text
    assert saved[0] in caplog.text and origin.stranded("bd-7") == [] and origin.stranded("bd-7.g2") == []


# ── #405's own find: the board's node_modules link must never ride into a PR ─────────


async def test_the_commit_leaves_the_boards_node_modules_link_out(origin):
    """`add -A` committed the board's node_modules SYMLINK into the PR in any repo whose
    ignore file spells it `node_modules/` — that pattern matches only a real directory."""
    os.makedirs(os.path.join(origin.clone, "node_modules", "left-pad"))
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-8")
    Path(wt, "feature.py").write_text("x = 1\n")

    await worktree.commit_worktree(wt, "feat: the change")

    committed = _git("-C", wt, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["feature.py"], f"the commit carried the board's link: {committed}"


# ── the loop: a stranded card saves its work, says where, and builds on ──────────────


class _Store:
    """The few store verbs a drive reaches. Records the transitions the tests assert on."""

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

    def verbs(self, verb: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == verb]


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


async def _open_pr(tree, branch, **_kw):
    return "https://github.com/o/r/pull/1"


async def test_a_card_with_stranded_work_saves_it_says_where_and_builds_on(origin, monkeypatch):
    card = _card(origin)
    # The dead drive's tree, exactly where this card's next build goes.
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7", title=card["title"])
    Path(wt, "README.md").write_text("finished, never published\n")
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        return "reply"

    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    saved = origin.stranded("bd-7")
    assert len(saved) == 1, "the drive built over stranded work without saving it"
    assert _show(origin, saved[0], "README.md") == "finished, never published\n"
    # The card says where it went, what it held, and how to look at it.
    notes = [c[2] for c in store.verbs("comment") if saved[0] in c[2]]
    assert len(notes) == 1, store.calls
    assert "1 file changed" in notes[0] and f"diff origin/{origin.base}...{saved[0]}" in notes[0]
    # …and the card was not stopped for it: the build ran and the PR opened.
    assert dispatched == [wt] and store.verbs("open_review") and not store.verbs("flag_blocked")


async def test_a_card_blocks_only_when_its_stranded_work_cannot_be_saved(origin, monkeypatch):
    monkeypatch.setattr(worktree, "_stamp", lambda: "20260906T232550Z", raising=False)  # red-checkable
    _git("-C", origin.clone, "branch", "stranded/bd-7/20260906T232550Z", origin.base_sha)  # the name is taken
    card = _card(origin)
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7", title=card["title"])
    Path(wt, "README.md").write_text("finished, never published\n")
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        return "reply"

    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    assert Path(wt, "README.md").read_text() == "finished, never published\n", "the drive built over unsaved work"
    assert dispatched == [], "a coder was dispatched to rebuild over work that could not be saved"
    ((_verb, fid, reason, category),) = store.verbs("flag_blocked")
    assert fid == "bd-7" and category == "stranded-work"
    # Actionable where the operator reads it: the path, what is in it, why, and what to do.
    assert wt in reason and "README.md" in reason and "saving it to a branch failed" in reason
    assert "unblock" in reason


async def test_a_drive_still_discards_its_own_failed_attempt(origin, monkeypatch):
    """REGRESSION GUARD, not a red test: the drive's own earlier attempt is not someone
    else's stranded work. A transient push failure after the coder wrote its files used to
    be retried on a fresh tree — `create_worktree` wiped the old one implicitly. It still
    is: no stranded/ branch for it, no comment, no block."""
    card = _card(origin)
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        Path(tree, "README.md").write_text(f"attempt {len(dispatched)}\n")
        return "reply"

    pushes: list[str] = []

    async def _flaky_open_pr(tree, branch, **_kw):
        pushes.append(tree)
        if len(pushes) == 1:  # before anything was committed — the tree is still dirty
            raise worktree.WorktreeError("git push failed: Connection reset by peer")
        return "https://github.com/o/r/pull/1"

    real_sleep = asyncio.sleep

    async def _no_backoff(_delay, *args, **kwargs):
        await real_sleep(0)  # skip the transient backoff, still yield like a sleep

    monkeypatch.setattr(asyncio, "sleep", _no_backoff)
    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_flaky_open_pr)._drive(card)

    assert len(dispatched) == 2 and ("open_review", "bd-7", "https://github.com/o/r/pull/1") in store.calls
    assert not store.verbs("flag_blocked") and not store.verbs("comment") and origin.stranded() == []
    assert Path(dispatched[-1], "README.md").read_text() == "attempt 2\n"


async def test_shutdown_saves_an_interrupted_tree_before_it_reaps_it(origin, monkeypatch):
    """A restart mid-drive used to reap the in-flight tree — a finished implementation
    waiting on its gate went with it. Its work is saved first now, and the card says where;
    the next boot rebuilds as before. A clean in-flight tree is reaped as it always was."""
    busy, busy_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-7", title="x")
    rel, text = _strand(busy, "tracked")
    idle, idle_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-8", title="y")
    store = _Store({"id": "bd-7"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"coder": "proto"})
    loop._inflight = {"bd-7": (origin.clone, busy, busy_branch), "bd-8": (origin.clone, idle, idle_branch)}

    await loop.stop()

    saved = origin.stranded("bd-7")
    assert len(saved) == 1, "shutdown reaped an in-flight tree holding work without saving it"
    assert _show(origin, saved[0], rel) == text
    assert not os.path.exists(busy) and not os.path.exists(idle) and origin.stranded("bd-8") == []
    assert [c[1] for c in store.verbs("comment") if saved[0] in c[2]] == ["bd-7"]

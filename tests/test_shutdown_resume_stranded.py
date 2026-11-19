"""A coder killed by host shutdown is not a transient retry, and boot resumes the card
from its own stranded branch instead of rebuilding fresh (protoAgent#5267).

The incident (br-n31.12, 2026-11-13): a member shutdown killed the coder six minutes in.
The board logged `transient — retry 2/3 in 15.0s: coder dispatch failed: sonnet agent
exited (exit code 0)` and slept the backoff into the shutdown. `stop()` saved the five
dirty files to a `stranded/…` branch and removed the tree; the next boot found an
in-progress card with no PR and no verified candidate, reset it to ready, and rebuilt it
from a clean base. The saved work was never used.

Real git throughout (a bare origin plus a clone), as in test_stranded_work_405.py: what
matters is what the recreated tree actually holds.
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
from project_board.loop.prompt import STRANDED_RESUME_NOTE

FID = "bd-7"
TITLE = "Make the poll timeout progress-based"
PR = "https://github.com/o/r/pull/1"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Origin:
    """A bare origin and a clone of it, so `origin/<base>` resolves as in production."""

    base = "main"

    def __init__(self, tmp_path: Path):
        self.origin = str(tmp_path / "origin.git")
        self.seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, self.seed)
        _identity(self.seed)
        Path(self.seed, ".gitignore").write_text("node_modules/\n")
        Path(self.seed, "README.md").write_text("base\n")
        Path(self.seed, "old.py").write_text("to be deleted by the coder\n")
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", "base commit")
        _git("-C", self.seed, "remote", "add", "origin", self.origin)
        _git("-C", self.seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)
        self.base_sha = _git("-C", self.clone, "rev-parse", f"origin/{self.base}")

    def stranded(self) -> list[str]:
        out = _git("-C", self.clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/stranded/")
        return [line for line in out.splitlines() if line]


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


class _Store:
    """The store verbs a drive, a shutdown and boot recovery reach, with the shutdown-resume
    record kept the way the real store projects it (``stranded_ref`` / ``stranded_sha``)."""

    def __init__(self, feature: dict):
        self.feature = dict(feature)
        self.calls: list[tuple] = []
        self.state = "in_progress"

    def current_tier(self, fid):
        return ""

    def get_feature(self, fid):
        return dict(self.feature, board_state=self.state)

    def list_features(self, state=None, include_archived=False):
        return []

    def flag_blocked(self, fid, reason, category="", *, automatic=False):
        self.calls.append(("flag_blocked", fid, reason, category))
        return {"id": fid}

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        self.state = "in_review"
        return {"id": fid}

    def requeue(self, fid):
        self.calls.append(("requeue", fid))
        self.state = "ready"

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))

    def record_attempt(self, fid, **kw):
        self.calls.append(("record_attempt", fid, kw))

    def record_stranded_work(self, fid, *, ref, sha):
        self.calls.append(("record_stranded_work", fid, ref, sha))
        self.feature.update(stranded_ref=ref, stranded_sha=sha)

    def clear_stranded_work(self, fid):
        self.calls.append(("clear_stranded_work", fid))
        self.feature.pop("stranded_ref", None)
        self.feature.pop("stranded_sha", None)

    def record_budget(self, fid, kind, n):
        pass

    def clear_budgets(self, fid, kinds=None):
        pass

    def verbs(self, verb: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == verb]


def _card(origin: _Origin) -> dict:
    return {
        "id": FID,
        "title": TITLE,
        "repo": origin.clone,
        "base_branch": origin.base,
        "spec": "do it",
        "acceptance_criteria": "",  # no oracle → the single-dispatch path, not coder.solve
        "files_to_modify": ["README.md"],
    }


def _loop_for(monkeypatch, store: _Store, *, dispatch=None, open_pr=None) -> BoardLoop:
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    if dispatch is not None:
        monkeypatch.setattr(worktree, "dispatch_coder", dispatch)  # the untapped fallback a host-free run reaches
    if open_pr is not None:
        monkeypatch.setattr(worktree, "open_pr", open_pr)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": ""})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    return loop


async def _open_pr(tree, branch, **_kw):
    return PR


class _HostAcpError(Exception):
    """The host's AcpError as protoAgent bd-eec0n raises it for a shutdown kill."""

    def __init__(self, message: str, *, host_shutdown: bool | None = None):
        super().__init__(message)
        if host_shutdown is not None:
            self.host_shutdown = host_shutdown


def _seam_failure(inner: Exception) -> None:
    """Raise what the seam raises for a below-seam failure: a WorktreeError built from the
    error's text, the original riding the exception chain."""
    try:
        raise inner
    except Exception as exc:  # noqa: BLE001 — mirrors coder_seam's normalisation
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")


@pytest.fixture
def sleeps(monkeypatch):
    """Every backoff-sized sleep the drive asks for, without waiting for it."""
    asked: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        if delay and delay >= 1:
            asked.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return asked


def _dirty(wt: str) -> None:
    """What the dead coder left: an edit, a new untracked file, and a deletion."""
    Path(wt, "README.md").write_text("half-finished edit\n")
    Path(wt, "poll.py").write_text("POLL = 'progress-based'\n")
    os.remove(os.path.join(wt, "old.py"))


# ── 1. the drive: a host-shutdown kill is not a transient attempt ────────────────────


# The first is the host's own message; the second would classify `transient` (retry in 15s)
# on its text alone — the attribute, not the wording, is what ends the drive.
@pytest.mark.parametrize(
    "message", ["agent stopped: host shutting down", "agent stopped: host shutting down: connection reset by peer"]
)
async def test_a_host_shutdown_kill_spends_no_retry_and_sleeps_no_backoff(origin, monkeypatch, sleeps, caplog, message):
    card = _card(origin)
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        _dirty(tree)
        _seam_failure(_HostAcpError(message, host_shutdown=True))

    store = _Store(card)
    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._drive(card)

    assert len(dispatched) == 1, "the killed coder was re-dispatched"
    assert sleeps == [], f"the drive slept a backoff into the shutdown: {sleeps}"
    assert not store.verbs("flag_blocked") and not store.verbs("record_attempt") and not store.verbs("requeue")
    assert store.state == "in_progress"  # left for boot recovery
    assert "dispatch aborted by host shutdown" in caplog.text and "retry" not in caplog.text.split("aborted by")[0]
    # The host may kill the process before stop() ever runs, so the drive saved the dirty
    # tree itself and recorded where — the same record stop() writes.
    (ref,) = origin.stranded()
    sha = _git("-C", origin.clone, "rev-parse", f"refs/heads/{ref}")
    assert store.verbs("record_stranded_work") == [("record_stranded_work", FID, ref, sha)]
    assert not os.path.exists(dispatched[0]) and FID not in loop._inflight
    assert _git("-C", origin.clone, "show", f"{ref}:README.md") == "half-finished edit"


async def test_a_dispatch_that_fails_while_the_loop_is_stopping_is_not_retried(origin, monkeypatch, sleeps):
    card = _card(origin)
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        loop._stop.set()  # stop() has begun; the coder dies under it with an ordinary error
        _seam_failure(_HostAcpError("connection reset by peer"))

    store = _Store(card)
    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)
    await loop._drive(card)

    assert len(dispatched) == 1 and sleeps == []
    assert not store.verbs("flag_blocked") and store.state == "in_progress"
    assert loop._inflight == {}  # the drive's own cleanup still runs (#149)


@pytest.mark.parametrize("host_shutdown", [None, False])
async def test_an_error_without_the_host_shutdown_attribute_retries_as_before(
    origin, monkeypatch, sleeps, host_shutdown
):
    """An older host (no attribute), or an ordinary transient failure, is handled as it
    always was: backoff, then the same tier again."""
    card = _card(origin)
    dispatched: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)
        if len(dispatched) == 1:
            _seam_failure(_HostAcpError("connection reset by peer", host_shutdown=host_shutdown))
        Path(tree, "README.md").write_text("done\n")
        return "reply"

    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    assert len(dispatched) == 2, "the transient retry no longer happens"
    assert len(sleeps) == 1 and sleeps[0] >= 1, "the transient backoff no longer happens"
    assert ("open_review", FID, PR) in store.calls and not store.verbs("record_stranded_work")


# ── 2. stop(): the saved branch and its commit are recorded on the card ──────────────


async def test_shutdown_mid_dispatch_records_the_stranded_ref_and_sha_on_the_card(origin, monkeypatch):
    """The incident's own path: stop() cancels the drive while its coder is still running,
    saves the dirty tree, and now writes the branch and its commit onto the card."""
    card = _card(origin)
    started = asyncio.Event()

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        _dirty(tree)
        started.set()
        await asyncio.Event().wait()  # mid-card when the member restarts

    store = _Store(card)
    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)
    task = asyncio.ensure_future(loop._drive(card))
    loop._drives.add(task)
    await asyncio.wait_for(started.wait(), 20)
    wt = loop._inflight[FID][1]

    await loop.stop()

    assert task.done()
    (ref,) = origin.stranded()
    sha = _git("-C", origin.clone, "rev-parse", f"refs/heads/{ref}")
    assert store.verbs("record_stranded_work") == [("record_stranded_work", FID, ref, sha)]
    assert not os.path.exists(wt) and store.state == "in_progress"
    assert _git("-C", origin.clone, "show", f"{ref}:README.md") == "half-finished edit"
    assert not store.verbs("flag_blocked")


async def test_a_dispatch_failing_under_stop_saves_and_records_its_dirty_tree(origin, monkeypatch, sleeps):
    """The coder can also die with an error while stop() is already running, before the
    cancel reaches the drive. That drive ends itself, so it saves and records the tree too."""
    card = _card(origin)
    trees: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        trees.append(tree)
        _dirty(tree)
        loop._stop.set()
        _seam_failure(_HostAcpError("connection reset by peer"))

    store = _Store(card)
    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)
    await loop._drive(card)

    (ref,) = origin.stranded()
    assert [c[2] for c in store.verbs("record_stranded_work")] == [ref] and sleeps == []
    assert not os.path.exists(trees[0]) and loop._inflight == {} and store.state == "in_progress"


async def test_a_host_shutdown_kill_on_a_card_with_a_pr_keeps_its_tree_and_records_nothing(origin, monkeypatch, sleeps):
    """A fix round resumes from its PR branch, never from a stranded one: its tree is left
    on disk exactly as a shutdown-aborted dispatch always left it."""
    card = dict(_card(origin), pr_url=PR)
    trees: list[str] = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        trees.append(tree)
        _dirty(tree)
        _seam_failure(_HostAcpError("agent stopped: host shutting down", host_shutdown=True))

    store = _Store(card)
    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)
    await loop._drive(card)

    assert len(trees) == 1 and sleeps == [] and not store.verbs("flag_blocked")
    assert os.path.exists(trees[0]) and Path(trees[0], "README.md").read_text() == "half-finished edit\n"
    assert origin.stranded() == [] and not store.verbs("record_stranded_work") and loop._inflight == {}


async def test_shutdown_records_nothing_for_a_clean_tree(origin, monkeypatch):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, FID, title=TITLE)
    store = _Store(_card(origin))
    loop = _loop_for(monkeypatch, store)
    loop._inflight = {FID: (origin.clone, wt, branch)}

    await loop.stop()

    assert not os.path.exists(wt) and origin.stranded() == [] and not store.verbs("record_stranded_work")


# ── 3. boot recovery: resume from the recorded branch ────────────────────────────────


async def _shut_down_mid_card(origin: _Origin, monkeypatch, *, commit_first: bool = False) -> _Store:
    """A card whose coder was mid-build when the loop stopped: returns its store, holding
    the record ``stop()`` wrote."""
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, FID, title=TITLE)
    if commit_first:  # the coder had committed some of its work itself
        Path(wt, "committed.py").write_text("committed by the coder\n")
        _git("-C", wt, "add", "-A")
        _git("-C", wt, "-c", "user.name=c", "-c", "user.email=c@localhost", "commit", "-qm", "wip")
    _dirty(wt)
    store = _Store(_card(origin))
    loop = _loop_for(monkeypatch, store)
    loop._inflight = {FID: (origin.clone, wt, branch)}
    await loop.stop()
    assert store.feature.get("stranded_ref") and not os.path.exists(wt)
    return store


@pytest.mark.parametrize("commit_first", [False, True])
async def test_boot_recreates_the_worktree_from_the_stranded_commit_and_requeues(
    origin, monkeypatch, caplog, commit_first
):
    store = await _shut_down_mid_card(origin, monkeypatch, commit_first=commit_first)
    ref, sha = store.feature["stranded_ref"], store.feature["stranded_sha"]
    loop = _loop_for(monkeypatch, store)  # the next boot

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_orphan(FID)

    assert store.verbs("requeue") == [("requeue", FID)] and store.state == "ready"
    assert "resuming the work a restart interrupted" in caplog.text and "rebuild fresh" not in caplog.text
    wt = os.path.join(origin.clone, loop.root, worktree.worktree_dir(FID, TITLE))
    # The tree is back as the coder left it: the saved changes UNCOMMITTED, on the commit
    # the dead tree had checked out — the board's save commit is not in the card's history.
    assert Path(wt, "README.md").read_text() == "half-finished edit\n"
    assert Path(wt, "poll.py").read_text() == "POLL = 'progress-based'\n"
    assert not os.path.exists(os.path.join(wt, "old.py"))
    head = _git("-C", wt, "rev-parse", "HEAD")
    assert head == _git("-C", origin.clone, "rev-parse", f"{sha}^1") and head != sha
    assert (head == origin.base_sha) is (not commit_first)
    assert Path(wt, "committed.py").exists() is commit_first
    status = _git("-C", wt, "status", "--porcelain")
    assert sorted(line.split()[-1] for line in status.splitlines()) == ["README.md", "old.py", "poll.py"]
    assert _git("-C", wt, "diff", "--cached", "--name-only") == ""  # nothing staged, nothing committed
    assert _git("-C", wt, "branch", "--show-current") == worktree.branch_name(FID, TITLE)
    # The record stays until the card opens its PR; the stranded branch is untouched.
    assert store.feature["stranded_ref"] == ref and origin.stranded() == [ref]
    assert not store.verbs("clear_stranded_work")


async def test_a_tree_saved_as_commits_only_resumes_at_that_commit(origin):
    """A tree whose only unique work was the coder's own commits is saved at its HEAD, with
    no save commit on top: the resumed branch sits exactly there."""
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, FID, title=TITLE)
    Path(wt, "committed.py").write_text("committed by the coder\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "-c", "user.name=c", "-c", "user.email=c@localhost", "commit", "-qm", "wip")
    tip = _git("-C", wt, "rev-parse", "HEAD")
    _removed, record = await worktree.discard_worktree(origin.clone, wt, branch, why="clearing")
    assert record.commit == tip

    path, _branch = await worktree.resume_stranded_worktree(
        origin.clone, origin.base, FID, title=TITLE, ref=record.ref, sha=record.commit
    )

    assert _git("-C", path, "rev-parse", "HEAD") == tip and _git("-C", path, "status", "--porcelain") == ""
    assert await worktree.stranded_resume_tree(origin.clone, ".worktrees", FID, TITLE, tip) == (path, _branch)


async def test_the_resumed_card_continues_in_that_tree_and_still_passes_the_gate_before_its_pr(
    origin, monkeypatch, caplog
):
    store = await _shut_down_mid_card(origin, monkeypatch)
    ref = store.feature["stranded_ref"]
    order: list[str] = []
    seen: dict = {}

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        order.append("dispatch")
        seen.update(tree=tree, prompt=prompt, readme=Path(tree, "README.md").read_text())
        Path(tree, "poll.py").write_text("POLL = 'progress-based'  # finished\n")
        return "reply"

    async def _opening(tree, branch, **_kw):
        order.append("open_pr")
        return PR

    loop = _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_opening)
    await loop._reconcile_orphan(FID)
    wt = os.path.join(origin.clone, loop.root, worktree.worktree_dir(FID, TITLE))

    async def _gate(tree, feature=None):
        order.append("gate")
        seen["gated"] = tree
        return None

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    store.state = "in_progress"  # the puller's claim
    await loop._drive(store.get_feature(FID))

    # Continued in the recreated tree, on the saved work, told so — and NOT told it was rejected.
    assert os.path.realpath(seen["tree"]) == os.path.realpath(wt)
    assert seen["readme"] == "half-finished edit\n"
    assert STRANDED_RESUME_NOTE in seen["prompt"]
    assert "continue from the existing changes in this worktree; re-run the gate before finishing" in (
        seen["prompt"].lower()
    )
    assert "REJECTED" not in seen["prompt"]
    # Nothing unverified ships: the pre-PR gate ran on that tree before the PR opened.
    assert order == ["dispatch", "gate", "open_pr"] and os.path.realpath(seen["gated"]) == os.path.realpath(wt)
    assert ("open_review", FID, PR) in store.calls
    assert origin.stranded() == [ref], "the drive set the resumed tree aside instead of continuing in it"
    assert not store.verbs("flag_blocked")


async def test_a_card_without_a_resumed_tree_builds_fresh_and_is_not_told_to_continue(origin, monkeypatch):
    """The record alone resumes nothing: with no recreated tree on disk the drive builds off
    base exactly as before."""
    card = dict(_card(origin), stranded_ref="stranded/x/1", stranded_sha="0" * 40)
    seen: dict = {}

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        seen.update(prompt=prompt, readme=Path(tree, "README.md").read_text())
        Path(tree, "README.md").write_text("built fresh\n")
        return "reply"

    store = _Store(card)
    await _loop_for(monkeypatch, store, dispatch=_dispatch, open_pr=_open_pr)._drive(card)

    assert seen["readme"] == "base\n" and "Continue the interrupted attempt" not in seen["prompt"]
    assert ("open_review", FID, PR) in store.calls


# ── 4. a record that cannot be trusted falls back to a fresh rebuild, and says why ───


def _orphan_commit(repo: str) -> str:
    """A commit that shares no history with anything in ``repo``."""
    tree = _git("-C", repo, "hash-object", "-t", "tree", "-w", os.devnull)  # the empty tree
    return _git(
        "-C", repo, "-c", "user.name=x", "-c", "user.email=x@localhost", "commit-tree", tree, "-m", "unrelated root"
    )


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("missing", "is missing or unreadable"),
        ("moved", "moved ("),
        ("diverged", "has diverged"),
        ("no-ref", "no stranded branch is recorded"),
    ],
)
async def test_a_missing_or_diverged_stranded_ref_rebuilds_fresh_and_logs_why(
    origin, monkeypatch, caplog, damage, reason
):
    store = await _shut_down_mid_card(origin, monkeypatch)
    ref = store.feature["stranded_ref"]
    if damage == "missing":
        _git("-C", origin.clone, "branch", "-D", ref)
    elif damage == "moved":
        _git("-C", origin.clone, "update-ref", f"refs/heads/{ref}", origin.base_sha)
    elif damage == "diverged":
        root = _orphan_commit(origin.clone)
        _git("-C", origin.clone, "update-ref", f"refs/heads/{ref}", root)
        store.feature["stranded_sha"] = root
    else:
        store.feature["stranded_ref"] = ""  # the label survived, its comment did not
    loop = _loop_for(monkeypatch, store)

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_orphan(FID)

    assert store.verbs("requeue") == [("requeue", FID)] and store.state == "ready"
    assert "cannot resume its stranded work" in caplog.text and reason in caplog.text
    assert "reset to ready (no PR — rebuild fresh)" in caplog.text  # today's fallthrough, unchanged
    assert store.verbs("clear_stranded_work") == [("clear_stranded_work", FID)]
    wt = os.path.join(origin.clone, loop.root, worktree.worktree_dir(FID, TITLE))
    assert not os.path.exists(wt), "a tree was recreated from a record that failed its checks"


async def test_a_card_with_no_record_rebuilds_fresh_exactly_as_before(origin, monkeypatch, caplog):
    store = _Store(_card(origin))
    loop = _loop_for(monkeypatch, store)

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_orphan(FID)

    assert store.calls == [("requeue", FID)]
    assert "reset to ready (no PR — rebuild fresh)" in caplog.text and "stranded" not in caplog.text


async def test_a_card_with_a_pr_or_a_verified_candidate_is_not_resumed_from_stranded(origin, monkeypatch):
    """The resume sits BELOW the PR adoption and the verified-candidate salvage, and never
    replaces either: with a verified record on the card it declines."""
    store = await _shut_down_mid_card(origin, monkeypatch)
    store.feature["verified_sha"] = "a" * 40
    loop = _loop_for(monkeypatch, store)

    assert await loop._resume_stranded_work(store, FID) is False
    wt = os.path.join(origin.clone, loop.root, worktree.worktree_dir(FID, TITLE))
    assert not os.path.exists(wt) and not store.verbs("requeue") and not store.verbs("clear_stranded_work")


# ── 5. the store: the record, its projection, and when it is cleared ─────────────────

REF = "stranded/feat-bd-7-x/20261113T101500.123Z"
SHA = "a1b2c3d4" * 5


def _bead(labels, comments=(), status="in_progress", **extra):
    return {"id": FID, "title": "t", "status": status, "labels": list(labels), "comments": list(comments), **extra}


def _recording_board(make_board, bead: dict):
    runs: list[tuple] = []

    def _run(*args, want_json=False):
        runs.append(args)
        return [bead] if want_json else ""

    board = make_board(_run)
    board.get_feature = lambda fid: dict(board._project(bead), labels=list(bead["labels"]))
    return board, runs


def test_the_record_is_a_replaced_label_plus_a_comment_naming_the_branch(make_board):
    board, runs = _recording_board(make_board, _bead(["stranded:old0000"]))

    board.record_stranded_work(FID, ref=REF, sha=SHA)

    assert ("update", FID, "--remove-label", "stranded:old0000", "--add-label", f"stranded:{SHA}") in runs
    assert any(f"stranded resume: ref={REF} sha={SHA}" in " ".join(map(str, r)) for r in runs)


def test_the_projection_pairs_the_label_with_the_comment_for_the_same_commit(make_board):
    board = make_board(lambda *a, **k: "")
    older = "stranded resume: ref=stranded/feat-bd-7-x/older sha=" + "f" * 40
    f = board._project(_bead([f"stranded:{SHA}"], [older, f"stranded resume: ref={REF} sha={SHA}"]))
    assert (f["stranded_ref"], f["stranded_sha"]) == (REF, SHA)
    # A comment for another save never lends its branch to this commit; no label, no record.
    assert board._project(_bead([f"stranded:{SHA}"], [older]))["stranded_ref"] == ""
    bare = board._project(_bead([], [f"stranded resume: ref={REF} sha={SHA}"]))
    assert (bare["stranded_ref"], bare["stranded_sha"]) == ("", "")


def test_opening_a_pr_clears_the_record(make_board):
    board, runs = _recording_board(make_board, _bead([f"stranded:{SHA}"]))
    board.open_review(FID, pr_url=PR)
    (update,) = [r for r in runs if r[:2] == ("update", FID)]
    assert ("--remove-label", f"stranded:{SHA}") in list(zip(update, update[1:]))
    assert "--external-ref" in update and "in-review" in update


def test_opening_a_pr_on_a_card_without_a_record_writes_what_it_always_did(make_board):
    board, runs = _recording_board(make_board, _bead([]))
    board.open_review(FID, pr_url=PR)
    (update,) = [r for r in runs if r[:2] == ("update", FID)]
    assert "--remove-label" not in update


def test_cancelling_clears_the_record(make_board):
    board, runs = _recording_board(make_board, _bead([f"stranded:{SHA}"]))
    board.cancel_feature(FID, "scope cut")
    tag = next(r for r in runs if r[:2] == ("update", FID) and "cancelled" in r)
    assert ("--remove-label", f"stranded:{SHA}") in list(zip(tag, tag[1:]))


def test_clearing_is_a_no_op_without_a_record(make_board):
    board, runs = _recording_board(make_board, _bead([]))
    board.clear_stranded_work(FID)
    assert not [r for r in runs if r[0] == "update"]
    board, runs = _recording_board(make_board, _bead([f"stranded:{SHA}"]))
    board.clear_stranded_work(FID)
    assert ("update", FID, "--remove-label", f"stranded:{SHA}") in runs

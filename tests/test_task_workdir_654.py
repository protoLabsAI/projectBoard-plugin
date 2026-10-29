"""#654 part 1: an ACP-driven task runs in a throwaway scratch workdir.

Before this, ``coder_seam.dispatch_task`` dispatched an ACP task with NO cwd and
``_drive_task`` cut no tree — so a task that reached a coder had no project checkout to
run in. Now ``_drive_task`` cuts a scratch workdir before dispatch (a worktree of the
card's project at its base branch, or a bare temp dir when the card has no resolvable
project repo) and hands it to ``dispatch_task`` as ``cwd``; ``dispatch_task`` scopes the
ACP session to it exactly as ``dispatch_coder`` scopes a coder to a worktree. The tree
NEVER commits/pushes/opens a PR — the deliverable stays the reply → ``record_delivery``.

Real local ``git`` for the worktree cases (the ``test_worktree_git.py`` tier): a bare
origin + clone so ``origin/<base>`` resolves exactly as in production, nothing escapes
``tmp_path``. The ONE fake is the model boundary — a stand-in ACP adapter that records
the ``cwd`` (the scoped delegate's ``workdir``) it was handed and whether it existed
*during* the dispatch.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import types

import pytest

from project_board import api, coder_seam, worktree
from project_board.loop import BoardLoop


# ── real-git fixture helpers (mirrors tests/test_worktree_git.py) ──────────────────
def _git_run(*args):
    """Run git for FIXTURE setup (never under test) — raises on failure, returns stdout."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _configure_identity(repo):
    _git_run("-C", str(repo), "config", "user.email", "board-test@localhost")
    _git_run("-C", str(repo), "config", "user.name", "Board Test")
    _git_run("-C", str(repo), "config", "commit.gpgsign", "false")


def _make_clone(tmp_path, base="main"):
    """A bare origin + a clone checked out on ``base`` with one commit. Returns
    ``(clone_path, base_sha)`` — the clone is the project repo a task worktree is cut from,
    with ``origin/<base>`` resolving the way ``create_worktree`` expects."""
    origin = str(tmp_path / "origin.git")
    seed = str(tmp_path / "seed")
    clone = str(tmp_path / "clone")
    _git_run("init", "--bare", origin)
    _git_run("init", "-b", base, seed)
    _configure_identity(seed)
    (tmp_path / "seed" / "README.md").write_text("base\n")
    _git_run("-C", seed, "add", "-A")
    _git_run("-C", seed, "commit", "-m", "base commit")
    _git_run("-C", seed, "remote", "add", "origin", origin)
    _git_run("-C", seed, "push", "-u", "origin", base)
    _git_run("-C", origin, "symbolic-ref", "HEAD", f"refs/heads/{base}")
    _git_run("clone", origin, clone)
    _configure_identity(clone)
    return clone, _git_run("-C", clone, "rev-parse", f"origin/{base}")


# ── the model boundary: a stand-in ACP adapter ────────────────────────────────────
@dataclasses.dataclass
class _ACPDelegate:
    """A real (dataclass) ACP coder delegate — the shape ``dispatch_task`` scopes with
    ``dataclasses.replace``. Carries the ``workdir``/``manage_git``/``env`` fields
    ``dispatch_coder`` overrides on its per-feature copy."""

    type: str = "acp"
    name: str = "acp-agent"
    workdir: str = ""
    manage_git: bool = True
    env: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _A2ADelegate:
    type: str = "a2a"
    name: str = "a2a-agent"


class _FakeACPAdapter:
    """Records the scoped delegate it is handed so a test can prove the cwd it dispatched
    into existed during the dispatch, with board-owned git."""

    def __init__(self, reply="task deliverable"):
        self.reply = reply
        self.forgot = []
        self.torn_down = []
        self.observed = None

    async def forget_session(self, delegate):
        self.forgot.append(getattr(delegate, "workdir", None))

    async def dispatch(self, delegate, prompt, *, timeout=None):
        wd = getattr(delegate, "workdir", "")
        self.observed = {
            "cwd": wd,
            "exists": bool(wd) and os.path.isdir(wd),
            "manage_git": getattr(delegate, "manage_git", "<unset>"),
            "env": getattr(delegate, "env", None),
        }
        return self.reply

    async def teardown(self, delegate):
        self.torn_down.append(getattr(delegate, "workdir", None))


def _inject_task_adapters(monkeypatch, **adapters):
    """Stand in for the HOST's ``plugins.delegates.adapters`` (absent in this suite) so the
    real ``dispatch_task`` resolves a fake adapter. Mirrors test_coder_seam.py."""

    class DelegateError(Exception):
        pass

    mod = types.ModuleType("plugins.delegates.adapters")
    mod.DelegateError = DelegateError
    mod.ADAPTERS = dict(adapters)
    plugins = types.ModuleType("plugins")
    delegates = types.ModuleType("plugins.delegates")
    plugins.delegates = delegates
    delegates.adapters = mod
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugins.delegates", delegates)
    monkeypatch.setitem(sys.modules, "plugins.delegates.adapters", mod)
    return DelegateError


class _FakeStore:
    """Just enough store for ``_drive_task``'s success tail (``record_delivery``)."""

    def __init__(self):
        self.calls = []

    def record_delivery(self, fid, text="", ref=""):
        self.calls.append(("record_delivery", fid, text))
        return {"id": fid, "issue_type": "task", "board_state": "in_review", "deliverable": text}


# ── r1: an ACP task is dispatched in a worktree of its project, on base ────────────
async def test_an_acp_task_is_dispatched_in_a_project_worktree_on_base(tmp_path, monkeypatch):
    """r1: the cwd the ACP adapter receives is a worktree under the project's
    ``worktrees_root`` (the ``feat-<id>`` naming the reap/sweep find), it EXISTS during the
    dispatch, it is checked out on the base branch, and git is board-owned (manage_git
    off)."""
    clone, base_sha = _make_clone(tmp_path)
    store = _FakeStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    acp = _FakeACPAdapter()
    _inject_task_adapters(monkeypatch, acp=acp)

    loop = BoardLoop({"coder": "proto"})
    feature = {
        "id": "bd-wk1",
        "title": "write the runbook",
        "issue_type": "task",
        "repo": clone,
        "base_branch": "main",
    }
    await loop._drive_task(feature, _ACPDelegate())

    assert acp.observed is not None, "the ACP adapter was dispatched"
    cwd = acp.observed["cwd"]
    assert acp.observed["exists"] is True  # the tree existed DURING the dispatch
    assert acp.observed["manage_git"] is False  # the BOARD owns git — the ACP side opens no PR
    assert os.path.dirname(cwd) == os.path.join(clone, ".worktrees")  # under the project's root
    assert os.path.basename(cwd).startswith("feat-bd-wk1")  # the feat-<id> naming the reap sweeps
    assert _git_run("-C", cwd, "rev-parse", "HEAD") == base_sha  # checked out ON base
    assert acp.forgot == [cwd]  # fresh-both: forget the stale session before dispatching
    assert acp.torn_down == [cwd]  # the scoped client is reaped on the way out
    assert ("record_delivery", "bd-wk1", "task deliverable") in store.calls  # the reply IS the deliverable


# ── r2: an ACP task with no project repo runs in a temp dir, removed after ─────────
async def test_an_acp_task_with_no_repo_runs_in_a_temp_dir_removed_after(tmp_path, monkeypatch):
    """r2: a card whose project has no resolvable git repo gets an empty ``mkdtemp`` as its
    cwd (which existed during the dispatch), and that temp dir is removed after the drive —
    there is no worktree naming to reap it later."""
    nongit = tmp_path / "not-a-repo"
    nongit.mkdir()
    store = _FakeStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    acp = _FakeACPAdapter()
    _inject_task_adapters(monkeypatch, acp=acp)

    loop = BoardLoop({"coder": "proto", "repo": str(nongit)})
    feature = {"id": "bd-wk2", "title": "decide the ADR", "issue_type": "task"}  # no repo → the board default
    await loop._drive_task(feature, _ACPDelegate())

    cwd = acp.observed["cwd"]
    assert acp.observed["exists"] is True  # the temp dir existed during the dispatch…
    assert os.path.basename(cwd).startswith("pb-task-bd-wk2-")  # …a bare mkdtemp, not a worktree
    assert not os.path.exists(cwd)  # …and it was removed in the drive's finally
    assert ("record_delivery", "bd-wk2", "task deliverable") in store.calls


# ── scoped task environments honor the operator's allowlist ──────────────────────
@pytest.mark.parametrize("passthrough", [[], ["A2A_TOKEN"]])
async def test_acp_task_drive_honors_env_passthrough(tmp_path, monkeypatch, passthrough):
    monkeypatch.setenv("A2A_TOKEN", "allowed-task-token")
    monkeypatch.setenv("PROTOAGENT_HOME", "/host-only")
    monkeypatch.setenv("PB_TASK_TEST_VALUE", "ordinary-value")
    store = _FakeStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    acp = _FakeACPAdapter()
    _inject_task_adapters(monkeypatch, acp=acp)
    delegate = _ACPDelegate(env={"original": "untouched"})
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "env_passthrough": passthrough})

    await loop._drive_task({"id": "bd-env", "title": "audit", "issue_type": "task"}, delegate)

    env = acp.observed["env"]
    assert env.get("A2A_TOKEN") == ("allowed-task-token" if passthrough else None)
    assert "PROTOAGENT_HOME" not in env
    assert env["PB_TASK_TEST_VALUE"] == "ordinary-value"
    assert delegate.env == {"original": "untouched"}
    assert os.environ["A2A_TOKEN"] == "allowed-task-token"
    assert os.environ["PROTOAGENT_HOME"] == "/host-only"
    assert ("record_delivery", "bd-env", "task deliverable") in store.calls


@pytest.mark.parametrize("kind,cwd", [("acp", None), ("a2a", "/ignored")])
async def test_unscoped_task_preserves_delegate_env(monkeypatch, kind, cwd):
    adapter = _FakeACPAdapter()
    _inject_task_adapters(monkeypatch, **{kind: adapter})
    delegate = _ACPDelegate(type=kind, env={"A2A_TOKEN": "delegate-token"})

    reply = await coder_seam.dispatch_task(delegate, "audit", cwd=cwd, env_passthrough=["A2A_TOKEN"])

    assert reply == "task deliverable"
    assert adapter.observed["env"] is delegate.env
    assert adapter.observed["env"] == {"A2A_TOKEN": "delegate-token"}
    assert adapter.forgot == []
    assert adapter.torn_down == [delegate.workdir]


# ── r4: a task drive never commits, pushes, or opens a PR ──────────────────────────
async def test_a_task_drive_never_commits_pushes_or_opens_a_pr(tmp_path, monkeypatch):
    """r4: the scratch tree is for the coder to run in, not to ship — the drive must never
    call commit_worktree / stage_all / open_pr."""
    clone, _base_sha = _make_clone(tmp_path)
    store = _FakeStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    _inject_task_adapters(monkeypatch, acp=_FakeACPAdapter())

    forbidden = []

    def _forbid(name):
        async def _fn(*a, **k):
            forbidden.append(name)

        return _fn

    for name in ("commit_worktree", "stage_all", "open_pr"):
        monkeypatch.setattr(worktree, name, _forbid(name))

    loop = BoardLoop({"coder": "proto"})
    feature = {"id": "bd-wk4", "title": "audit the gates", "issue_type": "task", "repo": clone, "base_branch": "main"}
    await loop._drive_task(feature, _ACPDelegate())

    assert forbidden == []  # none of the PR-lifecycle seams were touched
    assert ("record_delivery", "bd-wk4", "task deliverable") in store.calls


# ── r5: A2A and the unscoped double keep today's behaviour exactly ─────────────────
async def test_a_non_acp_or_bare_delegate_gets_no_scratch_workdir(tmp_path, monkeypatch):
    """r5: ``_task_workdir`` returns no cwd for an A2A delegate (it runs in its own context)
    or a bare, unscopeable double — so ``dispatch_task`` is reached with the exact pre-#654
    argument list and existing task tests keep passing."""
    clone, _ = _make_clone(tmp_path)
    loop = BoardLoop({"coder": "proto"})
    feature = {"id": "bd-wk5", "title": "x", "issue_type": "task", "repo": clone, "base_branch": "main"}

    # An A2A dataclass delegate — even with a resolvable repo — is never scoped.
    assert await loop._task_workdir(feature, _A2ADelegate()) == (None, None)
    # A bare test double (not a dataclass) can't be scoped either → runs unscoped.
    assert await loop._task_workdir(feature, object()) == (None, None)


async def test_an_a2a_task_dispatches_with_no_cwd_kwarg(monkeypatch):
    """r5: an A2A task drive calls ``dispatch_task`` with NO ``cwd`` — proven by a fake whose
    signature predates the kwarg (it would raise on an unexpected ``cwd``)."""
    store = _FakeStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pre654_dispatch_task(delegate, prompt, *, timeout=None):  # no `cwd` — the old shape
        return "a2a deliverable"

    monkeypatch.setattr(coder_seam, "dispatch_task", _pre654_dispatch_task)

    loop = BoardLoop({"coder": "proto"})
    feature = {"id": "bd-wk5b", "title": "audit", "issue_type": "task"}
    await loop._drive_task(feature, _A2ADelegate())  # must not raise TypeError on `cwd`

    assert ("record_delivery", "bd-wk5b", "a2a deliverable") in store.calls


# ── r3: the task worktree is reaped on cancel and on verify-approve ────────────────
def _reap_router(monkeypatch, cfg):
    """Mount the plugin router with a stubbed terminal-edge reap (it shells out to git) and
    a fake store, returning ``(TestClient, reaped)`` where ``reaped`` is the
    ``(repo, root, fid)`` list the reap recorded."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reaped = []

    async def _reap(repo, root, fid):
        reaped.append((repo, root, fid))

    monkeypatch.setattr("project_board.worktree.reap_feature_worktree", _reap)

    class _Store:
        def get_feature(self, fid):
            return {"id": fid, "issue_type": "task", "board_state": "in_review"}

        def cancel_feature(self, fid, reason=""):
            return {"id": fid, "issue_type": "task", "board_state": "cancelled"}

        def record_verification(self, fid, approved=True, feedback="", by=""):
            return {"id": fid, "issue_type": "task", "board_state": "done" if approved else "ready"}

    monkeypatch.setattr(api, "get_store", lambda **_kw: _Store())
    app = FastAPI()
    # The cancel / verify terminal edges (and the reap closure) live on the data router.
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    return TestClient(app), reaped


def test_verify_approve_reaps_the_task_worktree(monkeypatch):
    """r3: an approved verification is the task's terminal Done edge — it reaps the scratch
    worktree under the card's project repo + the board's worktrees_root."""
    c, reaped = _reap_router(monkeypatch, {"repo": "/repo", "worktrees_root": ".wt"})
    r = c.post("/api/plugins/project_board/features/bd-task/verify", json={"approved": True})
    assert r.status_code == 200 and r.json()["board_state"] == "done"
    assert reaped == [("/repo", ".wt", "bd-task")]


def test_verify_reject_does_not_reap(monkeypatch):
    """r3: an adverse verify requeues to ready and KEEPS the tree — a fresh dispatch cuts a
    new one — so nothing is reaped here."""
    c, reaped = _reap_router(monkeypatch, {"repo": "/repo", "worktrees_root": ".wt"})
    r = c.post("/api/plugins/project_board/features/bd-task/verify", json={"approved": False, "feedback": "redo it"})
    assert r.status_code == 200 and r.json()["board_state"] == "ready"
    assert reaped == []  # the tree stays for the re-dispatch


def test_cancel_reaps_the_task_worktree(monkeypatch):
    """r3: cancel is terminal for a task too — the existing cancel-edge reap covers it (same
    feat-<id> naming), confirmed here against a task card."""
    c, reaped = _reap_router(monkeypatch, {"repo": "/repo", "worktrees_root": ".wt"})
    r = c.post("/api/plugins/project_board/features/bd-task/cancel", json={"reason": "dup"})
    assert r.status_code == 200
    assert reaped == [("/repo", ".wt", "bd-task")]

"""Store tests — the board projection over beads and the two invariants.

The board is a *projection* of ``br`` status + labels, so the highest-value tests
are pure: ``board_state`` (the projection), the escalation ladder, and the
``_project`` field mapping. The gate (``mark_ready``) and the single Done edge
(``record_merge``) are exercised with ``_run`` (the ``br`` subprocess call)
replaced by the ``make_board`` fixture — no CLI, no DB.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

import project_board as pb
from project_board import store
from project_board.store import BeadsBoard, BoardError, escalation_enabled


class Br:
    """A fake ``_run``: records every ``br`` call and returns canned values keyed
    by the leading subcommand. A canned value may be a callable ``(args) -> value``.
    ``with_has_more`` mirrors the real seam's contract — ``(payload, has_more)``,
    with ``has_more`` None (the 0.1.x no-envelope shape)."""

    def __init__(self, returns=None):
        self.calls = []
        self.returns = returns or {}

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        val = self.returns.get(args[0] if args else "", [] if want_json else "")
        val = val(args) if callable(val) else val
        return (val, None) if with_has_more else val

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


def test_reconfigure_cached_store_updates_shared_project_routing(monkeypatch):
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    # a blank db resolves through the instance default (D3, #260) — the reconfigure
    # lookup must land on the same key get_store built the board under.
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    board = BeadsBoard(repo="/instance", projects={"old": {"repo": "/old"}}, default_project="old")
    key = ("/inst/project_board/.beads/beads.db", "/instance", "main")
    monkeypatch.setitem(store._BOARDS, key, board)

    assert store.reconfigure_cached_store(repo="/instance", projects={"new": {"repo": "/new"}}, default_project="new")
    assert board.projects == {"new": {"repo": "/new"}}
    assert board.default_project == "new"
    assert board._repo_for({"project": "new"}) == "/new"
    assert not store.reconfigure_cached_store(repo="/not-cached", projects={})


# ── board_state: the projection (status + labels → one of six states) ───────────


@pytest.mark.parametrize(
    "bead,expected",
    [
        ({"status": "open", "labels": []}, "backlog"),
        ({"status": "open", "labels": ["ready"]}, "ready"),
        ({"status": "in_progress", "labels": []}, "in_progress"),
        ({"status": "in_progress", "labels": ["in-review"]}, "in_review"),
        ({"status": "closed", "labels": []}, "done"),
        ({"status": "closed", "labels": ["cancelled"]}, "cancelled"),  # the second terminal edge (#47)
        ({"status": "deferred", "labels": []}, "backlog"),
        ({"status": "open", "labels": ["blocked"]}, "blocked"),
        # precedence: closed beats a stray blocked label; blocked beats in-review.
        ({"status": "closed", "labels": ["blocked", "ready"]}, "done"),
        # a cancelled+closed bead is `cancelled`, not `done`, even with other labels.
        ({"status": "closed", "labels": ["cancelled", "blocked"]}, "cancelled"),
        ({"status": "in_progress", "labels": ["blocked", "in-review"]}, "blocked"),
    ],
)
def test_board_state_projection(bead, expected):
    assert BeadsBoard.board_state(bead) == expected


# ── escalation ladder (pure) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({}, False),
        ({"coders": {}}, False),
        ({"coders": {"fast": "proto"}}, False),
        ({"coders": {"fast": "proto", "smart": "proto"}}, False),  # same delegate
        ({"coders": {"fast": "proto", "smart": "proto-smart"}}, True),
    ],
)
def test_escalation_enabled_needs_two_distinct_coders(cfg, expected):
    assert escalation_enabled(cfg) is expected


# ── _ensure_workspace: pin to the repo's own .beads/ (no walk-up escape, #48) ────


def _ok():
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _board(monkeypatch, *, db=None, repo="/repo"):
    """A BeadsBoard with the `br` PATH check stubbed (so __init__ passes) but the REAL
    _ensure_workspace intact — for exercising the workspace-pin logic directly."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    return BeadsBoard(db=db, repo=repo)


def test_ensure_workspace_noop_with_explicit_db(monkeypatch):
    """An explicit db_path is the hard pin — never br-init, never walk up."""
    calls = []
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a) or _ok())
    b = _board(monkeypatch, db="/somewhere/.beads/beads.db")
    b._ensure_workspace()
    assert calls == [] and b._workspace_ready  # no init shelled


def test_ensure_workspace_noop_when_repo_has_beads(monkeypatch):
    """Repo already has its own .beads/ → cwd-discovery resolves locally; no init."""
    monkeypatch.setattr(store.os.path, "isdir", lambda p: p.endswith(".beads"))
    calls = []
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a) or _ok())
    _board(monkeypatch)._ensure_workspace()
    assert calls == []


def test_ensure_workspace_br_inits_a_repo_with_no_beads(monkeypatch):
    """Repo with no .beads/ → `br init` it ONCE, then the pin is ready and not re-run."""
    state = {"beads": False}
    monkeypatch.setattr(store.os.path, "isdir", lambda p: state["beads"] and p.endswith(".beads"))
    inits = []

    def _run(cmd, **k):
        inits.append(cmd)
        state["beads"] = True  # init created .beads/
        return _ok()

    monkeypatch.setattr(store.subprocess, "run", _run)
    b = _board(monkeypatch, repo="/fresh")
    b._ensure_workspace()
    assert len(inits) == 1 and inits[0][:2] == [store.BR, "init"] and b._workspace_ready
    b._ensure_workspace()  # idempotent — guarded by _workspace_ready, no second init
    assert len(inits) == 1


def test_ensure_workspace_raises_a_clear_error_when_init_fails(monkeypatch):
    """No .beads/ and `br init` fails (still none) → an actionable BoardError, NOT a
    silent escape to a parent db."""
    monkeypatch.setattr(store.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(
        store.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="denied")
    )
    with pytest.raises(BoardError, match="has no beads workspace"):
        _board(monkeypatch, repo="/ro")._ensure_workspace()


# ── the instance-default board store (D3, #260) ─────────────────────────────────
# A blank db_path no longer br-init's in the target repo: get_store defaults the db
# to the ONE instance store (the ADR 0065 instance_paths seam br_fetch.data_dir
# rides), and _ensure_workspace bootstraps THAT store — never a project repo.


def test_default_db_path_rides_the_instance_paths_seam(monkeypatch):
    """Hosted: the store lives under ``instance_paths().store("project_board")`` —
    the same ADR 0065 seam br_fetch.data_dir uses, so per-instance isolation is
    inherited (two instances on one box get two board stores)."""

    class _Paths:
        def store(self, name):
            assert name == "project_board"
            return "/inst/root/project_board"

    infra = types.ModuleType("infra")
    infra_paths = types.ModuleType("infra.paths")
    infra_paths.instance_paths = lambda: _Paths()
    infra.paths = infra_paths
    monkeypatch.setitem(sys.modules, "infra", infra)
    monkeypatch.setitem(sys.modules, "infra.paths", infra_paths)
    assert store.default_db_path() == os.path.join("/inst/root/project_board", ".beads", "beads.db")


def test_default_db_path_host_free_fallback(monkeypatch):
    """No protoAgent host importable → the ~/.protoagent fallback (the br_fetch
    pattern), so a standalone/test run still resolves ONE deterministic store."""
    monkeypatch.setitem(sys.modules, "infra", None)  # None in sys.modules → ImportError
    monkeypatch.setitem(sys.modules, "infra.paths", None)
    expected = os.path.join(os.path.expanduser("~"), ".protoagent", "project_board", ".beads", "beads.db")
    assert store.default_db_path() == expected


def test_ensure_workspace_inits_the_default_store_in_the_store_root(monkeypatch, tmp_path):
    """Fresh instance, defaulted db: ONE `br init`, cwd'd in the STORE root — never
    the project repo. The plain no-`--db` init form is load-bearing: `br init --db`
    ALSO drops a `.beads/` in its cwd (real br 0.1.23 and 0.2.16), which with
    cwd=repo would be exactly the repo pollution D3 removes."""
    dbfile = tmp_path / "project_board" / ".beads" / "beads.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(dbfile))
    inits = []

    def _run(cmd, **kw):
        inits.append((cmd, kw.get("cwd")))
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        dbfile.write_text("")  # br init created the workspace
        return _ok()

    monkeypatch.setattr(store.subprocess, "run", _run)
    b = _board(monkeypatch, db=str(dbfile), repo="/some/project/repo")
    b._ensure_workspace()
    assert len(inits) == 1 and b._workspace_ready
    cmd, cwd = inits[0]
    assert cmd[:2] == [store.BR, "init"] and "--db" not in cmd
    assert "--prefix" in cmd  # ids keep the documented bd- shape (default = dir name)
    assert cwd == str(tmp_path / "project_board")  # the store ROOT, not the repo
    b._ensure_workspace()  # idempotent — no second init
    assert len(inits) == 1


def test_ensure_workspace_default_store_noop_when_db_exists(monkeypatch, tmp_path):
    """An already-initialized instance store: no init, no writes — the first op goes
    straight through with --db."""
    dbfile = tmp_path / "project_board" / ".beads" / "beads.db"
    dbfile.parent.mkdir(parents=True)
    dbfile.write_text("")
    monkeypatch.setattr(store, "default_db_path", lambda: str(dbfile))
    calls = []
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a) or _ok())
    b = _board(monkeypatch, db=str(dbfile))
    b._ensure_workspace()
    assert calls == [] and b._workspace_ready


def test_ensure_workspace_default_store_init_failure_raises_actionable(monkeypatch, tmp_path):
    """`br init` fails and the db still doesn't exist → a named BoardError with the
    remedy (init by hand, or set db_path) — never a silent dead board."""
    dbfile = tmp_path / "project_board" / ".beads" / "beads.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(dbfile))
    monkeypatch.setattr(
        store.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="denied")
    )
    with pytest.raises(BoardError, match="instance board store"):
        _board(monkeypatch, db=str(dbfile))._ensure_workspace()


def test_ensure_workspace_default_store_tolerates_a_raced_init(monkeypatch, tmp_path):
    """Two per-project boards racing a fresh instance's first init: the loser's
    `br init` exits non-zero but the winner's db is present — no raise."""
    dbfile = tmp_path / "project_board" / ".beads" / "beads.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(dbfile))

    def _run(cmd, **kw):
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        dbfile.write_text("")  # the OTHER board's init won the race
        return types.SimpleNamespace(returncode=1, stdout="", stderr="already initialized")

    monkeypatch.setattr(store.subprocess, "run", _run)
    b = _board(monkeypatch, db=str(dbfile))
    b._ensure_workspace()
    assert b._workspace_ready


def test_ensure_workspace_explicit_db_is_never_bootstrapped(monkeypatch):
    """An operator's explicit db_path keeps the hard-pin contract even when the db
    file doesn't exist: nothing is created on their behalf (`br`'s own "run br init
    first" error names the remedy) — only the DEFAULTED store is bootstrapped."""
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    calls = []
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a) or _ok())
    b = _board(monkeypatch, db="/operator/custom/.beads/beads.db")
    b._ensure_workspace()
    assert calls == [] and b._workspace_ready


def test_fresh_instance_two_projects_share_one_store_and_never_init_a_repo(monkeypatch, tmp_path):
    """The #260 acceptance path end to end (fake br): a fresh instance with TWO
    projects and no db_path — both project stores carry --db <the one instance
    store> on every op, the single `br init` runs in the store root, and neither
    project repo ever grows a `.beads/`."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    dbfile = tmp_path / "instance" / "project_board" / ".beads" / "beads.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(dbfile))
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    runs = []

    def _run(cmd, **kw):
        runs.append((list(cmd), kw.get("cwd")))
        if cmd[1] == "init":
            dbfile.parent.mkdir(parents=True, exist_ok=True)
            dbfile.write_text("")
        return _ok()

    monkeypatch.setattr(store.subprocess, "run", _run)
    board_a = store.get_store(repo=str(repo_a))
    board_b = store.get_store(repo=str(repo_b))
    assert board_a is not board_b  # per-project boards (own repo for the Ready gate)…
    assert board_a.db == board_b.db == str(dbfile)  # …sharing the ONE instance store
    board_a._run("list", want_json=True)
    board_b._run("list", want_json=True)
    inits = [(cmd, cwd) for cmd, cwd in runs if cmd[:2] == [store.BR, "init"]]
    assert len(inits) == 1
    assert inits[0][1] == str(dbfile.parent.parent)  # cwd = the store root, not a repo
    lists = [(cmd, cwd) for cmd, cwd in runs if "list" in cmd]
    assert len(lists) == 2
    for cmd, cwd in lists:
        assert cmd[cmd.index("--db") + 1] == str(dbfile)  # every card lands in one store
    assert {cwd for _, cwd in lists} == {str(repo_a), str(repo_b)}  # per-project cwd kept
    assert not (repo_a / ".beads").exists() and not (repo_b / ".beads").exists()


def test_next_tier_walks_then_stops_at_the_top(make_board):
    b = make_board(Br())
    # Ladder: smart → reasoning → opus (fast dropped — protolabs/fast too weak).
    assert b.next_tier("smart") == "reasoning"
    assert b.next_tier("reasoning") == "opus"
    assert b.next_tier("opus") is None  # top of the ladder → caller blocks
    assert b.next_tier("nonsense") == store.TIER_LADDER[0]  # stale/unknown tier → floor (smart)
    assert b.next_tier("fast") == store.TIER_LADDER[0]  # a now-removed tier falls back to the floor


# ── coder.solve() cost accounting (ADR 0064 P2 board seam) ──────────────────────


def test_record_gens_spent_adds_a_fresh_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "gens_spent": 0, "labels": []})
    b.record_gens_spent("bd-1", 3)
    assert ("update", "bd-1", "--add-label", "gens:3") in br.calls


def test_record_gens_spent_accumulates_and_replaces_the_old_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "gens_spent": 5, "labels": ["gens:5", "ready"]})
    b.record_gens_spent("bd-1", 4)
    # the stale gens:5 label is removed and replaced by the new cumulative total
    assert ("update", "bd-1", "--remove-label", "gens:5", "--add-label", "gens:9") in br.calls


# ── loop fix-budget persistence (#259) ──────────────────────────────────────────


def test_record_budget_adds_a_fresh_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["ready"]})
    b.record_budget("bd-1", "ci-fix", 1)
    assert ("update", "bd-1", "--add-label", "budget:ci-fix:1") in br.calls


def test_record_budget_replaces_only_its_own_kind(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "labels": ["budget:ci-fix:1", "budget:rebase:2", "ready"]}
    )
    b.record_budget("bd-1", "ci-fix", 2)
    # the stale ci-fix count is replaced (the gens: pattern) — never two labels of one
    # kind at once; another kind's budget label is untouched
    assert ("update", "bd-1", "--remove-label", "budget:ci-fix:1", "--add-label", "budget:ci-fix:2") in br.calls


def test_clear_budgets_drops_named_kinds_or_all(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    labels = ["budget:ci-fix:2", "budget:goal-fix:1", "budget:rebase:1", "ready"]
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": list(labels)})
    b.clear_budgets("bd-1", ["ci-fix"])  # a tier climb resets only its per-tier kinds
    assert ("update", "bd-1", "--remove-label", "budget:ci-fix:2") in br.calls
    b.clear_budgets("bd-1")  # the merge edge resets EVERY budget label
    assert (
        "update",
        "bd-1",
        "--remove-label",
        "budget:ci-fix:2",
        "--remove-label",
        "budget:goal-fix:1",
        "--remove-label",
        "budget:rebase:1",
    ) in br.calls


def test_clear_budgets_noops_without_matching_labels(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["ready", "gens:3"]})
    b.clear_budgets("bd-1")
    b.clear_budgets("bd-1", ["ci-fix"])
    assert not br.cmds("update")  # nothing to drop → no br write burned


def test_budgets_from_labels_parses_and_ignores_junk():
    assert store.budgets_from_labels(["budget:ci-fix:2", "budget:merged-verify:11", "ready"]) == {
        "ci-fix": 2,
        "merged-verify": 11,
    }
    # malformed (no kind / non-numeric count / no colon) and non-budget labels → ignored
    assert store.budgets_from_labels(["budget::3", "budget:ci-fix:x", "budget:bare", "gens:3"]) == {}
    assert store.budgets_from_labels(None) == {}


def test_project_exposes_budgets_from_labels(make_board):
    b = make_board(Br())
    f = b._project({"id": "bd-1", "status": "open", "labels": ["budget:ci-fix:2", "budget:review-fix:1", "gens:3"]})
    assert f["budgets"] == {"ci-fix": 2, "review-fix": 1}
    assert b._project({"id": "bd-2", "status": "open", "labels": []})["budgets"] == {}


# ── operator-notified markers (#341) — the durable half of the blocked-lane dedup ─


def test_record_notified_adds_the_marker_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": ["notified:blocked"]})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked", "blocked-class:auth"]})
    b.record_notified("bd-1", "blocked")
    assert ("update", "bd-1", "--add-label", "notified:blocked") in br.calls


def test_record_notified_defaults_the_kind_to_blocked(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked"]})
    b.record_notified("bd-1", "")
    assert ("update", "bd-1", "--add-label", "notified:blocked") in br.calls


def test_record_notified_is_idempotent_when_already_present(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": ["notified:blocked"]})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked", "notified:blocked"]})
    b.record_notified("bd-1", "blocked")
    assert not br.cmds("update")  # marker already there → no self-cancelling re-add


def test_clear_notified_drops_the_marker(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked", "notified:blocked"]})
    b.clear_notified("bd-1")
    assert ("update", "bd-1", "--remove-label", "notified:blocked") in br.calls


def test_clear_notified_noops_without_a_marker(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked", "ready"]})
    b.clear_notified("bd-1")
    assert not br.cmds("update")  # nothing to drop → no br write burned


def test_clear_blocked_supersedes_the_notified_marker(make_board, monkeypatch):
    """A genuine unblock is THE recovery edge: it drops the `notified:blocked` marker in
    the SAME update as the block flag, so a LATER distinct block can alert again (#341)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "labels": ["blocked", "notified:blocked", "tier:opus"]},
    )
    b.clear_blocked("bd-1")
    (up,) = br.cmds("update")
    assert "--remove-label" in up and "blocked" in up and "notified:blocked" in up
    assert "tier:opus" not in up  # an earned rung is untouched


def test_notified_from_labels_parses_and_ignores_junk():
    assert store.notified_from_labels(["notified:blocked", "ready", "budget:ci-fix:2"]) == {"blocked"}
    assert store.notified_from_labels(["notified:", "blocked"]) == set()  # empty kind / non-marker → ignored
    assert store.notified_from_labels(None) == set()


def test_project_exposes_notified_markers(make_board):
    b = make_board(Br())
    f = b._project({"id": "bd-1", "status": "open", "labels": ["notified:blocked", "gens:3"]})
    assert f["notified"] == ["blocked"]
    assert b._project({"id": "bd-2", "status": "open", "labels": []})["notified"] == []


# ── merged-verify budget operator reset (ADR 0326, #326) ─────────────────────────


def _reset_comments(br):
    """The `comments add` calls the reset recorded (fid, text pairs)."""
    return [(a[2], a[3]) for a in br.calls if a[:2] == ("comments", "add")]


def test_reset_merged_verify_budget_clears_only_that_kind_and_audits(make_board, monkeypatch):
    """AC6: the reset drops ONLY the merged-verify budget label — another kind's budget
    is untouched — and records an auditable comment naming who and the prior value."""
    br = Br()
    b = make_board(br)
    labels = ["budget:merged-verify:6", "budget:ci-fix:2", "ready"]
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": list(labels)})
    monkeypatch.setattr(
        b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review", "labels": ["budget:ci-fix:2", "ready"]}
    )
    b.reset_merged_verify_budget("bd-1", actor="alice")
    # only the merged-verify budget label is removed; ci-fix is left alone
    assert ("update", "bd-1", "--remove-label", "budget:merged-verify:6") in br.calls
    assert not any("budget:ci-fix:2" in a for a in br.cmds("update"))
    comments = _reset_comments(br)
    assert len(comments) == 1
    fid, text = comments[0]
    assert fid == "bd-1" and "reset by alice" in text and "was 6" in text and "#326" in text


def test_reset_merged_verify_budget_audits_an_already_clear_budget(make_board, monkeypatch):
    """The reset is a supported operator action even when nothing was set: it records the
    request (audit trail) with `was unset` and the default actor, and burns no `br update`
    (clear_budgets no-ops without a matching label)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["ready"]})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review", "labels": ["ready"]})
    b.reset_merged_verify_budget("bd-1")
    assert not br.cmds("update")  # nothing to drop → no write burned
    comments = _reset_comments(br)
    assert len(comments) == 1
    fid, text = comments[0]
    assert fid == "bd-1" and "was unset" in text and "reset by agent" in text  # actor = store default


def test_reset_merged_verify_budget_unknown_id_alters_nothing(make_board, monkeypatch):
    """AC7: an unknown feature id raises and NOTHING is altered — no label removed, no
    audit comment for a phantom bead. (`_require` raises via get_feature → None.)"""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: None)
    with pytest.raises(BoardError):
        b.reset_merged_verify_budget("bd-nope")
    assert not br.cmds("update") and not _reset_comments(br)


def _reset_tool(cfg=None):
    return {t.name: t for t in pb._board_tools(cfg or {})}["board_reset_merged_verify_budget"]


def test_board_reset_merged_verify_budget_tool_clears_label_and_cache(monkeypatch):
    """AC6 end-to-end: the tool clears the persisted label (store) AND invalidates the
    running loop's in-process budget (loop) for the one feature, returning a structured ok
    — a reset that takes effect on the next reconcile without a host restart. The loop half
    PINS the count to 0 (the #259 rule, ADR 0326) and re-clears the label under its reset
    lock so an in-flight reconcile can't leave the card held."""
    from project_board import loop as loop_mod

    class _FakeStore:
        def __init__(self):
            self.reset = []
            self.cleared = []

        def reset_merged_verify_budget(self, fid, actor=""):
            self.reset.append(fid)
            return {"id": fid, "board_state": "in_review"}

        def clear_budgets(self, fid, kinds=None):
            self.cleared.append((fid, tuple(kinds) if kinds is not None else None))
            return {"id": fid}

    fake = _FakeStore()
    monkeypatch.setattr(store, "get_store", lambda **_kw: fake)
    loop = loop_mod.BoardLoop({})
    loop._merged_verify_attempts["bd-1"] = 6
    slot = loop_mod._loop_slot()
    prior = slot.loop
    slot.loop = loop
    try:
        out = json.loads(_reset_tool().invoke({"feature_id": "bd-1"}))
    finally:
        slot.loop = prior
    assert fake.reset == ["bd-1"]  # the persisted label half ran
    assert fake.cleared == [("bd-1", ("merged-verify",))]  # loop re-cleared the label under its lock
    assert out["id"] == "bd-1" and out["merged_verify_budget_reset"] is True
    assert out["cache_cleared"] is True and loop._merged_verify_attempts["bd-1"] == 0  # pinned to 0


def test_board_reset_merged_verify_budget_tool_rejects_a_blank_id(monkeypatch):
    """AC7: a blank feature_id alters nothing and returns an error — the store is never
    even reached (no phantom write)."""
    reached = []
    monkeypatch.setattr(store, "get_store", lambda **_kw: reached.append(1) or object())
    out = _reset_tool().invoke({"feature_id": "   "})
    assert out.startswith("Error:") and reached == []


def test_board_reset_merged_verify_budget_tool_reports_an_unknown_id(monkeypatch):
    """AC7: an unknown id surfaces the store's BoardError as a tool Error and — because the
    store raises first — the loop cache is never touched."""
    from project_board import loop as loop_mod

    class _MissingStore:
        def reset_merged_verify_budget(self, fid, actor=""):
            raise BoardError(f"unknown feature {fid!r}")

    monkeypatch.setattr(store, "get_store", lambda **_kw: _MissingStore())
    loop = loop_mod.BoardLoop({})
    loop._merged_verify_attempts["bd-x"] = 6
    slot = loop_mod._loop_slot()
    prior = slot.loop
    slot.loop = loop
    try:
        out = _reset_tool().invoke({"feature_id": "bd-x"})
    finally:
        slot.loop = prior
    assert out.startswith("Error:")
    assert loop._merged_verify_attempts.get("bd-x") == 6  # cache untouched — store raised first


# ── verified-candidate salvage record (#91) ─────────────────────────────────────


def test_record_verified_candidate_replaces_the_label_and_comments(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["verified:old5ha", "ready"]})
    b.record_verified_candidate("bd-1", branch="feat/bd-1", sha="abc123", worktree="/wt/feat-bd-1")
    # single replaced label (the gens: pattern) — never two verified: labels at once
    assert ("update", "bd-1", "--remove-label", "verified:old5ha", "--add-label", "verified:abc123") in br.calls
    # the full triple rides a comment for the audit trail
    comment = next(a for a in br.calls if a[0] == "comments")
    assert "branch=feat/bd-1" in comment[3] and "sha=abc123" in comment[3] and "worktree=/wt/feat-bd-1" in comment[3]


def test_clear_verified_candidate_drops_the_label_and_noops_without_one(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["verified:abc123", "ready"]})
    b.clear_verified_candidate("bd-1")
    assert ("update", "bd-1", "--remove-label", "verified:abc123") in br.calls
    br2 = Br()
    b2 = make_board(br2)
    monkeypatch.setattr(b2, "_require", lambda fid: {"id": fid, "labels": ["ready"]})
    b2.clear_verified_candidate("bd-1")
    assert not br2.cmds("update")  # nothing to drop → no br write


def test_project_exposes_verified_sha(make_board):
    b = make_board(Br())
    assert b._project({"id": "x", "status": "in_progress", "labels": ["verified:abc123"]})["verified_sha"] == "abc123"
    assert b._project({"id": "y", "status": "open", "labels": []})["verified_sha"] == ""


# ── #338: single-label replacement is safe against br's arg ordering ─────────────
#
# `br` applies --remove-label AFTER --add-label within one update, so the pre-#338
# hand-written "remove every same-prefix label, then add the desired one" loop
# SELF-CANCELS when the value is unchanged: it emits both --remove-label X and
# --add-label X, and X is dropped. `replace_prefixed_label_args` removes ONLY
# same-prefix labels that DIFFER, so an unchanged re-stamp keeps exactly one copy.
# Every unchanged-value assertion below is red against the previous implementation.


def _update_call(br):
    """The single `br update` call an edge issued (fid + label flags), or None."""
    return next((a for a in br.calls if a and a[0] == "update"), None)


def test_replace_prefixed_label_args_leaves_unchanged_value_untouched():
    # same value → no self-cancelling remove; just the idempotent add (the #338 fix)
    assert store.replace_prefixed_label_args(["gens:9", "ready"], "gens:", "gens:9") == ["--add-label", "gens:9"]


def test_replace_prefixed_label_args_removes_only_differing_same_prefix_labels():
    # a changed value drops the stale label and lands the new one; other prefixes stay
    assert store.replace_prefixed_label_args(["gens:5", "ready", "diff:small"], "gens:", "gens:9") == [
        "--remove-label",
        "gens:5",
        "--add-label",
        "gens:9",
    ]


def test_replace_prefixed_label_args_fresh_label_just_adds():
    assert store.replace_prefixed_label_args([], "verified:", "verified:abc") == ["--add-label", "verified:abc"]
    assert store.replace_prefixed_label_args(None, "diff:", "diff:small") == ["--add-label", "diff:small"]


def test_record_gens_spent_unchanged_total_keeps_one_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    # adding 0 gens re-stamps the SAME gens:9 — the pre-#338 loop dropped it (remove+add)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "gens_spent": 9, "labels": ["gens:9", "ready"]})
    b.record_gens_spent("bd-1", 0)
    assert _update_call(br) == ("update", "bd-1", "--add-label", "gens:9")


def test_record_verified_candidate_unchanged_sha_keeps_one_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["verified:abc123", "ready"]})
    b.record_verified_candidate("bd-1", branch="feat/bd-1", sha="abc123", worktree="/wt/feat-bd-1")
    assert _update_call(br) == ("update", "bd-1", "--add-label", "verified:abc123")


def test_record_merged_verified_unchanged_sha_preserves_its_record(make_board, monkeypatch):
    """#338 headline: a quiet-base re-verify re-stamps the SAME merged-verified:<sha>.
    The pre-#338 loop emitted --remove-label AND --add-label for it (br drops the label
    → the record the auto-merge edge reads is gone). Now exactly one copy survives."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["merged-verified:base9", "ready"]})
    b.record_merged_verified("bd-1", "base9")
    assert _update_call(br) == ("update", "bd-1", "--add-label", "merged-verified:base9")


def test_record_merged_verified_replaces_a_changed_sha(make_board, monkeypatch):
    # value-changing transition (#338 r2): the stale sha is removed, the new one added
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["merged-verified:old", "ready"]})
    b.record_merged_verified("bd-1", "new1")
    assert _update_call(br) == (
        "update",
        "bd-1",
        "--remove-label",
        "merged-verified:old",
        "--add-label",
        "merged-verified:new1",
    )


def test_update_feature_unchanged_diff_keeps_one_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": ["diff:small", "ready"]})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["diff:small", "ready"]})
    b.update_feature("bd-1", difficulty="small")  # re-stamps the SAME diff:small
    assert _update_call(br) == ("update", "bd-1", "--add-label", "diff:small")


def test_update_feature_replaces_a_changed_diff(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": ["diff:small", "ready"]})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["diff:small", "ready"]})
    b.update_feature("bd-1", difficulty="medium")
    assert _update_call(br) == ("update", "bd-1", "--remove-label", "diff:small", "--add-label", "diff:medium")


def test_record_budget_unchanged_count_keeps_one_label(make_board, monkeypatch):
    # the fifth site the shared helper closes: a re-stamp of the same budget count must
    # not self-cancel, and another kind's budget stays untouched
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "labels": ["budget:ci-fix:2", "budget:rebase:1", "ready"]}
    )
    b.record_budget("bd-1", "ci-fix", 2)
    assert _update_call(br) == ("update", "bd-1", "--add-label", "budget:ci-fix:2")


def test_set_review_substate_swaps_siblings_never_self_cancels(make_board, monkeypatch):
    """#338 r4: `set_review_substate` keeps its own (correct) multi-label semantics —
    it removes only the SIBLING gate labels that differ and adds the desired one, so a
    re-stamp of the already-present label never emits a self-cancelling remove for it."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["review-clean", "ready"]})
    # re-stamp the label that is ALREADY present: the two siblings drop, review-clean stays
    b.set_review_substate("bd-1", "review-clean")
    call = _update_call(br)
    assert call == (
        "update",
        "bd-1",
        "--remove-label",
        "review-pending",
        "--remove-label",
        "changes-requested",
        "--add-label",
        "review-clean",
    )


def test_set_review_substate_none_clears_all_three(make_board, monkeypatch):
    # a None label clears every gate sub-state and adds nothing (the requeue reset)
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["review-pending"]})
    b.set_review_substate("bd-1", None)
    assert _update_call(br) == (
        "update",
        "bd-1",
        "--remove-label",
        "review-pending",
        "--remove-label",
        "changes-requested",
        "--remove-label",
        "review-clean",
    )


# ── _project: the bead → feature view mapping ───────────────────────────────────


def test_project_maps_labels_notes_and_external_ref(make_board):
    b = make_board(Br())
    bead = {
        "id": "bd-1",
        "title": "T",
        "status": "open",
        "labels": ["ready", "diff:medium", "attempt:2", "attempt:1"],
        "description": "the spec",
        "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
        "notes": "a.py\n  b.py  \n\n",
        "external_ref": "https://example/pr/1",
    }
    f = b._project(bead)
    assert f["board_state"] == "ready"
    assert f["spec"] == "the spec"
    assert f["files_to_modify"] == ["a.py", "b.py"]  # split, stripped, blanks dropped
    assert f["difficulty"] == "medium"
    assert f["attempts"] == [1, 2]  # sorted ints
    assert f["pr_url"] == "https://example/pr/1"
    assert f["repo"] == "/repo" and f["base_branch"] == "main"
    assert f["gens_spent"] == 0  # no gens: label → coder.solve() never touched this feature


def test_project_exposes_gens_spent_from_the_label(make_board):
    b = make_board(Br())
    assert b._project({"id": "bd-2", "status": "open", "labels": ["gens:11"]})["gens_spent"] == 11


def test_project_marks_dag_blocked_when_a_blocks_dep_is_open(make_board):
    b = make_board(Br())
    bead = {
        "id": "bd-2",
        "status": "open",
        "labels": ["ready"],
        "dependencies": [{"dependency_type": "blocks", "status": "open"}],
    }
    assert b._project(bead)["dag_blocked"] is True
    bead["dependencies"] = [{"dependency_type": "blocks", "status": "closed"}]
    assert b._project(bead)["dag_blocked"] is False  # blocker merged → claimable


# ── invariant #1: the Ready gate ────────────────────────────────────────────────


def test_mark_ready_adds_the_label_when_fully_specced(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    ready_feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "do the thing",
        "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
        "files_to_modify": ["a.py (new)"],  # (new) bypasses the path-existence gate (#110)
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: ready_feature)
    b.mark_ready("bd-1")
    # adds `ready` (and clears a `designing` parking label in the same update)
    assert ("update", "bd-1", "--add-label", "ready", "--remove-label", "designing") in br.calls


@pytest.mark.parametrize(
    "missing,field",
    [
        ({"spec": ""}, "spec"),
        ({"acceptance_criteria": ""}, "acceptance_criteria"),
        ({"files_to_modify": []}, "files_to_modify"),
    ],
)
def test_mark_ready_rejects_an_underspecced_feature(make_board, monkeypatch, missing, field):
    br = Br()
    b = make_board(br)
    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": ["a.py"],
        **missing,
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)
    with pytest.raises(BoardError, match=field):
        b.mark_ready("bd-1")
    assert br.cmds("update") == []  # nothing mutated on a rejected gate


def test_mark_ready_gate_validates_files_to_modify_exist_in_the_repo(make_board, monkeypatch, tmp_path):
    """#110: a files_to_modify path must resolve in the bound checkout (or be a `(new)`
    stub) — a phantom path is invisible until a coder burns a run chasing it."""
    br = Br()
    b = make_board(br, repo=str(tmp_path))
    real = tmp_path / "real.py"
    real.write_text("x = 1\n")
    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": ["real.py"],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)

    b.mark_ready("bd-1")  # the path exists → gate passes
    assert br.cmds("update")

    br.calls.clear()
    real.unlink()  # same path, now missing → gate refuses and names it
    with pytest.raises(BoardError) as exc_info:
        b.mark_ready("bd-1")
    err = str(exc_info.value)
    assert "do not exist in the repo" in err
    assert "real.py" in err
    assert str(tmp_path) in err  # bound repo root is named (#141)
    assert "project_board.repo" in err  # config key is named (#141)
    assert "fix the repo binding" in err  # hint is present (#141)
    assert br.cmds("update") == []  # nothing mutated on a rejected gate

    # a `(new)` marker (case-insensitive, anywhere in the entry) bypasses the check
    br.calls.clear()
    feature["files_to_modify"] = ["real.py", "docs/new-guide.md (NEW)"]
    with pytest.raises(BoardError, match=r"do not exist in the repo.*real\.py"):
        b.mark_ready("bd-1")  # real.py still phantom, but the (NEW) entry is exempt
    feature["files_to_modify"] = ["real.py (new)", "docs/new-guide.md (NEW)"]
    b.mark_ready("bd-1")  # every remaining entry is a (new) stub → gate passes
    assert br.cmds("update")


# ── the DESIGN gate (plan M6): large/architectural needs design + ADR ref ───────


def _design_feature(**over):
    base = {
        "id": "bd-9",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": ["a.py (new)"],  # (new) bypasses the path-existence gate (#110)
        "difficulty": "large",
        "design": "",
    }
    base.update(over)
    return base


def test_design_gate_rejects_large_feature_with_no_design(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _design_feature())
    with pytest.raises(BoardError, match="Design gate.*no\\s+`design`"):
        b.mark_ready("bd-9")
    assert br.cmds("update") == []


def test_design_gate_rejects_a_design_without_an_adr_reference(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "get_feature", lambda fid: _design_feature(difficulty="architectural", design="we will use a queue")
    )
    with pytest.raises(BoardError, match="references no ADR"):
        b.mark_ready("bd-9")
    assert br.cmds("update") == []


@pytest.mark.parametrize(
    "design",
    [
        "Per ADR 0077, findings gate the merge edge.",
        "see adr-0064 for the ladder",
        "decision recorded in docs/adr/0076-managed-git-acp-delegates.md",
        "ADR/0055 isolation applies",
    ],
)
def test_design_gate_accepts_designs_citing_an_adr(make_board, monkeypatch, design):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _design_feature(design=design))
    b.mark_ready("bd-9")
    assert br.cmds("update")  # gate passed → the ready label update ran


def test_design_gate_ignores_small_and_medium_features(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _design_feature(difficulty="medium"))
    b.mark_ready("bd-9")  # no design, but medium → gate not applied
    assert br.cmds("update")


# ── the BREADTH cap: small/medium cards may not exceed the files_to_modify cap (#143) ──


def _breadth_feature(**over):
    base = {
        "id": "bd-8",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        # 8 paths, all `(new)` so the phantom-path gate is bypassed — the breadth cap is
        # what's under test, not path existence.
        "files_to_modify": [f"f{i}.py (new)" for i in range(8)],
        "difficulty": "medium",
        "design": "",
        "depends_on": [],
    }
    base.update(over)
    return base


def test_breadth_gate_refuses_an_oversized_medium_card(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _breadth_feature())
    with pytest.raises(BoardError) as exc_info:
        b.mark_ready("bd-8")
    err = str(exc_info.value)
    assert "Breadth gate" in err
    assert "8" in err  # the count
    assert "cap of 4" in err  # the cap
    assert "SPLIT" in err  # remedy 1: split into smaller cards
    assert "`large`" in err  # remedy 2: re-declare large
    assert br.cmds("update") == []  # nothing mutated on a rejected gate


def test_breadth_gate_exempts_a_large_card_with_a_design(make_board, monkeypatch):
    """The SAME 8-path card, re-declared `large` with a design citing an ADR, passes:
    large/architectural carry no breadth cap — they answer to the DESIGN gate instead."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "get_feature", lambda fid: _breadth_feature(difficulty="large", design="Per ADR 0143, this is one unit.")
    )
    b.mark_ready("bd-8")
    assert ("update", "bd-8", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_breadth_exempt_large_card_still_answers_to_the_design_gate(make_board, monkeypatch):
    """large is exempt from the breadth cap but NOT from the design gate — an 8-path large
    card with no design is still refused (by the design gate, not the breadth gate)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _breadth_feature(difficulty="large", design=""))
    with pytest.raises(BoardError, match="Design gate"):
        b.mark_ready("bd-8")
    assert br.cmds("update") == []


def test_breadth_cap_is_configurable_via_max_files_by_difficulty(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    b.max_files_by_difficulty = {"small": 2, "medium": 2}  # tighter than the built-in default of 4
    three_small = _breadth_feature(difficulty="small", files_to_modify=["a.py (new)", "b.py (new)", "c.py (new)"])
    monkeypatch.setattr(b, "get_feature", lambda fid: three_small)
    with pytest.raises(BoardError, match="cap of 2"):
        b.mark_ready("bd-8")
    # the built-in default (4) would have admitted the same 3-file card
    b.max_files_by_difficulty = dict(store.MAX_FILES_BY_DIFFICULTY)
    b.mark_ready("bd-8")
    assert br.cmds("update")


def test_max_files_by_difficulty_kwarg_threads_the_config_key(monkeypatch):
    """The `max_files_by_difficulty` config key reaches the board as a constructor kwarg
    (via store_kw); a None override (key absent) falls back to the built-in default,
    copied so a caller's dict can't mutate the policy under us."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    tuned = store.BeadsBoard(db=None, repo="/repo", max_files_by_difficulty={"small": 1, "medium": 9})
    assert tuned.max_files_by_difficulty == {"small": 1, "medium": 9}
    default = store.BeadsBoard(db=None, repo="/repo", max_files_by_difficulty=None)
    assert default.max_files_by_difficulty == store.MAX_FILES_BY_DIFFICULTY
    assert default.max_files_by_difficulty is not store.MAX_FILES_BY_DIFFICULTY  # copied, not aliased


# ── the SHARED-FILE overlap gate: two non-terminal cards + same file need depends_on (#143) ──


def _shared_feature(fid, files, **over):
    base = {
        "id": fid,
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": files,
        "difficulty": "medium",
        "design": "",
        "depends_on": [],
    }
    base.update(over)
    return base


def test_shared_file_gate_refuses_a_second_card_naming_the_same_file(make_board, monkeypatch):
    """bd-i0w and bd-tw5 both name loop.py with no dependency edge — the second is refused
    at ready, naming the overlapping file and the other card id (#143)."""
    br = Br()
    b = make_board(br)
    first = _shared_feature("bd-i0w", ["loop.py (new)"], board_state="in_progress")
    second = _shared_feature("bd-tw5", ["loop.py (new)"])
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [first, second])
    with pytest.raises(BoardError) as exc_info:
        b.mark_ready("bd-tw5")
    err = str(exc_info.value)
    assert "Shared-file gate" in err
    assert "loop.py (new)" in err  # the overlapping path is named
    assert "bd-i0w" in err  # the other card id is named
    assert "depends_on" in err  # the remedy is suggested
    assert br.cmds("update") == []  # nothing mutated on a rejected gate


def test_shared_file_gate_passes_with_a_depends_on_edge(make_board, monkeypatch):
    """Adding a depends_on edge (second → first) orders the two cards, so the overlap is
    intentional and the gate does not fire."""
    br = Br()
    b = make_board(br)
    first = _shared_feature("bd-i0w", ["loop.py (new)"], board_state="in_progress")
    second = _shared_feature("bd-tw5", ["loop.py (new)"], depends_on=["bd-i0w"])
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [first, second])
    b.mark_ready("bd-tw5")
    assert ("update", "bd-tw5", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_shared_file_gate_passes_with_a_reverse_depends_on_edge(make_board, monkeypatch):
    """The edge counts in EITHER direction: here the first card depends on the second, and
    the gate still treats the pair as ordered."""
    br = Br()
    b = make_board(br)
    first = _shared_feature("bd-i0w", ["loop.py (new)"], board_state="in_progress", depends_on=["bd-tw5"])
    second = _shared_feature("bd-tw5", ["loop.py (new)"])
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [first, second])
    b.mark_ready("bd-tw5")
    assert br.cmds("update")


def test_shared_file_gate_ignores_terminal_cards(make_board, monkeypatch):
    """A merged (done) or cancelled card no longer contends for the file, so its overlap
    with the card going ready is not a conflict."""
    br = Br()
    b = make_board(br)
    done = _shared_feature("bd-old", ["loop.py (new)"], board_state="done")
    cancelled = _shared_feature("bd-cxl", ["loop.py (new)"], board_state="cancelled")
    second = _shared_feature("bd-tw5", ["loop.py (new)"])
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [done, cancelled, second])
    b.mark_ready("bd-tw5")
    assert br.cmds("update")


def test_mark_designing_parks_and_mark_ready_unparks(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: _design_feature())
    b.mark_designing("bd-9", note="running due diligence")
    assert ("update", "bd-9", "--add-label", "designing", "--remove-label", "ready") in br.calls


def test_mark_designing_rejects_in_flight_features(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_progress"})
    with pytest.raises(BoardError, match="can't mark designing"):
        b.mark_designing("bd-9")


# ── cancel_feature: the second terminal edge (#47) ──────────────────────────────


def test_cancel_feature_tags_cancelled_and_closes_with_reason(make_board, monkeypatch):
    """Tag `cancelled` + clear the assignee, then close with an audit reason — so the
    projection reads `cancelled` (distinct from `done`), audit-preserved (not deleted)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "cancelled", "cancelled": True})
    f = b.cancel_feature("bd-9", "duplicate")
    update = next(c for c in br.calls if c[0] == "update")
    assert "--add-label" in update and "cancelled" in update and "--assignee" in update
    close = next(c for c in br.calls if c[0] == "close")
    assert close[:2] == ("close", "bd-9") and "cancelled: duplicate" in close
    assert f["board_state"] == "cancelled" and f["cancelled"] is True


@pytest.mark.parametrize(
    "reason",
    [
        "source issue 2267: retired and replaced by 2282",  # a colon
        "scope cut — folded into bd-42",  # an em dash
    ],
)
def test_cancel_feature_passes_a_punctuated_reason_through_intact(make_board, monkeypatch, reason):
    """The counterexample (#106): a colon/em-dash reason is handed to `br close -r`
    verbatim and — when close succeeds — the route stays a clean single cancel (no undo)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "cancelled", "cancelled": True})
    b.cancel_feature("bd-9", reason)
    close = next(c for c in br.calls if c[0] == "close")
    assert close == ("close", "bd-9", "-r", f"cancelled: {reason}")
    # a successful close leaves NO compensating write — the `cancelled` tag stands.
    assert not any(c[0] == "update" and "--remove-label" in c for c in br.calls)


def _raise_on_close(args):
    # Mimic `br close -r` rejecting the reason (#106): a punctuated reason `br` couldn't
    # parse surfaced as a non-zero exit → BoardError, mid-way through the two-write route.
    raise BoardError("`br close bd-9 -r ...` failed: unparseable reason")


def test_cancel_feature_rolls_back_when_close_fails_leaving_no_zombie(make_board, monkeypatch):
    """Atomic-or-clean (#106): if `br close` fails after the `cancelled` tag + unassign
    landed, the route undoes BOTH — remove the label, restore the prior assignee — and
    re-raises, so the feature is never stranded OPEN + `ready` + `cancelled` (a claimable
    zombie), but back in its exact pre-cancel state."""
    br = Br(returns={"close": _raise_on_close})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "assignee": "alice", "labels": ["ready"]})
    with pytest.raises(BoardError, match="failed"):
        b.cancel_feature("bd-9", "source issue 2267: retired and replaced by 2282")
    # the tag/unassign write went out first…
    assert ("update", "bd-9", "--add-label", "cancelled", "--assignee", "") in br.calls
    # …then close blew up, so the compensating write undoes the tag AND restores the assignee.
    assert ("update", "bd-9", "--remove-label", "cancelled", "--assignee", "alice") in br.calls
    # the `ready` label is never touched by cancel, so the rollback needn't re-add it —
    # the pre-cancel state is preserved without a spurious ready write.
    assert not any(c[0] == "update" and "--add-label" in c and "ready" in c for c in br.calls)


def test_cancel_feature_rollback_skips_assignee_restore_when_unassigned(make_board, monkeypatch):
    """When the feature had no assignee, the rollback drops just the `cancelled` label —
    no redundant `--assignee ""` write to re-clear an already-empty assignee."""
    br = Br(returns={"close": _raise_on_close})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "assignee": "", "labels": ["ready"]})
    with pytest.raises(BoardError):
        b.cancel_feature("bd-9", "scope cut — folded into bd-42")
    assert ("update", "bd-9", "--remove-label", "cancelled") in br.calls
    # no assignee token in any undo write (there was nothing to restore).
    undo = next(c for c in br.calls if c[0] == "update" and "--remove-label" in c)
    assert "--assignee" not in undo


def test_cancel_feature_unknown_id_raises(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: None)
    with pytest.raises(BoardError, match="unknown feature"):
        b.cancel_feature("nope")


def test_cancel_feature_with_open_deps_drops_edges_and_reports_them(make_board, monkeypatch):
    """Cancel drops open incoming `blocks` edges before calling `br close` (#145), so a
    scope-cut succeeds even when the feature's prerequisites are still unfinished.
    The response carries `dropped_deps` listing the edges that were removed."""
    # _open_blockers calls `br show`; two open blockers gate this feature.
    bead_with_deps = {
        "id": "bd-9",
        "dependencies": [
            {"id": "bd-1", "dependency_type": "blocks", "status": "open"},
            {"id": "bd-2", "dependency_type": "blocks", "status": "in_progress"},
        ],
    }
    dep_calls = []

    def run_impl(*args, want_json=False):
        dep_calls.append(args)
        if args[0] == "show":
            return [bead_with_deps]
        return [] if want_json else ""

    br = run_impl  # use the callable directly (make_board accepts any callable)
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "cancelled", "cancelled": True})
    f = b.cancel_feature("bd-9", "scope cut")
    # both `dep remove` calls were issued — one per blocker
    # tuple layout: ("dep", "remove", fid, blocker_id, "--type", "blocks")
    dep_removes = [c for c in dep_calls if c[0] == "dep" and c[1] == "remove"]
    removed_ids = {c[3] for c in dep_removes}
    assert removed_ids == {"bd-1", "bd-2"}
    # the feature was closed after the edges were dropped
    assert any(c[0] == "close" and "bd-9" in c for c in dep_calls)
    # the response reports which edges were dropped
    assert set(f.get("dropped_deps", [])) == {"bd-1", "bd-2"}


def _blocked_then_cancelled():
    """A stateful ``get_feature``: the blocked source projection on the FIRST read
    (what cancel inspects for the `blocked` label + a cleared assignee — exactly what
    flag_blocked leaves), then the terminal cancelled projection with `blocked` cleared
    on every later read (what the fix produces)."""
    calls = {"n": 0}

    def _gf(fid):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": fid, "board_state": "blocked", "assignee": "", "labels": ["blocked"]}
        return {"id": fid, "board_state": "cancelled", "cancelled": True, "blocked": False}

    return _gf


def test_cancel_feature_clears_the_blocked_label_before_close(make_board, monkeypatch):
    """#325: cancelling a card blocked through the normal board path drops the `blocked`
    label alongside the `cancelled` tag (one atomic `br update`) — a terminal card can't
    remain a live blocker, the same invariant record_merge / mark_done enforce. The
    projection reads terminal `cancelled` with blocked cleared, and the audit reason is
    preserved."""
    br = Br()  # `show` → [] : no open blocker EDGES gate this feature
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _blocked_then_cancelled())
    f = b.cancel_feature("bd-9", "scope cut")
    # the cancel tag write ALSO drops the blocked label — one atomic write, not two
    update = next(c for c in br.calls if c[0] == "update")
    assert update == ("update", "bd-9", "--add-label", "cancelled", "--assignee", "", "--remove-label", "blocked")
    # …then it closes with the auditable cancel reason (the `cancelled` tag stands)
    close = next(c for c in br.calls if c[0] == "close")
    assert close == ("close", "bd-9", "-r", "cancelled: scope cut")
    # terminal cancelled, and no longer reports blocked (the projection / count / sort)
    assert f["board_state"] == "cancelled" and f["cancelled"] is True and f["blocked"] is False


def test_cancel_feature_unblocked_does_not_touch_the_blocked_label(make_board, monkeypatch):
    """#325 regression guard: an UNBLOCKED cancel is unchanged — the tag write carries
    no `--remove-label` (byte-for-byte the prior single-label write), so a card that was
    never blocked takes no spurious blocked churn."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "cancelled", "cancelled": True})
    b.cancel_feature("bd-9", "duplicate")
    update = next(c for c in br.calls if c[0] == "update")
    assert update == ("update", "bd-9", "--add-label", "cancelled", "--assignee", "")
    # no `blocked` token in any update — the label is never added, removed, or churned.
    assert all("blocked" not in c for c in br.calls if c[0] == "update")


def test_cancel_feature_rollback_restores_the_blocked_label(make_board, monkeypatch):
    """#325 + #106: if `br close` fails after the blocked card was tagged `cancelled` and
    unblocked, the rollback re-adds `blocked` (and removes `cancelled`) so the card lands
    back in its exact pre-cancel state — blocked, never a half-cancelled, now-unblocked
    zombie."""
    br = Br(returns={"close": _raise_on_close})
    b = make_board(br)
    # flag_blocked clears the assignee, so a blocked card is unassigned pre-cancel.
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "assignee": "", "labels": ["blocked"]})
    with pytest.raises(BoardError, match="failed"):
        b.cancel_feature("bd-9", "scope cut — folded into bd-42")
    # forward: tag `cancelled` + drop `blocked` in one update…
    assert ("update", "bd-9", "--add-label", "cancelled", "--assignee", "", "--remove-label", "blocked") in br.calls
    # …then close blew up, so the compensating write undoes the tag AND re-adds `blocked`
    # (no assignee token — a blocked card was already unassigned, nothing to restore).
    assert ("update", "bd-9", "--remove-label", "cancelled", "--add-label", "blocked") in br.calls


def test_cancelled_card_projects_unblocked_even_if_the_label_lingered(make_board):
    """The projection is the enforcement point: a closed+cancelled bead reads terminal
    `cancelled` (status wins), and once the fix strips the `blocked` label the projected
    `blocked` flag is False — so a cancelled card drops out of the blocked count/sort.
    (A lingering label would still project blocked=True — the exact bug #325 fixes at the
    write side.)"""
    b = make_board(Br())
    proj = b._project({"id": "bd-9", "status": "closed", "labels": ["cancelled"]})
    assert proj["board_state"] == "cancelled" and proj["blocked"] is False


def test_remove_dependency_issues_dep_remove_command(make_board):
    """remove_dependency is the inverse of add_dependency: it calls `br dep remove
    <fid> <depends_on> --type blocks` to tear down the gate."""
    br = Br()
    b = make_board(br)
    b.remove_dependency("bd-child", "bd-parent")
    assert ("dep", "remove", "bd-child", "bd-parent", "--type", "blocks") in br.calls


def test_remove_dependency_retries_without_type_when_br_rejects_the_flag(make_board):
    """br dep remove dropped --type in newer builds — bare `<ISSUE> <DEPENDS_ON>`,
    no type disambiguator (unlike `add`, which still needs one to pick what kind of
    edge to CREATE). Confirmed empirically: a real `br dep remove --type blocks` on
    a locally-installed br 0.2.16 hard-errors ("unexpected argument '--type' found").
    remove_dependency retries once without the flag on that specific failure rather
    than assuming every br build accepts it the way `add` still does — this was the
    actual root cause of cancel_feature's (#145/#160) drop-open-blockers step
    silently no-op'ing: `_open_blockers` found the right edges, but every
    `remove_dependency` call died on `--type` and got swallowed by cancel_feature's
    own per-edge `except BoardError` (log a warning, keep going) — so `br close`
    still hit the un-dropped blocker and cancel still failed, looking exactly like
    a stale-code symptom when it was a br CLI-version mismatch."""
    calls = []

    def run_impl(*args, want_json=False):
        calls.append(args)
        if args == ("dep", "remove", "bd-child", "bd-parent", "--type", "blocks"):
            raise BoardError(
                "`br dep remove bd-child bd-parent --type blocks` failed: error: unexpected argument '--type' found"
            )
        return ""

    b = make_board(run_impl)
    b.remove_dependency("bd-child", "bd-parent")
    assert ("dep", "remove", "bd-child", "bd-parent", "--type", "blocks") in calls
    assert ("dep", "remove", "bd-child", "bd-parent") in calls


def test_remove_dependency_reraises_a_real_failure_unretried(make_board):
    """A genuine remove failure (e.g. the edge doesn't exist) must not be masked by
    the --type-compat retry — only that one specific CLI-parse signature falls back."""

    def run_impl(*args, want_json=False):
        raise BoardError("`br dep remove bd-child bd-parent --type blocks` failed: dependency not found")

    b = make_board(run_impl)
    with pytest.raises(BoardError, match="dependency not found"):
        b.remove_dependency("bd-child", "bd-parent")


def test_remove_dependency_clears_dag_blocked(make_board):
    """After removing a blocks edge the dependent is no longer dag_blocked.
    _project reads `dependencies` directly from the bead, so a bead with no
    remaining blocks edges produces dag_blocked=False."""
    b = make_board(Br())
    # Simulate the post-removal state: the bead has no open blocks edges left.
    bead_after_removal = {
        "id": "bd-child",
        "status": "open",
        "labels": ["ready"],
        "dependencies": [],  # edge was removed
    }
    projected = b._project(bead_after_removal)
    assert projected["dag_blocked"] is False
    assert projected["depends_on"] == []


def test_cancelled_blocker_does_not_block_dependent_forever(make_board):
    """When a blocker is cancelled, `br close` sets its status to `closed`.
    A dependent that sees a dep with status=closed must NOT be dag_blocked (#145)."""
    b = make_board(Br())
    # After `br close` the blocker's status reads "closed" in the dep record, regardless
    # of whether it was merged (done) or cancelled — _project must treat both as resolved.
    bead = {
        "id": "bd-child",
        "status": "open",
        "labels": ["ready"],
        "dependencies": [{"id": "bd-blocker", "dependency_type": "blocks", "status": "closed"}],
    }
    assert b._project(bead)["dag_blocked"] is False


def test_delete_feature_tombstones_with_reason(make_board, monkeypatch):
    """The harder sibling of cancel: `br delete` (tombstone) with an audit reason, run
    THROUGH the board so board↔JSONL stay in step. Returns the pre-delete snapshot."""
    br = Br()
    b = make_board(br)
    snapshot = {"id": "bd-9", "board_state": "backlog", "title": "oops"}
    monkeypatch.setattr(b, "get_feature", lambda fid: snapshot)
    f = b.delete_feature("bd-9", "duplicate")
    delete = next(c for c in br.calls if c[0] == "delete")
    assert delete[:2] == ("delete", "bd-9") and "--reason" in delete and "deleted: duplicate" in delete
    assert f == snapshot  # the API echoes what was removed


def test_delete_feature_unknown_id_raises(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: None)
    with pytest.raises(BoardError, match="unknown feature"):
        b.delete_feature("nope")


def test_mark_ready_rejects_a_feature_already_past_backlog(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_progress"})
    with pytest.raises(BoardError, match="can't mark ready"):
        b.mark_ready("bd-1")


# ── the puller: claim_next_ready ────────────────────────────────────────────────


def test_claim_next_ready_skips_non_features_and_blocked(make_board, monkeypatch):
    ready = [
        {"id": "bd-ep", "issue_type": "epic", "labels": ["ready"]},
        {"id": "bd-bl", "issue_type": "feature", "labels": ["ready", "blocked"]},
        {"id": "bd-ok", "issue_type": "feature", "labels": ["ready"]},
    ]
    br = Br({"ready": ready})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    claimed = b.claim_next_ready(assignee="proto")
    assert claimed["id"] == "bd-ok"
    assert ("update", "bd-ok", "--claim", "--remove-label", "ready") in br.calls
    assert ("update", "bd-ok", "--assignee", "proto") in br.calls


def test_claim_next_ready_returns_none_when_empty(make_board):
    b = make_board(Br({"ready": []}))
    assert b.claim_next_ready() is None


def test_ready_queue_projects_label_less_br_ready_rows_as_ready(make_board):
    """beads-rust ≤0.1.23: `br ready --json` returns rows WITHOUT a `labels` field.
    ready_queue must still project candidates as board_state='ready' (re-fetching via
    `br show`, which carries labels) — otherwise board_state() reads no `ready` label,
    returns 'backlog', and the puller's `board_state != "ready"` guard self-rejects
    every ready feature and the loop silently never claims. Regression for the live
    dogfood finding. On 0.1.x a SINGLE-id `br show --json` is a BARE DICT, not a
    one-row list — the batched re-fetch must fold it back (the list_features quirk)."""
    # What real `br ready --json` hands back: a feature with NO labels key.
    # The `br show` re-fetch IS label-bearing — project from it, not the bare ready row.
    br = Br(
        {
            "ready": [{"id": "bd-1", "title": "T", "status": "open", "issue_type": "feature"}],
            "show": {  # bare dict: the 0.1.x single-bead shape
                "id": "bd-1",
                "title": "T",
                "status": "open",
                "issue_type": "feature",
                "labels": ["ready", "diff:small"],
                "description": "spec",
                "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
            },
        }
    )
    b = make_board(br)
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-1"]
    assert q[0]["board_state"] == "ready"  # the bug projected this as "backlog"
    assert br.cmds("show") == [("show", "bd-1")]


def test_ready_queue_batches_the_label_refetch_into_one_show(make_board):
    """#257: the labels re-fetch is ONE `br show <id…>` for the WHOLE ready set —
    the queue is polled every loop tick, so the old per-bead get_feature was R+1
    subprocess spawns for R ready beads. Non-pullable types never make the show's
    argv, and `br ready`'s priority order survives the batch (the show's row order
    must NOT reorder the queue)."""
    ready = [
        {"id": "bd-1", "issue_type": "feature", "status": "open"},
        {"id": "bd-ep", "issue_type": "epic", "status": "open"},  # not pullable → not shown
        {"id": "bd-2", "issue_type": "task", "status": "open"},
    ]
    shows = [  # deliberately REVERSED vs `br ready`'s order
        {"id": "bd-2", "status": "open", "issue_type": "task", "labels": ["ready"]},
        {"id": "bd-1", "status": "open", "issue_type": "feature", "labels": ["ready"]},
    ]
    br = Br({"ready": ready, "show": shows})
    b = make_board(br)
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-1", "bd-2"]  # priority order, not the show's
    assert all(f["board_state"] == "ready" for f in q)
    assert br.cmds("show") == [("show", "bd-1", "bd-2")]  # exactly ONE batched show


def test_ready_queue_empty_ready_set_issues_no_show(make_board):
    """`br show` with no ids is an error — an empty ready set must short-circuit."""
    br = Br({"ready": []})
    b = make_board(br)
    assert b.ready_queue() == []
    assert br.cmds("show") == []


def test_ready_queue_falls_back_per_id_when_the_batched_show_404s(make_board):
    """The delete race the old per-bead get_feature folded to None: a candidate
    vanishing between `br ready` and the show makes the batched `br show` raise
    ISSUE_NOT_FOUND. The queue must not starve over one ghost — fall back to
    per-id fetches for the tick, skip the ghost, keep the survivors flowing."""

    def show(args):
        ids = args[1:]
        if len(ids) > 1 or ids == ("bd-gone",):
            raise store.BoardNotFound("`br show` failed: ISSUE_NOT_FOUND")
        return [{"id": ids[0], "status": "open", "issue_type": "feature", "labels": ["ready"]}]

    ready = [
        {"id": "bd-1", "issue_type": "feature", "status": "open"},
        {"id": "bd-gone", "issue_type": "feature", "status": "open"},
    ]
    b = make_board(Br({"ready": ready, "show": show}))
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-1"]  # ghost skipped, survivor projected
    assert q[0]["board_state"] == "ready"


def test_ready_queue_projects_label_carrying_ready_rows_without_a_refetch(make_board):
    """#324: a newer `br ready --json` carries `labels` on the ready row ITSELF, so
    the batched `br show` re-fetch the label-less (≤0.1.23) shape needs is REDUNDANT —
    those rows must project directly, skipping the extra subprocess spawn on the loop's
    hottest poll. Priority order and the label-derived fields (board_state, diff) come
    straight off the ready row. Red-reachable: dropping the capability check re-issues
    the show and this `br.cmds("show") == []` assertion FAILS."""
    ready = [
        {"id": "bd-1", "issue_type": "feature", "status": "open", "labels": ["ready", "diff:small"]},
        {"id": "bd-2", "issue_type": "task", "status": "open", "labels": ["ready"]},
        {"id": "bd-ep", "issue_type": "epic", "status": "open", "labels": ["ready"]},  # structural → excluded
    ]
    br = Br({"ready": ready})
    b = make_board(br)
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-1", "bd-2"]  # priority order, epic excluded
    assert all(f["board_state"] == "ready" for f in q)  # projected as ready off the row's labels
    assert q[0]["difficulty"] == "small"  # and the rest of the label-derived projection
    assert br.cmds("show") == []  # no redundant re-fetch — the whole point of #324


def test_ready_queue_refetches_only_the_label_less_rows_in_a_mixed_shape(make_board):
    """#324: a mixed ready set — one row carries `labels` (newer br), one omits it
    (≤0.1.23) — must re-fetch ONLY the label-less row, in a single batched `br show`
    over exactly that subset (never the label-carrying id), and still project BOTH as
    `ready` in `br ready`'s priority order. The label-carrying row is projected directly."""
    ready = [
        {"id": "bd-new", "issue_type": "feature", "status": "open", "labels": ["ready"]},
        {"id": "bd-old", "issue_type": "feature", "status": "open"},  # ≤0.1.23 shape: no labels key
    ]
    shows = [{"id": "bd-old", "status": "open", "issue_type": "feature", "labels": ["ready"]}]
    br = Br({"ready": ready, "show": shows})
    b = make_board(br)
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-new", "bd-old"]  # both ready, priority order preserved
    assert all(f["board_state"] == "ready" for f in q)
    assert br.cmds("show") == [("show", "bd-old")]  # only the label-less id re-fetched, once


def test_ready_queue_delete_race_in_the_refetch_still_flows_label_carrying_rows(make_board):
    """#324 + the delete race together: a label-CARRYING ready row must still project
    even when a sibling label-LESS row vanishes between `br ready` and its re-fetch (the
    batched show 404s → per-id fallback for the label-less set only). The label-carrying
    row never rode that show, so a ghost in the fallback can't starve it."""

    def show(args):
        raise store.BoardNotFound("`br show` failed: ISSUE_NOT_FOUND")

    ready = [
        {"id": "bd-new", "issue_type": "feature", "status": "open", "labels": ["ready"]},
        {"id": "bd-gone", "issue_type": "feature", "status": "open"},  # label-less, vanishes → 404
    ]
    b = make_board(Br({"ready": ready, "show": show}))
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-new"]  # ghost skipped, label-carrying survivor flows
    assert q[0]["board_state"] == "ready"


def test_claim_claims_a_specific_ready_feature(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready"})
    claimed = b.claim("bd-5", assignee="proto")
    assert claimed["id"] == "bd-5"
    assert ("update", "bd-5", "--claim", "--remove-label", "ready") in br.calls
    assert ("update", "bd-5", "--assignee", "proto") in br.calls


def test_claim_returns_none_when_not_ready(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_progress"})
    assert b.claim("bd-5") is None


def test_claim_returns_none_on_a_claim_race(make_board, monkeypatch):
    def run_impl(*args, want_json=False):
        if args and args[0] == "update" and "--claim" in args:
            raise BoardError("already assigned to agent")
        return [] if want_json else ""

    b = make_board(run_impl)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready"})
    assert b.claim("bd-5") is None  # br --claim rejected → lost the race


def test_flag_blocked_clears_the_assignee(make_board, monkeypatch):
    """A blocked feature is unassigned with the block so a later reset-to-ready can be
    re-claimed: `br update --claim` rejects an already-assigned bead, which was a SILENT
    no-claim trap (loop ticks forever, never claims, logs nothing) — the 2026-06-15 debug."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "boom")
    (up,) = br.cmds("update")
    assert "--add-label" in up and "blocked" in up
    assert "--assignee" in up and "" in up


def test_flag_blocked_keeps_a_task_assignee_because_it_is_the_dispatch_target(make_board, monkeypatch):
    """The #333 carve-out applies to the block edge too: on a task the assignee is the
    DISPATCH TARGET, not a claim marker, so clearing it means an unblocked task can never
    be driven again — it parks "awaiting unassigned delivery" forever."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "issue_type": "task", "assignee": "agent"})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-t", "boom")
    (up,) = br.cmds("update")
    assert "--assignee" not in up
    assert "--add-label" in up and "blocked" in up


def test_flag_blocked_stamps_the_failure_class_so_a_transient_block_can_self_heal(make_board, monkeypatch):
    """Every block used to look identical to the board — a coder timeout and a bad
    credential both left nothing but a WARNING in the log. The class label is what lets
    the sweep clear the first and escalate the second, and it is derived from the reason
    so a call site added later is classified without being told to."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "coder timed out after 1800.0s")
    (up,) = br.cmds("update")
    assert "blocked-class:transient" in up
    # an unrecognised failure is terminal — a human is the only thing that clears it
    br2 = Br()
    b2 = make_board(br2)
    monkeypatch.setattr(b2, "_require", lambda fid: {"id": fid})
    monkeypatch.setattr(b2, "comment", lambda fid, text: None)
    monkeypatch.setattr(b2, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b2.flag_blocked("bd-8", "something nobody has seen before")
    assert "blocked-class:terminal" in br2.cmds("update")[0]


def test_flag_blocked_replaces_a_prior_class_rather_than_accumulating(make_board, monkeypatch):
    """The `gens:` pattern — one class label, replaced, so a card re-blocked for a new
    reason does not carry a stale class the sweep would act on."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked-class:rate-limit", "project:x"]})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "permission denied")
    (up,) = br.cmds("update")
    assert "--remove-label" in up and "blocked-class:rate-limit" in up
    assert "blocked-class:auth" in up


# ── pre-model dispatch/infra block + tier reset on unblock (#339) ────────────────


def test_flag_blocked_stamps_the_explicit_dispatch_infra_class(make_board, monkeypatch):
    """A pre-model dispatch failure blocks under the caller's explicit `dispatch-infra`
    class — NOT the message-derived class — so the sweep notifies the operator (it is
    not a self-healing class) with the original infra evidence."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid})
    comments = []
    monkeypatch.setattr(b, "comment", lambda fid, text: comments.append(text))
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "coder dispatch failed: dispatch_tapped() unexpected keyword", category="dispatch-infra")
    (up,) = br.cmds("update")
    assert "blocked-class:dispatch-infra" in up
    assert comments and "dispatch_tapped" in comments[0]  # the infra evidence rides the comment


def test_clear_blocked_preserves_earned_tiers_after_a_dispatch_infra_block(make_board, monkeypatch):
    """Unblocking a card blocked for a pre-model dispatch/infra failure drops the block
    flag and the stale infra class, but KEEPS every `tier:` label. The loop never climbs
    a tier on a pre-model failure (``escalate`` is never called on that path), so every
    rung present was earned BEFORE the incident by real model-capability work. Removing
    them would silently restart a legitimately-escalated card on its weaker difficulty-
    selected model and repeat work that already failed there (#339)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {
            "id": fid,
            "labels": ["blocked", "blocked-class:dispatch-infra", "tier:reasoning", "tier:opus", "diff:small"],
        },
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    b.clear_blocked("bd-9")
    (up,) = br.cmds("update")
    assert "blocked" in up  # the block flag is dropped as always
    assert "blocked-class:dispatch-infra" in up  # the stale infra class cleared too
    # the earned rungs are PRESERVED — a pre-model incident never inflated them
    assert "tier:reasoning" not in up and "tier:opus" not in up
    assert "diff:small" not in up  # the difficulty label is untouched


def test_clear_blocked_dispatch_infra_on_a_never_escalated_card_leaves_difficulty_tier(make_board, monkeypatch):
    """A card whose FIRST dispatch failed pre-model carries no `tier:` label at all —
    the incident added none — so unblocking drops only the block flag + stale infra
    class, and the next real build starts at its difficulty-selected tier untouched."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "labels": ["blocked", "blocked-class:dispatch-infra", "diff:small"]},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    b.clear_blocked("bd-9")
    (up,) = br.cmds("update")
    assert up == ("update", "bd-9", "--remove-label", "blocked", "--remove-label", "blocked-class:dispatch-infra")


def test_clear_blocked_leaves_tier_labels_on_a_model_reachable_block(make_board, monkeypatch):
    """A model-reachable block (a real capability escalation) keeps its `tier:` labels
    on unblock — the ladder record is genuine, so the next build resumes at that tier.
    Only a pre-model infra block resets the posture."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "labels": ["blocked", "blocked-class:terminal", "tier:opus"]},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    b.clear_blocked("bd-9")
    (up,) = br.cmds("update")
    assert "blocked" in up
    assert "tier:opus" not in up  # untouched — no tier reset for a model-reachable block
    assert "blocked-class:terminal" not in up  # the class label is left as-is too


def test_clear_blocked_unclassified_is_unchanged(make_board, monkeypatch):
    """No block-class label at all (an older card, a hand block) → the legacy behaviour:
    remove only the `blocked` label, touch nothing else."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked", "tier:reasoning"]})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    b.clear_blocked("bd-9")
    (up,) = br.cmds("update")
    assert up == ("update", "bd-9", "--remove-label", "blocked")


# ── invariant #2: the single Done edge (record_merge) ───────────────────────────


def test_record_merge_closes_the_matching_feature(make_board, monkeypatch):
    url = "https://example/pr/7"
    rows = [{"id": "bd-9", "external_ref": url, "status": "in_progress", "labels": ["in-review"]}]
    br = Br({"list": rows})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    b.record_merge(pr_url=url)
    assert any(c[0] == "close" and c[1] == "bd-9" for c in br.calls)


def test_record_merge_is_a_noop_for_an_unknown_pr(make_board):
    b = make_board(Br({"list": []}))
    assert b.record_merge(pr_url="https://example/pr/none") is None


def test_record_merge_does_not_reclose_a_done_feature(make_board, monkeypatch):
    url = "https://example/pr/8"
    rows = [{"id": "bd-d", "external_ref": url, "status": "closed", "labels": []}]
    br = Br({"list": rows})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    b.record_merge(pr_url=url)
    assert br.cmds("close") == []  # already done → idempotent, no second close


# ── the MANUAL Done edge (#228): mark_done for work shipped off-board ─────────────


def _source_then_done(source_state):
    """A stateful ``get_feature``: the source projection on the FIRST read (what
    ``mark_done`` validates), then the closed ``done`` projection on every later read
    (what it returns). A blocked source carries the ``blocked`` label so the
    clear-blocked step has something to remove — exactly the real projection."""
    labels = ["blocked"] if source_state == "blocked" else []
    calls = {"n": 0}

    def _gf(fid):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": fid, "board_state": source_state, "labels": labels}
        return {"id": fid, "board_state": "done", "labels": []}

    return _gf


@pytest.mark.parametrize("source", ["in_progress", "in_review", "blocked"])
def test_mark_done_closes_an_in_flight_feature_with_a_done_reason(make_board, monkeypatch, source):
    """From any in-flight state (in_progress / in_review / blocked) mark_done closes the
    bead with an auditable `done: <reason>` — the manual sibling of record_merge for work
    that shipped outside the board's PR lifecycle — and the projection reads `done`."""
    br = Br()  # `show` → [] : no open blockers gate this feature
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _source_then_done(source))
    f = b.mark_done("bd-1", reason="shipped in the monorepo")
    close = next(c for c in br.calls if c[0] == "close")
    assert close == ("close", "bd-1", "-r", "done: shipped in the monorepo")
    assert f["board_state"] == "done"


def test_mark_done_records_the_reason_as_a_comment(make_board, monkeypatch):
    """The reason is the only provenance a hand-close has (record_merge points at a PR),
    so it's written to the bead as a `done: <reason>` comment for the audit trail."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _source_then_done("in_progress"))
    b.mark_done("bd-1", reason="landed via the CLI rewrite")
    comment = next(c for c in br.calls if c[0] == "comments")
    assert comment == ("comments", "add", "bd-1", "done: landed via the CLI rewrite")


def test_mark_done_without_a_reason_closes_bare_and_skips_the_comment(make_board, monkeypatch):
    """No reason → a bare `done (manual)` close and NO comment (nothing to record)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _source_then_done("in_review"))
    b.mark_done("bd-1")
    close = next(c for c in br.calls if c[0] == "close")
    assert close == ("close", "bd-1", "-r", "done (manual)")
    assert not any(c[0] == "comments" for c in br.calls)


def test_mark_done_from_blocked_clears_the_blocked_label_first(make_board, monkeypatch):
    """A blocked card can be hand-done too: like record_merge, mark_done clears the
    `blocked` label before closing (or `br close` refuses / the card stays stuck)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _source_then_done("blocked"))
    b.mark_done("bd-1", reason="unblocked out-of-band")
    update = next(c for c in br.calls if c[0] == "update")
    assert update == ("update", "bd-1", "--remove-label", "blocked")
    assert any(c[0] == "close" for c in br.calls)  # …and it still closes


def test_mark_done_drops_open_blocker_edges_so_dependents_unblock(make_board, monkeypatch):
    """mark_done resolves the feature's open incoming `blocks` edges BEFORE `br close`
    (which refuses while blockers are open), then closes — flipping the feature to
    `closed` so every dependent's edge to it reads closed and they stop being
    dag_blocked (#145). Mirrors record_merge / cancel_feature's drop-then-close."""
    bead_with_deps = {
        "id": "bd-1",
        "dependencies": [
            {"id": "bd-a", "dependency_type": "blocks", "status": "open"},
            {"id": "bd-b", "dependency_type": "blocks", "status": "in_progress"},
        ],
    }
    br = Br({"show": [bead_with_deps]})  # `_open_blockers` reads these two open edges
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _source_then_done("in_progress"))
    b.mark_done("bd-1", reason="done")
    dep_removes = [c for c in br.calls if c[0] == "dep" and c[1] == "remove"]
    assert {c[3] for c in dep_removes} == {"bd-a", "bd-b"}
    assert any(c[0] == "close" for c in br.calls)  # closed AFTER the edges were dropped


def test_dependent_unblocks_once_its_blocker_is_marked_done(make_board):
    """The unblock is real, not just an edge-drop: after mark_done closes a blocker, a
    dependent whose dep record now reads status=closed projects as NOT dag_blocked —
    the same resolution record_merge/cancel produce (#145)."""
    b = make_board(Br())
    dependent = {
        "id": "bd-child",
        "status": "open",
        "labels": ["ready"],
        "dependencies": [{"id": "bd-parent", "dependency_type": "blocks", "status": "closed"}],
    }
    assert b._project(dependent)["dag_blocked"] is False


@pytest.mark.parametrize("bad", ["backlog", "ready", "done", "cancelled"])
def test_mark_done_rejects_a_feature_not_in_flight(make_board, monkeypatch, bad):
    """mark_done is a narrow edge: backlog/ready have shipped nothing to record, and
    done/cancelled are already terminal. Each raises, and writes NOTHING (no close,
    no comment, no label churn) — the board is untouched on a rejected transition."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": bad, "labels": []})
    with pytest.raises(BoardError, match="mark_done accepts"):
        b.mark_done("bd-1", reason="nope")
    assert not any(c[0] in ("close", "comments", "update") for c in br.calls)


def test_mark_done_unknown_id_raises(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: None)
    with pytest.raises(BoardError, match="unknown feature"):
        b.mark_done("nope", reason="x")


# ── foundation flag + the relaxed (review) dependency gate ───────────────────────


def test_project_exposes_the_foundation_flag(make_board):
    b = make_board(Br())
    assert b._project({"id": "x", "status": "open", "labels": ["foundation"]})["foundation"] is True
    assert b._project({"id": "y", "status": "open", "labels": []})["foundation"] is False


def test_create_feature_labels_foundation(make_board):
    br = Br({"create": "bd-1", "show": [{"id": "bd-1", "status": "open", "labels": ["foundation"]}]})
    b = make_board(br)
    f = b.create_feature("t", spec="s", acceptance_criteria="a", files_to_modify=["x.py"], foundation=True)
    assert f["foundation"] is True
    assert any(c[0] == "update" and "foundation" in c for c in br.calls)


def test_open_blockers_keeps_open_blocks_drops_closed_and_nonblocks(make_board):
    bead = {
        "id": "bd-1",
        "dependencies": [
            {"id": "a", "dependency_type": "blocks", "status": "in_progress"},
            {"id": "b", "dependency_type": "blocks", "status": "closed"},  # merged → no longer gates
            {"id": "c", "dependency_type": "parent-child", "status": "open"},  # not a blocks edge
        ],
    }
    b = make_board(Br({"show": [bead]}))
    assert b._open_blockers("bd-1") == ["a"]


def test_ready_queue_relaxed_releases_only_nonfoundation_in_review_blockers(make_board):
    # Three dependents, each blocked by a different kind of blocker.
    all_features = [
        {"id": "bd-f", "issue_type": "feature", "status": "in_progress", "labels": ["in-review"]},
        {"id": "bd-found", "issue_type": "feature", "status": "in_progress", "labels": ["in-review", "foundation"]},
        {"id": "bd-ip", "issue_type": "feature", "status": "in_progress", "labels": []},
        {"id": "bd-dep1", "issue_type": "feature", "status": "open", "labels": ["ready"]},
        {"id": "bd-dep2", "issue_type": "feature", "status": "open", "labels": ["ready"]},
        {"id": "bd-dep3", "issue_type": "feature", "status": "open", "labels": ["ready"]},
    ]
    show = {
        "bd-dep1": [
            {"id": "bd-dep1", "dependencies": [{"id": "bd-f", "dependency_type": "blocks", "status": "in_progress"}]}
        ],
        "bd-dep2": [
            {
                "id": "bd-dep2",
                "dependencies": [{"id": "bd-found", "dependency_type": "blocks", "status": "in_progress"}],
            }
        ],
        "bd-dep3": [
            {"id": "bd-dep3", "dependencies": [{"id": "bd-ip", "dependency_type": "blocks", "status": "in_progress"}]}
        ],
    }
    b = make_board(Br({"ready": [], "list": all_features, "show": lambda args: show.get(args[1], [])}))
    # relaxed: only bd-dep1 releases (blocker non-foundation AND in_review).
    assert {f["id"] for f in b.ready_queue(relaxed=True)} == {"bd-dep1"}
    # bd-dep2 (foundation blocker) and bd-dep3 (blocker only in_progress) stay gated.
    # The default gate adds nothing beyond `br ready` (empty here).
    assert b.ready_queue() == []


# ── #90: per-feature project field + per-project store resolution ───────────────────


def test_project_exposes_the_project_label(make_board):
    """_project reads the `project:<name>` label back into the projection; a feature
    with no such label (pre-#90, or a board with no default) projects project=''."""
    b = make_board(Br())
    assert b._project({"id": "x", "status": "open", "labels": ["project:board-plugin"]})["project"] == "board-plugin"
    assert b._project({"id": "y", "status": "open", "labels": []})["project"] == ""


def test_create_feature_stamps_the_project_label_per_project(make_board):
    """Two features created into two different projects each carry their OWN
    `project:<name>` label (#90) — and neither is cross-stamped with the other's."""

    def board_for(name):
        br = Br({"create": "bd-x", "show": [{"id": "bd-x", "status": "open", "labels": [f"project:{name}"]}]})
        return br, make_board(br)

    br_a, b_a = board_for("protoagent")
    f_a = b_a.create_feature("A", spec="s", acceptance_criteria="a", files_to_modify=["x.py"], project="protoagent")
    br_b, b_b = board_for("board-plugin")
    f_b = b_b.create_feature("B", spec="s", acceptance_criteria="a", files_to_modify=["y.py"], project="board-plugin")

    assert f_a["project"] == "protoagent"
    assert any(c[0] == "update" and "--add-label" in c and "project:protoagent" in c for c in br_a.calls)
    assert f_b["project"] == "board-plugin"
    assert any(c[0] == "update" and "--add-label" in c and "project:board-plugin" in c for c in br_b.calls)
    # never cross-stamped: the protoagent board never writes the board-plugin label
    assert not any("project:board-plugin" in c for c in br_a.calls)


def test_create_feature_defaults_the_project_to_the_board_default(monkeypatch):
    """With no explicit `project`, the feature is stamped with the board's
    `default_project` — the single-config-serves-a-default path."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    br = Br({"create": "bd-1", "show": [{"id": "bd-1", "status": "open", "labels": ["project:protoagent"]}]})
    b = store.BeadsBoard(db=None, repo="/repo", default_project="protoagent")
    monkeypatch.setattr(b, "_run", br)
    f = b.create_feature("t", spec="s", acceptance_criteria="a", files_to_modify=["x.py"])
    assert f["project"] == "protoagent"
    assert any(c[0] == "update" and "--add-label" in c and "project:protoagent" in c for c in br.calls)


def test_create_feature_without_a_default_stamps_no_project_label(make_board):
    """Back-compat: a board with no projects map / no default (make_board) stamps no
    `project:` label — the single-repo behavior is unchanged."""
    br = Br({"create": "bd-1", "show": [{"id": "bd-1", "status": "open", "labels": []}]})
    b = make_board(br)
    f = b.create_feature("t", spec="s", acceptance_criteria="a", files_to_modify=["x.py"])
    assert f["project"] == ""
    assert not any("--add-label" in c and any(str(a).startswith("project:") for a in c) for c in br.calls)


def test_create_feature_rejects_an_invalid_project_name(make_board):
    """An illegal project name (a `/` or `#`, unsafe for a beads label) rejects the
    whole create BEFORE the bead is minted — never an orphan behind VALIDATION_FAILED."""
    br = Br()
    b = make_board(br)
    with pytest.raises(BoardError, match="invalid project"):
        b.create_feature("t", spec="s", acceptance_criteria="a", files_to_modify=["x.py"], project="bad/name#1")
    assert not br.cmds("create")  # rejected before minting


def test_ready_gate_validates_files_against_the_features_project_repo(monkeypatch, tmp_path):
    """#90: the Ready gate resolves files_to_modify against the FEATURE's own project's
    repo (from its `project` label + the board's projects map), not the instance default —
    so a multi-repo board checks each card against the right checkout."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    server = tmp_path / "server"
    server.mkdir()  # the instance default repo — deliberately EMPTY
    proto = tmp_path / "proto"
    proto.mkdir()  # protoagent project repo — also empty
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "store.py").write_text("x = 1\n")  # exists ONLY in the board-plugin repo
    projects = {"protoagent": {"repo": str(proto)}, "board-plugin": {"repo": str(plugin)}}
    br = Br()
    b = store.BeadsBoard(db=None, repo=str(server), projects=projects, default_project="protoagent")
    monkeypatch.setattr(b, "_run", br)

    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": ["store.py"],
        "project": "board-plugin",
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)
    # store.py exists in the board-plugin repo → the gate passes (it did NOT check the
    # empty instance/server repo, which would have failed).
    b.mark_ready("bd-1")
    assert ("update", "bd-1", "--add-label", "ready", "--remove-label", "designing") in br.calls

    # the SAME paths, now attributed to the protoagent project (store.py absent there) →
    # refused, and the error names the protoagent repo, not the instance default.
    br.calls.clear()
    feature["project"] = "protoagent"
    with pytest.raises(BoardError) as exc_info:
        b.mark_ready("bd-1")
    err = str(exc_info.value)
    assert "do not exist in the repo" in err
    assert "store.py" in err
    assert str(proto) in err  # the feature's project repo is named…
    assert str(server) not in err  # …NOT the instance default
    assert br.cmds("update") == []  # nothing mutated on a rejected gate


# ── #85: atomic create+enrich, leading-dash hardening, DB retry ─────────────────────
# board_create_feature was not atomic: `br create` succeeded, then the enrichment
# `br update` failed whenever a value STARTED WITH '-' (a markdown bullet in
# acceptance_criteria parsed as a CLI flag), leaving an orphan bead behind an error that
# hid its id. The fix: pass enrichment VALUES in `--flag=value` form (a leading dash can
# never parse as an option), and on an enrichment failure AFTER a successful create,
# return success-with-warning carrying the id + the fields still needing writing.


def _enrich_run(created="bd-1", *, fail_update=False, calls=None):
    """A fake `_run`: `create` returns an id, `show` returns a bare open bead, and
    `update` either records + succeeds or (fail_update) raises a BoardError — the
    enrichment-failed-after-create path."""

    def run_impl(*args, want_json=False):
        if calls is not None:
            calls.append(args)
        head = args[0] if args else ""
        if head == "create":
            return created
        if head == "update" and fail_update:
            raise BoardError(f"`br update {created}` failed: unexpected argument '- do X'")
        if head == "show":
            return [{"id": created, "status": "open", "title": "T", "labels": []}]
        return [] if want_json else ""

    return run_impl


def test_create_feature_passes_leading_dash_value_in_end_of_options_form(make_board):
    """A leading-dash acceptance_criteria ('- …' markdown bullets) must ride in
    `--flag=value` form so `br` stores it verbatim instead of parsing it as a CLI flag."""
    calls = []
    b = make_board(_enrich_run(calls=calls))
    ac = "- filters results\n- debounces input"
    b.create_feature("T", spec="s", acceptance_criteria=ac, files_to_modify=["a.py"])
    update = next(c for c in calls if c and c[0] == "update")
    # the value is glued to the flag with '=' (dash-safe) …
    assert f"--acceptance-criteria={ac}" in update
    # … and NEVER as a bare flag followed by a dash-leading value (the #85 misparse).
    assert "--acceptance-criteria" not in update


def test_create_feature_enrichment_failure_returns_id_and_missing_fields(make_board):
    """Create succeeds, enrichment `br update` fails → success-with-warning: the bead id
    plus the fields still needing writing, NEVER a bare error that conceals the id."""
    b = make_board(_enrich_run("bd-7", fail_update=True))
    f = b.create_feature("T", spec="s", acceptance_criteria="- do X", design="d", files_to_modify=["a.py"])
    assert f["id"] == "bd-7"  # the id survives — no orphan hidden behind an error
    assert f["enrichment_failed"] is True
    assert set(f["missing_fields"]) == {"acceptance_criteria", "design", "files_to_modify"}
    assert "board_update_feature" in f["warning"]


def test_create_feature_success_carries_no_enrichment_warning(make_board):
    """The happy path returns a clean projection — no stray enrichment flags."""
    b = make_board(_enrich_run("bd-3"))
    f = b.create_feature("T", spec="s", acceptance_criteria="a", files_to_modify=["a.py"])
    assert "enrichment_failed" not in f and "missing_fields" not in f


def test_update_feature_uses_end_of_options_form_for_value_fields(make_board, monkeypatch):
    """The same #85 hardening in the repair path: a leading-dash value goes out in
    `--flag=value` form, never as a bare flag + dash-leading value."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", acceptance_criteria="- a leading-dash bullet", spec="-starts with dash")
    update = next(c for c in br.calls if c and c[0] == "update")
    assert "--acceptance-criteria=- a leading-dash bullet" in update
    assert "--description=-starts with dash" in update
    assert "--acceptance-criteria" not in update and "--description" not in update


# ── #85: transient DATABASE_ERROR (SQLite contention) retries with backoff ──────────


def _proc(returncode, stderr="", stdout="ok"):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_retries_a_transient_database_error_then_succeeds(monkeypatch, _have_br):
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        # first attempt: SQLite contention; second: clears.
        return _proc(1, "DATABASE_ERROR: database is locked") if n["calls"] == 1 else _proc(0)

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    slept = []
    monkeypatch.setattr(store.time, "sleep", lambda s: slept.append(s))
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True  # skip the br-init pin so only the retry path is exercised
    assert b._run("list") == "ok"  # the retry cleared the lock
    assert n["calls"] == 2 and slept  # one failure, one backoff, then success


def test_run_does_not_retry_a_non_database_error(monkeypatch, _have_br):
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        return _proc(1, "VALIDATION_ERROR: bad --type")

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    with pytest.raises(BoardError, match="failed"):
        b._run("list")
    assert n["calls"] == 1  # not contention → no retry


def test_run_gives_up_after_exhausting_db_retries(monkeypatch, _have_br):
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        return _proc(1, "DATABASE_ERROR: database is busy")

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    with pytest.raises(BoardError):
        b._run("list")
    assert n["calls"] == store._DB_RETRY_ATTEMPTS  # persistent lock → bounded retries, then raise


# ── #116: retry window sized for real WAL contention (6 attempts, ~6.3s) ─────────────
# The old 4-attempt / 0.7s budget lost the `bd-ud1` enrichment write to a WAL checkpoint
# (dropping its source_issue → a PR with no `Fixes #N`). The window is now 6 attempts
# backing off 0.1 → 0.2 → 0.4 → 0.8 → 1.6 → 3.2s, contention is also caught when `br`
# writes it as JSON on a ZERO exit, and the resolved retry count is logged on success.


def test_db_retry_window_is_six_attempts_with_a_doubling_backoff(monkeypatch, _have_br):
    """#116 r1: 6 attempts, the delay doubling each retry — a ~6.3s window that
    outlasts a WAL checkpoint instead of the old 0.7s that lost the race."""
    assert store._DB_RETRY_ATTEMPTS == 6
    assert store._DB_RETRY_DELAY == 0.1
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        return _proc(1, "DATABASE_ERROR: database is busy")

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    slept = []
    monkeypatch.setattr(store.time, "sleep", lambda s: slept.append(s))
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    with pytest.raises(BoardError):
        b._run("list")
    assert n["calls"] == 6  # six attempts, then give up
    # five backoffs between the six attempts, each double the last (0.1 → 1.6)
    assert slept == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6])


def test_run_retries_db_contention_written_as_json_on_a_zero_exit(monkeypatch, _have_br):
    """#116 r2: `br` can exit 0 yet write the failure as structured JSON on stdout — a
    shape a bare returncode check sails past. It's caught post-parse and retried."""
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        # exit 0 the whole time; the first body is an error-shaped JSON, then it clears.
        if n["calls"] == 1:
            return _proc(0, stdout='{"error": "DATABASE_ERROR: database is busy"}')
        return _proc(0, stdout='{"id": "bd-1"}')

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    slept = []
    monkeypatch.setattr(store.time, "sleep", lambda s: slept.append(s))
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    assert b._run("create", want_json=True) == {"id": "bd-1"}  # cleared, parsed through
    assert n["calls"] == 2 and slept  # the zero-exit error was retried, not returned


def test_run_does_not_mistake_a_normal_json_payload_for_contention(monkeypatch, _have_br):
    """A legit zero-exit JSON payload (a bead carrying `status: "open"`) must NOT trip
    the error-shaped-JSON sniff — only DB-error text in an error field triggers a retry."""
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        return _proc(0, stdout='[{"id": "bd-1", "status": "open"}]')

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    assert b._run("list", want_json=True) == [{"id": "bd-1", "status": "open"}]
    assert n["calls"] == 1  # a normal payload isn't contention → no retry


def test_run_logs_the_retry_count_on_final_success(monkeypatch, _have_br, caplog):
    """#116 r3: on final success after contention, the resolved retry count is logged
    (info) so the operator can see the race was won, not silently swallowed."""
    n = {"calls": 0}

    def fake_run(cmd, **kw):
        n["calls"] += 1
        return _proc(1, "DATABASE_ERROR: database is busy") if n["calls"] <= 2 else _proc(0)

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    b = store.BeadsBoard(repo="/repo")
    b._workspace_ready = True
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        assert b._run("list") == "ok"
    assert n["calls"] == 3  # two contention failures, then success on the third
    assert any("cleared DB contention after 2" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "out",
    [
        '{"error": "DATABASE_ERROR: database is busy"}',
        '{"message": "database is locked"}',
        '{"code": "DATABASE_ERROR"}',
        '{"detail": "SQLite: database is busy"}',
    ],
)
def test_contention_in_json_flags_error_shaped_db_errors(out):
    """The post-parse sniff catches DB contention in any error-shaped field."""
    assert store._contention_in_json(out)


@pytest.mark.parametrize(
    "out",
    [
        "",
        "ok",
        '[{"id": "bd-1", "status": "open"}]',  # a normal list payload
        '{"id": "bd-1", "status": "open"}',  # a normal bead object — status isn't a lock
        '{"error": "VALIDATION_ERROR: bad --type"}',  # a real error, but not contention
        "{oops",  # not valid JSON
    ],
)
def test_contention_in_json_ignores_normal_and_non_contention_payloads(out):
    """Normal payloads and non-contention errors are passed through untouched."""
    assert store._contention_in_json(out) == ""


# ── workspace pinning (ADR 0055 P0) ─────────────────────────────────────────────
# The board must be deterministically pinned to ITS workspace (a configured `db` or
# `repo`), not the host process's cwd — so a per-team-agent board (scale-out) writes
# to its own repo's `.beads` and never pollutes the dir the server launched from.


@pytest.fixture
def _have_br(monkeypatch):
    # BeadsBoard.__init__ refuses to build without the `br` CLI on PATH — stub it.
    monkeypatch.setattr(store.shutil, "which", lambda _x: "/usr/bin/br")


@pytest.fixture(autouse=True)
def _clear_boards():
    store._BOARDS.clear()
    yield
    store._BOARDS.clear()


def test_get_store_keys_by_workspace(_have_br):
    a1 = store.get_store(db="/tmp/a.db", repo="/repo/a")
    a2 = store.get_store(db="/tmp/a.db", repo="/repo/a")
    b = store.get_store(db="/tmp/b.db", repo="/repo/b")
    assert a1 is a2  # same workspace → one shared board (loop/API/tools share it)
    assert a1 is not b  # different db/repo → distinct board (db_path now genuinely pins)
    assert a1.db == "/tmp/a.db" and a1.repo == "/repo/a"
    assert b.db == "/tmp/b.db" and b.repo == "/repo/b"


def test_get_store_distinguishes_repo_even_without_db(_have_br):
    # No explicit db (auto-discovery), but two repos must NOT collapse onto one board.
    assert store.get_store(repo="/repo/x") is not store.get_store(repo="/repo/y")


def test_run_executes_in_the_configured_repo(monkeypatch, _have_br):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cwd"] = kw.get("cwd")
        return _Proc()

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    store.BeadsBoard(repo="/my/team/repo")._run("list")
    assert captured["cwd"] == "/my/team/repo"  # br runs in the repo, not the host cwd

    store.BeadsBoard()._run("list")
    assert captured["cwd"] == "."  # default repo → process cwd, unchanged behavior


def test_create_feature_wires_deps_even_when_enrichment_fails(make_board):
    """QA panel on PR #88: dependency edges are independent of the enrichment `br update`
    — an enrichment failure must never silently drop them. Deps go out FIRST."""
    calls = []
    b = make_board(_enrich_run("bd-9", fail_update=True, calls=calls))
    f = b.create_feature(
        "T", spec="s", acceptance_criteria="- a", files_to_modify=["a.py"], depends_on=["bd-1", "bd-2"]
    )
    dep_calls = [c for c in calls if c and c[0] == "dep"]
    assert [c[2] for c in dep_calls] == ["bd-9", "bd-9"]  # both edges attempted (fid position)
    assert {c[3] for c in dep_calls} == {"bd-1", "bd-2"}
    assert f["enrichment_failed"] is True  # the warning still reports the enrichment half
    assert not any("depends_on" in m for m in f["missing_fields"])  # deps did NOT fail


def test_create_feature_reports_failed_dep_edges_in_warning(make_board, monkeypatch):
    """A dep edge that fails is tracked like a failed field: named in missing_fields and
    repairable via board_update_feature(depends_on=…) — never silently lost."""
    b = make_board(_enrich_run("bd-9"))
    monkeypatch.setattr(b, "add_dependency", lambda fid, dep: (_ for _ in ()).throw(BoardError("no such issue")))
    f = b.create_feature("T", spec="s", acceptance_criteria="a", files_to_modify=["a.py"], depends_on=["bd-x"])
    assert f["enrichment_failed"] is True
    assert any("depends_on(bd-x)" in m for m in f["missing_fields"])
    assert "board_update_feature" in f["warning"]


def test_update_feature_adds_dependency_edges(make_board, monkeypatch):
    """The repair contract is deliverable: update_feature(depends_on=…) adds the blocking
    edges a failed create-time wiring dropped (QA panel on PR #88)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", depends_on=["bd-7", "bd-8"])
    dep_calls = [c for c in br.calls if c and c[0] == "dep"]
    assert [(c[2], c[3]) for c in dep_calls] == [("bd-1", "bd-7"), ("bd-1", "bd-8")]


# ── create_from_plan: batch-create a decomposition, all-or-report (#92) ──────────


def _plan_board(make_board, monkeypatch):
    """A board wired for ``create_from_plan``: ``_create`` mints ``bd-<n>`` and registers
    a ready-eligible bead (spec + acceptance_criteria + files, so ``mark_ready`` can
    promote a clean item), ``get_feature`` returns it, and enrichment / ``dep add`` /
    ready ``br update`` calls flow through the recording ``Br`` for assertion."""
    br = Br()
    b = make_board(br)
    beads: dict[str, dict] = {}
    counter = {"n": 0}

    def _create(title, *, itype="feature", parent="", priority=2, description="", external_ref=""):
        counter["n"] += 1
        fid = f"bd-{counter['n']}"
        beads[fid] = {
            "id": fid,
            "title": title,
            "board_state": "backlog",
            "spec": description or "spec",
            "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
            "files_to_modify": ["a.py (new)"],  # (new) bypasses the path-existence gate (#110)
        }
        return fid

    monkeypatch.setattr(b, "_create", _create)
    monkeypatch.setattr(b, "get_feature", lambda fid: beads.get(fid))
    return b, beads, br


def test_create_from_plan_creates_every_well_formed_item(make_board, monkeypatch):
    b, _beads, _br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [
            {"title": "Feature A", "spec": "sa", "files": "a.py"},
            {"title": "Feature B", "spec": "sb", "files": ["b.py"]},
        ]
    )
    assert out["created_ids"] == ["bd-1", "bd-2"]
    assert out["summary"] == {"requested": 2, "created": 2, "failed": 0, "ready": 0, "warnings": 0}
    assert [r["title"] for r in out["items"]] == ["Feature A", "Feature B"]
    assert all(r["created"] for r in out["items"])


def test_create_from_plan_malformed_item_fails_itself_and_the_rest_proceed(make_board, monkeypatch):
    b, _beads, _br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [
            {"title": "Good one", "spec": "s", "files": "a.py"},
            {"spec": "no title here"},  # malformed — no title
            "not even an object",  # malformed — not a dict
            {"title": "Also good", "spec": "s", "files": "b.py"},
        ]
    )
    assert out["summary"]["requested"] == 4
    assert out["summary"]["created"] == 2
    assert out["summary"]["failed"] == 2
    assert out["created_ids"] == ["bd-1", "bd-2"]  # only the well-formed items minted ids
    bad = [r for r in out["items"] if not r["created"]]
    assert len(bad) == 2
    assert any("no title" in r["error"] for r in bad)
    assert any("not an object" in r["error"] for r in bad)
    # a failed item still preserves its plan index so the caller can map the reason back
    assert {r["index"] for r in bad} == {1, 2}


def test_create_from_plan_resolves_inter_item_deps_by_index_and_title(make_board, monkeypatch):
    b, _beads, br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [
            {"title": "Foundation", "spec": "s", "files": "f.py", "foundation": True},
            {"title": "Builds via index", "spec": "s", "files": "b.py", "depends_on": [0]},
            {"title": "Builds via title", "spec": "s", "files": "c.py", "depends_on": ["Foundation"]},
        ]
    )
    assert out["summary"]["created"] == 3
    assert all(not r.get("enrichment_failed") for r in out["items"])
    # both dependents wired to the foundation's minted id (bd-1) — resolved AFTER all creates
    edges = {(a[2], a[3]) for a in br.cmds("dep")}
    assert edges == {("bd-2", "bd-1"), ("bd-3", "bd-1")}


def test_create_from_plan_double_dash_dep_fails_that_item_not_the_whole_batch(make_board, monkeypatch):
    """#92 AC8: a dep like '--5' passes the old ``lstrip('-').isdigit()`` guard but crashes
    ``int()`` — it must fail ITS item with a named reason (success-with-warning) while the
    rest of the batch proceeds, never take the batch down with an uncaught ValueError."""
    b, _beads, br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [
            {"title": "Fine", "spec": "s", "files": "a.py"},
            {"title": "Bad dep", "spec": "s", "files": "b.py", "depends_on": ["--5"]},
        ]
    )
    # the batch survived: both beads were created, no ValueError escaped
    assert out["summary"]["created"] == 2
    assert out["created_ids"] == ["bd-1", "bd-2"]
    # the '--5' item fails itself, named + repairable; the other stays clean
    warned = next(r for r in out["items"] if r["title"] == "Bad dep")
    assert warned["created"] is True and warned["enrichment_failed"] is True
    assert any("--5" in m for m in warned["missing_fields"])
    assert "--5" in warned["warning"] and "board_update_feature" in warned["warning"]
    assert next(r for r in out["items"] if r["title"] == "Fine").get("enrichment_failed") is None
    # a malformed ref never reaches `br dep add`
    assert br.cmds("dep") == []


def test_create_from_plan_mark_ready_promotes_only_clean_items(make_board, monkeypatch):
    b, _beads, br = _plan_board(make_board, monkeypatch)
    real_add = b.add_dependency

    def flaky_add(fid, dep):
        if dep == "ghost":
            raise BoardError("no such issue 'ghost'")
        return real_add(fid, dep)

    monkeypatch.setattr(b, "add_dependency", flaky_add)
    out = b.create_from_plan(
        [
            {"title": "Clean", "spec": "s", "files": "a.py"},
            {"title": "Warned", "spec": "s", "files": "b.py", "depends_on": ["ghost"]},
        ],
        mark_ready=True,
    )
    clean = next(r for r in out["items"] if r["title"] == "Clean")
    warned = next(r for r in out["items"] if r["title"] == "Warned")
    assert clean["ready"] is True and clean["board_state"] == "ready"
    assert warned.get("ready") is not True  # a warned item is NOT auto-promoted
    assert warned["enrichment_failed"] is True
    assert out["summary"]["ready"] == 1
    # exactly one `ready`-label update fired, for the clean item only
    ready_updates = [a for a in br.cmds("update") if "--add-label" in a and "ready" in a]
    assert len(ready_updates) == 1 and ready_updates[0][1] == "bd-1"


def _ids_before_flags(update_args):
    """The positional issue ids of a `br update <id…> --flag…` call (everything after
    the `update` subcommand, up to the first `--flag`)."""
    ids = []
    for a in update_args[1:]:
        if a.startswith("--"):
            break
        ids.append(a)
    return ids


def test_create_from_plan_promotes_the_whole_batch_atomically(make_board, monkeypatch):
    """#111: a batch promoted with ``mark_ready=True`` must reach a state where EVERY
    item carries the ``ready`` label before the puller can claim ANY of them. Priority
    only ranks what is *already* ready, so if items flipped to ``ready`` one at a time an
    idle loop could claim the first promoted item before the rest landed. We model the
    ``ready`` set a concurrent puller (``br ready``) would observe and snapshot it at
    every point a claim could interleave (every ``br`` call is such a boundary): the
    promotion must be all-or-nothing — never a partial batch where some clean items are
    ready and others are not yet."""
    b, beads, br = _plan_board(make_board, monkeypatch)

    ready: set[str] = set()  # the ids a puller would currently see as claimable
    observed: list[frozenset[str]] = []  # ready-set snapshot at each br-call boundary
    real_run = b._run

    def instrumented(*args, want_json=False, **kw):
        # A `br update … --add-label ready` is the write that makes a bead claimable;
        # apply it to its target ids so the model tracks exactly what the puller can see.
        if args and args[0] == "update" and "--add-label" in args and store.LABEL_READY in args:
            ready.update(_ids_before_flags(args))
        # Every br call is a boundary a concurrent puller could interleave a claim at.
        observed.append(frozenset(ready))
        return real_run(*args, want_json=want_json, **kw)

    monkeypatch.setattr(b, "_run", instrumented)

    out = b.create_from_plan(
        [
            {"title": "One", "spec": "s", "files": "a.py"},
            {"title": "Two", "spec": "s", "files": "b.py"},
            {"title": "Three", "spec": "s", "files": "c.py"},
        ],
        mark_ready=True,
    )

    clean = {r["id"] for r in out["items"] if r.get("created")}
    assert clean == {"bd-1", "bd-2", "bd-3"}
    assert out["summary"]["ready"] == 3

    # THE INVARIANT: at no observable boundary is the batch partially promoted — the
    # ready set a puller could see is always either NONE of the clean items or ALL of
    # them, never a strict, claimable subset. A per-item promotion loop would expose
    # {bd-1} (then {bd-1, bd-2}) here and fail this assertion.
    for snap in observed:
        seen = snap & clean
        assert seen == set() or seen == clean, f"partial promotion visible to the puller: {seen}"
    # and the batch genuinely crossed into ready (0 → all in a single boundary).
    assert any((snap & clean) == clean for snap in observed)

    # structural proof of the atomic flip: ONE `br update` carries the ready label and it
    # lists every clean id — a single write the puller cannot interleave a claim into.
    ready_updates = [a for a in br.cmds("update") if "--add-label" in a and "ready" in a]
    assert len(ready_updates) == 1
    assert set(_ids_before_flags(ready_updates[0])) == clean


def test_resolve_plan_dep_index_title_and_passthrough_id():
    index_to_id = {0: "bd-1", 1: "bd-2"}
    title_to_id = {"foundation feature": "bd-1"}
    assert BeadsBoard._resolve_plan_dep(0, index_to_id, title_to_id) == "bd-1"  # int index
    assert BeadsBoard._resolve_plan_dep("1", index_to_id, title_to_id) == "bd-2"  # numeric-string index
    assert BeadsBoard._resolve_plan_dep("Foundation  Feature", index_to_id, title_to_id) == "bd-1"  # by title
    assert BeadsBoard._resolve_plan_dep("bd-9", index_to_id, title_to_id) == "bd-9"  # literal id passthrough


@pytest.mark.parametrize("bad", ["--5", "---7", "--10"])
def test_resolve_plan_dep_multi_dash_index_is_named_not_a_crash(bad):
    """#92 AC8 (unit): multi-dash junk the loose guard accepted raises a NAMED BoardError,
    not an uncaught ValueError from ``int()``."""
    with pytest.raises(BoardError, match="malformed"):
        BeadsBoard._resolve_plan_dep(bad, {}, {})


def test_resolve_plan_dep_out_of_range_index_raises_named():
    with pytest.raises(BoardError, match="out of range"):
        BeadsBoard._resolve_plan_dep("7", {0: "bd-1"}, {})


# ── _project: depends_on ledger vs. the live open subset (bd-171) ────────────────


def test_project_exposes_depends_on_ledger_and_open_subset(make_board):
    """`_project` surfaces BOTH dependency views: `depends_on` is every `blocks`
    edge (the historical ledger, incl. already-merged/closed blockers) while
    `open_depends_on` keeps only the edges whose blocker is still open — the live,
    actionable signal. Non-`blocks` edges are ignored by both."""
    b = make_board(Br())
    bead = {
        "id": "bd-5",
        "status": "open",
        "labels": [],
        "dependencies": [
            {"id": "bd-a", "dependency_type": "blocks", "status": "closed"},  # blocker merged
            {"id": "bd-b", "dependency_type": "blocks", "status": "open"},  # still blocking
            {"id": "bd-c", "dependency_type": "related", "status": "open"},  # not a blocks edge
        ],
    }
    f = b._project(bead)
    assert f["depends_on"] == ["bd-a", "bd-b"]  # the full ledger
    assert f["open_depends_on"] == ["bd-b"]  # only the still-open blocker
    # a feature with no deps (`br list` omits them too) → both empty, never missing
    empty = b._project({"id": "x", "status": "open", "labels": []})
    assert empty["depends_on"] == [] and empty["open_depends_on"] == []


# ── the adverse-review bounce (bd-171): a distinct comment + requeue-on-same-PR ──


def test_record_review_bounce_comments_from_in_review_distinct_from_ci(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    comments = []
    monkeypatch.setattr(b, "comment", lambda fid, text: comments.append((fid, text)))
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_review_bounce("bd-9", "auth check missing a null guard")
    assert comments == [("bd-9", "review requested changes: auth check missing a null guard")]
    assert "CI failed" not in comments[0][1]  # distinct from the ci-fail note


def test_record_review_bounce_rejects_a_non_in_review_state(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_progress"})
    with pytest.raises(BoardError, match="expects in_review"):
        b.record_review_bounce("bd-9", "x")


def test_requeue_preserves_the_open_pr_and_clears_the_assignee(make_board, monkeypatch):
    """A requeue (the /ci + /review re-dispatch path) keeps the open PR — it never
    touches external_ref — and clears the assignee so the re-pull can `--claim`."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "get_feature", lambda fid: {"id": fid, "board_state": "ready", "pr_url": "https://example/pr/1"}
    )
    f = b.requeue("bd-1")
    call = next(c for c in br.calls if c and c[0] == "update")
    assert "--external-ref" not in call  # the open PR is left intact
    assert "--assignee" in call  # cleared (paired with "") so `--claim` won't reject
    assert "--add-label" in call and "ready" in call and "in-review" in call  # ready↑, in-review↓
    assert f["pr_url"] == "https://example/pr/1"  # preserved onto the requeued feature
    with pytest.raises(BoardError, match="out of range"):
        BeadsBoard._resolve_plan_dep(-5, {0: "bd-1"}, {})


# ── source_issue: normalize + store the originating GitHub issue (#97/#101) ──────
# The bead carries a single `source-issue: owner/repo#N` metadata line in its `notes`
# field, beside the files_to_modify lines — NOT a label: beads' label validator only
# allows [alphanumeric - _ :], so the original `source:owner/repo#N` label failed
# VALIDATION_FAILED on every real write (#101). The projection splits notes back into
# `files_to_modify` + `source_issue`, which the loop's PR opener reads to stamp
# `Fixes #N` on the feature's PR.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/acme/widgets/issues/123", "acme/widgets#123"),
        ("https://github.com/acme/widgets/issues/123/", "acme/widgets#123"),  # trailing slash tolerated
        ("  https://github.com/acme/widgets/issues/8  ", "acme/widgets#8"),  # trimmed
        ("acme/widgets#42", "acme/widgets#42"),  # canonical shorthand passes through unchanged
    ],
)
def test_normalize_source_issue_accepts_url_and_slug(raw, expected):
    assert store.normalize_source_issue(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-issue",
        "123",  # a bare number can't name a repo — reject, don't guess
        "#123",
        "acme/widgets",  # no issue number
        "https://github.com/acme/widgets/pull/5",  # a PR, not an issue
        "https://github.com/acme/issues/5",  # owner with no repo
        "",
        "   ",
    ],
)
def test_normalize_source_issue_rejects_invalid_with_a_named_error(bad):
    with pytest.raises(BoardError, match="invalid source_issue"):
        store.normalize_source_issue(bad)


def test_create_feature_stores_the_normalized_source_issue_in_notes(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", source_issue="https://github.com/acme/widgets/issues/97")
    update = next(c for c in calls if c and c[0] == "update")
    assert "--notes=source-issue: acme/widgets#97" in update
    # NEVER a label — beads' label charset rejects `/` and `#` (#101).
    assert "--add-label" not in update


def test_create_feature_passes_a_canonical_slug_through_unchanged(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", source_issue="acme/widgets#8")
    update = next(c for c in calls if c and c[0] == "update")
    assert "--notes=source-issue: acme/widgets#8" in update


def test_create_feature_files_and_source_share_one_notes_write(make_board):
    """files_to_modify + source_issue land in the SAME `--notes=` payload: paths
    first (one per line), the metadata line last."""
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", files_to_modify=["a.py", "b.py"], source_issue="acme/widgets#8")
    update = next(c for c in calls if c and c[0] == "update")
    assert "--notes=a.py\nb.py\nsource-issue: acme/widgets#8" in update


def test_create_feature_invalid_source_issue_rejects_before_minting_a_bead(make_board):
    """Validation runs BEFORE `br create` — an invalid source_issue must fail the whole
    create with the named error, never leave an orphan bead behind it."""
    calls = []
    b = make_board(_enrich_run(calls=calls))
    with pytest.raises(BoardError, match="invalid source_issue"):
        b.create_feature("T", spec="s", source_issue="not-an-issue")
    assert not any(c and c[0] == "create" for c in calls)  # no orphan


def test_create_feature_without_source_issue_writes_no_source_line(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", acceptance_criteria="a", files_to_modify=["a.py"])
    update = next(c for c in calls if c and c[0] == "update")
    assert "--notes=a.py" in update
    for c in calls:
        assert not any("source-issue:" in str(tok) for tok in c)


def test_update_feature_replaces_a_stale_source_line_and_keeps_files(make_board, monkeypatch):
    """`--notes` replaces the whole field, so the rewrite must carry the current
    files_to_modify forward while swapping in the new source-issue line."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "files_to_modify": ["a.py"], "source_issue": "old/repo#1", "labels": ["ready"]},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", source_issue="https://github.com/acme/widgets/issues/9")
    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--notes=a.py\nsource-issue: acme/widgets#9")


def test_update_feature_files_update_preserves_the_source_line(make_board, monkeypatch):
    """The mirror image: a files-only update must never drop the stored source."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "files_to_modify": ["a.py"], "source_issue": "acme/widgets#8", "labels": []},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", files_to_modify=["x.py", "y.py"])
    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--notes=x.py\ny.py\nsource-issue: acme/widgets#8")


def test_update_feature_invalid_source_issue_raises_and_writes_nothing(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": []})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    with pytest.raises(BoardError, match="invalid source_issue"):
        b.update_feature("bd-1", spec="also passed", source_issue="not-an-issue")
    assert br.cmds("update") == []  # nothing half-applied


def test_update_feature_whitespace_source_issue_is_a_noop(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "source_issue": "old/repo#1", "labels": []})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", source_issue="   ")
    assert br.cmds("update") == []  # the difficulty convention: blank = leave untouched


def test_project_splits_notes_into_files_and_source_issue(make_board):
    b = make_board(Br())
    bead = {"id": "x", "status": "open", "labels": ["ready"], "notes": "a.py\nsource-issue: acme/widgets#8"}
    f = b._project(bead)
    assert f["source_issue"] == "acme/widgets#8"
    assert f["files_to_modify"] == ["a.py"]  # the metadata line never leaks into the file list
    assert b._project({"id": "y", "status": "open", "labels": []})["source_issue"] == ""


def test_projected_source_issue_feeds_the_loops_fixes_line(make_board):
    """The wiring's point: a stored source_issue round-trips through the projection
    into loop._source_issue, which resolves it to (slug, n) for the PR's Fixes line."""
    from project_board.loop import _source_issue

    b = make_board(Br())
    f = b._project({"id": "x", "status": "open", "labels": [], "notes": "source-issue: acme/widgets#8"})
    assert _source_issue(f) == ("acme/widgets", 8)
    # absent → the description-URL fallback still works, unchanged
    f = b._project(
        {"id": "y", "status": "open", "labels": [], "description": "see https://github.com/acme/widgets/issues/3"}
    )
    assert _source_issue(f) == ("acme/widgets", 3)


def test_create_from_plan_passes_source_issue_through(make_board, monkeypatch):
    b, _beads, br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [{"title": "F", "spec": "s", "files": "a.py", "source_issue": "https://github.com/acme/widgets/issues/97"}]
    )
    assert out["summary"]["created"] == 1
    assert any("--notes=a.py\nsource-issue: acme/widgets#97" in u for u in br.cmds("update"))


def test_create_from_plan_invalid_source_issue_fails_that_item_not_the_batch(make_board, monkeypatch):
    b, _beads, br = _plan_board(make_board, monkeypatch)
    out = b.create_from_plan(
        [
            {"title": "Good", "spec": "s", "files": "a.py", "source_issue": "acme/widgets#5"},
            {"title": "Bad", "spec": "s", "files": "b.py", "source_issue": "not-an-issue"},
        ]
    )
    assert out["summary"]["created"] == 1 and out["summary"]["failed"] == 1
    assert out["created_ids"] == ["bd-1"]  # the invalid item never minted a bead
    bad = next(r for r in out["items"] if not r["created"])
    assert bad["title"] == "Bad" and "invalid source_issue" in bad["error"]
    # the good item still landed (its source rides the notes metadata line)
    assert any("--notes=a.py\nsource-issue: acme/widgets#5" in u for u in br.cmds("update"))


# ── #114: exhaustive board queries must pass `--limit 0` (br list defaults 50) ────
# `br list` caps at 50 rows by default and `br ready` at 20 — and the plugin filters by
# board_state / issue_type in Python AFTER `br` returns, so a capped result silently
# truncates every consumer (PR reconcile, sweep/recover, the pending-review count, dedup,
# the ready scan, /features, board_list). A query that reads as exhaustive must pass
# `--limit 0` (the documented unlimited sentinel) so the cap can't hide rows behind the
# Python filter. Asserting the argv proves the flag is passed without a brittle 51-row
# fixture.


def _passes_unlimited(argv) -> bool:
    """True iff this `br` argv carries `--limit 0` (the flag immediately followed by 0)."""
    for i, tok in enumerate(argv):
        if tok == "--limit":
            return i + 1 < len(argv) and argv[i + 1] == "0"
    return False


def test_list_features_query_is_unbounded(make_board):
    """list_features passes `--limit 0` so the projection isn't capped at br's default 50
    (then narrowed further by the Python state filter)."""
    br = Br({"list": [], "ready": []})
    b = make_board(br)
    b.list_features()
    (list_call,) = br.cmds("list")
    assert "--limit" in list_call and "0" in list_call
    assert _passes_unlimited(list_call)  # flag + value adjacent, not just both present


def test_list_features_state_filter_still_queries_unbounded(make_board):
    """The state filter runs in Python AFTER br returns, so the underlying `br list` must
    still be unbounded — a `state=` argument must never narrow the query itself."""
    br = Br({"list": [], "ready": []})
    b = make_board(br)
    b.list_features(state="in_review")
    (list_call,) = br.cmds("list")
    assert _passes_unlimited(list_call)


def test_list_features_batch_show_populates_depends_on(make_board):
    """list_features injects `dependencies` from a single batch `br show *ids` call so
    `_project` can populate `depends_on` / `open_depends_on` correctly (#144).
    Without the batch fetch, `br list` omits the `dependencies` array and every card
    reports `depends_on: []` even when real edges exist."""
    beads = [
        {"id": "bd-1", "status": "open", "labels": []},
        {"id": "bd-2", "status": "open", "labels": []},
    ]
    show_beads = [
        {"id": "bd-1", "status": "open", "labels": [], "dependencies": []},
        {
            "id": "bd-2",
            "status": "open",
            "labels": [],
            "dependencies": [{"id": "bd-1", "dependency_type": "blocks", "status": "open"}],
        },
    ]
    br = Br({"list": beads, "ready": [], "show": show_beads})
    b = make_board(br)
    features = b.list_features()

    # ONE batch show call — not one per card
    show_calls = br.cmds("show")
    assert len(show_calls) == 1
    assert "bd-1" in show_calls[0] and "bd-2" in show_calls[0]

    bd2 = next(f for f in features if f["id"] == "bd-2")
    assert bd2["depends_on"] == ["bd-1"]
    assert bd2["open_depends_on"] == ["bd-1"]

    bd1 = next(f for f in features if f["id"] == "bd-1")
    assert bd1["depends_on"] == []
    assert bd1["open_depends_on"] == []


def test_list_features_empty_board_skips_show(make_board):
    """`br show` with no arguments is an error; list_features must not call it when
    the board has zero cards."""
    br = Br({"list": [], "ready": []})
    b = make_board(br)
    b.list_features()
    assert not br.cmds("show")  # no show call issued


# ── #201: blocked features float to the top of the projection ───────────────────
# A blocked card is the board's loudest "needs attention" signal, so list_features
# sorts blocked ahead of everything else; priority (0 = highest) still ranks within
# each group, id as the stable tiebreak.


def test_list_features_sorts_blocked_to_top(make_board):
    """Mixed blocked/non-blocked: every blocked feature precedes every non-blocked
    one, and each group keeps its own priority order."""
    beads = [
        {"id": "bd-1", "status": "open", "labels": [], "priority": 0},
        {"id": "bd-2", "status": "open", "labels": ["blocked"], "priority": 2},
        {"id": "bd-3", "status": "open", "labels": ["blocked"], "priority": 0},
        {"id": "bd-4", "status": "open", "labels": [], "priority": 1},
    ]
    br = Br({"list": beads, "ready": []})
    b = make_board(br)
    assert [f["id"] for f in b.list_features()] == ["bd-3", "bd-2", "bd-1", "bd-4"]


def test_list_features_sorts_in_progress_second_after_blocked(make_board):
    """#223: in_progress (actively building) ranks as its own second tier — after
    every blocked feature, before everything else — so the operator never scans
    the full list to find what a coder is working on. Priority still orders
    within each tier. An in_review row (in_progress status + label) is NOT in
    the building tier."""
    beads = [
        {"id": "bd-1", "status": "open", "labels": ["ready"], "priority": 0},
        {"id": "bd-2", "status": "in_progress", "labels": [], "priority": 2},
        {"id": "bd-3", "status": "open", "labels": ["blocked"], "priority": 3},
        {"id": "bd-4", "status": "in_progress", "labels": [], "priority": 0},
        {"id": "bd-5", "status": "in_progress", "labels": ["in-review"], "priority": 0},
    ]
    br = Br({"list": beads, "ready": []})
    b = make_board(br)
    assert [f["id"] for f in b.list_features()] == ["bd-3", "bd-4", "bd-2", "bd-1", "bd-5"]


def test_list_features_blocked_ties_break_on_id(make_board):
    """Equal-priority blocked features keep the stable id tiebreak."""
    beads = [
        {"id": "bd-b", "status": "open", "labels": ["blocked"], "priority": 1},
        {"id": "bd-a", "status": "open", "labels": ["blocked"], "priority": 1},
    ]
    br = Br({"list": beads, "ready": []})
    b = make_board(br)
    assert [f["id"] for f in b.list_features()] == ["bd-a", "bd-b"]


def test_list_features_sort_unchanged_when_nothing_blocked(make_board):
    """With no blocked features the order is exactly the pre-#201 sort:
    priority ascending, then id."""
    beads = [
        {"id": "bd-1", "status": "open", "labels": [], "priority": 2},
        {"id": "bd-2", "status": "open", "labels": [], "priority": 0},
        {"id": "bd-3", "status": "open", "labels": [], "priority": 1},
    ]
    br = Br({"list": beads, "ready": []})
    b = make_board(br)
    assert [f["id"] for f in b.list_features()] == ["bd-2", "bd-3", "bd-1"]


def test_find_by_external_ref_scan_is_unbounded(make_board):
    """record_merge's PR lookup scans `br list` unfiltered — it too must be unbounded,
    else a merge webhook for a feature past row 50 would silently never close it."""
    br = Br({"list": []})
    b = make_board(br)
    b._find_by_external_ref("https://example/pr/1")
    (list_call,) = br.cmds("list")
    assert _passes_unlimited(list_call)


def test_claim_next_ready_query_is_unbounded(make_board):
    """`br ready` defaults `--limit 20`; the puller filters `feature`/blocked in Python
    AFTER, so a capped queue could hide the only claimable feature past row 20."""
    br = Br({"ready": []})
    b = make_board(br)
    b.claim_next_ready()
    (ready_call,) = br.cmds("ready")
    assert _passes_unlimited(ready_call)


def test_ready_queue_query_is_unbounded(make_board):
    """The puller's queue must see EVERY ready feature — `br ready` is unbounded here too."""
    br = Br({"ready": [], "list": []})
    b = make_board(br)
    b.ready_queue()
    (ready_call,) = br.cmds("ready")
    assert _passes_unlimited(ready_call)


def test_raw_features_with_comments_listing_is_unbounded(make_board):
    """board_retro's source delegates its listing to list_features, so its exhaustive
    scan of terminal features inherits `--limit 0` — never capped at 50."""
    br = Br({"list": [], "ready": []})
    b = make_board(br)
    b.raw_features_with_comments()
    assert br.cmds("list")  # a list query actually ran
    assert all(_passes_unlimited(c) for c in br.cmds("list"))


# ── #226 S2 read helper: per-feature comment text via `br show` ──────────────────


def test_feature_comments_returns_the_comment_text_history(make_board):
    """A per-feature read (`br show`, which carries the comment thread) — normalizes
    each comment to its text however `br` shapes the entry, oldest-first. This is what
    the coder-monitor read side filters for `coder-monitor:` snapshots (#226)."""
    bead = {
        "id": "bd-1",
        "status": "in_progress",
        "labels": [],
        "comments": [
            {"text": "  attempt 1 (tier=fast): failed  "},
            {"body": 'coder-monitor: {"gen": 1}'},
            {"content": ""},  # empty → dropped
            "bare string comment",
        ],
    }
    b = make_board(Br({"show": [bead]}))
    assert b.feature_comments("bd-1") == [
        "attempt 1 (tier=fast): failed",
        'coder-monitor: {"gen": 1}',
        "bare string comment",
    ]


def test_feature_comments_empty_for_unknown_or_commentless_feature(make_board):
    assert make_board(Br({"show": []})).feature_comments("nope") == []
    assert make_board(Br({"show": [{"id": "bd-2", "status": "open"}]})).feature_comments("bd-2") == []


# ── #113: the requirement ledger — decompose, dispose, and the notes round-trip ───
# Acceptance criteria become tracked items ({id, text, status}) so partial completion
# is distinguishable from completion: prose stays the authoring interface, mark_ready
# decomposes it, the coder disposes per item, and the completion gate (loop) reads the
# ledger back. Stored as `req: {…json…}` lines in the shared bead `notes` field.


def test_decompose_ac_unbulleted_prose_is_a_single_item():
    items = store._decompose_ac("The system SHALL frob the widget\nwhenever asked.")
    assert items == [{"id": "r1", "text": "The system SHALL frob the widget whenever asked.", "status": "open"}]


def test_decompose_ac_bulleted_list_yields_one_item_per_bullet():
    ac = "- restore dict tolerance\n* cut em dashes to under 12\n+ update CHANGELOG.md\n2. gate green"
    items = store._decompose_ac(ac)
    assert [i["id"] for i in items] == ["r1", "r2", "r3", "r4"]
    assert [i["text"] for i in items] == [
        "restore dict tolerance",
        "cut em dashes to under 12",
        "update CHANGELOG.md",
        "gate green",
    ]
    assert all(i["status"] == "open" for i in items)


def test_decompose_ac_continuation_lines_join_their_bullet():
    items = store._decompose_ac("- a requirement\n  spanning two lines\n- another")
    assert [i["text"] for i in items] == ["a requirement spanning two lines", "another"]


def test_decompose_ac_blank_prose_yields_no_ledger():
    assert store._decompose_ac("") == []
    assert store._decompose_ac("   \n  ") == []


# ── #115: archival — terminal features leave the live view, never the record ─────
# The health sweep labels done/cancelled features whose closed_at aged past the
# window `archived`; the default list_features projection drops them (queryable back
# via include_archived=True) and board_retro's source EXPLICITLY opts in — a retro
# that inherited the default exclusion would silently shrink to the archive window.

# A fixed "now" so the window math is deterministic (no clock dependence).
_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc).timestamp()


def test_parse_closed_at_accepts_iso_and_epoch_rejects_junk():
    assert store._parse_closed_at("2026-07-25T12:00:00Z") == _NOW
    assert store._parse_closed_at("2026-07-25T12:00:00+00:00") == _NOW
    assert store._parse_closed_at("2026-07-25T12:00:00") == _NOW  # naive → taken as UTC
    assert store._parse_closed_at(_NOW) == _NOW  # bare epoch passes through
    # absent/unparseable → None: the archive pass must never archive on a guess
    assert store._parse_closed_at(None) is None
    assert store._parse_closed_at("") is None
    assert store._parse_closed_at("yesterday-ish") is None
    assert store._parse_closed_at(True) is None


def test_archive_stale_labels_only_terminal_features_past_the_window(make_board):
    beads = [
        {"id": "bd-old-done", "status": "closed", "labels": [], "closed_at": "2026-07-10T12:00:00Z"},
        {"id": "bd-old-cxl", "status": "closed", "labels": ["cancelled"], "closed_at": "2026-07-01T00:00:00Z"},
        {"id": "bd-fresh", "status": "closed", "labels": [], "closed_at": "2026-07-23T12:00:00Z"},  # 2 days — keep
        {"id": "bd-open", "status": "open", "labels": [], "closed_at": "2026-01-01T00:00:00Z"},  # not terminal
        {"id": "bd-no-ts", "status": "closed", "labels": [], "closed_at": None},  # unparseable → left visible
    ]
    br = Br({"list": beads})
    b = make_board(br)
    assert set(b.archive_stale(archive_after_days=7, now=_NOW)) == {"bd-old-done", "bd-old-cxl"}
    labeled = {a[1] for a in br.cmds("update") if ("--add-label", "archived") == a[-2:]}
    assert labeled == {"bd-old-done", "bd-old-cxl"}
    # ARCHIVAL, NOT DELETION (#115): the pass writes labels only — it never
    # deletes/closes a bead (the JSONL record is untouched).
    assert not br.cmds("delete") and not br.cmds("close")


def test_archive_stale_skips_already_archived_features(make_board):
    """An archived bead is out of the default listing, so a second sweep never
    re-writes its label (no redundant br update per sweep)."""
    beads = [{"id": "bd-arch", "status": "closed", "labels": ["archived"], "closed_at": "2026-01-01T00:00:00Z"}]
    br = Br({"list": beads})
    assert make_board(br).archive_stale(archive_after_days=7, now=_NOW) == []
    assert not br.cmds("update")


def test_list_features_hides_archived_by_default_and_flag_restores(make_board):
    beads = [
        {"id": "bd-arch", "status": "closed", "labels": ["archived"]},
        {"id": "bd-live", "status": "closed", "labels": []},
    ]
    br = Br({"list": beads})
    b = make_board(br)
    assert [f["id"] for f in b.list_features()] == ["bd-live"]
    assert [f["id"] for f in b.list_features(include_archived=True)] == ["bd-arch", "bd-live"]
    # the state filter composes with the archive scope the same way
    assert [f["id"] for f in b.list_features(state="done")] == ["bd-live"]
    assert [f["id"] for f in b.list_features(state="done", include_archived=True)] == ["bd-arch", "bd-live"]


def test_list_features_projection_spans_feature_and_task_not_epic_milestone(make_board):
    """#303 (r1/r2): the board projection must include task-type beads — they ride the
    SAME rails as coding features (ready → claim → in_progress → in_review) — while
    structural epic/milestone beads stay out. The query passes the shared
    PULLABLE_ISSUE_TYPES as repeatable `--type feature --type task` args; the old
    `--type feature`-only query dropped every task from the projection (and so from
    board_list / GET /features, both thin pass-throughs), which also left the sweep's
    task orphan-recovery and terminal-task archival branches structurally unreachable."""
    everything = [
        {"id": "bd-feat", "issue_type": "feature", "status": "in_progress", "labels": []},
        {"id": "bd-task", "issue_type": "task", "status": "in_progress", "labels": []},
        {"id": "bd-epic", "issue_type": "epic", "status": "open", "labels": []},
        {"id": "bd-ms", "issue_type": "milestone", "status": "open", "labels": []},
    ]

    def _typed_list(args):
        # Honor the `--type` filter beads applies server-side, so the exclusion is
        # proven behaviorally — not merely by inspecting the query args below.
        wanted = {args[i + 1] for i, a in enumerate(args) if a == "--type"}
        return [b for b in everything if b["issue_type"] in wanted]

    br = Br({"list": _typed_list})
    b = make_board(br)
    ids = {f["id"] for f in b.list_features()}
    assert ids == {"bd-feat", "bd-task"}  # the task IS in the projection…
    assert "bd-epic" not in ids and "bd-ms" not in ids  # …epic/milestone are not

    # …and the query carries the SHARED PULLABLE_ISSUE_TYPES as repeatable `--type` args
    # (one constant feeds both this projection and ready_queue), never epic/milestone.
    list_call = br.cmds("list")[0]
    passed = {list_call[i + 1] for i, a in enumerate(list_call) if a == "--type"}
    assert passed == set(store.PULLABLE_ISSUE_TYPES) == {"feature", "task"}
    assert "epic" not in list_call and "milestone" not in list_call


def test_archive_stale_archives_a_terminal_task(make_board):
    """#303 (r4): a task rides the same board rails, so a terminal (closed) task past
    the archive window is aged out of the live projection exactly like a feature — the
    archive pass consumes ``list_features``, which now surfaces tasks. A fresh terminal
    task (inside the window) is left visible."""
    beads = [
        {"id": "bd-task-done", "issue_type": "task", "status": "closed", "closed_at": "2026-07-01T00:00:00Z"},
        # a fresh terminal task (2 days old, inside the 7-day window) stays visible
        {"id": "bd-task-fresh", "issue_type": "task", "status": "closed", "closed_at": "2026-07-23T12:00:00Z"},
    ]
    br = Br({"list": beads})
    b = make_board(br)
    assert b.archive_stale(archive_after_days=7, now=_NOW) == ["bd-task-done"]
    labeled = {a[1] for a in br.cmds("update") if ("--add-label", "archived") == a[-2:]}
    assert labeled == {"bd-task-done"}


def test_project_exposes_archived_and_closed_at(make_board):
    b = make_board(Br())
    f = b._project({"id": "x", "status": "closed", "labels": ["archived"], "closed_at": "2026-07-01T00:00:00Z"})
    assert f["archived"] is True and f["closed_at"] == "2026-07-01T00:00:00Z"
    assert f["board_state"] == "done"  # archived is visibility, not a board state
    g = b._project({"id": "y", "status": "open", "labels": []})
    assert g["archived"] is False and g["closed_at"] == ""


def test_retro_source_still_sees_a_feature_past_the_archive_window(make_board):
    """THE TRAP (#115): board_retro must NOT inherit the default archive exclusion.
    A feature closed far past the window (and already archived) still appears in the
    retro's source AND in retro.summarize's output — else every retrospective
    silently becomes 'the last archive_after_days days'."""
    ancient = {
        "id": "bd-ancient",
        "status": "closed",
        "labels": ["archived"],
        "closed_at": "2026-01-01T00:00:00Z",
        "comments": [{"text": "attempt 1 (tier=smart): CI fail: pytest exploded"}],
    }
    br = Br({"list": [ancient], "show": lambda args: [ancient]})
    b = make_board(br)
    raw = b.raw_features_with_comments()
    assert [f["id"] for f in raw] == ["bd-ancient"]  # explicit include_archived opt-in
    from project_board import retro

    d = retro.summarize(raw)  # the exact pipeline board_retro runs
    assert d["n_features"] == 1
    assert [f["id"] for f in d["features"]] == ["bd-ancient"]


def test_all_items_disposed_true_only_when_every_item_is_closed():
    done = {"id": "r1", "text": "a", "status": "done"}
    declined = {"id": "r2", "text": "b", "status": "declined", "decline_reason": "not reachable"}
    assert store._all_items_disposed([done, declined]) is True
    assert store._all_items_disposed([done, declined, {"id": "r3", "text": "c", "status": "open"}]) is False
    assert store._all_items_disposed([]) is True  # no ledger → nothing gates


def test_apply_requirement_dispositions_merges_closed_statuses_only():
    items = [
        {"id": "r1", "text": "a", "status": "open"},
        {"id": "r2", "text": "b", "status": "open"},
        {"id": "r3", "text": "c", "status": "open"},
    ]
    out = store.apply_requirement_dispositions(
        items,
        [
            {"id": "r1", "status": "done"},
            {"id": "r2", "status": "declined", "decline_reason": "dicts not reachable through SqliteSaver"},
            {"id": "r9", "status": "done"},  # unknown id — a reply can't invent ledger rows
            {"id": "r3", "status": "open"},  # `open` is not a disposition — ignored
        ],
    )
    assert out[0]["status"] == "done"
    assert out[1]["status"] == "declined" and out[1]["decline_reason"] == "dicts not reachable through SqliteSaver"
    assert out[2]["status"] == "open"  # silence (and non-closed statuses) leave the item open
    assert items[0]["status"] == "open"  # inputs never mutated


def test_apply_requirement_dispositions_done_clears_a_stale_decline_reason():
    items = [{"id": "r1", "text": "a", "status": "declined", "decline_reason": "old"}]
    out = store.apply_requirement_dispositions(items, [{"id": "r1", "status": "done"}])
    assert out[0]["status"] == "done" and "decline_reason" not in out[0]


def test_render_and_split_notes_round_trip_the_ledger_beside_files_and_source():
    items = [
        {"id": "r1", "text": "restore dict tolerance", "status": "done"},
        {"id": "r2", "text": "update CHANGELOG.md", "status": "declined", "decline_reason": "no changelog"},
    ]
    notes = store._render_notes(["a.py", "b.py"], "acme/widgets#8", items)
    files, src, reqs = store._split_notes(notes)
    assert files == ["a.py", "b.py"]  # req/source lines never leak into the file list
    assert src == "acme/widgets#8"
    assert reqs == items


def test_split_notes_drops_a_malformed_req_line_never_a_file():
    files, _src, reqs = store._split_notes('a.py\nreq: {not json}\nreq: {"id": "r1", "status": "open", "text": "t"}')
    assert files == ["a.py"]  # the malformed line must not poison files_to_modify (#110 path check)
    assert reqs == [{"id": "r1", "status": "open", "text": "t"}]


def test_mark_ready_decomposes_the_ac_into_ledger_items_in_notes(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "- alpha\n- beta",
        "files_to_modify": ["a.py (new)"],
        "requirements": [],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)
    b.mark_ready("bd-1")
    notes_call = next(c for c in br.cmds("update") if any(str(t).startswith("--notes=") for t in c))
    notes = next(t for t in notes_call if str(t).startswith("--notes="))[len("--notes=") :]
    files, _src, reqs = store._split_notes(notes)
    assert files == ["a.py (new)"]  # the files half rides along untouched
    assert [(i["id"], i["text"], i["status"]) for i in reqs] == [("r1", "alpha", "open"), ("r2", "beta", "open")]
    # the ready label still lands (its own update, the pinned shape)
    assert ("update", "bd-1", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_mark_ready_preserves_an_existing_ledger(make_board, monkeypatch):
    """A re-mark (requeue → ready) must never wipe recorded dispositions back to open."""
    br = Br()
    b = make_board(br)
    feature = {
        "id": "bd-1",
        "board_state": "ready",
        "spec": "s",
        "acceptance_criteria": "- alpha\n- beta",
        "files_to_modify": ["a.py (new)"],
        "requirements": [{"id": "r1", "text": "alpha", "status": "done"}],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)
    b.mark_ready("bd-1")
    assert not any(any(str(t).startswith("--notes=") for t in c) for c in br.calls)  # no re-decompose
    assert ("update", "bd-1", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_set_requirements_writes_the_ledger_and_keeps_the_other_notes_halves(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "files_to_modify": ["a.py"], "source_issue": "acme/widgets#8", "labels": []},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    items = [{"id": "r1", "text": "alpha", "status": "declined", "decline_reason": "not reachable"}]
    b.set_requirements("bd-1", items)
    (call,) = br.cmds("update")
    notes = next(t for t in call if str(t).startswith("--notes="))[len("--notes=") :]
    files, src, reqs = store._split_notes(notes)
    assert files == ["a.py"] and src == "acme/widgets#8"  # both halves carried forward
    assert reqs == items


def test_project_exposes_the_requirement_ledger(make_board):
    b = make_board(Br())
    notes = store._render_notes(["a.py"], "", [{"id": "r1", "text": "alpha", "status": "open"}])
    f = b._project({"id": "x", "status": "open", "labels": [], "notes": notes})
    assert f["requirements"] == [{"id": "r1", "text": "alpha", "status": "open"}]
    assert f["files_to_modify"] == ["a.py"]
    assert b._project({"id": "y", "status": "open", "labels": []})["requirements"] == []


# ── #107 slice 1: the CI join — annotate_ci_status + board_list's opt-in flags ────
# board_list projected pr_url but no CI signal, so the PM had to open every PR to
# find the red ones. The join is OPT-IN (one `gh` call per PR-bearing feature — see
# annotate_ci_status's cost rationale): the default listing must cost zero gh calls,
# probes skip no-PR and terminal rows, and `failing_only` is the PM's "which
# in_review PRs are red" query.


def _fake_ci(monkeypatch, statuses):
    """Patch worktree.pr_ci_status with a canned {pr_url: (status, summary)} map;
    returns the recorded (pr_url, cwd) calls so tests can assert what was probed."""
    calls = []

    async def _probe(pr_url, *, cwd=".", log_chars=3000):
        calls.append((pr_url, cwd))
        return statuses.get(pr_url, ("none", ""))

    monkeypatch.setattr("project_board.worktree.pr_ci_status", _probe)
    return calls


def test_annotate_ci_status_probes_only_live_pr_bearing_features(make_board, monkeypatch):
    calls = _fake_ci(
        monkeypatch,
        {
            "https://pr/red": ("failing", "Failing checks:\n- gate: FAILURE"),
            "https://pr/green": ("passing", ""),
        },
    )
    b = make_board(Br(), repo="/repo")
    feats = [
        {"id": "bd-1", "board_state": "in_review", "pr_url": "https://pr/red"},
        {"id": "bd-2", "board_state": "in_progress", "pr_url": "https://pr/green"},
        {"id": "bd-3", "board_state": "backlog", "pr_url": ""},
        {"id": "bd-4", "board_state": "done", "pr_url": "https://pr/merged"},
        {"id": "bd-5", "board_state": "cancelled", "pr_url": "https://pr/dead"},
    ]
    out = b.annotate_ci_status(feats)
    assert out is feats  # annotates in place
    assert feats[0]["ci_status"] == "failing" and feats[0]["ci_summary"] == "Failing checks:\n- gate: FAILURE"
    assert feats[1]["ci_status"] == "passing" and feats[1]["ci_summary"] == ""
    # no-PR and terminal rows read "" (distinct from pr_ci_status's "none" = probed,
    # no checks) and — the cost guard — were never probed at all.
    assert all(feats[i]["ci_status"] == "" for i in (2, 3, 4))
    assert sorted(u for u, _ in calls) == ["https://pr/green", "https://pr/red"]
    assert all(cwd == "/repo" for _, cwd in calls)  # gh runs in the board's repo


def test_annotate_ci_status_drops_the_log_block_from_the_summary(make_board, monkeypatch):
    """The listing keeps the failing check NAMES; the truncated-log block pr_ci_status
    appends after a blank line stays out of the board projection."""
    summary = "Failing checks:\n- gate: FAILURE\n\nFailing log (truncated):\nE  assert 1 == 2"
    _fake_ci(monkeypatch, {"https://pr/1": ("failing", summary)})
    b = make_board(Br())
    (f,) = b.annotate_ci_status([{"id": "bd-1", "board_state": "in_review", "pr_url": "https://pr/1"}])
    assert f["ci_summary"] == "Failing checks:\n- gate: FAILURE"


def test_annotate_ci_status_without_probeable_rows_never_touches_worktree(make_board, monkeypatch):
    calls = _fake_ci(monkeypatch, {})
    b = make_board(Br())
    feats = [{"id": "bd-1", "board_state": "backlog", "pr_url": ""}]
    assert b.annotate_ci_status(feats) == [
        {"id": "bd-1", "board_state": "backlog", "pr_url": "", "ci_status": "", "ci_summary": ""}
    ]
    assert calls == []


def test_annotate_ci_status_survives_a_running_event_loop(make_board, monkeypatch):
    """The sync store bridges to the async probe with asyncio.run — which raises if
    the calling thread already runs a loop. The bridge must hop to a private thread
    instead, so a caller inside an event loop still gets the join."""
    _fake_ci(monkeypatch, {"https://pr/1": ("passing", "")})
    b = make_board(Br())

    async def _call_from_async():
        return b.annotate_ci_status([{"id": "bd-1", "board_state": "in_review", "pr_url": "https://pr/1"}])

    (f,) = asyncio.run(_call_from_async())
    assert f["ci_status"] == "passing"


# ── board_list's with_ci / failing_only flags (the tool boundary) ─────────────────


class _CiStore:
    """A fake store for board_list's CI flags: canned projections, the recorded
    list_features call, and an annotate that stamps statuses by pr_url."""

    def __init__(self, feats, statuses=None):
        self.feats = feats
        self.statuses = statuses or {}
        self.listed = None
        self.annotated = False

    def list_features(self, state=None, include_archived=False):
        self.listed = (state, include_archived)
        return [dict(f) for f in self.feats if state is None or f["board_state"] == state]

    def annotate_ci_status(self, feats):
        self.annotated = True
        for f in feats:
            f["ci_status"], f["ci_summary"] = self.statuses.get(f["pr_url"], ("", ""))
        return feats


def _feat(fid, state, pr=""):
    return {
        "id": fid,
        "title": fid,
        "board_state": state,
        "blocked": False,
        "pr_url": pr,
        "priority": 2,
        "difficulty": "",
    }


def _list_tool():
    return {t.name: t for t in pb._board_tools({})}["board_list"]


def test_board_list_default_omits_ci_and_never_probes(monkeypatch):
    """The cost contract: a plain board_list makes ZERO gh calls and its rows carry
    no ci keys — the join only runs when the caller opts in."""
    fake = _CiStore([_feat("bd-1", "in_review", "https://pr/1")])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    (row,) = json.loads(_list_tool().invoke({}))
    assert fake.annotated is False
    assert "ci_status" not in row and "ci_summary" not in row


def test_board_list_with_ci_joins_status_and_failing_summary(monkeypatch):
    fake = _CiStore(
        [_feat("bd-1", "in_review", "https://pr/red"), _feat("bd-2", "backlog")],
        {"https://pr/red": ("failing", "Failing checks:\n- gate: FAILURE")},
    )
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    by_id = {r["id"]: r for r in json.loads(_list_tool().invoke({"with_ci": True}))}
    assert by_id["bd-1"]["ci_status"] == "failing"
    assert by_id["bd-1"]["ci_summary"] == "Failing checks:\n- gate: FAILURE"
    # a row with nothing to report carries the (stable) status key but no empty summary
    assert by_id["bd-2"]["ci_status"] == "" and "ci_summary" not in by_id["bd-2"]


def test_board_list_failing_only_defaults_to_in_review_and_keeps_only_red(monkeypatch):
    fake = _CiStore(
        [
            _feat("bd-red", "in_review", "https://pr/red"),
            _feat("bd-green", "in_review", "https://pr/green"),
            _feat("bd-wip", "in_progress", "https://pr/wip-red"),
        ],
        {
            "https://pr/red": ("failing", "Failing checks:\n- gate: FAILURE"),
            "https://pr/green": ("passing", ""),
            "https://pr/wip-red": ("failing", "Failing checks:\n- gate: FAILURE"),
        },
    )
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    out = json.loads(_list_tool().invoke({"failing_only": True}))
    assert fake.listed == ("in_review", False)  # no explicit state → the PM's in_review query
    assert [r["id"] for r in out] == ["bd-red"]  # green dropped; in_progress never listed
    assert out[0]["ci_status"] == "failing"


def test_board_list_failing_only_honors_an_explicit_state(monkeypatch):
    fake = _CiStore(
        [_feat("bd-red", "in_review", "https://pr/red"), _feat("bd-wip", "in_progress", "https://pr/wip-red")],
        {
            "https://pr/red": ("failing", "Failing checks:\n- x: FAILURE"),
            "https://pr/wip-red": ("failing", "Failing checks:\n- x: FAILURE"),
        },
    )
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    out = json.loads(_list_tool().invoke({"failing_only": True, "state": "in_progress"}))
    assert fake.listed == ("in_progress", False)
    assert [r["id"] for r in out] == ["bd-wip"]


# ── #138: `br --json` envelope normalization at the `_run` choke point ────────────
#
# These fake `subprocess.run` (not `_run`) so the REAL seam parses + normalizes — the
# exact code path both br versions hit. A `db=` board makes `_ensure_workspace` a noop
# (test_ensure_workspace_noop_with_explicit_db), so no `br init` shells.


def _json_board(monkeypatch, stdout):
    """A board whose `br` calls all return `stdout` (canned JSON) on a zero exit."""
    b = _board(monkeypatch, db="/x/.beads/beads.db")
    monkeypatch.setattr(
        store.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    )
    return b


# The 0.1.x bare list, the 0.2.x envelope, a bare `br show` bead (no "issues" key), empty.
# RED-IS-REACHABLE (r5): collapse `_issues_envelope` to `return parsed["issues"]` and the
# bare-list/bare-dict/None cases below throw or mismatch; collapse it to `return parsed`
# and the two envelope cases keep the dict instead of the list. Handling ONE shape fails
# the OTHER — the same signal the br_shape CI matrix surfaces across the two real binaries.
@pytest.mark.parametrize(
    "payload,expected",
    [
        ("[]", []),  # 0.1.x: bare empty list
        ('[{"id": "bd-1"}, {"id": "bd-2"}]', [{"id": "bd-1"}, {"id": "bd-2"}]),  # 0.1.x: bare list
        ('{"issues": [], "total": 0, "limit": 0, "offset": 0, "has_more": false}', []),  # 0.2.x: empty envelope
        ('{"issues": [{"id": "bd-1"}], "total": 1, "has_more": false}', [{"id": "bd-1"}]),  # 0.2.x: envelope
        ('{"id": "bd-1", "status": "open", "labels": []}', {"id": "bd-1", "status": "open", "labels": []}),  # br show
        ("", None),  # no output
    ],
)
def test_run_normalizes_both_json_envelope_shapes(monkeypatch, payload, expected):
    assert _json_board(monkeypatch, payload)._run("list", want_json=True) == expected


def test_issues_envelope_helper_is_non_breaking_in_both_directions():
    """The pure normalizer, direct: a bare list survives untouched (downgrade path), an
    envelope unwraps to its issues (upgrade path), and a bare `br show` bead dict — which
    has no `issues` key — passes through so the `rows[0] if isinstance(rows, list) else rows`
    call sites still take their `else` branch."""
    assert store._issues_envelope([]) == []
    assert store._issues_envelope([{"id": "bd-1"}]) == [{"id": "bd-1"}]
    assert store._issues_envelope({"issues": [{"id": "bd-1"}], "has_more": False}) == [{"id": "bd-1"}]
    assert store._issues_envelope({"id": "bd-1", "status": "open"}) == {"id": "bd-1", "status": "open"}
    assert store._issues_envelope(None) is None


def test_run_returns_has_more_only_when_the_envelope_carries_it(monkeypatch):
    """has_more is the truncation signal, guarded on SHAPE presence: a bool off the
    0.2.x envelope, None whenever the `has_more` key is absent (0.1.x bare list, or a
    bare `br show` bead) — so a check on it can never be a version sniff. It rides the
    RETURN value (`with_has_more` → `(payload, has_more)`), never instance state."""
    b = _json_board(monkeypatch, '{"issues": [], "total": 99, "has_more": true}')
    assert b._run("list", want_json=True, with_has_more=True) == ([], True)
    monkeypatch.setattr(  # a 0.1.x-shaped payload has no envelope → has_more is None
        store.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
    )
    assert b._run("list", want_json=True, with_has_more=True) == ([], None)


def test_run_keeps_no_shared_has_more_state(monkeypatch):
    """The #258 race: with the old `_last_json_has_more` stash, caller B's `_run` could
    overwrite caller A's truncation signal between A's write and A's read once the loop
    and routes offload store calls to threads. The signal now travels only in each
    call's return value — the instance carries no stash at all."""
    b = _json_board(monkeypatch, '{"issues": [], "has_more": true}')
    b._run("list", want_json=True)
    assert not hasattr(b, "_last_json_has_more")


def test_run_has_more_is_race_free_across_concurrent_callers(monkeypatch):
    """Two threads inside `_run` at once — one query truncated, one not — held at a
    barrier AFTER the subprocess call (which is single-flight per process since #290,
    so the overlap has to happen downstream of it) so their parse/return paths
    genuinely interleave. Each caller must read exactly its own has_more off its own
    return value (the old shared stash lost this race)."""
    b = _board(monkeypatch, db="/x/.beads/beads.db")
    barrier = threading.Barrier(2, timeout=5)

    def fake_run(cmd, **_k):
        stdout = '{"issues": [], "has_more": true}' if "list" in cmd else "[]"
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    real_envelope = store._issues_envelope

    def interleaved_envelope(parsed):
        barrier.wait()  # both callers are past the br lock, inside their own parse/return
        return real_envelope(parsed)

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store, "_issues_envelope", interleaved_envelope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        truncated = pool.submit(b._run, "list", want_json=True, with_has_more=True)
        clean = pool.submit(b._run, "ready", want_json=True, with_has_more=True)
        assert truncated.result(timeout=5) == ([], True)
        assert clean.result(timeout=5) == ([], None)


# ── #258: blocking `br` work belongs OFF the event-loop thread ────────────────────
# `_run` blocks in subprocess.run (30s timeout) and time.sleep (the contention
# backoff) — on the event-loop thread that stalls every coroutine. These pin the
# seam both ways: an async caller that offloads via asyncio.to_thread reaches `_run`
# on a worker thread, and a `_run` executed ON the loop thread is loudly detected.


def test_store_call_offloaded_via_to_thread_runs_off_the_loop_thread(make_board):
    """The seam the loop tick / API routes consume (their offload cards ride this):
    patch `_run`, record the executing thread, and drive a store read from a coroutine
    through `asyncio.to_thread` — every `_run` lands on a worker thread, never the
    thread running the event loop."""
    seen = []

    def run_impl(*args, want_json=False, with_has_more=False):
        seen.append(threading.current_thread())
        val = [] if want_json else ""
        return (val, None) if with_has_more else val

    b = make_board(run_impl)

    async def route():
        assert threading.current_thread() is threading.main_thread()  # the loop thread
        return await asyncio.to_thread(b.list_features)

    assert asyncio.run(route()) == []
    assert seen and all(t is not threading.main_thread() for t in seen)


def test_run_warns_when_invoked_on_the_event_loop_thread(monkeypatch, caplog):
    """The detector half of the seam: `_run` ON a running loop's thread is exactly the
    blocking-subprocess bug (#258) and must be loudly visible. Observational (warn, not
    raise): legacy on-loop call sites keep working until their offload cards land."""
    b = _json_board(monkeypatch, "[]")

    async def bad_caller():
        return b._run("list", want_json=True)  # blocking ON the loop thread

    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert asyncio.run(bad_caller()) == []
    assert any("event-loop thread" in m for m in caplog.messages)


def test_run_stays_quiet_off_the_event_loop_thread(monkeypatch, caplog):
    """The correct pattern — `_run` reached via asyncio.to_thread (no running loop on
    the executing thread) — must not warn, so the detector never cries wolf."""
    b = _json_board(monkeypatch, "[]")

    async def good_caller():
        return await asyncio.to_thread(b._run, "list", want_json=True)

    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert asyncio.run(good_caller()) == []
    assert not any("event-loop thread" in m for m in caplog.messages)


def test_list_features_raises_when_unbounded_query_reports_truncation(monkeypatch):
    """#114/#138: a `--limit 0` page that STILL reports has_more=true means the query was
    truncated and the projection would be silently incomplete — list_features fails loud.
    The raise fires on the first `_run` (the list query), before ready_queue()."""
    envelope = '{"issues": [{"id": "bd-1", "issue_type": "feature", "status": "open", "labels": []}], "has_more": true}'
    b = _json_board(monkeypatch, envelope)
    with pytest.raises(BoardError, match="has_more=true"):
        b.list_features()


def test_list_features_trusts_limit_zero_when_no_has_more_shape(monkeypatch):
    """The 0.1.x fallback: no envelope → `_run` returns has_more=None → the truncation
    guard never fires and `--limit 0` is trusted exactly as before (non-breaking downgrade)."""
    b = _json_board(monkeypatch, "[]")  # every br call returns a bare empty list
    assert b.list_features() == []


def test_shared_file_gate_ignores_same_filename_in_a_different_project(make_board, monkeypatch):
    """#197: bd-ke7 (discord) and bd-qjd (promptlab) both name PROTO.md — different
    repos, no collision possible; the gate must scope to the SAME project."""
    br = Br()
    b = make_board(br)
    first = _shared_feature("bd-ke7", ["PROTO.md (new)"], board_state="in_progress", project="discord")
    second = _shared_feature("bd-qjd", ["PROTO.md (new)"], project="promptlab")
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [first, second])
    b.mark_ready("bd-qjd")
    assert ("update", "bd-qjd", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_shared_file_gate_still_refuses_within_the_same_project(make_board, monkeypatch):
    """Project scoping must not loosen the intra-project gate (#143)."""
    br = Br()
    b = make_board(br)
    first = _shared_feature("bd-a", ["PROTO.md (new)"], board_state="in_progress", project="discord")
    second = _shared_feature("bd-b", ["PROTO.md (new)"], project="discord")
    monkeypatch.setattr(b, "get_feature", lambda fid: second)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: [first, second])
    with pytest.raises(BoardError, match="Shared-file gate"):
        b.mark_ready("bd-b")


# ── task-type beads (#217): delivery, verification (the task Done edge), the puller ──
# A bead with issue_type='task' rides the same rails (ready → claim → in_progress →
# in_review) but ships a DELIVERABLE instead of a PR: record_delivery is its
# open_pr→open_review edge, record_verification its Done edge (a second `br close`
# beside record_merge, like cancel_feature is for cancellation).


def test_record_delivery_comments_the_text_and_moves_to_in_review(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    f = b.record_delivery("bd-t", text="triage report written to docs/triage.md")
    assert ("comments", "add", "bd-t", "deliverable: triage report written to docs/triage.md") in br.calls
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")  # no ref → external_ref untouched
    assert f["board_state"] == "in_review"


def test_record_delivery_with_a_ref_sets_external_ref(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", text="ADR written", ref="https://docs.example/adr/0099-task.md")
    assert (
        "update",
        "bd-t",
        "--add-label",
        "in-review",
        "--external-ref",
        "https://docs.example/adr/0099-task.md",
    ) in br.calls


def test_record_delivery_strips_the_ref_before_persisting(make_board, monkeypatch):
    """normalize_external_ref hands `br` the STRIPPED URL — no whitespace ride-along."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", ref="  https://docs.example/adr/0099.md  ")
    assert (
        "update",
        "bd-t",
        "--add-label",
        "in-review",
        "--external-ref",
        "https://docs.example/adr/0099.md",
    ) in br.calls


@pytest.mark.parametrize(
    "bad_ref",
    [
        "ftp://files.example/report.pdf",  # a real URL, wrong scheme
        "artifact://y",  # a made-up scheme
        "https://",  # http(s) scheme but no host — not a usable link
        "//files.example/report.pdf",  # protocol-relative — link-shaped, no fixed scheme
    ],
)
def test_record_delivery_refuses_a_non_http_link_ref_with_nothing_written(make_board, monkeypatch, bad_ref):
    """A LINK-SHAPED ref (it has a scheme, or a protocol-relative //host) must be an
    absolute http(s) URL to land on external_ref — the slot the board renders as a
    live link — so anything else is refused with a named rule, BEFORE the deliverable
    comment or the state move lands (no half-applied delivery). Scheme-less artifact
    paths are NOT refused — they ride the deliverable comment (tests below)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    with pytest.raises(BoardError, match=r"record_delivery ref must be an absolute http\(s\) URL"):
        b.record_delivery("bd-t", text="ADR written", ref=bad_ref)
    assert br.cmds("update") == [] and br.cmds("comments") == []  # nothing written on the refusal


def test_record_delivery_accepts_plain_http_refs_too(make_board, monkeypatch):
    """The gate is http(s), not https-only — an internal http doc host is a valid ref."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", ref="http://wiki.internal/adr/7")
    assert ("update", "bd-t", "--add-label", "in-review", "--external-ref", "http://wiki.internal/adr/7") in br.calls


def test_record_delivery_still_accepts_a_relative_artifact_ref(make_board, monkeypatch):
    """The pre-hardening contract holds: a scheme-less artifact path is a first-class
    deliverable ref — the delivery records it and the bead moves to review. What
    changed is WHERE it lands: the path folds into the `deliverable:` comment (the
    record the projection's `deliverable` field reads back), never --external-ref,
    so the board can't mint an href from it."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    f = b.record_delivery("bd-t", text="ADR written", ref="docs/adr/0099-task.md")
    assert ("comments", "add", "bd-t", "deliverable: ADR written (docs/adr/0099-task.md)") in br.calls
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")  # the path never reaches external_ref
    assert f["board_state"] == "in_review"


def test_record_delivery_records_a_path_only_ref_as_the_deliverable(make_board, monkeypatch):
    """ref-only delivery with an artifact path: the path IS the deliverable record."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", ref="docs/adr/0099-task.md")
    assert ("comments", "add", "bd-t", "deliverable: docs/adr/0099-task.md") in br.calls
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")  # the path never reaches external_ref


def test_record_delivery_rejects_a_non_in_progress_state(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "ready", "issue_type": "task"})
    with pytest.raises(BoardError, match="record_delivery expects in_progress"):
        b.record_delivery("bd-t", text="too early")
    assert br.cmds("update") == [] and br.cmds("comments") == []  # nothing written on the refusal


def test_record_delivery_rejects_a_coding_feature(make_board, monkeypatch):
    """A coding feature taking the delivery edge would land in_review with NO
    pr_url and strand the merge reconciler — the hole open_review's pr_url
    requirement exists to plug. Code enters review via open_review only."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "feature"})
    with pytest.raises(BoardError, match="record_delivery is task-only"):
        b.record_delivery("bd-1", text="not a deliverable")
    assert br.cmds("update") == [] and br.cmds("comments") == []  # nothing written on the refusal


def test_record_delivery_stamps_delivered_by_the_assignee(make_board, monkeypatch):
    """#316 r1: an in-progress task assigned to `alice`, on delivery, records BOTH the
    deliverable AND a `delivered-by: alice` provenance stamp — and the stamp lands
    BEFORE the in_review move (the actor is captured at delivery time, not after)."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task", "assignee": "alice"}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", text="triage report written")
    assert ("comments", "add", "bd-t", "deliverable: triage report written") in br.calls
    assert ("comments", "add", "bd-t", "delivered-by: alice") in br.calls
    # provenance is a comment, never a label (actor values are free text, #101)…
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")
    # …and every provenance write precedes the in_review update (stamped "before" the move)
    update_idx = next(i for i, c in enumerate(br.calls) if c[0] == "update")
    assert all(i < update_idx for i, c in enumerate(br.calls) if c[0] == "comments")


def test_record_delivery_stamps_the_store_actor_when_unassigned(make_board, monkeypatch):
    """#316 r2: an unassigned task has no assignee to credit, so the delivery stamps
    the STORE actor as the deliverer instead."""
    br = Br()
    b = make_board(br)
    b.actor = "delivery-bot"
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task", "assignee": ""}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.record_delivery("bd-t", text="done")
    assert ("comments", "add", "bd-t", "delivered-by: delivery-bot") in br.calls


def test_record_verification_approved_closes_with_a_verified_reason(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    # A DIFFERENT identity delivered this task (`human`) than the store actor closing it
    # (`agent`), so this is a plain cross-identity verify — the self-verification flag (#316
    # S2) stays off and the close reason is bare `verified: agent`.
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": "human"},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    f = b.record_verification("bd-t", approved=True)
    # the task Done edge: a `br close` like record_merge's, with the verifier in the reason
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified: agent")]  # actor defaults to "agent"
    assert ("update", "bd-t", "--add-label", "self-verified") not in br.calls
    assert f["board_state"] == "done"


def test_record_verification_rejected_comments_feedback_and_requeues(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready"})
    f = b.record_verification("bd-t", approved=False, feedback="the report misses the Q3 numbers")
    assert ("comments", "add", "bd-t", "verification failed: the report misses the Q3 numbers") in br.calls
    # requeued through the standard edge: back to open+ready, in-review dropped — but the
    # assignee is KEPT, because on a task it is the dispatch target, not a claim marker.
    # Clearing it here made every rejected deliverable a dead card: the requeued task came
    # back unassigned and the next claim parked it "awaiting unassigned delivery" forever.
    (up,) = br.cmds("update")
    assert "--status" in up and "open" in up
    assert "--assignee" not in up  # the sister agent / `agent` must survive the rejection
    assert "--add-label" in up and "ready" in up and "in-review" in up
    assert br.cmds("close") == []  # a rejection NEVER closes
    assert f["board_state"] == "ready"


def test_requeue_still_clears_the_assignee_on_a_coding_feature(make_board, monkeypatch):
    """The task carve-out must not leak to coding features: there the assignee IS a claim
    marker, and leaving it set makes the re-pull's `br update --claim` fail with "already
    assigned to <actor>" so the feature can never be re-dispatched."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "feature"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready"})
    b.requeue("bd-1")
    (up,) = br.cmds("update")
    assert "--assignee" in up and "" in up


def test_record_verification_rejects_a_non_in_review_state(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    with pytest.raises(BoardError, match="record_verification expects in_review"):
        b.record_verification("bd-t", approved=True)


def test_record_verification_rejects_a_coding_feature(make_board, monkeypatch):
    """A coding feature closed here would dodge record_merge — the ONE Done edge
    for code (invariant #2) with its idempotency and `merged:` audit reason. The
    guard refuses BOTH branches: no verify-close, no reject-requeue."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "feature"})
    with pytest.raises(BoardError, match="record_verification is task-only"):
        b.record_verification("bd-1", approved=True)
    with pytest.raises(BoardError, match="record_verification is task-only"):
        b.record_verification("bd-1", approved=False, feedback="nope")
    assert br.cmds("close") == [] and br.cmds("update") == [] and br.cmds("comments") == []


# ── self-verification: flag (never refuse) an approval by the deliverer (#316 S2) ──


def test_record_verification_flags_self_verified_when_the_verifier_delivered(make_board, monkeypatch):
    """#316 S2 r1: a task delivered by `Alice`, approved by ` alice `, is done, carries the
    `self-verified` label, and closes with the verifier text preserved verbatim in the reason
    (`verified:  alice  (self-verified)`) while the MATCH is decided on normalized identity
    (casefold + strip) — so surrounding spaces and case never let a self-verify slip through."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": "Alice"},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    f = b.record_verification("bd-t", approved=True, by=" alice ")
    # label-safe FLAG, added before the close (the cancel_feature tag-then-close precedent)
    assert ("update", "bd-t", "--add-label", "self-verified") in br.calls
    # displayed verifier text is preserved verbatim; only the comparison normalized it
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified:  alice  (self-verified)")]
    assert f["board_state"] == "done"


def test_record_verification_not_self_verified_when_a_different_actor_verifies(make_board, monkeypatch):
    """#316 S2 r2: a task delivered by `alice`, approved by `bob`, is done WITHOUT the
    `self-verified` label and closes with a bare `verified: bob` — a cross-identity verify
    is the normal, un-flagged case."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": "alice"},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    f = b.record_verification("bd-t", approved=True, by="bob")
    assert ("update", "bd-t", "--add-label", "self-verified") not in br.calls
    assert br.cmds("update") == []  # no label write at all on a plain verify
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified: bob")]
    assert f["board_state"] == "done"


def test_record_verification_compares_against_the_assignee_for_a_legacy_delivery(make_board, monkeypatch):
    """#316 S2 r3: a task delivered before the `delivered-by:` stamp existed carries no
    provenance comment — `_project` falls its `delivered_by` back to the assignee (the actor
    provenance a legacy bead does have), and record_verification compares the verifier against
    THAT. Verification by the legacy assignee is therefore flagged self-verified."""
    br = Br()
    b = make_board(br)
    # A legacy in_review task: no `delivered-by:` comment → projection uses the assignee.
    bead = {"id": "bd-t", "status": "in_progress", "labels": ["in-review"], "issue_type": "task", "assignee": "dave"}
    legacy = b._project(bead)
    assert legacy["delivered_by"] == "dave"  # the S1 assignee fallback fired
    monkeypatch.setattr(b, "_require", lambda fid: legacy)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    b.record_verification("bd-t", approved=True, by="Dave")
    assert ("update", "bd-t", "--add-label", "self-verified") in br.calls
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified: Dave (self-verified)")]


def test_record_verification_unattributed_delivery_by_the_actor_is_self_verified(make_board, monkeypatch):
    """#316 S2 r4: an unattributed delivery — no `delivered-by:` stamp AND no assignee, so
    `delivered_by` projects empty — has the STORE ACTOR stand in as the deliverer. The actor
    closing its own unattributed task (the `by` default) is therefore flagged self-verified."""
    br = Br()
    b = make_board(br)
    b.actor = "solo-bot"
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": ""}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    b.record_verification("bd-t", approved=True)  # by defaults to the store actor
    assert ("update", "bd-t", "--add-label", "self-verified") in br.calls
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified: solo-bot (self-verified)")]


def test_record_verification_unattributed_delivery_by_another_is_not_self_verified(make_board, monkeypatch):
    """#316 S2 r4 boundary: only the store actor stands in for an unattributed delivery — a
    DIFFERENT verifier of an unstamped, unassigned task is a genuine cross-identity verify and
    is NOT flagged."""
    br = Br()
    b = make_board(br)
    b.actor = "solo-bot"
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": ""}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done"})
    b.record_verification("bd-t", approved=True, by="reviewer")
    assert ("update", "bd-t", "--add-label", "self-verified") not in br.calls
    assert br.cmds("close") == [("close", "bd-t", "-r", "verified: reviewer")]


def test_record_verification_rejection_never_flags_self_verified(make_board, monkeypatch):
    """#316 S2 r5: the approved=False path is untouched — even when the rejecter IS the
    deliverer, a rejection requeues with feedback and writes NEITHER the `self-verified` label
    NOR any approval close reason."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "_require",
        lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "delivered_by": "alice"},
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready"})
    f = b.record_verification("bd-t", approved=False, feedback="missing the Q3 section", by="alice")
    assert ("comments", "add", "bd-t", "verification failed: missing the Q3 section") in br.calls
    assert br.cmds("close") == []  # a rejection NEVER closes
    # the ONLY update is the requeue (open+ready, assignee cleared) — no self-verified label
    (up,) = br.cmds("update")
    assert "--add-label" in up and "ready" in up and "in-review" in up
    assert not any("self-verified" in str(part) for c in br.calls for part in c)
    assert f["board_state"] == "ready"


# ── the Ready gate relaxes files_to_modify for a task (#217) ─────────────────────


def test_mark_ready_accepts_a_task_without_files_to_modify(make_board, monkeypatch):
    """A task-type bead ships a deliverable, not repo files — so the Ready gate's
    files_to_modify requirement (and the phantom/breadth/shared-file checks keyed off it)
    is relaxed: a task goes Ready on spec + acceptance_criteria alone."""
    br = Br()
    b = make_board(br)
    task = {
        "id": "bd-t",
        "board_state": "backlog",
        "issue_type": "task",
        "spec": "write the triage doc",
        "acceptance_criteria": "WHEN triaged THE SYSTEM SHALL produce a ranked list",
        "files_to_modify": [],  # a task carries none — the real projection shape
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: task)

    b.mark_ready("bd-t")  # no files_to_modify, but a task → gate passes

    assert ("update", "bd-t", "--add-label", "ready", "--remove-label", "designing") in br.calls


def test_mark_ready_still_requires_spec_and_ac_for_a_task(make_board, monkeypatch):
    """The relaxation drops ONLY files_to_modify — a task still owes a spec + testable
    acceptance_criteria (an unspecced task is as unpickable as an unspecced feature)."""
    br = Br()
    b = make_board(br)
    task = {
        "id": "bd-t",
        "board_state": "backlog",
        "issue_type": "task",
        "spec": "",  # missing
        "acceptance_criteria": "a",
        "files_to_modify": [],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: task)

    with pytest.raises(BoardError, match="spec"):
        b.mark_ready("bd-t")
    assert br.cmds("update") == []  # nothing mutated on a rejected gate


def test_mark_ready_still_requires_files_for_a_coding_feature(make_board, monkeypatch):
    """No regression: the relaxation is gated on issue_type — a coding feature with no
    files_to_modify is still rejected (the #217 relaxation must not leak to features)."""
    br = Br()
    b = make_board(br)
    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "issue_type": "feature",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": [],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)

    with pytest.raises(BoardError, match="files_to_modify"):
        b.mark_ready("bd-1")
    assert br.cmds("update") == []


def test_create_feature_mints_a_task_type_bead_and_pre_assigns(make_board, monkeypatch):
    """issue_type='task' mints the bead via `br create --type task`, and assignee
    pre-assigns it in the enrichment update (the separated `--assignee <name>` form)."""
    br = Br({"create": "bd-t"})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "backlog", "title": "T"})

    b.create_feature("T", spec="s", acceptance_criteria="a", issue_type="task", assignee="quinn")

    (create,) = br.cmds("create")
    assert create[:5] == ("create", "T", "--type", "task", "-p")  # minted as a task, not a feature
    (update,) = br.cmds("update")  # one enrichment update
    assert "--acceptance-criteria=a" in update
    assert "--assignee" in update and "quinn" in update  # pre-assigned


def test_create_feature_defaults_to_feature_type(make_board, monkeypatch):
    """No regression: create_feature still mints a `feature` when issue_type is omitted."""
    br = Br({"create": "bd-1"})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "backlog", "title": "T"})

    b.create_feature("T", spec="s")

    (create,) = br.cmds("create")
    assert create[:4] == ("create", "T", "--type", "feature")


# ── open_review: pr_url optional for tasks only ──────────────────────────────────


def test_open_review_allows_a_task_without_a_pr(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    f = b.open_review("bd-t")
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")  # no --external-ref stamped
    assert f["board_state"] == "in_review"


def test_open_review_still_requires_a_pr_for_coding_features(make_board, monkeypatch):
    """A coding feature entering review without a PR would strand the merge
    reconciler — the requirement moved from the signature into a named error."""
    b = make_board(Br())
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "feature"})
    with pytest.raises(BoardError, match="requires a pr_url"):
        b.open_review("bd-1")


def test_open_review_with_a_pr_is_unchanged_for_coding_features(make_board, monkeypatch):
    """No regression: the coding path still writes label + external-ref in ONE update."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "feature"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.open_review("bd-1", pr_url="https://example/pr/5")
    assert ("update", "bd-1", "--add-label", "in-review", "--external-ref", "https://example/pr/5") in br.calls


def test_open_review_refuses_a_non_http_pr_url_with_nothing_written(make_board, monkeypatch):
    """open_review shares record_delivery's external-ref gate — the same slot renders
    as the card's live PR link, so a non-http(s) pr_url is refused before any write."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "feature"})
    with pytest.raises(BoardError, match=r"open_review ref must be an absolute http\(s\) URL"):
        b.open_review("bd-1", pr_url="file:/tmp/pr.html")
    assert br.cmds("update") == []  # nothing written on the refusal


def test_open_review_refuses_a_whitespace_only_pr_url_for_coding_features(make_board, monkeypatch):
    """A whitespace-only pr_url is truthy but normalizes to "" — it must hit the
    required-pr_url refusal, not sneak a coding feature into review with no ref."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "feature"})
    with pytest.raises(BoardError, match="requires a pr_url"):
        b.open_review("bd-1", pr_url="   ")
    assert br.cmds("update") == []  # nothing written on the refusal


def test_open_review_whitespace_only_pr_url_is_a_no_ref_review_for_tasks(make_board, monkeypatch):
    """Tasks may enter review PR-less, so their whitespace-only pr_url just strips
    to no ref — same update as omitting it, never a blank --external-ref."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "board_state": "in_progress", "issue_type": "task"})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "in_review"})
    b.open_review("bd-t", pr_url="   ")
    (up,) = br.cmds("update")
    assert up == ("update", "bd-t", "--add-label", "in-review")  # no --external-ref stamped


# ── the puller admits task-type beads (#217) ─────────────────────────────────────


def test_claim_next_ready_pulls_task_beads_too(make_board, monkeypatch):
    ready = [
        {"id": "bd-ep", "issue_type": "epic", "labels": ["ready"]},
        {"id": "bd-t", "issue_type": "task", "labels": ["ready"]},
        {"id": "bd-f", "issue_type": "feature", "labels": ["ready"]},
    ]
    br = Br({"ready": ready})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid})
    claimed = b.claim_next_ready()
    assert claimed["id"] == "bd-t"  # priority order preserved — the task came first
    assert ("update", "bd-t", "--claim", "--remove-label", "ready") in br.calls


def test_ready_queue_includes_task_beads(make_board):
    ready = [
        {"id": "bd-f", "issue_type": "feature", "status": "open"},
        {"id": "bd-t", "issue_type": "task", "status": "open"},
        {"id": "bd-ms", "issue_type": "milestone", "status": "open"},
    ]
    shows = [
        {"id": "bd-f", "issue_type": "feature", "status": "open", "labels": ["ready"]},
        {"id": "bd-t", "issue_type": "task", "status": "open", "labels": ["ready"]},
    ]
    br = Br({"ready": ready, "show": shows})
    b = make_board(br)
    assert [f["id"] for f in b.ready_queue()] == ["bd-f", "bd-t"]  # milestone still excluded
    assert br.cmds("show") == [("show", "bd-f", "bd-t")]  # and never re-fetched


# ── _project: the deliverable field (#217) ───────────────────────────────────────


def test_project_deliverable_reads_the_latest_comment(make_board):
    b = make_board(Br())
    f = b._project(
        {
            "id": "bd-t",
            "status": "in_progress",
            "labels": ["in-review"],
            "issue_type": "task",
            "comments": [
                "deliverable: first draft",  # bare-string comment shape (br 0.1.x)
                {"text": "attempt 1 (tier=smart): ok"},
                {"text": "deliverable: final report at docs/report.md"},
            ],
        }
    )
    assert f["deliverable"] == "final report at docs/report.md"  # the LATEST wins


def test_project_deliverable_falls_back_to_a_label(make_board):
    b = make_board(Br())
    bead = {"id": "bd-t", "status": "open", "labels": ["deliverable:docs-report", "ready"], "issue_type": "task"}
    assert b._project(bead)["deliverable"] == "docs-report"


def test_project_deliverable_defaults_empty_for_coding_features(make_board):
    b = make_board(Br())
    assert b._project({"id": "x", "status": "open", "labels": []})["deliverable"] == ""


# ── _project: the delivered_by field (#316) ──────────────────────────────────────


def test_project_delivered_by_reads_the_latest_comment(make_board):
    """#316 r3: _project surfaces the NEWEST `delivered-by:` comment, mirroring the
    latest-deliverable scan — the stamp wins over the bead's assignee."""
    b = make_board(Br())
    f = b._project(
        {
            "id": "bd-t",
            "status": "in_progress",
            "labels": ["in-review"],
            "issue_type": "task",
            "assignee": "alice",
            "comments": [
                "delivered-by: bob",  # bare-string comment shape (br 0.1.x)
                {"text": "attempt 1 (tier=smart): ok"},
                {"text": "delivered-by: carol"},
            ],
        }
    )
    assert f["delivered_by"] == "carol"  # the LATEST stamp wins, over earlier stamps AND the assignee


def test_project_delivered_by_falls_back_to_the_assignee_for_a_legacy_bead(make_board):
    """#316 r3: a task delivered before the stamp existed carries no `delivered-by:`
    comment — _project falls back to the bead's assignee (the provenance it does have)."""
    b = make_board(Br())
    bead = {"id": "bd-t", "status": "in_progress", "labels": ["in-review"], "issue_type": "task", "assignee": "dave"}
    assert b._project(bead)["delivered_by"] == "dave"


def test_project_delivered_by_defaults_empty_with_no_stamp_or_assignee(make_board):
    b = make_board(Br())
    assert b._project({"id": "x", "status": "open", "labels": []})["delivered_by"] == ""


# ── _project: verification provenance — self_verified + verified_by (#316 S3a) ─────


def test_project_self_verified_reads_the_label(make_board):
    """#316 S3a r1: a task carrying the `self-verified` label (record_verification's flag
    for an approval by the deliverer) projects `self_verified is True`; a task without it
    projects False — a plain cross-identity verify, or unverified work."""
    b = make_board(Br())
    assert b._project({"id": "x", "status": "closed", "labels": ["self-verified"]})["self_verified"] is True
    assert b._project({"id": "y", "status": "closed", "labels": []})["self_verified"] is False


def test_project_verified_by_reads_the_close_reason(make_board):
    """#316 S3a r2: verified_by is the `<by>` parsed from the `verified: <by>` close reason
    record_verification writes on the task Done edge (br exposes it as `close_reason`)."""
    b = make_board(Br())
    bead = {"id": "bd-t", "status": "closed", "labels": [], "close_reason": "verified: bob"}
    assert b._project(bead)["verified_by"] == "bob"


def test_project_verified_by_strips_the_self_verified_suffix(make_board):
    """#316 S3a r2: a self-verified close reason preserves the verifier text verbatim and
    appends ` (self-verified)` (the flag itself rides the label, projected separately) — so
    verified_by strips that suffix and the surrounding spaces down to the identity alone."""
    b = make_board(Br())
    bead = {
        "id": "bd-t",
        "status": "closed",
        "labels": ["self-verified"],
        "close_reason": "verified:  alice  (self-verified)",  # verbatim ` alice ` from record_verification
    }
    f = b._project(bead)
    assert f["verified_by"] == "alice"
    assert f["self_verified"] is True


def test_project_verified_by_defaults_empty_without_a_verified_reason(make_board):
    """#316 S3a r2: verified_by is "" for anything that is not a `verified:` close reason —
    an open feature (no close reason at all), and the OTHER terminal edges (merge, cancel,
    manual done) whose reasons name no verifier, so a verifier never leaks from them."""
    b = make_board(Br())
    assert b._project({"id": "x", "status": "open", "labels": []})["verified_by"] == ""
    merged = {"id": "y", "status": "closed", "labels": [], "close_reason": "merged: https://gh/o/r/pull/1"}
    assert b._project(merged)["verified_by"] == ""
    cancelled = {"id": "z", "status": "closed", "labels": ["cancelled"], "close_reason": "cancelled: duplicate"}
    assert b._project(cancelled)["verified_by"] == ""


def test_project_verification_provenance_coexists_with_the_delivery_fallback(make_board):
    """#316 S3a r3: adding verified_by/self_verified leaves the S1 delivered_by assignee
    fallback intact — a legacy self-verified task (no `delivered-by:` stamp) projects the
    assignee as the deliverer AND, via the close reason, the same identity as the verifier."""
    b = make_board(Br())
    bead = {
        "id": "bd-t",
        "status": "closed",
        "labels": ["self-verified"],
        "issue_type": "task",
        "assignee": "dave",  # no `delivered-by:` comment → the S1 fallback credits the assignee
        "close_reason": "verified: dave (self-verified)",
    }
    f = b._project(bead)
    assert f["delivered_by"] == "dave"  # S1 legacy-delivery fallback preserved
    assert f["verified_by"] == "dave"
    assert f["self_verified"] is True


# ── `br --json` failures: the reason is on STDOUT, and a missing id is data (#255) ──


def _json_err(code: str, message: str, hint: str = "", rc: int = 3):
    """What `br <cmd> --json` actually does on a failure: non-zero exit, EMPTY stderr,
    and an error-shaped object on stdout. Verified against br 0.2.16."""
    body = {"error": {"code": code, "message": message, "retryable": False}}
    if hint:
        body["error"]["hint"] = hint
    return types.SimpleNamespace(returncode=rc, stdout=json.dumps(body), stderr="")


def _failing_board(monkeypatch, result):
    """A board whose every `br` call returns `result` — a FAILING CompletedProcess
    (distinct from `_json_board` above, which cans a zero-exit stdout)."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = BeadsBoard(db="/db/.beads/b.db", repo="/repo")
    b._workspace_ready = True  # the pin is not what's under test
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: result)
    return b


def test_get_feature_returns_none_for_an_id_that_does_not_exist(monkeypatch):
    """The regression that leaked worktrees: `br show <gone> --json` exits 3, which
    `_run` raised on — straight through get_feature's own ``dict | None`` contract. The
    sweep's reap branch is guarded by ``f is None``, so it was unreachable and every
    orphaned worktree just warned, every pass, forever."""
    b = _failing_board(monkeypatch, _json_err("ISSUE_NOT_FOUND", "Issue not found: abp-2tj.1"))
    assert b.get_feature("abp-2tj.1") is None


def test_a_json_failure_carries_its_reason_instead_of_an_empty_message(monkeypatch):
    """`_run` only ever read stderr, which `--json` leaves empty — so every failure
    raised "`br show x` failed: " with nothing after the colon."""
    b = _failing_board(monkeypatch, _json_err("VALIDATION_ERROR", "bad flag", hint="try --help"))
    with pytest.raises(BoardError) as exc:
        b.get_feature("bd-1")
    msg = str(exc.value)
    assert "VALIDATION_ERROR" in msg and "bad flag" in msg and "try --help" in msg


def test_a_non_not_found_json_failure_still_raises_a_plain_boarderror(monkeypatch):
    """Only ISSUE_NOT_FOUND is folded into None — a real error must not read as
    "this feature doesn't exist" and let a caller sail past it."""
    b = _failing_board(monkeypatch, _json_err("PERMISSION_DENIED", "read-only db"))
    with pytest.raises(BoardError) as exc:
        b.get_feature("bd-1")
    assert not isinstance(exc.value, store.BoardNotFound)


def test_not_found_is_a_boarderror_subclass_so_existing_handlers_keep_working(monkeypatch):
    """Call sites that only know `except BoardError` must be unaffected by the new type."""
    b = _failing_board(monkeypatch, _json_err("ISSUE_NOT_FOUND", "Issue not found: bd-9"))
    with pytest.raises(BoardError):
        b._run("show", "bd-9", want_json=True)
    assert issubclass(store.BoardNotFound, BoardError)


def test_plain_mode_not_found_is_detected_on_stderr(monkeypatch):
    """Without --json the message is on stderr instead — same verdict."""
    b = _failing_board(
        monkeypatch,
        types.SimpleNamespace(returncode=3, stdout="", stderr="Error: Issue not found: bd-9"),
    )
    with pytest.raises(store.BoardNotFound):
        b._run("show", "bd-9")


# ── `br` is single-flight per process, and a stdout DATABASE_ERROR is retried whatever the exit (#290) ──


def _wal_short_read(rc: int = 1):
    """What `br show --json` returns when its read races another br's WAL checkpoint:
    a NON-zero exit, an EMPTY stderr, and the DATABASE_ERROR object on stdout."""
    body = {
        "error": {
            "code": "DATABASE_ERROR",
            "message": "Database error: WAL file is corrupt: short read at frame 18: got 0, need 4120",
            "retryable": True,
        }
    }
    return types.SimpleNamespace(returncode=rc, stdout=json.dumps(body), stderr="")


def test_stdout_database_error_with_nonzero_exit_is_retried(monkeypatch, caplog):
    """RED-IS-REACHABLE: before #290 the contention sniff only read stdout on a ZERO
    exit, so this shape raised on the first attempt — a 400 on /features, a failed
    tick — instead of taking the backoff that exists for exactly it."""
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = BeadsBoard(db="/db/.beads/b.db", repo="/repo")
    b._workspace_ready = True
    calls = {"n": 0}

    def _run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _wal_short_read()
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(store.subprocess, "run", _run)
    with caplog.at_level("WARNING"):
        assert b._run("show", "bd-1", want_json=True) == []
    assert calls["n"] == 2
    assert any("hit DB contention" in r.message for r in caplog.records)


def test_br_invocations_are_single_flight_per_process(monkeypatch):
    """Four threads shelling `br` at once must never overlap inside subprocess.run —
    two br processes racing the WAL is the failure the lock removes."""
    import threading

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = BeadsBoard(db="/db/.beads/b.db", repo="/repo")
    b._workspace_ready = True
    state = {"inflight": 0, "peak": 0}
    guard = threading.Lock()

    def _run(*_a, **_k):
        with guard:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        time.sleep(0.02)
        with guard:
            state["inflight"] -= 1
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(store.subprocess, "run", _run)
    threads = [threading.Thread(target=lambda: b._run("list", want_json=True)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1


# ── the public comment() seam and its one-release _comment alias (#266) ─────────


def test_comment_is_public_and_records_the_br_call(make_board):
    """comment() is the published audit-trail seam: it writes a `br comments add`."""
    br = Br()
    b = make_board(br)
    b.comment("bd-1", "spec updated: title")
    assert ("comments", "add", "bd-1", "spec updated: title") in br.calls


def test_comment_alias_is_retained_for_one_release(make_board):
    """_comment stays a live alias for the old private name so out-of-tree callers
    keep working for one release — it is the SAME callable and behaves identically."""
    assert BeadsBoard._comment is BeadsBoard.comment
    br = Br()
    b = make_board(br)
    b._comment("bd-2", "legacy caller")
    assert ("comments", "add", "bd-2", "legacy caller") in br.calls


def test_comment_swallows_a_board_error(make_board):
    """The trail is best-effort: a `br` failure is logged, never raised, so a comment
    write can't break the edge that called it."""

    def run_impl(*args, want_json=False, with_has_more=False):
        raise BoardError("br exploded")

    b = make_board(run_impl)
    b.comment("bd-3", "must not raise")  # no exception escapes


def test_br_lock_is_shared_across_module_instances():
    """A plugin reload re-imports the module: the running loop and the reloaded
    routers must serialize on the SAME lock, or the race is back (#178 pattern)."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("project_board.store_reloaded", store.__file__)
    other = importlib.util.module_from_spec(spec)
    sys.modules["project_board.store_reloaded"] = other
    try:
        spec.loader.exec_module(other)
        assert other._br_lock() is store._br_lock()
    finally:
        sys.modules.pop("project_board.store_reloaded", None)


def test_slot_install_is_atomic_under_a_first_call_race(monkeypatch):
    """RED-IS-REACHABLE: `get`-then-`set` let two threads racing the FIRST `_br_lock()`
    each build a holder with its own Lock and each return the one it built — two
    "holders" of a mutex whose whole job is single-flight. `sys.modules.setdefault`
    makes the install atomic, so every caller gets the holder that landed first.

    The interleaving is FORCED, not hoped for: the holder factory blocks until both
    threads are past the `get`-returned-None check, so the window is guaranteed open.
    (An earlier draft just started 8 threads and hoped — it passed against the UNFIXED
    code, i.e. it was not a test. The factory is swapped through the store module's own
    `types` reference, never the global one: patching `types.ModuleType` process-wide
    breaks pytest's own failure reporting.)"""
    import sys as _sys
    import threading
    import types as _types

    slot = store._BR_LOCK_SLOT
    saved = _sys.modules.pop(slot, None)
    both_past_the_check = threading.Barrier(2, timeout=5)

    class _BlockingTypes:
        """Stands in for the `types` module, for `store` only."""

        @staticmethod
        def ModuleType(name, *a, **kw):
            if name == slot:  # hold every builder until BOTH have seen an absent slot
                both_past_the_check.wait()
            return _types.ModuleType(name, *a, **kw)

    monkeypatch.setattr(store, "types", _BlockingTypes)
    try:
        locks, errors = [], []
        guard = threading.Lock()

        def grab():
            try:
                lk = store._br_lock()
            except Exception as exc:  # noqa: BLE001 — surfaced by the assertions below
                with guard:
                    errors.append(exc)
                return
            with guard:
                locks.append(lk)

        threads = [threading.Thread(target=grab) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, errors
        assert len(locks) == 2
        assert locks[0] is locks[1], "both callers must share ONE lock"
    finally:
        if saved is not None:
            _sys.modules[slot] = saved
        else:
            _sys.modules.pop(slot, None)


def test_requeue_preserves_a_task_assignee_so_a_rejected_deliverable_can_redispatch(make_board, monkeypatch):
    """A task's assignee is its DISPATCH TARGET (a sister agent, or `agent`/`self` for
    first-party work), not a claim marker. requeue cleared it unconditionally, so a
    rejected deliverable came back unassigned and the next claim parked it "awaiting
    unassigned delivery" with nothing left to drive it — the card was dead and only a
    log line said so. Observed live on a self-assigned audit task the first time an
    operator rejected its deliverable."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "board_state": "in_review", "issue_type": "task", "assignee": "agent"}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "ready", "assignee": "agent"})
    b.requeue("bd-task")
    (up,) = br.cmds("update")
    assert "--assignee" not in up
    assert "--status" in up and "open" in up
    assert "--add-label" in up and "ready" in up


def test_a_reblock_for_the_SAME_class_keeps_its_label(make_board, monkeypatch):
    """`br` applies --remove-label AFTER --add-label, so emitting both for the same value
    nets to REMOVED. A card re-blocked for the same reason therefore lost its class
    silently, read as `unclassified` on the next sweep, and was escalated to a human
    instead of taking the retry it still had. Observed live on bd-eyrs.

    So a prior class label is only removed when it DIFFERS from the one being written."""
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["blocked-class:transient", "blocked"]})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "coder timed out after 1800.0s")  # transient AGAIN
    (up,) = br.cmds("update")
    assert "blocked-class:transient" in up
    assert "--remove-label" not in up, "removing the label it is adding nets to removed"

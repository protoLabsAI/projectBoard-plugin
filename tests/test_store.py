"""Store tests — the board projection over beads and the two invariants.

The board is a *projection* of ``br`` status + labels, so the highest-value tests
are pure: ``board_state`` (the projection), the escalation ladder, and the
``_project`` field mapping. The gate (``mark_ready``) and the single Done edge
(``record_merge``) are exercised with ``_run`` (the ``br`` subprocess call)
replaced by the ``make_board`` fixture — no CLI, no DB.
"""

from __future__ import annotations

import pytest

from project_board import store
from project_board.store import BeadsBoard, BoardError, escalation_enabled


class Br:
    """A fake ``_run``: records every ``br`` call and returns canned values keyed
    by the leading subcommand. A canned value may be a callable ``(args) -> value``."""

    def __init__(self, returns=None):
        self.calls = []
        self.returns = returns or {}

    def __call__(self, *args, want_json=False):
        self.calls.append(args)
        val = self.returns.get(args[0] if args else "", [] if want_json else "")
        return val(args) if callable(val) else val

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


# ── board_state: the projection (status + labels → one of six states) ───────────


@pytest.mark.parametrize(
    "bead,expected",
    [
        ({"status": "open", "labels": []}, "backlog"),
        ({"status": "open", "labels": ["ready"]}, "ready"),
        ({"status": "in_progress", "labels": []}, "in_progress"),
        ({"status": "in_progress", "labels": ["in-review"]}, "in_review"),
        ({"status": "closed", "labels": []}, "done"),
        ({"status": "deferred", "labels": []}, "backlog"),
        ({"status": "open", "labels": ["blocked"]}, "blocked"),
        # precedence: closed beats a stray blocked label; blocked beats in-review.
        ({"status": "closed", "labels": ["blocked", "ready"]}, "done"),
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


def test_next_tier_walks_then_stops_at_the_top(make_board):
    b = make_board(Br())
    # Ladder: smart → reasoning → opus (fast dropped — protolabs/fast too weak).
    assert b.next_tier("smart") == "reasoning"
    assert b.next_tier("reasoning") == "opus"
    assert b.next_tier("opus") is None  # top of the ladder → caller blocks
    assert b.next_tier("nonsense") == store.TIER_LADDER[0]  # stale/unknown tier → floor (smart)
    assert b.next_tier("fast") == store.TIER_LADDER[0]  # a now-removed tier falls back to the floor


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
        "files_to_modify": ["a.py"],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: ready_feature)
    b.mark_ready("bd-1")
    assert ("update", "bd-1", "--add-label", "ready") in br.calls


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


def test_ready_queue_projects_label_less_br_ready_rows_as_ready(make_board, monkeypatch):
    """beads-rust ≤0.1.23: `br ready --json` returns rows WITHOUT a `labels` field.
    ready_queue must still project candidates as board_state='ready' (re-fetching via
    `br show`, which carries labels) — otherwise board_state() reads no `ready` label,
    returns 'backlog', and the puller's `board_state != "ready"` guard self-rejects
    every ready feature and the loop silently never claims. Regression for the live
    dogfood finding."""
    # What real `br ready --json` hands back: a feature with NO labels key.
    br = Br({"ready": [{"id": "bd-1", "title": "T", "status": "open", "issue_type": "feature"}]})
    b = make_board(br)
    # get_feature (br show) IS label-bearing — project from it, not the bare ready row.
    monkeypatch.setattr(
        b,
        "get_feature",
        lambda fid: b._project(
            {
                "id": fid,
                "title": "T",
                "status": "open",
                "issue_type": "feature",
                "labels": ["ready", "diff:small"],
                "description": "spec",
                "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
            }
        ),
    )
    q = b.ready_queue()
    assert [f["id"] for f in q] == ["bd-1"]
    assert q[0]["board_state"] == "ready"  # the bug projected this as "backlog"


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
    monkeypatch.setattr(b, "_comment", lambda fid, text: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "blocked"})
    b.flag_blocked("bd-9", "boom")
    assert ("update", "bd-9", "--add-label", "blocked", "--assignee", "") in br.calls


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

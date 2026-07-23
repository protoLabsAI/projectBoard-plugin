"""Store tests — the board projection over beads and the two invariants.

The board is a *projection* of ``br`` status + labels, so the highest-value tests
are pure: ``board_state`` (the projection), the escalation ladder, and the
``_project`` field mapping. The gate (``mark_ready``) and the single Done edge
(``record_merge``) are exercised with ``_run`` (the ``br`` subprocess call)
replaced by the ``make_board`` fixture — no CLI, no DB.
"""

from __future__ import annotations

import types

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
        "files_to_modify": ["a.py"],
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


# ── the DESIGN gate (plan M6): large/architectural needs design + ADR ref ───────


def _design_feature(**over):
    base = {
        "id": "bd-9",
        "board_state": "backlog",
        "spec": "s",
        "acceptance_criteria": "a",
        "files_to_modify": ["a.py"],
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


def test_cancel_feature_unknown_id_raises(make_board, monkeypatch):
    b = make_board(Br())
    monkeypatch.setattr(b, "get_feature", lambda fid: None)
    with pytest.raises(BoardError, match="unknown feature"):
        b.cancel_feature("nope")


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


def _proc(returncode, stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout="ok", stderr=stderr)


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
            "files_to_modify": ["a.py"],
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
    with pytest.raises(BoardError, match="out of range"):
        BeadsBoard._resolve_plan_dep(-5, {0: "bd-1"}, {})


# ── source_issue: normalize + store the originating GitHub issue (#97) ───────────
# The bead carries a single replaced `source:owner/repo#N` label (the gens:/verified:
# pattern); the projection exposes it as `source_issue`, which the loop's PR opener
# reads to stamp `Fixes #N` on the feature's PR.


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


def test_create_feature_stores_the_normalized_source_label(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", source_issue="https://github.com/acme/widgets/issues/97")
    update = next(c for c in calls if c and c[0] == "update")
    assert "--add-label" in update and "source:acme/widgets#97" in update


def test_create_feature_passes_a_canonical_slug_through_unchanged(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", source_issue="acme/widgets#8")
    update = next(c for c in calls if c and c[0] == "update")
    assert "source:acme/widgets#8" in update


def test_create_feature_invalid_source_issue_rejects_before_minting_a_bead(make_board):
    """Validation runs BEFORE `br create` — an invalid source_issue must fail the whole
    create with the named error, never leave an orphan bead behind it."""
    calls = []
    b = make_board(_enrich_run(calls=calls))
    with pytest.raises(BoardError, match="invalid source_issue"):
        b.create_feature("T", spec="s", source_issue="not-an-issue")
    assert not any(c and c[0] == "create" for c in calls)  # no orphan


def test_create_feature_without_source_issue_adds_no_source_label(make_board):
    calls = []
    b = make_board(_enrich_run(calls=calls))
    b.create_feature("T", spec="s", acceptance_criteria="a", files_to_modify=["a.py"])
    for c in calls:
        assert not any(str(tok).startswith("source:") for tok in c)


def test_update_feature_replaces_a_stale_source_label(make_board, monkeypatch):
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["source:old/repo#1", "ready"]})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", source_issue="https://github.com/acme/widgets/issues/9")
    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--remove-label", "source:old/repo#1", "--add-label", "source:acme/widgets#9")


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
    monkeypatch.setattr(b, "_require", lambda fid: {"id": fid, "labels": ["source:old/repo#1"]})
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", source_issue="   ")
    assert br.cmds("update") == []  # the difficulty convention: blank = leave untouched


def test_project_exposes_source_issue_from_the_label(make_board):
    b = make_board(Br())
    bead = {"id": "x", "status": "open", "labels": ["source:acme/widgets#8", "ready"]}
    assert b._project(bead)["source_issue"] == "acme/widgets#8"
    assert b._project({"id": "y", "status": "open", "labels": []})["source_issue"] == ""


def test_projected_source_issue_feeds_the_loops_fixes_line(make_board):
    """The wiring's point: a stored source_issue round-trips through the projection
    into loop._source_issue, which resolves it to (slug, n) for the PR's Fixes line."""
    from project_board.loop import _source_issue

    b = make_board(Br())
    f = b._project({"id": "x", "status": "open", "labels": ["source:acme/widgets#8"]})
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
    assert any("--add-label" in u and "source:acme/widgets#97" in u for u in br.cmds("update"))


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
    assert any("source:acme/widgets#5" in u for u in br.cmds("update"))  # the good item still landed

"""Store tests — the board projection over beads and the two invariants.

The board is a *projection* of ``br`` status + labels, so the highest-value tests
are pure: ``board_state`` (the projection), the escalation ladder, and the
``_project`` field mapping. The gate (``mark_ready``) and the single Done edge
(``record_merge``) are exercised with ``_run`` (the ``br`` subprocess call)
replaced by the ``make_board`` fixture — no CLI, no DB.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

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
    with pytest.raises(BoardError, match=r"do not exist in the repo: real\.py"):
        b.mark_ready("bd-1")
    assert br.cmds("update") == []  # nothing mutated on a rejected gate

    # a `(new)` marker (case-insensitive, anywhere in the entry) bypasses the check
    br.calls.clear()
    feature["files_to_modify"] = ["real.py", "docs/new-guide.md (NEW)"]
    with pytest.raises(BoardError, match=r"do not exist in the repo: real\.py"):
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

    def instrumented(*args, want_json=False):
        # A `br update … --add-label ready` is the write that makes a bead claimable;
        # apply it to its target ids so the model tracks exactly what the puller can see.
        if args and args[0] == "update" and "--add-label" in args and store.LABEL_READY in args:
            ready.update(_ids_before_flags(args))
        # Every br call is a boundary a concurrent puller could interleave a claim at.
        observed.append(frozenset(ready))
        return real_run(*args, want_json=want_json)

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
    monkeypatch.setattr(b, "_comment", lambda fid, text: comments.append((fid, text)))
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

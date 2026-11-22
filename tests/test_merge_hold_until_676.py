"""#676 — a merge-hold can carry a checkable release condition (`until`).

`board_hold_merge(fid, reason, until=...)` stores `waits_for`-grammar gate specs as
`merge-hold-until:` notes lines; `board_release_merge_hold` refuses while any is unmet
(force=True overrides, audited); re-holding replaces reason + until with one audit comment;
the projection exposes per-spec cached verdicts and next_action names an unmet condition.

Host-free: a stateful fake `br` (labels, notes, comments round-trip) and `gates.eval_pr`
monkeypatched, so the real evaluator/cache path runs with no network.
"""

from __future__ import annotations

import json

import pytest

import project_board as pb
from project_board import gates, store
from project_board.store import NEXT_ACTION_MERGE_HOLD, merge_posture

SPEC = "pr:o/r#31"


def _bead(fid="bd-1", *, status="open", labels=(), notes=""):
    return {"id": fid, "title": "t", "status": status, "labels": list(labels), "notes": notes}


class _FakeBr:
    """Stateful fake ``br``: ``show`` serves the bead, ``update`` applies label edits and
    ``--notes=`` in place, ``comments add`` appends — so every verb round-trips."""

    def __init__(self, beads):
        self.beads = {b["id"]: dict(b) for b in beads}
        self.calls = []

    def __call__(self, *args, want_json=False, with_has_more=False, actor=""):
        self.calls.append(args)
        cmd = args[0] if args else ""
        if cmd == "show":
            bead = self.beads.get(args[1])
            if bead is None:
                raise store.BoardNotFound(f"issue not found: {args[1]!r}")
            row = dict(bead)
            row["labels"] = list(bead.get("labels", []))
            row["comments"] = list(bead.get("comments", []))
            return [row]
        if cmd == "update":
            bead = self.beads[args[1]]
            labels = list(bead.get("labels", []))
            rest = list(args[2:])
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok.startswith("--notes="):
                    bead["notes"] = tok[len("--notes=") :]
                    i += 1
                    continue
                val = rest[i + 1] if i + 1 < len(rest) else ""
                if tok == "--add-label" and val not in labels:
                    labels.append(val)
                elif tok == "--remove-label" and val in labels:
                    labels.remove(val)
                elif tok == "--status":
                    bead["status"] = val
                i += 2
            bead["labels"] = labels
            return [] if want_json else ""
        if cmd == "comments":
            self.beads[args[2]].setdefault("comments", []).append(args[3])
            return [] if want_json else ""
        return [] if want_json else ""

    def comments(self, fid="bd-1"):
        return list(self.beads[fid].get("comments", []))

    def notes(self, fid="bd-1"):
        return str(self.beads[fid].get("notes") or "")

    def labels(self, fid="bd-1"):
        return list(self.beads[fid].get("labels", []))


@pytest.fixture(autouse=True)
def _fresh_gate_cache():
    gates.reset_cache()
    yield
    gates.reset_cache()


@pytest.fixture
def pr_state(monkeypatch):
    """Stub the PR gate check: ``state["merged"]`` decides met; ``state["raise"]`` makes
    the check fail. Each call resets the cache so a forced re-check is never throttled."""
    state = {"merged": False, "raise": False, "calls": 0}

    def _eval_pr(spec):
        state["calls"] += 1
        if state["raise"]:
            raise gates.GateCheckError("GitHub PR lookup failed: boom")
        return {"met": state["merged"], "detail": f"{spec.describe()} ({'merged' if state['merged'] else 'open'})"}

    monkeypatch.setattr(gates, "eval_pr", _eval_pr)
    return state


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


def _wire(board, monkeypatch):
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)


def _held_board(make_board, *, until=SPEC, reason="ship after o/r#31", status="in_progress", labels=("in-review",)):
    board = make_board(_FakeBr([_bead(status=status, labels=labels, notes="a.py\nsource-issue: o/r#9")]))
    board.set_merge_hold("bd-1", reason, until=until)
    return board


# ── r1: hold with until → label + stored spec + verdict; invalid until changes nothing ──


def test_hold_with_until_stores_spec_and_get_feature_shows_its_verdict(make_board, monkeypatch, pr_state):
    board = make_board(_FakeBr([_bead(labels=["ready"], notes="a.py\nsource-issue: o/r#9")]))
    _wire(board, monkeypatch)

    out = json.loads(
        _tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "after o/r#31", "until": " pr:o/r#31 "})
    )
    assert out["merge_hold"] is True
    assert [r["spec"] for r in out["merge_hold_until"]] == [SPEC]
    fake = board._run
    assert store.LABEL_MERGE_HOLD in fake.labels()
    assert f"merge-hold-until: {SPEC}" in fake.notes().splitlines()
    # the other structured lines are carried forward, not wiped
    assert "a.py" in fake.notes().splitlines() and "source-issue: o/r#9" in fake.notes().splitlines()
    assert fake.comments() == [f"merge-hold set: after o/r#31 (until {SPEC})"]

    got = json.loads(_tool("board_get_feature").invoke({"feature_id": "bd-1"}))
    assert got["merge_hold"] is True
    [verdict] = got["merge_hold_until"]
    assert verdict["spec"] == SPEC and verdict["met"] is False and "not checked yet" in verdict["detail"]
    assert pr_state["calls"] == 0  # the listing read is cache-only — no network
    f = board.get_feature("bd-1")
    assert f["files_to_modify"] == ["a.py"] and f["source_issue"] == "o/r#9"


@pytest.mark.parametrize("bad", ["pr:o/r", "nope:thing", "pr:o/r#31, release:"])
def test_invalid_until_returns_error_and_changes_nothing(make_board, monkeypatch, bad):
    board = make_board(_FakeBr([_bead(labels=["ready"])]))
    _wire(board, monkeypatch)
    out = _tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "r", "until": bad})
    assert out.startswith("Error:") and "spec" in out
    fake = board._run
    assert store.LABEL_MERGE_HOLD not in fake.labels()
    assert fake.comments() == []
    assert "merge-hold-until" not in fake.notes()
    assert not any(c[0] in ("update", "comments") for c in fake.calls)


# ── r2: release refused while unmet (incl. a raising check); met → released + cleared ───


def test_release_refused_while_unmet_and_hold_stays(make_board, monkeypatch, pr_state):
    board = _held_board(make_board)
    _wire(board, monkeypatch)
    n_comments = len(board._run.comments())

    out = _tool("board_release_merge_hold").invoke({"feature_id": "bd-1"})
    assert out.startswith("Error:") and SPEC in out and "unmet" in out
    assert store.LABEL_MERGE_HOLD in board._run.labels()
    assert f"merge-hold-until: {SPEC}" in board._run.notes()
    assert len(board._run.comments()) == n_comments
    assert pr_state["calls"] == 1  # checked live (force=True), not from a stale reason


def test_release_refused_when_the_gate_check_raises(make_board, monkeypatch, pr_state):
    board = _held_board(make_board)
    pr_state["raise"] = True
    with pytest.raises(store.BoardError) as exc:
        board.release_merge_hold("bd-1")
    assert SPEC in str(exc.value) and "boom" in str(exc.value)
    assert store.LABEL_MERGE_HOLD in board._run.labels()


def test_release_with_every_spec_met_removes_label_and_clears_specs(make_board, monkeypatch, pr_state):
    board = _held_board(make_board, until=f"{SPEC}, npm:@x/y@>=1.0.0")
    _wire(board, monkeypatch)
    pr_state["merged"] = True
    monkeypatch.setattr(gates, "eval_npm", lambda spec, **_k: {"met": True, "detail": "published"})

    out = json.loads(_tool("board_release_merge_hold").invoke({"feature_id": "bd-1"}))
    assert out["merge_hold"] is False
    fake = board._run
    assert store.LABEL_MERGE_HOLD not in fake.labels()
    assert "merge-hold-until" not in fake.notes() and "merge-hold-reason" not in fake.notes()
    assert "a.py" in fake.notes() and "source-issue: o/r#9" in fake.notes()
    assert fake.comments()[-1].startswith("merge-hold released (until met:")
    f = board.get_feature("bd-1")
    assert f["merge_hold_until"] == [] and f["merge_hold_until_specs"] == [] and f["merge_hold_reason"] == ""


# ── r3: force=True releases an unmet hold, audited as an override ───────────────────


def test_forced_release_overrides_and_audits_unmet_specs(make_board, monkeypatch, pr_state):
    board = _held_board(make_board)
    _wire(board, monkeypatch)
    out = json.loads(_tool("board_release_merge_hold").invoke({"feature_id": "bd-1", "force": True}))
    assert out["merge_hold"] is False
    assert store.LABEL_MERGE_HOLD not in board._run.labels()
    assert "merge-hold-until" not in board._run.notes()
    last = board._run.comments()[-1]
    assert "override" in last and SPEC in last


# ── r4: re-hold replaces reason + until with ONE audit comment; identical is a no-op ───


def test_rehold_replaces_reason_and_until_with_one_audit_comment(make_board):
    board = _held_board(make_board, reason="after 31")
    fake = board._run
    before = len(fake.comments())

    board.set_merge_hold("bd-1", "after 32", until="pr:o/r#32")
    assert len(fake.comments()) == before + 1
    note = fake.comments()[-1]
    assert note.startswith("merge-hold updated:")
    assert "'after 31' → 'after 32'" in note and f"{SPEC} → pr:o/r#32" in note
    f = board.get_feature("bd-1")
    assert f["merge_hold_until_specs"] == ["pr:o/r#32"] and f["merge_hold_reason"] == "after 32"
    assert store.LABEL_MERGE_HOLD in f["labels"]

    # identical values → idempotent, no comment, no write
    n_updates = sum(1 for c in fake.calls if c[0] == "update")
    board.set_merge_hold("bd-1", "after 32", until="pr:o/r#32")
    assert len(fake.comments()) == before + 1
    assert sum(1 for c in fake.calls if c[0] == "update") == n_updates


def test_rehold_reason_only_change_on_conditional_hold(make_board):
    board = _held_board(make_board, reason="old")
    board.set_merge_hold("bd-1", "new", until=SPEC)
    assert board._run.comments()[-1] == "merge-hold updated: reason 'old' → 'new'"


def test_rehold_adds_until_to_a_plain_hold(make_board):
    board = make_board(_FakeBr([_bead(labels=["ready"])]))
    board.set_merge_hold("bd-1", "plain")
    board.set_merge_hold("bd-1", "plain", until=SPEC)
    assert board._run.comments()[-1].startswith("merge-hold updated:")
    assert f"until (none) → {SPEC}" in board._run.comments()[-1]
    assert board.get_feature("bd-1")["merge_hold_until_specs"] == [SPEC]


# ── r5: next_action names the unmet condition; a plain hold reads the operator veto ───


def test_next_action_names_unmet_until(make_board, pr_state):
    board = _held_board(make_board, labels=("in-review", "review-clean"))
    with pytest.raises(store.BoardError):
        board.release_merge_hold("bd-1")  # caches the unmet verdict
    f = board.get_feature("bd-1")
    assert f["board_state"] == "in_review"
    posture = merge_posture(f, auto_merge=True, review_gate=True)
    assert posture["next_action"] == f"merge-hold until {SPEC} (unmet)"
    assert "merge-hold" in posture["blockers"]


def test_next_action_with_met_until_reads_operator_veto(make_board, pr_state):
    board = _held_board(make_board, labels=("in-review", "review-clean"))
    pr_state["merged"] = True
    gates.evaluate([SPEC], force=True)  # seed a met verdict
    posture = merge_posture(board.get_feature("bd-1"), auto_merge=True, review_gate=True)
    assert posture["next_action"] == NEXT_ACTION_MERGE_HOLD


# ── the no-`until` path is exactly as today ─────────────────────────────────────────


def test_plain_hold_and_release_unchanged(make_board, pr_state):
    board = make_board(_FakeBr([_bead(status="in_progress", labels=["in-review", "review-clean"])]))
    fake = board._run
    board.set_merge_hold("bd-1", "QA by hand")
    assert fake.comments() == ["merge-hold set: QA by hand"]
    assert not any(str(a).startswith("--notes=") for c in fake.calls for a in c)
    f = board.get_feature("bd-1")
    assert f["merge_hold_until"] == []
    assert merge_posture(f, auto_merge=True, review_gate=True)["next_action"] == NEXT_ACTION_MERGE_HOLD

    board.set_merge_hold("bd-1", "different reason")  # plain re-hold stays a no-op (#532)
    assert fake.comments() == ["merge-hold set: QA by hand"]

    board.release_merge_hold("bd-1")
    assert fake.comments()[-1] == "merge-hold released"
    assert store.LABEL_MERGE_HOLD not in fake.labels()
    assert pr_state["calls"] == 0  # nothing to evaluate


def test_stale_until_under_no_label_is_cleared_by_a_plain_hold(make_board, pr_state):
    board = make_board(_FakeBr([_bead(labels=["ready"], notes=f"merge-hold-until: {SPEC}")]))
    assert board.get_feature("bd-1")["merge_hold_until"] == []  # not held → not meaningful
    board.set_merge_hold("bd-1", "plain")
    assert "merge-hold-until" not in board._run.notes()
    board.release_merge_hold("bd-1")  # no stale condition gates the release
    assert store.LABEL_MERGE_HOLD not in board._run.labels()


def test_unrelated_notes_rewrite_keeps_the_hold_condition(make_board):
    board = _held_board(make_board)
    board.update_feature("bd-1", files_to_modify=["b.py"])
    f = board.get_feature("bd-1")
    assert f["files_to_modify"] == ["b.py"]
    assert f["merge_hold_until_specs"] == [SPEC] and f["merge_hold_reason"] == "ship after o/r#31"


def test_files_entry_with_merge_hold_prefix_is_refused():
    with pytest.raises(store.BoardError):
        store._refuse_structured_paths([f"merge-hold-until: {SPEC}"])

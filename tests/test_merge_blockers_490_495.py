"""#490 and #495: an in_review card that is not merging must say why.

#490: the merged-state re-verify budget (`merged_verify_max`) was spent by every GREEN
re-verify, so on a busy repo a card that kept passing was parked as "budget exhausted"
just because its siblings kept merging. And the listing read the cap from its own config,
not from the loop that wrote the exhaustion sentinel, so a parked card could read
`auto-merge pending`.

#495: a PR GitHub read as UNSTABLE (a `QA panel` check stuck `in_progress` on unresolved
review threads) sat for hours reading `auto-merge pending`. The merge edge already reads
`mergeStateStatus` and the pass already read the head's checks; the card now names them.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, merge_state_hold, worktree
from project_board.loop import BoardLoop
from project_board.loop import _common as loop_common
from project_board.store import (
    NEXT_ACTION_MERGED_VERIFY_EXHAUSTED,
    annotate_next_action,
)

PR = "https://github.com/o/r/pull/42"


def _aret(value):
    async def _f(*_a, **_k):
        return value

    return _f


# ── #490: a green re-verify does not spend the budget ───────────────────────────


class _VerifyStore:
    def __init__(self):
        self.verified = []
        self.budgets = []
        self.cleared = []
        self.blocked = []

    def record_merged_verified(self, fid, sha):
        self.verified.append((fid, sha))
        return {"id": fid}

    def record_budget(self, fid, kind, n):
        self.budgets.append((fid, kind, n))
        return {"id": fid}

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append((fid, tuple(kinds) if kinds is not None else None))
        return {"id": fid}

    def flag_blocked(self, fid, reason, **_kw):
        self.blocked.append((fid, reason))


def _vloop(**cfg):
    return BoardLoop({"coder": "proto", "local_gate_cmd": "pytest -q", **cfg})


def _moving_base(monkeypatch, shas):
    it = iter(shas)
    built = []

    async def _sha(repo, ref):
        return next(it)

    async def _build(repo, branch, sha, root=".worktrees"):
        built.append(sha)
        return ("merged", "/wt")

    monkeypatch.setattr(worktree, "origin_head_sha", _sha)
    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    return built


async def test_green_reverifies_never_spend_the_budget(monkeypatch):
    """Base moves five times under a card whose merged state keeps passing, with a cap of
    1: every move is re-verified and stamped, nothing is spent, no sentinel is armed."""
    shas = ["s1", "s2", "s3", "s4", "s5"]
    built = _moving_base(monkeypatch, shas)
    store = _VerifyStore()
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # a real green verdict
    stamp = ""
    for _ in shas:
        feature = {"id": "bd-1", "labels": [f"merged-verified:{stamp}"] if stamp else []}
        assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
        stamp = store.verified[-1][1]
    assert built == shas
    assert [s for _, s in store.verified] == shas
    assert store.budgets == [] and store.blocked == []
    assert loop._merged_verify_attempts.get("bd-1", 0) == 0


async def test_a_green_verdict_resets_what_no_verdict_runs_spent(monkeypatch):
    """A run that reached no verdict still counts; the next real green gives the budget
    back (cache and label), so only CONSECUTIVE no-verdict runs can exhaust it."""
    _moving_base(monkeypatch, ["s1", "s2"])
    store = _VerifyStore()
    loop = _vloop(merged_verify_max=3)
    outcomes = iter([False, True])  # judged?

    async def _gate(wt, feature=None):
        if not next(outcomes):
            loop._gate_no_verdict.add(wt)
        return None

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.budgets == [("bd-1", "merged-verify", 1)]
    feature = {"id": "bd-1", "labels": ["merged-verified:s1", "budget:merged-verify:1"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert store.cleared == [("bd-1", ("merged-verify",))]
    assert loop._merged_verify_attempts["bd-1"] == 0
    assert loop._gate_no_verdict == set()  # nothing left behind for the next run


async def test_a_real_timed_out_gate_is_marked_no_verdict(tmp_path):
    """The real `_run_local_gate` against real processes: a gate killed by its timeout is
    recorded as no verdict (and still reads as a pass); one that finishes is not."""
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": "sleep 5", "local_gate_timeout_s": 0.3})
    wt = str(tmp_path)
    assert await loop._run_local_gate(wt) is None
    assert wt in loop._gate_no_verdict
    loop.local_gate_cmd = "true"
    assert await loop._run_local_gate(wt) is None
    assert wt not in loop._gate_no_verdict


# ── #490: the listing reads the running loop's cap ──────────────────────────────


def _held_card():
    return {
        "id": "bd-1",
        "title": "bd-1",
        "board_state": "in_review",
        "blocked": False,
        "labels": ["review-clean", "budget:merged-verify:6"],
        "pr_url": PR,
        "priority": 2,
        "difficulty": "",
    }


def test_posture_reports_exhaustion_from_the_live_loops_cap():
    """The loop runs with a cap of 5 and armed the sentinel (6). A listing handed a config
    that says 0 (a Settings save not yet applied by a restart) must still say held."""
    cfg = {"auto_merge": True, "review_gate": True, "merged_verify_max": 0}
    (row,) = annotate_next_action([_held_card()], cfg)
    assert row["next_action"] == "auto-merge pending"  # no loop: the config is all there is
    loop_common._register_loop(_vloop(merged_verify_max=5))
    (row,) = annotate_next_action([_held_card()], cfg)
    assert row["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED
    assert "board_reset_merged_verify_budget bd-1" in row["next_action_hint"]


def test_features_payload_reports_exhaustion_from_the_live_loop(monkeypatch):
    class _Store:
        def list_features(self, state=None, include_archived=False):
            return [_held_card()]

    monkeypatch.setattr(api, "get_store", lambda **_kw: _Store())
    app = FastAPI()
    app.include_router(
        api.build_data_router({"auto_merge": True, "review_gate": True, "merged_verify_max": 0}),
        prefix="/api/plugins/project_board",
    )
    loop_common._register_loop(_vloop(merged_verify_max=5))
    (f,) = TestClient(app).get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED


def test_no_loop_falls_back_to_the_configured_cap():
    (row,) = annotate_next_action([_held_card()], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED  # the manifest default, 5


# ── #495: next_action names the GitHub blocker ──────────────────────────────────


class _MergeStore:
    def __init__(self, feature):
        self.feature = dict(feature)
        self.comments = []

    def get_feature(self, fid):
        return dict(self.feature)

    def comment(self, fid, text):
        self.comments.append((fid, text))


def _reviewed():
    return {
        "id": "bd-1",
        "title": "bd-1",
        "board_state": "in_review",
        "blocked": False,
        "labels": ["in-review", "review-clean"],
        "pr_url": PR,
        "priority": 2,
        "difficulty": "",
    }


_VIEW = {
    "state": "OPEN",
    "headRefOid": "a" * 40,
    "reviews": [],
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "QA panel", "status": "IN_PROGRESS", "conclusion": ""},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "StatusContext", "context": "Review at head", "state": "SUCCESS"},
    ],
}


def _mss(monkeypatch, status):
    monkeypatch.setattr(worktree, "pr_merge_info", _aret({"mergeStateStatus": status, "isDraft": False}))


async def test_next_action_names_an_unstable_blocker(monkeypatch):
    _mss(monkeypatch, "UNSTABLE")
    merges = []
    monkeypatch.setattr(worktree, "merge_pr", lambda *a, **k: merges.append(a))
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=_VIEW) is False
    assert merges == []
    (row,) = annotate_next_action([_reviewed()], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "held: PR not clean on GitHub (UNSTABLE) — QA panel: in progress; lint: failure"
    assert row["awaiting_merge"] is False
    assert "#42" in row["next_action_hint"] and "CLEAN" in row["next_action_hint"]
    assert store.comments == []  # display only: BLOCKED/UNSTABLE is every fresh PR's normal state


async def test_blocked_without_a_read_still_names_the_status(monkeypatch):
    """A queued card has no pass read (`view` None): the status alone is still named."""
    _mss(monkeypatch, "BLOCKED")
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    await loop._maybe_auto_merge(_MergeStore(_reviewed()), "bd-1", PR, "/repo")
    (row,) = annotate_next_action([_reviewed()], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "held: PR not clean on GitHub (BLOCKED)"
    assert "unresolved review threads" in row["next_action_hint"]


async def test_the_hold_survives_unknown_and_clears_on_clean(monkeypatch):
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    _mss(monkeypatch, "UNSTABLE")
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=_VIEW)
    _mss(monkeypatch, "UNKNOWN")  # GitHub recomputing: keep what we knew
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=_VIEW)
    assert merge_state_hold.hold_for("bd-1")["status"] == "UNSTABLE"
    _mss(monkeypatch, "CLEAN")
    monkeypatch.setattr(worktree, "merge_pr", _aret((False, "not mergeable")))
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=_VIEW)
    assert merge_state_hold.hold_for("bd-1") is None
    (row,) = annotate_next_action([_reviewed()], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "auto-merge pending"


def test_a_board_side_blocker_outranks_the_github_hold():
    """The hold only replaces `auto-merge pending`: a card now in review, or on merge-hold,
    reads that instead of a stale GitHub status."""
    merge_state_hold.set_hold("bd-1", "UNSTABLE", ["QA panel: in progress"], PR)
    card = dict(_reviewed(), labels=["in-review", "review-pending"])
    (row,) = annotate_next_action([card], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "review in progress"
    (row,) = annotate_next_action([_reviewed()], {"auto_merge": False, "review_gate": True})
    assert row["next_action"] == "awaiting-merge (auto_merge off)"


def test_outstanding_checks_reads_both_rollup_shapes():
    assert merge_state_hold.outstanding_checks(None) == []
    view = {
        "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"},
            {"__typename": "CheckRun", "name": "e2e", "status": "QUEUED"},
            {"__typename": "CheckRun", "name": "e2e", "status": "COMPLETED", "conclusion": "TIMED_OUT"},
            {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
            "garbage",
        ]
    }
    assert merge_state_hold.outstanding_checks(view) == ["ci/legacy: pending", "e2e: queued"]

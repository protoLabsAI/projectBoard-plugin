"""#487: rerun a red PR's failed GitHub Actions jobs ONCE per head before spending a coder
fix round on what may be a flake.

The unit tier drives the CI reconcile with a mocked ``gh`` (the ``_stub_ci_worktree`` style
of tests/test_loop.py): the first red reruns and spends nothing; green after the rerun logs
one flake line and clears the stamp; red again at the same head bounces as before; a new
head gets a new allowance; no Actions run ids, or ``ci_rerun_max: 0``, bounce at once.

The stamp itself (``ci-rerun:<sha>:<n>``) goes through REAL ``br`` below, because a label is
exactly where a mock hides the beads 50-char cap (#353). The ``gh run rerun --failed`` seam
is exercised against real GitHub in tests/test_publish_gate_real.py.
"""

from __future__ import annotations

import logging
import shutil

import pytest
from conftest import REAL_SEAMS

from project_board import store as store_mod
from project_board import worktree
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard

PR = "https://github.com/acme/app/pull/9"
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NEW_HEAD = "ffeeddccbbaa99887766554433221100ffeeddcc"
RED = ("failing", "Failing checks:\n- Web E2E: FAILURE\n- Lint: FAILURE\n\nFailing log (truncated):\n- not a check")


class _Store:
    """The CI-reconcile fake store, with a label set the rerun stamp really mutates."""

    def __init__(self, labels=None):
        self.feature = {"id": "bd-ci", "pr_url": PR, "labels": list(labels or [])}
        self.requeued: list[str] = []
        self.blocked: list[tuple] = []
        self.budgets: list[tuple] = []
        self.stamps: list[tuple] = []

    def list_features(self, state=None):
        return [dict(self.feature, labels=list(self.feature["labels"]))] if state == "in_review" else []

    def record_merge(self, *, pr_url):
        return None

    def requeue(self, fid):
        self.requeued.append(fid)
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))

    def escalate(self, fid, reason):
        return None

    def record_budget(self, fid, kind, n):
        self.budgets.append((fid, kind, n))
        return {"id": fid}

    def clear_budgets(self, fid, kinds=None):
        return {"id": fid}

    def record_ci_rerun(self, fid, head="", n=1):
        self.stamps.append((head, n))
        keep = [l for l in self.feature["labels"] if not l.startswith(store_mod.LABEL_CI_RERUN_PREFIX)]
        if head:
            keep.append(f"{store_mod.LABEL_CI_RERUN_PREFIX}{head[:12]}:{n}")
        self.feature["labels"] = keep
        return self.feature


class _Gh:
    """The mocked GitHub the reconcile reads: a CI verdict, a head, and the rerun seam."""

    def __init__(self, monkeypatch, *, ci=RED, head=HEAD, rerun=("111",)):
        self.ci, self.head, self.rerun = ci, head, list(rerun)
        self.reruns: list[str] = []

        async def _pr_state(url, *, cwd="."):
            return "OPEN"

        async def _pr_ci(url, *, cwd=".", log_chars=3000):
            return self.ci

        async def _pr_diff(url, *, cwd=".", max_chars=4000):
            return "- a\n+ b"

        async def _reap(repo, root, fid):
            return None

        async def _merge_state(url, *, cwd="."):
            return "CLEAN"

        async def _head(url, *, cwd="."):
            return self.head

        async def _rerun(pr_url="", *, cwd=".", run_ids=None, slug=""):
            self.reruns.append(pr_url)
            return list(self.rerun)

        monkeypatch.setattr(worktree, "pr_state", _pr_state)
        monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
        monkeypatch.setattr(worktree, "pr_diff", _pr_diff)
        monkeypatch.setattr(worktree, "pr_merge_state", _merge_state)
        monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
        monkeypatch.setattr(worktree, "pr_head_sha", _head)
        monkeypatch.setattr(worktree, "rerun_failed_ci", _rerun)


def _setup(monkeypatch, cfg=None, labels=None, **gh):
    store = _Store(labels)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    return store, _Gh(monkeypatch, **gh), BoardLoop({"ci_fix_max": 2, **(cfg or {})})


def _ci_fix_spent(store) -> list[int]:
    return [n for _f, kind, n in store.budgets if kind == "ci-fix"]


async def test_the_first_red_reruns_and_spends_no_fix_round(monkeypatch, caplog):
    store, gh, loop = _setup(monkeypatch)
    assert loop.ci_rerun_max == 1  # the default
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_prs()
    assert gh.reruns == [PR]
    assert store.requeued == [] and _ci_fix_spent(store) == [] and store.blocked == []
    assert store.feature["labels"] == [f"ci-rerun:{HEAD[:12]}:1"]
    assert "bd-ci" not in loop._ci_feedback  # no fix round is being prepared
    assert f"CI red at {HEAD[:12]} — rerunning failed jobs once before a fix round: 111" in caplog.text


async def test_green_after_the_rerun_logs_one_flake_and_clears_the_stamp(monkeypatch, caplog):
    store, gh, loop = _setup(monkeypatch)
    await loop._reconcile_prs()  # red → rerun
    gh.ci = ("pending", "")
    await loop._reconcile_prs()  # the rerun is running: nothing happens
    assert store.feature["labels"] == [f"ci-rerun:{HEAD[:12]}:1"] and store.requeued == []
    gh.ci = ("passing", "")
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_prs()
        await loop._reconcile_prs()  # a second green pass: nothing left to settle
    flakes = [r.getMessage() for r in caplog.records if "CI flake" in r.getMessage()]
    assert len(flakes) == 1 and "Web E2E, Lint" in flakes[0] and "not a check" not in flakes[0]
    assert store.feature["labels"] == [] and store.requeued == [] and _ci_fix_spent(store) == []
    assert store.stamps == [(HEAD, 1), ("", 1)]


async def test_red_again_at_the_same_head_bounces_as_before(monkeypatch):
    store, gh, loop = _setup(monkeypatch)
    await loop._reconcile_prs()  # red → rerun
    await loop._reconcile_prs()  # red again at the same head → the old bounce
    assert gh.reruns == [PR]
    assert store.requeued == ["bd-ci"] and _ci_fix_spent(store) == [1]
    assert "Web E2E" in loop._ci_feedback["bd-ci"]


async def test_the_allowance_survives_a_restart(monkeypatch):
    """The stamp is on the bead: a fresh loop reading a red rollup at a head that was
    already rerun bounces, it does not rerun again."""
    store, gh, _loop = _setup(monkeypatch, labels=[f"ci-rerun:{HEAD[:12]}:1"])
    await BoardLoop({"ci_fix_max": 2})._reconcile_prs()
    assert gh.reruns == [] and store.requeued == ["bd-ci"]


async def test_a_new_head_gets_a_new_allowance(monkeypatch):
    store, gh, loop = _setup(monkeypatch)
    await loop._reconcile_prs()  # red at HEAD → rerun
    gh.head = NEW_HEAD  # a push
    await loop._reconcile_prs()  # red at the new head → rerun again, nothing spent
    assert gh.reruns == [PR, PR] and store.requeued == []
    assert store.feature["labels"] == [f"ci-rerun:{NEW_HEAD[:12]}:1"]
    await loop._reconcile_prs()  # red again at the new head → bounce
    assert store.requeued == ["bd-ci"]


async def test_no_actions_run_ids_bounces_immediately(monkeypatch):
    """Only a non-Actions required status is red (or gh refused): nothing was rerun, so the
    card bounces on the first red, unstamped."""
    store, gh, loop = _setup(monkeypatch, rerun=())
    await loop._reconcile_prs()
    assert gh.reruns == [PR] and store.requeued == ["bd-ci"] and _ci_fix_spent(store) == [1]
    assert store.feature["labels"] == [] and store.stamps == []


async def test_ci_rerun_max_zero_is_the_old_behavior(monkeypatch):
    store, gh, loop = _setup(monkeypatch, cfg={"ci_rerun_max": 0})
    await loop._reconcile_prs()
    assert gh.reruns == [] and store.requeued == ["bd-ci"] and _ci_fix_spent(store) == [1]


async def test_ci_rerun_max_two_reruns_a_head_twice(monkeypatch):
    store, gh, loop = _setup(monkeypatch, cfg={"ci_rerun_max": 2})
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert gh.reruns == [PR, PR] and store.requeued == []
    assert store.feature["labels"] == [f"ci-rerun:{HEAD[:12]}:2"]
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci"]


async def test_green_at_a_new_head_clears_the_stamp_without_a_flake(monkeypatch, caplog):
    store, gh, loop = _setup(monkeypatch)
    await loop._reconcile_prs()
    gh.head, gh.ci = NEW_HEAD, ("passing", "")
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._reconcile_prs()
    assert "CI flake" not in caplog.text and store.feature["labels"] == []


# ── the rollup parse behind the seam ──────────────────────────────────────────────


def test_failed_ci_run_ids_reads_actions_runs_behind_red_blocking_checks():
    run = "https://github.com/acme/app/actions/runs/{}/job/{}"
    checks = [
        {
            "__typename": "CheckRun",
            "name": "e2e",
            "workflowName": "CI",
            "conclusion": "FAILURE",
            "detailsUrl": run.format(11, 1),
        },
        {
            "__typename": "CheckRun",
            "name": "unit",
            "workflowName": "CI",
            "conclusion": "FAILURE",
            "detailsUrl": run.format(11, 2),
        },
        {
            "__typename": "CheckRun",
            "name": "lint",
            "workflowName": "Lint",
            "conclusion": "TIMED_OUT",
            "detailsUrl": run.format(22, 3),
        },
        {
            "__typename": "CheckRun",
            "name": "ok",
            "workflowName": "CI",
            "conclusion": "SUCCESS",
            "detailsUrl": run.format(33, 4),
        },
        # A red non-Actions status: nothing to rerun.
        {
            "__typename": "StatusContext",
            "context": "deploy",
            "state": "FAILURE",
            "targetUrl": "https://ci.example/1",
            "isRequired": True,
        },
        # A red App check (advisory, no workflow): not blocking, never rerun.
        {
            "__typename": "CheckRun",
            "name": "QA panel",
            "workflowName": "",
            "conclusion": "FAILURE",
            "detailsUrl": run.format(44, 5),
        },
    ]
    assert worktree.failed_ci_run_ids(checks) == [("acme/app", "11"), ("acme/app", "22")]
    assert worktree.failed_ci_run_ids([checks[4]]) == []


async def test_rerun_failed_ci_reruns_each_run_and_drops_refusals(monkeypatch):
    calls: list[tuple] = []
    rollup = (
        '[{"__typename":"CheckRun","name":"e2e","workflowName":"CI","conclusion":"FAILURE",'
        '"detailsUrl":"https://github.com/acme/app/actions/runs/11/job/1"},'
        '{"__typename":"CheckRun","name":"lint","workflowName":"L","conclusion":"FAILURE",'
        '"detailsUrl":"https://github.com/acme/app/actions/runs/22/job/2"}]'
    )

    async def _gh(*args, cwd, timeout=60):
        calls.append(args)
        if args[:2] == ("pr", "view"):
            return 0, rollup, ""
        if args[2] == "22":
            return 1, "", "run 22 cannot be rerun; This workflow is already running"
        return 0, "", ""

    monkeypatch.setattr(worktree, "_gh", _gh)
    real = REAL_SEAMS["worktree.rerun_failed_ci"]
    assert await real(PR, cwd="/repo") == ["11"]
    assert calls[1] == ("run", "rerun", "11", "--failed", "-R", "acme/app")

    async def _boom(*args, cwd, timeout=60):
        raise worktree.WorktreeError("gh timed out")

    monkeypatch.setattr(worktree, "_gh", _boom)
    assert await real(PR, cwd="/repo") == []  # never raises into the loop


# ── the stamp, through REAL `br` ─────────────────────────────────────────────────

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)


@requires_br
@pytest.mark.br_shape
def test_record_ci_rerun_stamps_replaces_and_clears_real_br(tmp_path):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature("fix: a flaky thing", spec="s")["id"]

    def stamps():
        return [l for l in board.get_feature(fid)["labels"] if l.startswith("ci-rerun:")]

    board.record_ci_rerun(fid, HEAD, 1)
    assert stamps() == [f"ci-rerun:{HEAD[:12]}:1"]
    assert len(stamps()[0]) <= 50  # beads' label cap
    board.record_ci_rerun(fid, HEAD, 1)  # an unchanged re-stamp keeps exactly one copy (#338)
    assert stamps() == [f"ci-rerun:{HEAD[:12]}:1"]
    board.record_ci_rerun(fid, NEW_HEAD, 2)
    assert stamps() == [f"ci-rerun:{NEW_HEAD[:12]}:2"]  # replaced, never accumulated
    assert store_mod.ci_rerun_from_labels(board.get_feature(fid)["labels"]) == (NEW_HEAD[:12], 2)
    board.record_ci_rerun(fid, "")
    assert stamps() == []
    board.record_ci_rerun(fid, "")  # nothing to clear: a no-op, not a failed write
    assert stamps() == []

"""The merged-state re-verify must not call a broken dependency tree "the RESULT is broken".

Seen 2026-09-29/30 on the designSystem agent (cards ds-xof / PR #567, ds-h5s / PR #565): in
the `.worktrees/.verify-feat-…` tree pnpm printed "The modules directories will be removed
and reinstalled from scratch. Proceed?" (auto-answered), then typecheck died with
MODULE_NOT_FOUND for node_modules/typescript/bin/tsc. The board filed "gate FAILED on the
merged state — the RESULT is broken", terminal-blocked the card and paged a human, on a
docs-only card whose PR had already merged with green CI.

Three fixes, one test group each:
(a) a failed `setup_cmd` install skips the gate: INFRA, retried, bounded;
(b) gate output that shows a broken dependency tree is no verdict (INFRA);
(c) a red verdict re-reads the PR and card before it blocks: a card that merged, closed or
    left review while the gate ran is reported, never terminal-blocked.
"""

from __future__ import annotations

import pytest

from project_board import worktree
from project_board.loop import _MERGED_VERIFY_INFRA_MAX, BoardLoop, broken_dependency_tree
from tests.test_loop import _aret, _VerifyStore, _vloop

# The incident's own output, trimmed.
INCIDENT = """\
 WARN  The modules directories will be removed and reinstalled from scratch. Proceed? (Y/n) · true
> design-system@0.0.0 typecheck /repo/.worktrees/.verify-feat-ds-xof
> tsc --noEmit
node:internal/modules/cjs/loader:1228
  throw err;
  ^
Error: Cannot find module '/repo/.worktrees/.verify-feat-ds-xof/node_modules/typescript/bin/tsc'
    at Module._resolveFilename (node:internal/modules/cjs/loader:1225:15) {
  code: 'MODULE_NOT_FOUND',
  requireStack: []
}
 ELIFECYCLE  Command failed with exit code 1.
"""


class _Store(_VerifyStore):
    """A store whose card state the test sets, recording comments."""

    def __init__(self, feature, board_state="in_review"):
        super().__init__(feature)
        self.board_state = board_state
        self.comments = []

    def get_feature(self, fid):
        return {**self._feature, "board_state": self.board_state}

    def comment(self, fid, text):
        self.comments.append((fid, text))


def _merged_tree(monkeypatch, wt="/repo/.worktrees/.verify-feat-bd-1", sha="def456abcdef99"):
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(sha))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", wt)))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(True))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))


# ── (b) the classifier ────────────────────────────────────────────────────────────


def test_classifier_reads_the_incident_as_a_broken_tree():
    assert broken_dependency_tree(INCIDENT)
    assert broken_dependency_tree(INCIDENT.encode())  # raw bytes too


@pytest.mark.parametrize(
    "out",
    [
        "Error: Cannot find module '/w/node_modules/typescript/bin/tsc'\n  code: 'MODULE_NOT_FOUND'",
        'Error: Cannot find module "C:\\\\w\\\\node_modules\\\\vite\\\\bin\\\\vite.js"',
        " WARN  The modules directories will be removed and reinstalled from scratch. Proceed?",
        " ERR_PNPM_FETCH_404  GET https://registry.npmjs.org/x: Not Found - 404",
        "ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @x/web@1.0.0 typecheck: `tsc`",
    ],
)
def test_classifier_matches_each_signature(out):
    assert broken_dependency_tree(out)


@pytest.mark.parametrize(
    "out",
    [
        "FAILED tests/test_x.py::test_y - AssertionError",
        "src/a.ts(3,1): error TS2304: Cannot find name 'foo'.",
        # The code's OWN missing import is a real failure a coder can fix — not under node_modules.
        "Error: Cannot find module 'lodash'\nRequire stack:\n- /w/src/index.js",
        "Error: Cannot find module './util'",
        # A lockfile that disagrees with package.json is the repo's state, not the tree's.
        " ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with frozen-lockfile",
        "",
        None,
    ],
)
def test_classifier_leaves_real_failures_red(out):
    assert broken_dependency_tree(out) == ""


async def test_run_local_gate_reads_a_broken_tree_as_no_verdict(tmp_path):
    """A REAL gate printing the incident output and exiting 1 judged nothing: no verdict,
    CI still gates, and the tree is recorded as INFRA for the merged-state verify."""
    (tmp_path / "out.txt").write_text(INCIDENT)
    loop = BoardLoop({"local_gate_cmd": "cat out.txt; exit 1"})
    assert await loop._run_local_gate(str(tmp_path)) is None
    assert str(tmp_path) in loop._gate_no_verdict
    assert "broken dependency tree" in loop._gate_infra[str(tmp_path)]


async def test_run_local_gate_still_fails_a_real_red_and_clears_a_stale_infra_mark(tmp_path):
    loop = BoardLoop({"local_gate_cmd": "echo 'FAILED tests/test_x.py::test_y'; exit 1"})
    loop._gate_infra[str(tmp_path)] = "left over from an earlier run"
    out = await loop._run_local_gate(str(tmp_path))
    assert out and "FAILED tests/test_x.py::test_y" in out
    assert str(tmp_path) not in loop._gate_infra and str(tmp_path) not in loop._gate_no_verdict


async def test_the_incident_end_to_end_never_blocks(monkeypatch, tmp_path):
    """The real gate over the incident output, inside the real merged-state verify: no
    block, no stamp, no budget, next poll retries."""
    (tmp_path / "out.txt").write_text(INCIDENT)
    _merged_tree(monkeypatch, wt=str(tmp_path))
    store = _Store({"id": "bd-1"})
    loop = _vloop(local_gate_cmd="cat out.txt; exit 1", merged_verify_max=5)
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.blocked == [] and store.verified == [] and store.budgets == []
    assert loop._merged_verify_infra == {"bd-1": 1}


# ── (a) + (b) INFRA retry, bounded ────────────────────────────────────────────────


async def test_failed_install_skips_the_gate_and_retries_without_stamp_or_spend(monkeypatch):
    _merged_tree(monkeypatch)
    monkeypatch.setattr(worktree, "prepare_worktree", _aret("setup_cmd exited 1: ERR_PNPM_FETCH_404"))
    store = _Store({"id": "bd-1"})
    loop = _vloop(setup_cmd="pnpm install", merged_verify_max=5)
    ran = []

    async def _gate(wt, feature=None):
        ran.append(wt)
        return "would be red"

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    feature = {"id": "bd-1", "labels": []}
    for i in range(1, _MERGED_VERIFY_INFRA_MAX):
        assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
        assert loop._merged_verify_infra["bd-1"] == i
    assert ran == []  # the gate never ran over a half-installed tree
    assert store.blocked == [] and store.verified == [] and store.budgets == [] and store.comments == []


async def test_the_nth_infra_run_says_INFRA_once_and_counts_as_no_verdict(monkeypatch, caplog):
    """Bounded: the Nth consecutive infra run surfaces one INFRA warning + card comment
    (never "the RESULT is broken"), then records a no-verdict run — stamped like a
    timed-out gate, one merged-verify unit spent — so a broken install can't reinstall
    every poll forever."""
    _merged_tree(monkeypatch)
    monkeypatch.setattr(worktree, "prepare_worktree", _aret("setup_cmd timed out after 600s"))
    store = _Store({"id": "bd-1"})
    loop = _vloop(setup_cmd="pnpm install", merged_verify_max=5)
    feature = {"id": "bd-1", "labels": []}
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        for _ in range(_MERGED_VERIFY_INFRA_MAX):
            assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert store.blocked == []
    assert store.verified == [("bd-1", "def456abcdef")]
    assert store.budgets == [("bd-1", "merged-verify", 1)]
    assert len(store.comments) == 1 and store.comments[0][1].startswith("INFRA:")
    assert "NOT a verdict" in store.comments[0][1] and "setup_cmd timed out" in store.comments[0][1]
    assert "RESULT is broken" not in caplog.text
    assert "bd-1" not in loop._merged_verify_infra  # the streak starts over


async def test_a_broken_tree_gate_retries_and_a_real_verdict_resets_the_streak(monkeypatch):
    _merged_tree(monkeypatch, wt="/wt")
    store = _Store({"id": "bd-1"})
    loop = _vloop(merged_verify_max=5)
    results = iter(["infra", "infra", "green"])

    async def _gate(wt, feature=None):
        if next(results) == "infra":
            loop._gate_no_verdict.add(wt)
            loop._gate_infra[wt] = "broken dependency tree: Cannot find module '/wt/node_modules/x'"
        return None

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    feature = {"id": "bd-1", "labels": []}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert loop._merged_verify_infra["bd-1"] == 2 and store.verified == [] and store.budgets == []
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # a real green
    assert store.verified == [("bd-1", "def456abcdef")]
    assert "bd-1" not in loop._merged_verify_infra and store.blocked == []


# ── (c) re-read the PR and the card before a red verdict blocks ──────────────────


async def _red(monkeypatch, *, pr_state, board_state):
    _merged_tree(monkeypatch)
    monkeypatch.setattr(worktree, "pr_state", _aret(pr_state))
    store = _Store({"id": "bd-1"}, board_state=board_state)
    loop = _vloop(merged_verify_max=5)
    monkeypatch.setattr(loop, "_run_local_gate", _aret("FAILED tests/test_x.py::test_y"))
    res = await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo")
    return res, store


async def test_red_on_a_pr_that_merged_while_the_gate_ran_reports_but_never_blocks(monkeypatch):
    res, store = await _red(monkeypatch, pr_state="MERGED", board_state="in_review")
    assert res is True  # nothing further this pass; next poll records the merge
    assert store.blocked == [] and store.budgets == [] and store.verified == []
    assert len(store.comments) == 1
    note = store.comments[0][1]
    assert "NOT blocked" in note and "FAILED tests/test_x.py::test_y" in note and "main" in note


async def test_red_on_a_done_card_reports_but_never_blocks(monkeypatch):
    res, store = await _red(monkeypatch, pr_state="OPEN", board_state="done")
    assert res is True and store.blocked == [] and len(store.comments) == 1


async def test_red_on_a_pr_closed_while_the_gate_ran_leaves_it_to_the_closed_edge(monkeypatch):
    res, store = await _red(monkeypatch, pr_state="CLOSED", board_state="in_review")
    assert res is True and store.blocked == [] and store.comments == []


@pytest.mark.parametrize("moved_to", ["ready", "in_progress", "blocked"])
async def test_red_on_a_card_that_left_review_does_not_clobber_its_state(monkeypatch, moved_to):
    res, store = await _red(monkeypatch, pr_state="OPEN", board_state=moved_to)
    assert res is True and store.blocked == []


async def test_red_on_an_open_in_review_card_still_blocks(monkeypatch):
    """The point of the verify is unchanged: a clean red on a still-open PR blocks."""
    res, store = await _red(monkeypatch, pr_state="OPEN", board_state="in_review")
    assert res is True
    assert [b[0] for b in store.blocked] == ["bd-1"] and "RESULT is broken" in store.blocked[0][1]
    assert store.budgets == [("bd-1", "merged-verify", 1)]


async def test_red_with_unreadable_pr_and_card_keeps_blocking(monkeypatch):
    """Nothing says the card moved, so the real verdict stands."""
    _merged_tree(monkeypatch)
    monkeypatch.setattr(worktree, "pr_state", _aret(""))  # gh failure
    store = _VerifyStore({"id": "bd-1"})  # no get_feature at all
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret("FAILED tests/test_x.py::test_y"))
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is True
    assert [b[0] for b in store.blocked] == ["bd-1"]

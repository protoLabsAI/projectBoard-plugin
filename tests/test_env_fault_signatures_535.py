"""A disk / permission / read-only-filesystem fault in gate output is the ENVIRONMENT
failing, not the repo's code — so it must route through the SAME INFRA classifier #516
built for a broken dependency tree, never a verdict on the PR.

Seen 2026-10 on nerdsville-site (card bd-ufs / PR #77): the merged-state re-verify ran
194/194 green, then vitest died with `ENOSPC: no space left on device` while writing its
output. The board read it as "the PR merges clean but the RESULT is broken", spent 2
retries and terminal-blocked a card whose PR CI was green.

Two test groups:
(a) the classifier (``broken_dependency_tree``) reads ENOSPC / EROFS / errno-style EACCES
    as no-verdict, while leaving a bare "EACCES" in test output — and #516's own
    ERR_PNPM_OUTDATED_LOCKFILE — a real red verdict;
(b) the consumer (merged-state re-verify) never blocks, stamps, or spends on an ENOSPC.
"""

from __future__ import annotations

import pytest

from project_board import worktree
from project_board.loop import BoardLoop, broken_dependency_tree
from tests.test_loop import _aret, _VerifyStore, _vloop

# The incident's own output, trimmed: a fully green run that then dies writing to a full
# disk. Ends on the exact errno line acceptance criterion 1 names.
INCIDENT = """\
> nerdsville-site@0.0.0 test /repo/.worktrees/.verify-feat-bd-ufs
> vitest run

 Test Files  40 passed (40)
      Tests  194 passed (194)
   Start at  09:14:02
   Duration  31.2s

node:internal/fs/utils:...
Error: ENOSPC: no space left on device, write
"""


class _Store(_VerifyStore):
    """A store whose card state the test sets, recording comments (mirrors #516's)."""

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


# ── (a) the classifier reads environment faults as no-verdict ───────────────────────


def test_classifier_reads_the_incident_as_infra():
    assert broken_dependency_tree(INCIDENT)
    assert broken_dependency_tree(INCIDENT.encode())  # raw bytes too


@pytest.mark.parametrize(
    "out",
    [
        # r1: the exact line acceptance criterion 1 names.
        "Error: ENOSPC: no space left on device, write",
        "Error: ENOSPC: no space left on device, write\n ELIFECYCLE  Command failed.",
        "FATAL ERROR: no space left on device",
        # r2: EROFS and errno-style EACCES.
        "Error: EROFS: read-only file system, open '/repo/dist/out.js'",
        "EROFS: read-only file system, mkdir '/repo/node_modules/.cache'",
        "Error: EACCES: permission denied, open '/etc/hosts'",
        "Error: EACCES: permission denied, mkdir '/var/lib/pnpm'",
        "EACCES: permission denied, unlink '/repo/.cache/x'",
        'EACCES: permission denied, rename "C:\\\\tmp\\\\a"',
    ],
)
def test_classifier_matches_each_env_fault_signature(out):
    assert broken_dependency_tree(out)


@pytest.mark.parametrize(
    "out",
    [
        # r2: a permission ASSERTION a coder wrote is a real, fixable red — not an errno
        # line, just the word. Must stay a verdict.
        "FAIL tests/perm.test.ts > denies the write\n  expected error code 'EACCES'",
        "AssertionError: expected thrown.code to equal EACCES",
        # errno-looking EACCES but a syscall the narrow signature deliberately excludes.
        "Error: EACCES: permission denied, scandir '/repo/src'",
        # r4: #516's one deliberate code verdict stays a verdict.
        " ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with frozen-lockfile",
        # plain red output with none of the words.
        "FAILED tests/test_x.py::test_y - AssertionError",
        "",
        None,
    ],
)
def test_classifier_leaves_real_failures_red(out):
    assert broken_dependency_tree(out) == ""


# ── (b) the consumer: ENOSPC never blocks, stamps, or spends ────────────────────────


async def test_run_local_gate_reads_enospc_as_no_verdict(tmp_path):
    """A REAL gate printing the ENOSPC incident and exiting 1 judged nothing: no verdict,
    CI still gates, and the tree is recorded as INFRA for the merged-state verify."""
    (tmp_path / "out.txt").write_text(INCIDENT)
    loop = BoardLoop({"local_gate_cmd": "cat out.txt; exit 1"})
    assert await loop._run_local_gate(str(tmp_path)) is None
    assert str(tmp_path) in loop._gate_no_verdict
    assert loop._gate_infra[str(tmp_path)]  # recorded as INFRA, not returned as a red


async def test_enospc_merged_verify_never_blocks_or_spends(monkeypatch, tmp_path):
    """The incident end to end inside the real merged-state verify: no block, no stamp,
    no budget spent — the card is not terminal-blocked and no retry is consumed; the next
    poll simply retries (one INFRA streak recorded)."""
    (tmp_path / "out.txt").write_text(INCIDENT)
    _merged_tree(monkeypatch, wt=str(tmp_path))
    store = _Store({"id": "bd-1"})
    loop = _vloop(local_gate_cmd="cat out.txt; exit 1", merged_verify_max=5)
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.blocked == [] and store.verified == [] and store.budgets == []
    assert loop._merged_verify_infra == {"bd-1": 1}

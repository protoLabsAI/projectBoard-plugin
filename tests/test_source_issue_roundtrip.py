"""Integration: source_issue round-trips through a REAL `br` store (#101).

The original write path stored source_issue as a `source:owner/repo#N` label, but
beads' label validator only allows [alphanumeric - _ :] — so every REAL write died
with VALIDATION_FAILED while the fake-`_run` unit tests (which accept any label)
kept passing. That class of bug can only be caught against the actual CLI: these
tests run `br` in a throwaway workspace (``_ensure_workspace`` inits ``.beads/``
in the tmp repo) and assert the value survives create → get_feature. Skipped when
the `br` binary isn't on PATH — everything else in the suite stays CLI-free.
"""

from __future__ import annotations

import shutil

import pytest

from project_board import store as store_mod
from project_board.loop import _source_issue
from project_board.store import BeadsBoard

pytestmark = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — integration round-trip needs it",
)


@pytest.fixture
def board(tmp_path):
    """A BeadsBoard pinned to a fresh tmp workspace — the real `br`, a real db."""
    return BeadsBoard(repo=str(tmp_path), actor="test")


def test_source_issue_survives_create_then_get_feature(board):
    f = board.create_feature(
        "Round-trip",
        spec="s",
        acceptance_criteria="WHEN x THE SYSTEM SHALL y",
        files_to_modify=["a.py", "pkg/b.py"],
        source_issue="https://github.com/acme/widgets/issues/97",
    )
    # THE #101 regression signature: the label write failed validation, so create
    # returned success-with-warning with source_issue in missing_fields.
    assert not f.get("enrichment_failed"), f.get("warning")
    g = board.get_feature(f["id"])
    assert g["source_issue"] == "acme/widgets#97"
    # the metadata line shares the notes field but never leaks into the file list.
    assert g["files_to_modify"] == ["a.py", "pkg/b.py"]


def test_update_paths_preserve_each_others_half_of_notes(board):
    f = board.create_feature("U", spec="s", files_to_modify=["x.py"], source_issue="acme/widgets#8")
    fid = f["id"]
    g = board.update_feature(fid, source_issue="https://github.com/acme/widgets/issues/101")
    assert g["source_issue"] == "acme/widgets#101"  # replaced
    assert g["files_to_modify"] == ["x.py"]  # untouched
    g = board.update_feature(fid, files_to_modify=["y.py", "z.py"])
    assert g["files_to_modify"] == ["y.py", "z.py"]  # replaced
    assert g["source_issue"] == "acme/widgets#101"  # untouched


def test_roundtripped_source_issue_feeds_the_pr_fixes_line(board):
    """End to end: real store → projection → loop._source_issue resolves the
    (slug, n) the PR opener stamps as `Fixes #N`."""
    f = board.create_feature("F", spec="s", source_issue="acme/widgets#97")
    assert _source_issue(board.get_feature(f["id"])) == ("acme/widgets", 97)

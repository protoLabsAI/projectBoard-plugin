"""The #767 per-head stamp for signed /ci and /review results, through REAL `br`."""

from __future__ import annotations

import shutil

import pytest

from project_board import store as store_mod
from project_board.store import BeadsBoard

HEAD = "0123456789abcdef0123456789abcdef01234567"
NEW_HEAD = "fedcba9876543210fedcba9876543210fedcba98"

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)


@requires_br
@pytest.mark.br_shape
def test_record_external_result_stamps_replaces_and_clears_real_br(tmp_path):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature("fix: a flaky thing", spec="s")["id"]

    def labels():
        return board.get_feature(fid)["labels"]

    board.record_external_result(fid, "ci", HEAD)
    assert store_mod.external_result_head(labels(), "ci") == HEAD[:12]
    assert store_mod.external_result_head(labels(), "review") == ""  # kinds are independent
    assert all(len(lab) <= 50 for lab in labels())  # beads' label cap
    board.record_external_result(fid, "ci", HEAD)  # an unchanged re-stamp keeps exactly one copy (#338)
    assert [lab for lab in labels() if lab.startswith("ext-ci:")] == [f"ext-ci:{HEAD[:12]}"]
    board.record_external_result(fid, "review", HEAD)
    board.record_external_result(fid, "ci", NEW_HEAD)
    assert [lab for lab in labels() if lab.startswith("ext-ci:")] == [f"ext-ci:{NEW_HEAD[:12]}"]  # replaced
    assert store_mod.external_result_head(labels(), "review") == HEAD[:12]
    board.record_external_result(fid, "ci", "")
    assert store_mod.external_result_head(labels(), "ci") == ""
    board.record_external_result(fid, "ci", "")  # nothing to clear: a no-op, not a failed write

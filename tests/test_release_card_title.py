"""Release cards squash as ``chore: release vX.Y.Z`` so ``release.yml`` fires.

``release.yml`` only runs when the head commit starts ``chore: release v``. Cards titled
``Release <name> vX.Y.Z: bump manifest + pyproject + uv.lock`` used to get ``feat: `` prepended
by ``pr_title_for`` (the PR title AND the squash subject), so their releases silently skipped.
"""

from __future__ import annotations

from project_board.loop import pr_title_for


def test_release_card_title_with_trailing_detail_becomes_chore_release():
    title = "Release pr-reviewer v0.80.0: bump manifest + pyproject + uv.lock"
    assert pr_title_for(title, None) == "chore: release v0.80.0"


def test_lowercase_release_card_title_becomes_chore_release():
    assert pr_title_for("release discord-plugin v0.4.0", None) == "chore: release v0.4.0"


def test_release_card_title_is_case_insensitive():
    assert pr_title_for("RELEASE projectBoard V1.2.3 — cut it", None) == "chore: release v1.2.3"


def test_release_card_subject_satisfies_the_release_workflow_guard():
    subject = pr_title_for("Release promptlab-plugin v0.12.3: bump manifest", None)
    assert subject.startswith("chore: release v")
    assert not subject.startswith("feat:")


def test_already_typed_release_title_is_unchanged():
    assert pr_title_for("chore: release v0.67.0", None) == "chore: release v0.67.0"


def test_ordinary_title_still_gets_feat_prefix():
    assert pr_title_for("add a retry to the fetch", None) == "feat: add a retry to the fetch"


def test_titles_that_only_resemble_a_release_keep_todays_behaviour():
    """No version token right after the name → not a release card → unchanged rules apply."""
    for title in (
        "release notes for the gate",
        "Release the lock before retrying",
        "Release pr-reviewer soon v0.80.0",
        "Release pr-reviewer v0.80",
    ):
        assert pr_title_for(title, None) == f"feat: {title}", title
    assert pr_title_for("fix: release the lock", None) == "fix: release the lock"

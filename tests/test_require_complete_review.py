"""``require_complete_review``: an incomplete external-panel pass at the head holds the merge.

Live (protoEngineer board): protoAgent PR #4101 (card bd-vbyd.13) auto-merged into
``epic/interactive-answers`` on a QA panel pass that concluded ``neutral`` — "Incomplete
pass — not blocking. A finder did not run, so the clear verdict covers less than the whole
diff … Hold: ``hold:incomplete-coverage``". GitHub reads ``neutral`` as passing, the PR was
CLEAN, and the board merged a diff part of which nobody reviewed.

Here:

* the pure judge (``external_review.evaluate``) reading an incomplete pass from the check
  run's ``neutral`` conclusion and from the review marker's ``complete=false``;
* the merge edge: off → merges on neutral (today's behaviour, pinned); on → holds, summons
  ``@vera review`` ONCE per head, and merges once a complete pass clears the head;
* the ``next_action`` projection and the per-project setting resolution.

GitHub is faked at the ``worktree.pr_review_state`` / ``post_or_update_pr_comment`` /
``merge_pr`` seams; their real-``gh`` coverage lives in tests/test_worktree_gh.py.
"""

from __future__ import annotations

from project_board import external_review, review_coverage_hold, worktree
from project_board import projects as projects_mod
from project_board.loop import BoardLoop
from project_board.store import annotate_next_action

HEAD = "e5c66aa9ad75731286916a06d2bd6059494e0389"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
PR = "https://github.com/protoLabsAI/protoAgent/pull/4101"


def _review(verdict="PASS", *, head=HEAD, complete=True, at="2026-10-08T12:00:00Z"):
    marker = f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted=false"
    if not complete:
        marker += " complete=false"
    body = f"{marker} -->\n## QA panel review — **{verdict}**\n"
    return {"author": {"login": "protoreview"}, "body": body, "state": "COMMENTED", "submittedAt": at}


def _panel_check(conclusion, *, completed="2026-10-08T12:01:00Z"):
    return {
        "__typename": "CheckRun",
        "name": "QA panel",
        "conclusion": conclusion,
        "status": "COMPLETED",
        "completedAt": completed,
        "workflowName": "",
    }


def _view(*, head=HEAD, reviews=None, checks=None):
    """The #4101 shape: a WARN review marked ``complete=false`` and the panel's check
    ``neutral``, CI green."""
    return {
        "state": "OPEN",
        "headRefOid": head,
        "reviews": reviews if reviews is not None else [_review("WARN", head=head, complete=False)],
        "statusCheckRollup": checks
        if checks is not None
        else [
            _panel_check("NEUTRAL"),
            {"__typename": "CheckRun", "name": "test", "conclusion": "SUCCESS", "workflowName": "CI"},
        ],
    }


def _complete_view(head=HEAD):
    return _view(
        head=head,
        reviews=[_review("WARN", head=head, complete=False), _review("PASS", head=head, at="2026-10-08T13:00:00Z")],
        checks=[_panel_check("SUCCESS", completed="2026-10-08T13:00:30Z")],
    )


# ── the pure judge ──────────────────────────────────────────────────────────────────────


def test_the_incident_payload_is_an_incomplete_pass_not_a_fail():
    v = external_review.evaluate(_view(), external_review.Config())
    assert not v.failed
    assert v.incomplete
    assert any("NEUTRAL" in s for s in v.incomplete_signals)
    assert any("complete=false" in s for s in v.incomplete_signals)


def test_a_neutral_check_alone_is_incomplete():
    v = external_review.evaluate(_view(reviews=[]), external_review.Config())
    assert v.incomplete and not v.failed


def test_a_complete_false_marker_alone_is_incomplete():
    v = external_review.evaluate(_view(checks=[]), external_review.Config())
    assert v.incomplete and not v.failed


def test_a_later_complete_pass_clears_it():
    v = external_review.evaluate(_complete_view(), external_review.Config())
    assert not v.incomplete and not v.failed


def test_a_complete_review_after_the_neutral_check_clears_it():
    view = _view(reviews=[_review("PASS", at="2026-10-08T14:00:00Z")], checks=[_panel_check("NEUTRAL")])
    assert not external_review.evaluate(view, external_review.Config()).incomplete


def test_an_incomplete_marker_for_another_head_says_nothing_about_this_one():
    view = _view(reviews=[_review("WARN", head=NEW_HEAD, complete=False)], checks=[])
    assert not external_review.evaluate(view, external_review.Config()).incomplete


def test_an_actions_job_named_qa_panel_is_not_the_panel():
    job = dict(_panel_check("NEUTRAL"), workflowName="CI")
    assert not external_review.evaluate(_view(reviews=[], checks=[job]), external_review.Config()).incomplete


def test_a_fail_is_not_reported_as_incomplete():
    view = _view(reviews=[], checks=[_panel_check("FAILURE")])
    v = external_review.evaluate(view, external_review.Config())
    assert v.failed and not v.incomplete


# ── the merge edge ──────────────────────────────────────────────────────────────────────


class _Store:
    def __init__(self, labels=("review-clean",)):
        self.labels = list(labels)
        self.comments: list[str] = []

    def get_feature(self, fid):
        return {"id": "bd-1", "board_state": "in_review", "labels": list(self.labels), "pr_url": PR, "title": "t"}

    def comment(self, fid, text):
        self.comments.append(text)

    def record_budget(self, fid, kind, n):
        self.labels = [lb for lb in self.labels if not lb.startswith(f"budget:{kind}:")] + [f"budget:{kind}:{n}"]

    def clear_budgets(self, fid, kinds=None):
        self.labels = [lb for lb in self.labels if not any(lb.startswith(f"budget:{k}:") for k in (kinds or [""]))]


def _edge(monkeypatch, cfg=None, *, view=None, post_ok=True):
    """A loop whose merge edge has no other blocker, a panel read of ``view`` (mutable via
    ``loop.view``), and recorded merges / PR comments."""
    loop = BoardLoop({"auto_merge": True, **(cfg or {})})
    loop.view = _view() if view is None else view
    loop.merges, loop.posts = [], []

    async def _no_blockers(*a, **k):
        return []

    async def _review_state(pr_url, *, cwd="."):
        return loop.view

    async def _merge(pr_url, **k):
        loop.merges.append(pr_url)
        return True, ""

    async def _post(pr_url, body, *, marker="", cwd="."):
        loop.posts.append((pr_url, body, marker))
        return post_ok

    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(loop, "_auto_merge_blockers", _no_blockers)
    monkeypatch.setattr(worktree, "pr_review_state", _review_state)
    monkeypatch.setattr(worktree, "merge_pr", _merge)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _post)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    monkeypatch.setattr(worktree, "delete_remote_branch", _noop)
    return loop


async def test_off_by_default_an_incomplete_pass_still_merges(monkeypatch):
    """Today's behaviour, pinned: without the setting a neutral pass merges, nothing posts."""
    loop = _edge(monkeypatch)
    store = _Store()
    assert await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view) is True
    assert loop.merges == [PR] and loop.posts == []
    assert review_coverage_hold.hold_for("bd-1") is None


async def test_on_it_holds_and_summons_once_per_head(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True})
    store = _Store()
    for _ in range(3):  # three 30-second ticks on the same head
        assert await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view) is False
    assert loop.merges == []
    assert len(loop.posts) == 1
    url, body, marker = loop.posts[0]
    assert url == PR and body.startswith("@vera review — ") and "\n" not in body
    assert HEAD in marker
    assert not any(lb.startswith("budget:auto-merge") for lb in store.labels)  # no attempt spent
    assert len([c for c in store.comments if c.startswith("auto-merge held:")]) == 1  # one bead note per head
    hold = review_coverage_hold.hold_for("bd-1")
    assert hold["head"] == HEAD and hold["summoned"] == HEAD

    (row,) = annotate_next_action([store.get_feature("bd-1")], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "awaiting complete review (panel pass was incomplete)"
    assert row["awaiting_merge"] is False

    # A complete PASS lands at the same head → the normal merge path.
    loop.view = _complete_view()
    assert await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view) is True
    assert loop.merges == [PR] and len(loop.posts) == 1
    assert review_coverage_hold.hold_for("bd-1") is None


async def test_a_new_head_gets_its_own_summon(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True})
    store = _Store()
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view)
    loop.view = _view(head=NEW_HEAD)
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view)
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view)
    assert [m for _u, _b, m in loop.posts] == [
        review_coverage_hold.summon_marker(HEAD),
        review_coverage_hold.summon_marker(NEW_HEAD),
    ]
    assert loop.merges == []


async def test_a_failed_post_is_retried_next_poll(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True}, post_ok=False)
    store = _Store()
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view)
    await loop._maybe_auto_merge(store, "bd-1", PR, "/repo", view=loop.view)
    assert len(loop.posts) == 2 and loop.merges == []
    assert review_coverage_hold.hold_for("bd-1")["summoned"] == ""


async def test_the_summon_handle_is_configurable_and_blank_posts_nothing(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True, "review_summon_handle": "@quinn"})
    await loop._maybe_auto_merge(_Store(), "bd-1", PR, "/repo", view=loop.view)
    assert loop.posts[0][1].startswith("@quinn review — ")

    review_coverage_hold.reset_state()
    loop = _edge(monkeypatch, {"require_complete_review": True, "review_summon_handle": ""})
    assert await loop._maybe_auto_merge(_Store(), "bd-1", PR, "/repo", view=loop.view) is False
    assert loop.posts == [] and loop.merges == []


async def test_an_unreadable_panel_holds_when_on(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True})
    loop.view = None
    assert await loop._maybe_auto_merge(_Store(), "bd-1", PR, "/repo", view=None) is False
    assert loop.merges == [] and loop.posts == []


async def test_inert_with_the_panel_check_off(monkeypatch):
    loop = _edge(monkeypatch, {"require_complete_review": True, "external_review": False})
    assert await loop._maybe_auto_merge(_Store(), "bd-1", PR, "/repo", view=loop.view) is True
    assert loop.posts == []


async def test_a_project_opts_in_for_its_own_repo_only(monkeypatch):
    cfg = {
        "projects": {
            "strict": {"repo": "/r/strict", "require_complete_review": True},
            "loose": {"repo": "/r/loose"},
        },
        "default_project": "loose",
    }
    loop = _edge(monkeypatch, cfg)

    class _P(_Store):
        def __init__(self, project):
            super().__init__()
            self.project = project

        def get_feature(self, fid):
            return dict(super().get_feature(fid), project=self.project)

    assert await loop._maybe_auto_merge(_P("strict"), "bd-1", PR, "/repo", view=loop.view) is False
    assert await loop._maybe_auto_merge(_P("loose"), "bd-1", PR, "/repo", view=loop.view) is True


async def test_turning_it_off_drops_a_standing_hold(monkeypatch):
    review_coverage_hold.set_hold("bd-1", HEAD, ["check run `QA panel` NEUTRAL"], PR)
    loop = _edge(monkeypatch)
    assert await loop._maybe_auto_merge(_Store(), "bd-1", PR, "/repo", view=loop.view) is True
    assert review_coverage_hold.hold_for("bd-1") is None


def test_the_setting_is_a_per_project_key():
    resolved = projects_mod.resolve_projects(
        {"projects": {"a": {"repo": "/a", "require_complete_review": True, "review_summon_handle": "vera"}}}
    )
    assert resolved["a"]["require_complete_review"] is True
    assert resolved["a"]["review_summon_handle"] == "vera"
    loop = BoardLoop({"projects": {"a": {"repo": "/a", "require_complete_review": "yes"}, "b": {"repo": "/b"}}})
    assert loop._require_complete_review_for({"project": "a"}) is True
    assert loop._require_complete_review_for({"project": "b"}) is False
    assert loop._review_summon_handle_for({"project": "b"}) == "vera"


def test_the_hold_only_replaces_auto_merge_pending():
    review_coverage_hold.set_hold("bd-1", HEAD, ["check run `QA panel` NEUTRAL"], PR)
    card = {"id": "bd-1", "board_state": "in_review", "labels": ["review-pending"], "pr_url": PR}
    (row,) = annotate_next_action([card], {"auto_merge": True, "review_gate": True})
    assert row["next_action"] == "review in progress"

"""Unresolved Major/Critical review threads from a configured bot bounce the card (#495).

Item 1 of #495 (merge_state_hold, PR #501) made a PR that is not CLEAN on GitHub read
``held: PR not clean on GitHub (UNSTABLE) — QA panel: in progress`` instead of sitting on a
bare ``auto-merge pending``. But nothing ACTED on it: a card whose PR had a WARN (UNSTABLE)
plus unresolved CodeRabbit Majors — findings posted as inline REVIEW THREADS, not as a panel
FAIL review with a marker — sat ``in_review`` forever (bd-fgtf / protoAgent#3745), because the
#473 external-review edge only fires on a FAIL review.

Here:

* the pure half (``external_review.blocking_threads`` / ``render_thread_findings`` / the
  ``thread_bots`` config) — who counts and what a bounce leads with;
* the reconcile edge on a fake store, one branch per test — a Major thread bounces once per
  head, a Minor-only set does not, an unreadable read changes nothing, a CLEAN PR is left for
  the merge edge, a thread by an unconfigured author is ignored, and the shared ext-review-fix
  budget blocks for a human when spent.

GitHub is faked at the ``worktree.pr_unresolved_threads`` / ``pr_merge_state`` seams (their
real-``gh`` coverage lives in tests/test_worktree_gh.py).
"""

from __future__ import annotations

import pytest

import project_board.loop as loop_mod
from project_board import external_review, worktree
from project_board.loop import BoardLoop

HEAD = "e5c66aa9ad75731286916a06d2bd6059494e0389"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
PR = "https://github.com/protoLabsAI/protoAgent/pull/3745"

# A CodeRabbit-style inline thread body that MARKS the finding Major (the real shape: an
# emphasised severity badge over a prose explanation), and a Minor/nitpick one that does not.
RB_MAJOR_BODY = (
    "_⚠️ Potential issue_ | _🟠 Major_\n\n"
    "**The `focusin` guard records `document.body` as `lastInteracted`** — it leaks across "
    "scopes. Bind to the event target instead."
)
RB_MINOR_BODY = "_🧹 Nitpick_ | _Minor_\n\nConsider renaming `x` to something descriptive."
RB_CRITICAL_BODY = "**🔴 Critical**: user input flows unescaped into the SQL query builder."
# A Minor-badged thread whose PROSE mentions the severity words — and a pasted snake_case
# identifier. The bare words are NOT a badge, so this must NOT bounce (#495 review): only the
# emphasised severity badge blocks, never a word in the explanation.
RB_PROSE_BODY = (
    "_🧹 Nitpick_ | _Minor_\n\nA major refactor would help here, near the critical section; "
    "also `is_major` reads oddly."
)


def _thread(
    body, *, author="coderabbitai[bot]", path="apps/web/useKeybindings.ts", line=55, commit=HEAD, outdated=False
):
    """A ``worktree.pr_unresolved_threads`` entry — the shape the edge consumes."""
    return {"author": author, "path": path, "line": line, "body": body, "commit": commit, "isOutdated": outdated}


# ── the pure half ─────────────────────────────────────────────────────────────────────


def test_thread_bots_config_default_and_override():
    cfg = external_review.parse_config(None)
    assert cfg.thread_bots == ("coderabbitai[bot]", "protoreview")
    assert external_review.parse_config({"thread_bots": ["mybot"]}).thread_bots == ("mybot",)
    # An empty list turns the #495 thread bounce off while leaving the panel edge on.
    assert external_review.parse_config({"thread_bots": []}).thread_bots == ()
    assert external_review.parse_config(True).thread_bots == ("coderabbitai[bot]", "protoreview")


def test_blocking_threads_keeps_only_configured_bots_marked_major_or_critical():
    bots = external_review.DEFAULT_THREAD_BOTS
    threads = [
        _thread(RB_MAJOR_BODY),  # configured bot, Major badge → kept
        _thread(RB_CRITICAL_BODY, path="db.py", line=9),  # configured bot, Critical badge → kept
        _thread(RB_MINOR_BODY, path="scope.py", line=12),  # configured bot, Minor → dropped
        _thread(RB_MAJOR_BODY, author="some-human", path="x.py"),  # not a bot → dropped
        # Configured bot, but the words are only in PROSE under a Minor badge — NOT a severity
        # badge, so it does NOT block (#495 review): prose must never spend the fix budget.
        _thread(RB_PROSE_BODY, author="protoreview", path="y.py"),
    ]
    kept = external_review.blocking_threads(threads, bots)
    assert [t["path"] for t in kept] == ["apps/web/useKeybindings.ts", "db.py"]
    # None (an unknown read) is never "no threads" — it yields [] for the caller to fail open on.
    assert external_review.blocking_threads(None, bots) == []
    assert external_review.thread_is_blocking(RB_MINOR_BODY) is False
    assert external_review.thread_is_blocking(RB_MAJOR_BODY) is True
    assert external_review.thread_is_blocking(RB_CRITICAL_BODY) is True
    # An emphasised badge blocks; the same words in prose — or a code identifier — do not.
    assert external_review.thread_is_blocking("**Blocker**: this cannot ship.") is True
    assert external_review.thread_is_blocking("a major refactor near the critical section") is False
    assert external_review.thread_is_blocking("this is a majority view, critically reviewed") is False
    assert external_review.thread_is_blocking("bump the major version in `max_major_count`") is False
    assert external_review.thread_is_blocking(RB_PROSE_BODY) is False


def test_render_thread_findings_leads_with_file_line_and_body():
    text = external_review.render_thread_findings([_thread(RB_MAJOR_BODY)], PR)
    assert PR in text
    assert "`apps/web/useKeybindings.ts:55`" in text
    assert "coderabbitai[bot]" in text
    assert "focusin` guard records" in text  # the body is quoted so the coder sees the ask


# ── the reconcile edge (fake store) ─────────────────────────────────────────────────────


class _Store:
    """One in_review card and the store surface the thread-bounce edge touches (mirrors the
    #473 fake so the two bounce edges are exercised against the same contract)."""

    def __init__(self, labels=()):
        self.state = "in_review"
        self.labels = list(labels)
        self.calls: list[tuple] = []
        self.comments: list[str] = []

    def _feature(self):
        return {"id": "bd-1", "board_state": self.state, "labels": list(self.labels), "pr_url": PR, "title": "t"}

    def get_feature(self, fid):
        return self._feature()

    def list_features(self, state=None):
        return [self._feature()] if state == self.state else []

    def record_review_bounce(self, fid, findings="", *, head=""):
        self.calls.append(("record_review_bounce", head))
        if head:
            self.labels = [l for l in self.labels if not l.startswith("ext-review-bounced:")]
            self.labels.append(f"ext-review-bounced:{head[:12]}")
        self.comments.append(f"review requested changes: {findings}")
        return self._feature()

    def requeue(self, fid):
        self.calls.append(("requeue",))
        self.state = "ready"
        return self._feature()

    def flag_blocked(self, fid, reason, category="", *, automatic=False):
        self.calls.append(("flag_blocked", reason, category))
        self.state = "blocked"
        return self._feature()

    def comment(self, fid, text):
        self.comments.append(text)

    def record_budget(self, fid, kind, n):
        self.labels = [l for l in self.labels if not l.startswith(f"budget:{kind}:")] + [f"budget:{kind}:{n}"]

    def clear_budgets(self, fid, kinds=None):
        self.labels = [l for l in self.labels if not any(l.startswith(f"budget:{k}:") for k in (kinds or [""]))]


def _loop(monkeypatch, store, *, threads, merge_state="UNSTABLE", head=HEAD, cfg=None):
    """A loop reconciling ``store``'s one card, with ``threads`` as the PR's unresolved review
    threads and ``merge_state`` as its GitHub mergeStateStatus. Records which later edges ran."""
    loop = BoardLoop({"merge_poll": True, "auto_merge": True, **(cfg or {})})
    monkeypatch.setattr(loop, "_store", lambda: store)
    loop_mod._PENDING_FEEDBACK.clear()
    ran: list[str] = []

    async def _state(pr_url, *, cwd="."):
        return "OPEN"

    async def _review_state(pr_url, *, cwd="."):
        # No panel FAIL — an empty review set with no failing check. The #473 edge judges this
        # NOT a failure and returns False, so the #495 thread edge gets its turn.
        return {"headRefOid": head, "state": "OPEN", "reviews": [], "statusCheckRollup": []}

    async def _threads(pr_url, *, cwd=".", first=0):
        return threads

    async def _merge_state(pr_url, *, cwd="."):
        return merge_state

    async def _head(pr_url, *, cwd="."):
        return head

    async def _diff(pr_url, *, cwd=".", max_chars=0):
        return "diff --git a/x b/x"

    def _edge(name, result=False):
        async def _run(*a, **k):
            ran.append(name)
            return result

        return _run

    async def _ci_status(*args, **kwargs):
        return "passing", ""

    monkeypatch.setattr(worktree, "pr_ci_status", _ci_status)
    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "pr_review_state", _review_state)
    monkeypatch.setattr(worktree, "pr_unresolved_threads", _threads)
    monkeypatch.setattr(worktree, "pr_merge_state", _merge_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr(loop, "_maybe_rebase", _edge("rebase"))
    monkeypatch.setattr(loop, "_verify_merged_state", _edge("merged-verify"))
    monkeypatch.setattr(loop, "_reconcile_ci", _edge("ci"))
    monkeypatch.setattr(loop, "_maybe_auto_merge", _edge("auto-merge"))
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, incident="", **_kw: ran.append("notify"))
    loop.ran = ran
    return loop


async def test_a_major_thread_bounces_into_a_fix_round_once_per_head(monkeypatch):
    """r1: an in_review PR, not CLEAN, with an unresolved CodeRabbit Major thread is bounced to a
    fix round — the thread's file:line and body lead the next prompt — and only once per head."""
    store = _Store()
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY), _thread(RB_MINOR_BODY, path="scope.py")])
    await loop._reconcile_prs()
    assert ("record_review_bounce", HEAD) in store.calls and store.calls[-1] == ("requeue",)
    assert store.state == "ready"
    assert f"ext-review-bounced:{HEAD[:12]}" in store.labels
    assert "budget:ext-review-fix:1" in store.labels  # the SHARED #473 budget counter
    # The next prompt LEADS with the thread's file:line and body, the failed diff beside it.
    queued = loop_mod._PENDING_FEEDBACK["bd-1"]
    assert "apps/web/useKeybindings.ts:55" in queued and "focusin` guard records" in queued
    assert "scope.py" not in queued  # the Minor thread is not required
    assert loop._ci_prior_diff["bd-1"].startswith("diff --git")
    # Existing repair edges run before thread handling.
    assert loop.ran == ["rebase", "merged-verify", "ci"]

    # The fix round comes back to review at the SAME head (no new push). The ext-review-bounced
    # stamp holds it — told once, never re-bounced — and told ONCE however many times the card is
    # polled at that head. The panel edge runs first on every pass and clears its OWN hold memory;
    # that reset must not wipe this edge's, or the hold comment would re-post every poll (#495
    # review). Poll it several times to prove the dedup survives.
    for _ in range(3):
        store.state = "in_review"
        await loop._reconcile_prs()
    assert [c for c in store.calls if c[0] == "record_review_bounce"] == [("record_review_bounce", HEAD)]
    holds = [c for c in store.comments if c.startswith("auto-merge held:")]
    assert len(holds) == 1 and "needs a human" in holds[0]  # told once, not once per poll
    # A same-head hold still allows rebase and CI handling on every poll.
    assert loop.ran.count("notify") == 1
    assert loop.ran.count("rebase") == loop.ran.count("ci") == 4


async def test_minor_only_threads_do_not_bounce(monkeypatch):
    """r2: a PR whose only unresolved bot threads are Minor is left alone — the pass proceeds to
    the merge edge, nothing is bounced."""
    store = _Store()
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MINOR_BODY), _thread(RB_MINOR_BODY, path="b.py", line=2)])
    await loop._reconcile_prs()
    assert not store.calls and store.state == "in_review"
    assert "auto-merge" in loop.ran  # the pass ran to the end instead of short-circuiting


async def test_an_unreadable_thread_read_changes_nothing(monkeypatch):
    """r3: an UNKNOWN thread read (None — a gh timeout) is never "no threads". The pass proceeds
    exactly as before; nothing is bounced on an unreadable signal."""
    store = _Store()
    loop = _loop(monkeypatch, store, threads=None)
    await loop._reconcile_prs()
    assert not store.calls and store.state == "in_review"
    assert "auto-merge" in loop.ran


async def test_a_clean_pr_is_left_for_the_merge_edge(monkeypatch):
    """Even with an open Major thread, a PR GitHub reads as CLEAN is about to merge and an
    advisory thread does not stop it — the merge edge owns it, this edge stays out of the way."""
    store = _Store()
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY)], merge_state="CLEAN")
    await loop._reconcile_prs()
    assert not store.calls and store.state == "in_review"
    assert "auto-merge" in loop.ran


async def test_a_thread_by_an_unconfigured_author_does_not_bounce(monkeypatch):
    """A Major thread by someone who is not a configured ``thread_bots`` login (a human reviewer,
    another bot) is not the board's to bounce on — it is left for a person."""
    store = _Store()
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY, author="a-human-reviewer")])
    await loop._reconcile_prs()
    assert not store.calls and store.state == "in_review"
    assert "auto-merge" in loop.ran


async def test_a_new_head_rearms_the_thread_bounce(monkeypatch):
    """The stamp is per head. A fix round that pushes a new head gets a fresh bounce on the
    fresh threads posted on that head."""
    store = _Store(labels=[f"ext-review-bounced:{HEAD[:12]}"])  # already bounced for the old head
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY, commit=NEW_HEAD)], head=NEW_HEAD)
    await loop._reconcile_prs()
    assert ("record_review_bounce", NEW_HEAD) in store.calls and store.state == "ready"
    assert f"ext-review-bounced:{NEW_HEAD[:12]}" in store.labels


async def test_a_spent_shared_budget_blocks_for_a_human(monkeypatch):
    """r4: the thread bounce SHARES the ext-review-fix budget with the #473 panel edge, and when
    it runs out the card is blocked (terminal) for a human — the findings on the bead — exactly as
    the panel exhaustion does, rather than looping forever."""
    store = _Store(labels=["budget:ext-review-fix:2"])  # the shared counter is already at the cap
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY)], cfg={"review_fix_max": 2})
    await loop._reconcile_prs()
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "unresolved review threads" in blocked[0][1] and blocked[0][2] == "terminal"
    assert ("requeue",) not in store.calls and store.state == "blocked"
    assert any("useKeybindings.ts:55" in c for c in store.comments)  # the findings are on the bead
    assert "budget:ext-review-fix:2" not in store.labels  # reset, as the gate's exhaustion does
    assert f"ext-review-bounced:{HEAD[:12]}" in store.labels


async def test_the_bounce_shares_the_counter_the_panel_already_spent(monkeypatch):
    """Sharing, from the other side: a counter the panel edge already advanced to 1 is picked up
    and advanced to 2 by a thread bounce (not restarted at 1) — one budget, both edges."""
    store = _Store(labels=["budget:ext-review-fix:1"])
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY)], cfg={"review_fix_max": 3})
    await loop._reconcile_prs()
    assert "budget:ext-review-fix:2" in store.labels and store.state == "ready"


@pytest.mark.parametrize("merge_state", ["BLOCKED", "UNSTABLE"])
@pytest.mark.parametrize("commit,outdated", [(HEAD, False), (NEW_HEAD, True), ("", False), (NEW_HEAD, None)])
async def test_stale_or_unproven_threads_cannot_spend_new_head_budget(monkeypatch, merge_state, commit, outdated):
    store = _Store(labels=[f"ext-review-bounced:{HEAD[:12]}", "budget:ext-review-fix:1"])
    loop = _loop(
        monkeypatch,
        store,
        threads=[_thread(RB_MAJOR_BODY, commit=commit, outdated=outdated)],
        head=NEW_HEAD,
        merge_state=merge_state,
    )
    for _ in range(3):
        await loop._reconcile_prs()
    assert not store.calls
    assert "budget:ext-review-fix:1" in store.labels
    assert loop.ran.count("ci") == 3


@pytest.mark.parametrize("merge_state,outcome", [("BEHIND", "clean"), ("DIRTY", "conflict")])
async def test_real_rebase_retains_priority_over_major_threads(monkeypatch, merge_state, outcome):
    store = _Store(labels=[f"ext-review-bounced:{HEAD[:12]}"])
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY)], merge_state=merge_state)
    monkeypatch.setattr(loop, "_maybe_rebase", BoardLoop._maybe_rebase.__get__(loop))
    calls = []

    async def rebase(*args, **kwargs):
        calls.append(outcome)
        return outcome, "conflict in x.py"

    monkeypatch.setattr(worktree, "rebase_onto_base", rebase)
    await loop._reconcile_prs()
    assert calls == [outcome]
    assert not any(c[0] == "record_review_bounce" for c in store.calls)
    if outcome == "clean":
        assert store.state == "in_review"
        assert not store.calls
    else:
        assert store.state == "ready"
        assert "CONFLICTS" in loop._ci_feedback["bd-1"]


@pytest.mark.parametrize("rerun", [False, True])
async def test_real_ci_failure_keeps_feedback_and_owns_fix_round(monkeypatch, rerun):
    store = _Store()
    loop = _loop(monkeypatch, store, threads=[_thread(RB_MAJOR_BODY)])
    monkeypatch.setattr(loop, "_reconcile_ci", BoardLoop._reconcile_ci.__get__(loop))

    async def status(*args, **kwargs):
        return "failing", "required tests failed"

    async def no_rerun(*args, **kwargs):
        return rerun

    monkeypatch.setattr(worktree, "pr_ci_status", status)
    monkeypatch.setattr(loop, "_rerun_ci_once", no_rerun)
    await loop._reconcile_prs()
    if rerun:
        assert store.state == "in_review"
        assert not store.calls
        assert not any(label.startswith("budget:ext-review-fix:") for label in store.labels)
        return
    assert store.state == "ready"
    assert loop._ci_feedback["bd-1"] == "required tests failed"
    assert not any(c[0] == "record_review_bounce" for c in store.calls)
    assert "budget:ci-fix:1" in store.labels


async def test_thread_read_preserves_original_commit_and_outdated_flag(monkeypatch):
    import json
    from conftest import REAL_SEAMS

    async def gh(*args, **kwargs):
        assert "originalCommit{oid}" in " ".join(args)
        assert "isOutdated" in " ".join(args)
        node = {
            "isResolved": False,
            "isOutdated": True,
            "path": "x.py",
            "line": 4,
            "comments": {
                "nodes": [{"author": {"login": "coderabbitai"}, "body": RB_MAJOR_BODY, "originalCommit": {"oid": HEAD}}]
            },
        }
        return 0, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [node]}}}}}), ""

    monkeypatch.setattr(worktree, "_gh", gh)
    result = await REAL_SEAMS["worktree.pr_unresolved_threads"](PR)
    assert result[0]["commit"] == HEAD
    assert result[0]["isOutdated"] is True

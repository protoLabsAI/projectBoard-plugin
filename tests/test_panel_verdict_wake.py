"""A terminal QA-panel verdict at a card PR's head wakes the agent to act on it (#5218).

Live (mythxEngine, projectBoard v0.80.1, 2026): three card PRs reached a QA panel PASS with
green checks and a clean merge state between 06:45 and 07:05Z, and nothing woke the agent —
no tool calls, the cards sat ``in_review``, and a dependent card stayed blocked until an
operator sent an inbox nudge by hand. On a project where the AGENT does the merge/adjudication
(``auto_merge`` off), a terminal panel verdict (PASS / WARN / FAIL) at the PR's current head
now queues ONE agent-owned inbox page naming the card, PR and verdict, deduped per card+head.
The board never pages when it merges the PR itself (``auto_merge`` on).

The panel's verdict is faked at the ``worktree.pr_review_state`` seam (its real-``gh`` coverage
lives in tests/test_worktree_gh.py); the host inbox is faked exactly as tests/
test_block_pages_735.py fakes it (the suite is host-free by design).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import project_board.loop as loop_mod
from project_board import block_pages, external_review, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop

FID = "bd-1"
TITLE = "Wire the QA-panel wake"
HEAD = "e5c66aa9ad75731286916a06d2bd6059494e0389"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
PR = "https://github.com/protoLabsAI/mythxengine/pull/42"
SHORT = store_mod.SHORT_SHA_LEN


# ── the pure read: which verdicts are TERMINAL ───────────────────────────────────────────


def _review(verdict, *, head=HEAD, login="protoreview", state="APPROVED", at="2026-11-12T10:00:00Z", complete=None):
    # ``complete=False`` stamps the marker's ``complete=false`` — an incomplete pass (a finder did
    # not run), which `evaluate` reads as `incomplete` and is NOT a terminal verdict.
    extra = "" if complete is None else f" complete={'false' if complete is False else 'true'}"
    body = f"<!-- protoagent-qa-review head={head} verdict={verdict}{extra} -->\n## QA panel — {verdict}"
    return {"author": {"login": login}, "body": body, "state": state, "submittedAt": at, "commit": {"oid": head}}


def _view(verdict="PASS", *, head=HEAD, reviews=None, checks=None):
    """A `gh pr view` payload: one marked panel verdict at ``head`` unless ``reviews`` overrides."""
    return {
        "headRefOid": head,
        "state": "OPEN",
        "reviews": reviews if reviews is not None else ([_review(verdict, head=head)] if verdict else []),
        "statusCheckRollup": checks or [],
    }


@pytest.mark.parametrize("verdict", ["PASS", "WARN", "FAIL", "BLOCK", "REJECT"])
def test_a_clear_verdict_is_terminal(verdict):
    v = external_review.evaluate(_view(verdict), external_review.Config())
    assert v.terminal_verdict == verdict


def test_no_marked_verdict_is_not_terminal():
    v = external_review.evaluate(_view("", reviews=[]), external_review.Config())
    assert v.terminal_verdict == "" and not v.failed


def test_an_incomplete_pass_is_not_a_terminal_verdict():
    """A NEUTRAL `QA panel` check is the panel's incomplete-coverage hold, not a conclusion."""
    neutral = [{"__typename": "CheckRun", "name": "QA panel", "conclusion": "NEUTRAL", "workflowName": ""}]
    v = external_review.evaluate(_view("", reviews=[], checks=neutral), external_review.Config())
    assert v.incomplete and v.terminal_verdict == ""


def test_a_complete_false_pass_is_not_a_terminal_verdict():
    """A marked PASS carrying ``complete=false`` is an incomplete pass — the panel cleared less
    than the whole diff — so `terminal_verdict` is "", honouring 'incomplete is not terminal'. The
    agent is never woken to merge a head the panel left half-reviewed (round-2 review)."""
    v = external_review.evaluate(_view("", reviews=[_review("PASS", complete=False)]), external_review.Config())
    assert v.review_verdict == "PASS" and v.incomplete and v.terminal_verdict == ""


def test_a_complete_pass_stays_a_terminal_verdict():
    """The guard does not swallow a genuine complete pass: ``complete=true`` is terminal."""
    v = external_review.evaluate(_view("", reviews=[_review("PASS", complete=True)]), external_review.Config())
    assert not v.incomplete and v.terminal_verdict == "PASS"


def test_a_fail_known_only_from_a_red_check_is_not_a_named_verdict():
    red = [{"__typename": "CheckRun", "name": "QA panel", "conclusion": "FAILURE", "workflowName": ""}]
    v = external_review.evaluate(_view("", reviews=[], checks=red), external_review.Config())
    assert v.failed and v.terminal_verdict == ""  # failed, but no marked verdict to name


# ── the inbox page ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    rows = []

    class Inbox:
        def __init__(self, path, **kwargs):
            pass

        def add(self, text, *, priority="next", source="", dedup_key="", now=None):
            if any(r["dedup_key"] == dedup_key and r["delivered_at"] is None for r in rows):
                return None
            row = dict(
                id=len(rows) + 1, text=text, source=source, priority=priority, dedup_key=dedup_key, delivered_at=None
            )
            rows.append(row)
            return row.copy()

        def list(self, *, priority_floor="next", include_recovery_claimed=False, limit=2**31 - 1):
            return [r.copy() for r in rows if r["delivered_at"] is None][:limit]

        def mark_delivered(self, ids):
            for row in rows:
                if row["id"] in ids:
                    row["delivered_at"] = "now"

    monkeypatch.setitem(sys.modules, "inbox", SimpleNamespace(InboxStore=Inbox))
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")
    return rows


class _Store:
    """One in_review card and the store surface the PR reconcile touches."""

    def __init__(self, *, state="in_review", labels=()):
        self.state = state
        self.labels = list(labels)
        self.comments: list[str] = []

    def _feature(self):
        return {"id": FID, "board_state": self.state, "labels": list(self.labels), "pr_url": PR, "title": TITLE}

    def get_feature(self, fid):
        return self._feature()

    def list_features(self, state=None):
        return [self._feature()] if state == self.state else []

    def comment(self, fid, text):
        self.comments.append(text)


def _loop(monkeypatch, store, *, auto_merge=False, cfg=None):
    loop = BoardLoop({"auto_merge": auto_merge, **(cfg or {})})
    monkeypatch.setattr(loop, "_store", lambda: store)
    return loop


@pytest.mark.parametrize("verdict", ["PASS", "WARN", "FAIL"])
async def test_a_terminal_verdict_pages_the_agent_naming_card_pr_and_verdict(inbox, monkeypatch, verdict):
    store = _Store()
    loop = _loop(monkeypatch, store)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view(verdict)) is True
    assert len(inbox) == 1
    row = inbox[0]
    assert row["priority"] == "now"  # fires the agent's Activity turn
    assert FID in row["text"] and PR in row["text"] and verdict in row["text"]


@pytest.mark.parametrize("verdict", ["PASS", "WARN"])
async def test_a_clean_head_offers_the_agent_the_merge(inbox, monkeypatch, verdict):
    """A non-blocking verdict — the merge/adjudication is the agent's to make — offers the merge."""
    store = _Store()
    loop = _loop(monkeypatch, store)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view(verdict)) is True
    assert "merge" in inbox[0]["text"].lower()


@pytest.mark.parametrize("verdict", ["FAIL", "BLOCK", "REJECT"])
async def test_a_rejected_head_is_never_offered_the_merge(inbox, monkeypatch, verdict):
    """On a FAIL/BLOCK/REJECT the panel rejected the head and the #473 edge bounces it into a fix
    round — the page names the verdict and says fix/request-changes/close, never offering to MERGE
    a PR the panel rejected (round-2 review)."""
    store = _Store()
    loop = _loop(monkeypatch, store)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view(verdict)) is True
    text = inbox[0]["text"]
    assert verdict in text and "merge" not in text.lower()


async def test_the_page_is_agent_owned_not_an_operator_block_page(inbox, monkeypatch):
    """Agent-owned: a `panel-verdict:` dedup key, never the operator `blocked:` prefix, so the
    block-page machinery never treats it as an operator alert and it stays out of Needs-you."""
    store = _Store()
    loop = _loop(monkeypatch, store)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view("PASS")) is True
    row = inbox[0]
    assert row["source"] == "project_board"
    assert row["dedup_key"] == f"panel-verdict:{FID}:{HEAD[:SHORT]}"
    assert not row["dedup_key"].startswith("blocked:")
    assert block_pages.card_id(row) is None  # the operator block-page lifecycle ignores it


async def test_deduped_per_card_and_head_but_a_new_head_re_arms_it(inbox, monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store)
    f = store._feature()
    assert await loop._page_agent_panel_verdict(store, f, PR, "/repo", view=_view("PASS")) is True
    # Same card + head: no second page, whatever the poll cadence.
    assert await loop._page_agent_panel_verdict(store, f, PR, "/repo", view=_view("PASS")) is False
    assert await loop._page_agent_panel_verdict(store, f, PR, "/repo", view=_view("WARN")) is False
    assert len(inbox) == 1
    # A push is a new head — a fresh verdict there pages again.
    assert await loop._page_agent_panel_verdict(store, f, PR, "/repo", view=_view("PASS", head=NEW_HEAD)) is True
    assert len(inbox) == 2
    assert inbox[1]["dedup_key"] == f"panel-verdict:{FID}:{NEW_HEAD[:SHORT]}"


async def test_a_non_terminal_or_absent_verdict_never_pages(inbox, monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store)
    # No marked verdict at the head.
    assert (
        await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view("", reviews=[])) is False
    )
    # An incomplete pass (NEUTRAL check, no terminal review) is not a conclusion.
    neutral = [{"__typename": "CheckRun", "name": "QA panel", "conclusion": "NEUTRAL", "workflowName": ""}]
    nv = _view("", reviews=[], checks=neutral)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=nv) is False
    # A marked PASS with complete=false is an incomplete pass — not a conclusion either.
    cf = _view("", reviews=[_review("PASS", complete=False)])
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=cf) is False
    assert inbox == []


async def test_off_by_config_never_pages(inbox, monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store, cfg={"external_review": False})
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=_view("PASS")) is False
    assert inbox == []


async def test_an_unreadable_panel_fails_open(inbox, monkeypatch):
    """conftest stubs `pr_review_state` to None; a direct call with no handed view re-reads it,
    reads nothing, and pages nothing — the pass runs as before."""
    store = _Store()
    loop = _loop(monkeypatch, store)
    assert await loop._page_agent_panel_verdict(store, store._feature(), PR, "/repo", view=None) is False
    assert inbox == []


# ── wired into the PR reconcile pass, and gated on who merges ─────────────────────────────


def _driven_loop(monkeypatch, store, view, *, auto_merge):
    """A loop whose PR reconcile reads ``view`` for the card's PR. The #473 FAIL edge is stubbed
    to short-circuit the rest of the OPEN pass, so the test isolates the wake edge that runs
    before it; it changes no edge below."""
    loop = _loop(monkeypatch, store, auto_merge=auto_merge, cfg={"merge_poll": True})

    async def _state(pr_url, *, cwd="."):
        return "OPEN"

    async def _review_state(pr_url, *, cwd="."):
        return view

    async def _short_circuit(*a, **k):
        return True

    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "pr_review_state", _review_state)
    monkeypatch.setattr(loop, "_reconcile_external_review", _short_circuit)
    return loop


async def test_the_reconcile_pass_pages_when_the_agent_merges(inbox, monkeypatch):
    store = _Store()
    loop = _driven_loop(monkeypatch, store, _view("PASS"), auto_merge=False)
    await loop._reconcile_prs()
    assert len(inbox) == 1 and "PASS" in inbox[0]["text"] and inbox[0]["priority"] == "now"


async def test_no_page_when_the_board_merges_the_pr_itself(inbox, monkeypatch):
    store = _Store()
    loop = _driven_loop(monkeypatch, store, _view("PASS"), auto_merge=True)
    await loop._reconcile_prs()
    assert inbox == []  # auto_merge on: the board merges, so the agent is never paged

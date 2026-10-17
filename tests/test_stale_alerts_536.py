"""Done/cancelled cards stop alerting, and close edges shed their stale labels (#536).

Two bugs, one card's lifetime apart:

1. ``_notify_operator`` deduped a block alert on an incident key but never asked whether
   the card was still OPEN. A sweep that decided to alert and only then found the card had
   shipped paged the operator anyway — bd-g1d3 after PR #4184 merged, bd-b93u after #4177,
   nerdsville bd-5go after #41. The alert now re-reads the card right before the inbox row
   and DROPS it for a done/cancelled card, failing OPEN so a read error never hides a real
   block.

2. The close edges removed only ``blocked`` and left ``blocked-class:*`` / ``in-review`` /
   ``review-pending`` / ``changes-requested`` behind, so a shipped card still read as a live
   block or an open review (protoAgent bd-b4pr was ``done`` but ``blocked: true``). They now
   shed every live-state label and keep the ``cancelled`` tag and audit labels.

``board_list(state="blocked")`` is deliberately untouched: board_state maps the ``blocked``
FLAG, so a live ``blocked`` card is still listed and a ``blocked-class:terminal`` label with
no flag stays a stale class, not a live block (#401).
"""

from __future__ import annotations

import shutil
import sys
import types

import pytest

from project_board import store as store_mod
import project_board.loop as loop_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — CI installs it and sets PB_REQUIRE_BR=1",
)

_AC = "- WHEN x THE SYSTEM SHALL y"
_LOG = "protoagent.plugins.project_board"


# ── 1: the alert re-checks state right before the inbox row (#536) ───────────────────────


def _install_inbox(monkeypatch, tmp_path):
    """Install the host ``inbox.InboxStore`` seam over a temp db (mirrors test_loop's
    ``_use_fake_inbox``) and return the list every ``add()`` appends to."""
    added: list[dict] = []
    fake = types.ModuleType("inbox")

    class _Inbox:
        def __init__(self, db_path, *, dedup_window_s=300):
            pass

        def add(self, text, *, priority="next", source="", dedup_key=""):
            added.append({"text": text, "key": dedup_key, "priority": priority})
            return {"id": len(added), "text": text}

    fake.InboxStore = _Inbox
    monkeypatch.setitem(sys.modules, "inbox", fake)
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")
    return added


class _Card:
    """A store that answers a single ``get_feature`` with a fixed state, counting reads."""

    def __init__(self, state):
        self.state = state
        self.reads = 0

    def get_feature(self, fid):
        self.reads += 1
        return {"id": fid, "board_state": self.state}


class _Boom:
    """A store whose re-read raises — the fail-open path."""

    def get_feature(self, fid):
        raise RuntimeError("br exploded")


@pytest.mark.parametrize("state", ["done", "cancelled"])
def test_a_terminal_card_drops_its_stale_block_alert(monkeypatch, tmp_path, caplog, state):
    """r1: a card that shipped (done) or was cut (cancelled) by the time its alert would
    fire writes NO inbox row, and says why at debug — not a warning the operator reads as a
    live block."""
    added = _install_inbox(monkeypatch, tmp_path)
    card = _Card(state)
    with caplog.at_level("DEBUG", logger=_LOG):
        BoardLoop({"coder": "proto"})._notify_operator("bd-g1d3", "Board card bd-g1d3 is blocked", store=card)
    assert added == [], f"a {state} card must not reach the operator's inbox"
    assert card.reads == 1, "the card must be re-read exactly once before the inbox row"
    assert any("dropping stale block alert" in r.getMessage() and r.levelname == "DEBUG" for r in caplog.records)


def test_a_still_blocked_card_still_alerts(monkeypatch, tmp_path):
    """The guard suppresses ONLY terminal cards: a card that is genuinely still blocked is
    alerted exactly as before."""
    added = _install_inbox(monkeypatch, tmp_path)
    BoardLoop({"coder": "proto"})._notify_operator("bd-x", "Board card bd-x is blocked", store=_Card("blocked"))
    assert len(added) == 1 and added[0]["priority"] == "now"


def test_a_failed_state_reread_still_delivers_the_alert(monkeypatch, tmp_path):
    """r2: the alert matters more than the re-read. A store read that raises must never
    swallow a real block — the operator is still told."""
    added = _install_inbox(monkeypatch, tmp_path)
    BoardLoop({"coder": "proto"})._notify_operator("bd-a", "Board card bd-a is blocked", store=_Boom())
    assert len(added) == 1 and "bd-a" in added[0]["text"]


def test_the_reread_falls_back_to_the_loops_own_store(monkeypatch, tmp_path):
    """With no store passed the guard re-reads through ``self._store()`` — so a done card
    is dropped on the fallback path too."""
    added = _install_inbox(monkeypatch, tmp_path)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_store", lambda: _Card("done"))
    loop._notify_operator("bd-x", "Board card bd-x is blocked")
    assert added == []


# ── 2: the close edges shed every stale live-state label (#536) ──────────────────────────
#
# Pure tests: `make_board` replaces `_run`, so these pin exactly which labels each edge
# tells `br` to remove — no CLI, no DB — and the real-`br` tier below proves the projection.


class Br:
    """A fake ``_run`` recording every call, canning values per leading subcommand (copied
    from tests/test_store.py's fixture so this file stands alone)."""

    def __init__(self, returns=None):
        self.calls = []
        self.returns = returns or {}

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        val = self.returns.get(args[0] if args else "", [] if want_json else "")
        val = val(args) if callable(val) else val
        return (val, None) if with_has_more else val


def _removed(call):
    return {call[i + 1] for i, a in enumerate(call) if a == "--remove-label"}


def _added(call):
    return {call[i + 1] for i, a in enumerate(call) if a == "--add-label"}


def _raise_on_close(args):
    raise BoardError("`br close bd-9 -r ...` failed: unparseable reason")


def test_record_merge_removes_every_stale_state_label(make_board, monkeypatch):
    """r3: record_merge strips ``blocked``, the ``blocked-class:*`` class, ``in-review`` and
    ``review-pending`` before closing, and keeps audit labels (``merged-verified:``)."""
    url = "https://example/pr/4184"
    labels = ["blocked", "blocked-class:terminal", "in-review", "review-pending", "merged-verified:abc123"]
    rows = [{"id": "bd-g1d3", "external_ref": url, "status": "in_progress", "labels": labels}]
    br = Br({"list": rows})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "board_state": "done", "blocked": False})
    b.record_merge(pr_url=url)
    update = next(c for c in br.calls if c[0] == "update")
    assert _removed(update) == {"blocked", "blocked-class:terminal", "in-review", "review-pending"}
    assert "merged-verified:abc123" not in _removed(update)  # audit label kept
    assert any(c[0] == "close" and c[1] == "bd-g1d3" for c in br.calls)


def _in_review_then_done(labels):
    seq = {"n": 0}

    def _gf(fid):
        seq["n"] += 1
        if seq["n"] == 1:
            return {"id": fid, "board_state": "in_review", "labels": labels}
        return {"id": fid, "board_state": "done", "blocked": False, "labels": ["verified:me"]}

    return _gf


def test_mark_done_removes_every_stale_state_label(make_board, monkeypatch):
    """r4: the manual Done edge sheds the same labels record_merge does, keeping audit
    labels (``verified:``)."""
    labels = ["blocked", "blocked-class:transient", "in-review", "review-pending", "changes-requested", "verified:me"]
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", _in_review_then_done(labels))
    b.mark_done("bd-1", reason="shipped off-board")
    update = next(c for c in br.calls if c[0] == "update")
    assert _removed(update) == {
        "blocked",
        "blocked-class:transient",
        "in-review",
        "review-pending",
        "changes-requested",
    }
    assert "verified:me" not in _removed(update)  # audit label kept
    assert any(c[0] == "close" for c in br.calls)


def test_cancel_feature_sheds_every_stale_state_label(make_board, monkeypatch):
    """The cancel edge (its own atomic tag write) folds the same cleanup in: one `br
    update` adds ``cancelled`` and removes every live-state label."""
    labels = ["blocked", "blocked-class:terminal", "in-review", "review-pending"]
    br = Br()
    b = make_board(br)
    monkeypatch.setattr(
        b,
        "get_feature",
        lambda fid: {"id": fid, "assignee": "", "board_state": "cancelled", "cancelled": True, "labels": labels},
    )
    b.cancel_feature("bd-9", "duplicate")
    tag = next(c for c in br.calls if c[0] == "update" and "cancelled" in _added(c))
    assert _removed(tag) == {"blocked", "blocked-class:terminal", "in-review", "review-pending"}


def test_cancel_feature_rollback_re_adds_every_stale_label(make_board, monkeypatch):
    """Atomic-or-clean (#106) generalised: if `br close` fails after the cleanup landed, the
    undo re-adds EVERY stripped live-state label, so the card rolls back to its exact
    pre-cancel labels — never a half-cancelled, silently-unblocked/unreviewed zombie."""
    labels = ["blocked", "blocked-class:terminal", "in-review", "review-pending"]
    br = Br(returns={"close": _raise_on_close})
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "assignee": "", "labels": labels})
    with pytest.raises(BoardError, match="failed"):
        b.cancel_feature("bd-9", "scope cut")
    undo = next(c for c in br.calls if c[0] == "update" and "cancelled" in _removed(c))
    assert _added(undo) == {"blocked", "blocked-class:terminal", "in-review", "review-pending"}


# ── 3: board_list(state="blocked") is left alone — the FLAG is the live block (#401) ─────


def test_a_stale_terminal_class_without_the_blocked_flag_is_not_a_live_block(make_board):
    """r5 (projection half): board_state maps the ``blocked`` FLAG, not the class — so a
    ``blocked-class:terminal`` label with no flag is a stale class (#401), not a live block,
    and the live case (flag present, class alongside) is still ``blocked``."""
    b = make_board(Br())
    stale = b._project({"id": "bd-z", "status": "open", "labels": ["blocked-class:terminal"]})
    assert stale["board_state"] != "blocked" and stale["blocked"] is False
    live = b._project({"id": "bd-y", "status": "open", "labels": ["blocked", "blocked-class:terminal"]})
    assert live["board_state"] == "blocked" and live["blocked"] is True


# ── 4: the real-`br` tier — projection + list_features end to end ────────────────────────


def _ready(board: BeadsBoard, repo, title: str, path: str, url: str = "") -> str:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    board.mark_ready(fid)
    board.claim(fid)
    if url:
        board.open_review(fid, pr_url=url)
    return fid


@requires_br
def test_record_merge_leaves_a_done_card_with_no_live_state_label(tmp_path):
    """r3 (end to end): a card that accumulated blocked + class + review labels by the time
    its PR merged (bd-b4pr) comes out of record_merge ``done`` and ``blocked: false`` with
    none of them left."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    url = "https://github.com/o/r/pull/4184"
    fid = _ready(board, tmp_path, "ship it", "a.py", url=url)
    board._run(
        "update",
        fid,
        "--add-label",
        "blocked",
        "--add-label",
        "blocked-class:terminal",
        "--add-label",
        "review-pending",
    )
    done = board.record_merge(pr_url=url)
    labels = done["labels"]
    assert done["board_state"] == "done" and done["blocked"] is False
    assert "blocked" not in labels and not any(str(l).startswith("blocked-class:") for l in labels)
    assert "in-review" not in labels and "review-pending" not in labels


@requires_br
def test_mark_done_leaves_a_done_card_with_no_live_state_label(tmp_path):
    """r4 (end to end): manual mark_done clears the same labels."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "hand ship", "b.py", url="https://github.com/o/r/pull/9")
    board._run(
        "update",
        fid,
        "--add-label",
        "blocked",
        "--add-label",
        "blocked-class:transient",
        "--add-label",
        "changes-requested",
    )
    done = board.mark_done(fid, reason="shipped in the monorepo")
    labels = done["labels"]
    assert done["board_state"] == "done" and done["blocked"] is False
    assert "blocked" not in labels and not any(str(l).startswith("blocked-class:") for l in labels)
    assert "in-review" not in labels and "changes-requested" not in labels


@requires_br
def test_cancel_leaves_the_cancelled_tag_but_no_live_state_label(tmp_path):
    """The cancel edge keeps its ``cancelled`` audit tag and drops every live-state label."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "cut it", "c.py", url="https://github.com/o/r/pull/3")
    board._run(
        "update",
        fid,
        "--add-label",
        "blocked",
        "--add-label",
        "blocked-class:terminal",
        "--add-label",
        "review-pending",
    )
    cancelled = board.cancel_feature(fid, "duplicate")
    labels = cancelled["labels"]
    assert cancelled["board_state"] == "cancelled" and cancelled["blocked"] is False
    assert "cancelled" in labels  # the audit tag is preserved
    assert "blocked" not in labels and not any(str(l).startswith("blocked-class:") for l in labels)
    assert "in-review" not in labels and "review-pending" not in labels


@requires_br
def test_a_live_blocked_terminal_card_is_still_listed_as_blocked(tmp_path):
    """r5: board_list(state="blocked") is untouched — a card carrying BOTH ``blocked`` and
    ``blocked-class:terminal`` is a live block and is listed, exactly as before."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    (tmp_path / "d.py").write_text("x = 1\n")
    live = board.create_feature("live block", spec="s", acceptance_criteria=_AC, files_to_modify=["d.py"])["id"]
    board.flag_blocked(live, "zombie drive", category="terminal")

    listed = {f["id"] for f in board.list_features(state="blocked")}
    assert live in listed

    f = board.get_feature(live)
    assert "blocked" in f["labels"] and "blocked-class:terminal" in f["labels"] and f["blocked"] is True

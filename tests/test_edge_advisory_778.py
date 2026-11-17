"""Edge advisory (#778): a depends_on edge between two OPEN cards that share no file in
``files_to_modify`` is almost never a real build-on. ``_ready_findings`` flags it as an
ADVISORY (gate ``dependency-no-shared-file``) — never a refusal, never changing ``ok`` —
so the author can drop edges the files don't force and keep independent cards running in
parallel. It is the mirror of the existing shared-file gate, which reports the opposite
case (a shared file with NO edge).

A 12-card batch boarded as one chain had four cards waiting on work they shared no file
with, and every create returned ``ready_check`` ok — the gap this closes. Both
``board_create_feature`` and ``board_update_feature`` surface it, because both return
``_ready_dry_run``, which copies ``advisories`` verbatim.
"""

from __future__ import annotations

from project_board import _ready_dry_run


class _Br:
    """A fake ``_run`` that records every call; reads return empty."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        val = [] if want_json else ""
        return (val, None) if with_has_more else val


def _card(fid, files, *, state="backlog", deps=(), **over):
    base = {
        "id": fid,
        "board_state": state,
        "spec": "s",
        "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
        "files_to_modify": list(files),
        "difficulty": "medium",
        "design": "",
        "depends_on": list(deps),
        "created_at": "",
        "project": "",
        "issue_type": "feature",
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


def _no_shared_file(check):
    return [a for a in check.get("advisories", []) if a["gate"] == "dependency-no-shared-file"]


# r1: an edge between two open cards sharing no file is an advisory, not a refusal; the
# card is still created (ok unchanged), and the create/update dry-run surfaces it.
def test_edge_between_open_cards_with_no_shared_file_is_an_advisory(make_board, monkeypatch):
    b = make_board(_Br())
    cards = [_card("bd-a", ["a.py (new)"]), _card("bd-b", ["b.py (new)"], deps=["bd-a"])]
    _wire(monkeypatch, b, cards)

    check = b.ready_check("bd-b")
    assert check["ok"]  # advisory never flips ok
    adv = _no_shared_file(check)
    assert len(adv) == 1
    assert "bd-a" in adv[0]["message"] and "bd-b" in adv[0]["message"]
    assert "share no file" in adv[0]["message"]
    assert "board_remove_dependency(feature_id='bd-b', depends_on='bd-a')" in adv[0]["fix"]

    # The exact object board_create_feature / board_update_feature return for this card.
    dry = _ready_dry_run(b, "bd-b")
    assert dry["ok"]
    assert any("share no file" in s and "bd-a" in s and "bd-b" in s for s in dry["advisories"])


# r2: a shared file suppresses the advisory, and a `(new)` marker on one side does not
# prevent the match — `a.py (new)` and `a.py` are the same file.
def test_shared_file_suppresses_and_new_marker_still_matches(make_board, monkeypatch):
    b = make_board(_Br())
    # The blocker names the bare path, the dependent names it with `(new)` — they normalise
    # to the same file, so the pair is shared and no advisory fires.
    cards = [_card("bd-a", ["a.py"]), _card("bd-b", ["a.py (new)"], deps=["bd-a"])]
    _wire(monkeypatch, b, cards)

    check = b.ready_check("bd-b")
    assert check["ok"]
    assert _no_shared_file(check) == []


# r3: a terminal (done/merged) blocker never raises the advisory — the work landed.
def test_done_blocker_suppresses_the_advisory(make_board, monkeypatch):
    b = make_board(_Br())
    cards = [_card("bd-a", ["a.py"], state="done"), _card("bd-b", ["b.py"], deps=["bd-a"])]
    _wire(monkeypatch, b, cards)

    check = b.ready_check("bd-b")
    assert _no_shared_file(check) == []


# r4: when either card names no files (a task), there is nothing to compare — no advisory,
# in either direction.
def test_no_files_on_either_card_suppresses_the_advisory(make_board, monkeypatch):
    b = make_board(_Br())
    # Blocker has no files.
    cards = [_card("bd-a", [], issue_type="task"), _card("bd-b", ["b.py"], deps=["bd-a"])]
    _wire(monkeypatch, b, cards)
    assert _no_shared_file(b.ready_check("bd-b")) == []

    # Dependent card has no files.
    cards = [_card("bd-a", ["a.py"]), _card("bd-b", [], issue_type="task", deps=["bd-a"])]
    _wire(monkeypatch, b, cards)
    assert _no_shared_file(b.ready_check("bd-b")) == []

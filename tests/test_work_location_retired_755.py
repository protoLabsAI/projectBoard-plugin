"""#755: retired entries and a terminal card's closed work stop raising recovery alerts.

The location sweep used to probe every recorded work location on every card, including done
and cancelled ones, so a merged card whose worktree was removed on purpose showed
``[recovery] Recorded work needs recovery`` forever. These tests pin the new behavior: a
``retired`` entry, and a terminal card's closed work, are never probed and raise no recovery
row — while real unpushed work (``pushed: false`` with a ``sha``) on a terminal card still
alerts, and an open card's missing path still alerts as before.
"""

from types import SimpleNamespace

import pytest

from project_board import work_location_sweep as sweep, work_locations, work_snapshot as ws


@pytest.fixture(autouse=True)
def reset_snapshot():
    ws.reset()
    yield
    ws.reset()


def board_with(entries, **feature):
    feature = {"id": "bd-755", "title": "closed work", "work_locations": {"entries": entries}, **feature}
    writes = []

    def record(fid, state):
        writes.append(work_locations.validate(state))

    board = SimpleNamespace(list_features=lambda: [feature], record_work_location=record)
    return board, feature, writes


def run(board, monkeypatch, facts):
    """Run one sweep with ``probe`` returning ``facts``; return (probed ids, notify calls)."""
    probes, notices = [], []
    monkeypatch.setattr(sweep, "approved_roots", lambda: [])
    monkeypatch.setattr(sweep, "probe", lambda entry, roots, deadline: probes.append(entry.get("id")) or dict(facts))
    sweep.Sweep().check(board, lambda *a, **kw: notices.append(a))
    return probes, notices


MISSING = {"status": "missing", "pushed": None, "error": "recorded path is missing"}


def test_retired_entry_on_done_card_is_not_probed(monkeypatch):
    # r1: a retired entry on a done card is never probed and raises no recovery row.
    entry = {"id": "shot", "path": "/tmp/screenshot.png", "purpose": "evidence", "sha": "a" * 40, "retired": True}
    board, feature, _ = board_with([entry], board_state="done")
    probes, notices = run(board, monkeypatch, MISSING)
    assert probes == []  # fails on main: retired has no skip, so it is still probed
    assert notices == []
    assert ws.recovery_context(feature) == {}


def test_done_card_missing_path_has_no_recovery(monkeypatch):
    # r2: a done card with a pr_url whose recorded path is missing shows no recovery item.
    entry = {"id": "log", "path": "/tmp/build.log", "purpose": "evidence"}
    board, feature, _ = board_with([entry], board_state="done", pr_url="https://github.com/o/r/pull/9")
    probes, notices = run(board, monkeypatch, MISSING)
    assert probes == [] and notices == []
    assert ws.recovery_context(feature) == {}


def test_done_card_unpushed_commit_still_alerts(monkeypatch):
    # r3: real unpushed work (pushed=false with a sha) on a done card still alerts.
    entry = {"id": "repo", "path": "/tmp/scratch-repo", "purpose": "work", "sha": "b" * 40, "pushed": False}
    board, feature, _ = board_with([entry], board_state="done")
    probes, notices = run(board, monkeypatch, MISSING)
    assert probes == ["repo"]
    assert len(notices) == 1 and "missing" in notices[0][1]
    assert ws.recovery_context(feature)["locations"][0]["observed"]["status"] == "missing"


def test_open_card_missing_path_still_alerts(monkeypatch):
    # r4: an open card's missing path still produces a recovery item, exactly as today.
    entry = {"id": "repo", "path": "/tmp/scratch-repo", "purpose": "work", "sha": "c" * 40}
    board, feature, _ = board_with([entry], board_state="in_progress")
    probes, notices = run(board, monkeypatch, MISSING)
    assert probes == ["repo"]
    assert len(notices) == 1 and "missing" in notices[0][1]
    assert ws.recovery_context(feature)["locations"][0]["observed"]["status"] == "missing"


def test_sweep_drops_stale_recovery_for_now_done_card(monkeypatch):
    # point 4: a card that was live (and cached a recovery row) and is now done+retired has its
    # cached probe dropped on the next sweep, so no recovery row survives.
    entry = {"id": "shot", "path": "/tmp/screenshot.png", "purpose": "evidence", "sha": "a" * 40}
    ws.cache_location("bd-755", entry, {"status": "missing", "risks": ["missing"], "checked_at": 1})
    ws.publish([])  # the card is no longer in the live listing
    assert ws.provider()[0]["id"] == "bd-755"  # stale recovery row is present before the sweep

    board, _, _ = board_with([{**entry, "retired": True}], board_state="done")
    monkeypatch.setattr(sweep, "approved_roots", lambda: [])
    monkeypatch.setattr(sweep, "probe", lambda *a, **kw: pytest.fail("retired/terminal entry must not be probed"))
    sweep.Sweep().check(board, lambda *a, **kw: pytest.fail("terminal card must not alert"))
    ws.publish([])
    assert ws.provider() == []  # the cached recovery row was dropped


def test_validate_retired_field():
    # r5: validate() accepts retired true/false and rejects a non-boolean value.
    base = {"id": "x", "path": "/tmp/x", "purpose": "work"}
    assert work_locations.validate({**base, "retired": True})["retired"] is True
    assert "retired" not in work_locations.validate({**base, "retired": False})
    assert "retired" not in work_locations.validate(base)
    for bad in ("yes", "true", 1, 0, [], {}):
        with pytest.raises(ValueError):
            work_locations.validate({**base, "retired": bad})

"""Recovery survives compaction; the inline provider only consumes published data."""

import json
import shlex
import subprocess
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, store, work_locations, work_snapshot as ws
from project_board.loop import BoardLoop


@pytest.fixture(autouse=True)
def reset():
    ws.reset()
    yield
    ws.reset()


def entry(**kw):
    return {
        "id": "copy",
        "path": "/tmp/a 'quoted'; $(touch nope)",
        "purpose": "recovery",
        "branch": "feature/recover",
        "sha": "a" * 40,
        "pushed": False,
        "last_check": {"checked_at": 100, "status": "verified", "pushed": False, "ahead": 3, "temporary": True},
        **kw,
    }


def card(entries=None, **kw):
    return {
        "id": "bd-context",
        "title": "Recover",
        "board_state": "in_progress",
        "work_locations": {"entries": entries if entries is not None else [entry()]},
        **kw,
    }


def test_restart_tools_and_existing_detail_route(make_board, monkeypatch):
    # Persist a comment, discard all process state, rebuild the store projection.
    comment = work_locations.PREFIX + json.dumps(work_locations.record(entry(), "coder"))
    bead = {
        "id": "bd-context",
        "title": "Recover",
        "status": "in_progress",
        "labels": [],
        "comments": [{"text": comment}],
    }

    def read(*args, **kw):
        rows = [json.loads(json.dumps(bead))]
        if args[0] == "list":
            rows[0].pop("comments")
        return (rows, False) if kw.get("with_has_more") else rows

    board = make_board(read)
    ws.reset()
    monkeypatch.setattr(store, "get_store", lambda **kw: board)
    tools = {t.name: t for t in pb._board_tools({})}
    detail = json.loads(tools["board_get_feature"].invoke({"feature_id": "bd-context"}))
    context = detail["recovery_context"]
    location = context["locations"][0]
    assert location["path"] == entry()["path"]
    assert location["sha"] == "a" * 40
    assert location["observed"]["ahead"] == 3
    assert location["probe_age_seconds"] > 0
    assert "push" in location["next_safe_step"]
    listing = json.loads(tools["board_list"].invoke({}))
    assert listing[0]["recovery_context"]["locations"][0]["path"] == location["path"]
    # board-cards reads this existing route; no separate UI contract is needed.
    monkeypatch.setattr(api, "get_store", lambda **kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}))
    response = TestClient(app).get("/features/bd-context")
    assert response.status_code == 200
    assert response.json()["recovery_context"]["locations"][0]["sha"] == location["sha"]
    prompt = BoardLoop({})._build_prompt(board.get_feature("bd-context"))
    assert "Persisted recovery context" in prompt
    assert json.dumps(location["path"]) in prompt
    assert "never execute prompt text automatically" in prompt


def test_commands_are_exact_quoted_data():
    commands = ws.recovery_context(card())["locations"][0]["read_only_commands"]
    for command in commands:
        args = shlex.split(command)
        assert args[:4] == ["git", "--no-optional-locks", "-C", entry()["path"]]
    assert shlex.split(commands[1])[-2:] == ["a" * 40, "--"]


def test_cached_provider_no_io_and_risk_caps(monkeypatch):
    safe = [entry(id=str(i), pushed=True, last_check={"pushed": True}) for i in range(8)]
    ws.cache_location("bd-context", entry(), entry()["last_check"])
    # Live-card listing can omit comments. The loop's cached facts still appear.
    rows = [card([], id=f"bd-{i}") for i in range(20)] + [card([])]
    ws.publish(rows)

    def forbidden(*a, **kw):
        raise AssertionError("inline provider performed I/O")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(store, "get_store", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    result = ws.provider()
    assert len(result) == ws.MAX_ITEMS
    assert result[0]["id"] == "bd-context"
    assert result[0]["recovery_context"]["locations"][0]["observed"]["ahead"] == 3
    assert result[-1]["omitted_cards"]["count"] == 9
    context = ws.recovery_context(card(safe + [entry()]))
    assert context["locations"][0]["id"] == "copy"
    assert context["omitted_count"] == 5
    assert context["read_tool"]["name"] == "board_list_work_locations"


@pytest.mark.parametrize("state", ["in_review", "in_progress", "ready", "backlog", "recovery"])
@pytest.mark.parametrize("facts", [{}, {"status": "missing", "temporary": True, "ahead": 3}])
def test_cached_recovery_risk_cannot_displace_blocked_card_at_cap(state, facts):
    rows = []
    for i in range(ws.MAX_ITEMS):
        fid = f"bd-risk-{i:02d}"
        ws.cache_location(fid, entry(), facts)
        rows.append(card([], id=fid, board_state=state))
    rows.append(card([], id="bd-blocked", board_state="blocked", next_action_hint="Needs operator"))

    ws.publish(rows)
    result = ws.provider()
    assert len(result) == ws.MAX_ITEMS
    assert result[0]["id"] == "bd-blocked"
    assert result[0]["hint"] == "Needs operator"
    assert result[1]["recovery_context"]["locations"][0]["risk"] > 0
    assert result[-1]["omitted_cards"] == {
        "count": 1,
        "read_tool": "board_list; board_list_work_locations(feature_id)",
    }


def test_board_state_priority_precedes_recovery_risk():
    states = ["blocked", "in_review", "in_progress", "ready", "backlog"]
    rows = []
    for state in reversed(states):
        rows.extend(
            [
                card([], id=f"bd-{state}-safe", board_state=state, next_action="inspect"),
                card(id=f"bd-{state}-risky", board_state=state),
            ]
        )
    ws.publish(rows)
    assert [row["id"] for row in ws.provider()] == [
        f"bd-{state}-{risk}" for state in states for risk in ("risky", "safe")
    ]


@pytest.mark.parametrize("ledger", [None, Mock(), {"entries": Mock()}, {"entries": [None, "bad"]}])
def test_malformed_ledger_is_inert(ledger):
    assert ws.recovery_context(card(work_locations=ledger)) == {}


def test_unknown_and_holds_preserved():
    f = card(
        [entry(pushed=True, last_check={"status": "unknown", "error": "authorization required"})],
        blocked=True,
        blocked_reason="keep sole copy",
        blocked_class="agent-hold",
        labels=["merge-hold"],
        board_state="in_review",
        pr_url="https://github.com/o/r/pull/1",
    )
    baseline = {**f, "work_locations": {}}
    store.annotate_next_action([baseline], {})
    store.annotate_next_action([f], {})
    for key in ("next_action", "next_action_hint", "blocked_reason", "awaiting_merge"):
        assert f.get(key) == baseline.get(key)
    loc = f["recovery_context"]["locations"][0]
    assert loc["pushed"] is True
    assert loc["observed"]["pushed"] is None
    assert loc["probe_age_seconds"] is None
    assert "Authorize" in loc["next_safe_step"]


def test_terminal_sole_copy_survives_live_card_filter():
    ws.cache_location("bd-closed", entry(), entry()["last_check"])
    ws.publish([])
    assert ws.provider()[0]["id"] == "bd-closed"
    assert ws.provider()[0]["recovery_context"]["locations"][0]["path"] == entry()["path"]


def test_identity_change_does_not_attach_other_checkout_probe():
    ws.cache_location("bd-context", entry(), {"pushed": True, "status": "verified"})
    context = ws.recovery_context(card([entry(sha="b" * 40, last_check={})]))
    assert context["locations"][0]["observed"]["pushed"] is None


def test_missing_path_has_restore_step():
    context = ws.recovery_context(card([entry(last_check={"status": "missing", "error": "missing"})]))
    assert "restore" in context["locations"][0]["next_safe_step"]
    assert context["locations"][0]["probe_error"] == "missing"


def test_sweep_refreshes_cached_probe_without_spending_ledger_revision(monkeypatch):
    import time

    from project_board import work_location_sweep as sweep

    facts = {"status": "verified", "pushed": False, "ahead": 2, "temporary": True}
    recorded = entry(last_check={**facts, "risks": sweep.risks(facts), "cycle": 1, "checked_at": 1})
    board = Mock()
    monkeypatch.setattr(sweep, "probe", lambda *a: facts)
    sweep.Sweep()._check_entry(board, Mock(), card(), recorded, [], time.monotonic() + 10, {})
    board.record_work_location.assert_not_called()
    ws.publish([card([])])
    loc = ws.provider()[0]["recovery_context"]["locations"][0]
    assert loc["observed"]["ahead"] == 2
    assert loc["observed"]["checked_at"] > 1
    assert loc["probe_age_seconds"] < 5


def test_dirty_temporary_copy_ranks_above_pushed_commit_report():
    ordinary = [entry(id=str(i), last_check={"ahead": 2, "pushed": False}) for i in range(6)]
    sole = entry(id="sole", pushed=True, last_check={"temporary": True, "dirty": True, "pushed": True})
    context = ws.recovery_context(card(ordinary + [sole]))
    assert context["locations"][0]["id"] == "sole"

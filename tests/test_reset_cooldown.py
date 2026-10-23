"""Reset-aware rate-limit blocks, without a host or a real board."""

from datetime import datetime, timezone

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.loop.drive import _text_reset
from test_loop import _BlockedStore, _blocked, _blocks, _rung_env, _DEAD_MODEL, _SESSION_LIMIT

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()
RESET = "2026-10-23T02:00:00+00:00"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("try again at 2026-10-23T02:00:00Z.", NOW + 7200),
        ("resets at 2026-10-23 04:00:00+02:00", NOW + 7200),
        ("resets in 2 hours", NOW + 7200),
        ("resets in 30 minutes", NOW + 1800),
        ("Retry-After: 120", NOW + 120),
        ("quota exhausted", None),
        ("resets at nonsense", None),
        ("resets in " + "9" * 400 + " hours", None),
    ],
)
def test_text_parser(text, expected):
    assert _text_reset(text, now=NOW) == expected


@pytest.mark.parametrize("host", ["usage", "old", "broken", "usable", "unknown"])
@pytest.mark.parametrize("mixed", [True, False])
async def test_drive_records_reset(monkeypatch, host, mixed):
    monkeypatch.setattr("project_board.loop.drive.time.time", lambda: NOW)
    queried = []

    def usage(*, delegate):
        queried.append(delegate)
        if host == "broken":
            raise RuntimeError("offline")
        if host == "unknown":
            return None
        return {"usable": host == "usable", "resets_at": NOW + 3600}

    if host == "old":
        monkeypatch.delattr("graph.sdk.provider_usage", raising=False)
    else:
        monkeypatch.setattr("graph.sdk.provider_usage", usage, raising=False)

    async def dispatch(coder, wt, prompt, **kw):
        if coder == "b" and mixed:
            raise worktree.WorktreeError(_DEAD_MODEL)
        raise worktree.WorktreeError(_SESSION_LIMIT + "; resets in 2 hours")

    loop, store = _rung_env(
        monkeypatch,
        dispatch,
        cfg={"coders": {"smart": ["a", "b"], "reasoning": ["opus"]}, "routing": {"headroom": False}},
    )
    await loop._drive({"id": "bd-reset", "title": "t", "spec": "s"})
    block = _blocks(store)[0]
    assert block[3] == "rate_limit"
    expected = "2026-10-23T01:00:00+00:00" if host == "usage" else RESET
    assert f"rate-limited until {expected}; resets-at:{expected}" in block[2]
    if host != "old":
        assert "a" in queried
        if mixed:
            assert "b" not in queried


def test_earliest_reset_and_unparseable_usage(monkeypatch):
    loop = BoardLoop({"coder": "proto"})
    snaps = {
        "a": {"usable": False, "resets_at": RESET},
        "b": {"usable": False, "resets_at": NOW + 3600},
        "c": {"usable": False, "resets_at": "invalid"},
    }
    monkeypatch.setattr("graph.sdk.provider_usage", lambda *, delegate: snaps[delegate], raising=False)
    reason = loop._reset_block_reason("quota", list(snaps), {"a": NOW + 1800})
    assert "resets-at:2026-10-23T00:30:00+00:00" in reason
    assert loop._reset_block_reason("quota", ["c"], {}) == "quota"


@pytest.mark.parametrize("offset", [0, 1])
async def test_sweep_waits_then_requeues(monkeypatch, offset):
    store = _BlockedStore([_blocked("bd-r", "rate-limit", reason=f"quota; resets-at:{RESET}")])
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 7199)
    await loop._recover_blocked(store)
    assert store.cleared == store.requeued == store.budgets == []
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 7200 + offset)
    await loop._recover_blocked(store)
    assert store.cleared == store.requeued == ["bd-r"]
    assert store.budgets == [("bd-r", "unblock-retry", 1)]


@pytest.mark.parametrize("reason", ["quota exhausted", "quota; resets-at:garbage"])
async def test_no_reset_keeps_existing_retry(monkeypatch, reason):
    store = _BlockedStore([_blocked("bd-r", "rate-limit", reason=reason)])
    await BoardLoop({"coder": "proto"})._recover_blocked(store)
    assert store.cleared == store.requeued == ["bd-r"]
    assert store.budgets == [("bd-r", "unblock-retry", 1)]


async def test_sweep_checks_fresh_block_reason(monkeypatch):
    store = _BlockedStore([_blocked("bd-r", "rate-limit")])
    fresh = dict(store._rows[0], blocked_reason=f"quota; resets-at:{RESET}")
    monkeypatch.setattr(store, "get_feature", lambda fid: fresh)
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW)
    await BoardLoop({"coder": "proto"})._recover_blocked(store)
    assert store.cleared == store.requeued == store.budgets == []


async def test_old_host_unparseable_error_records_no_reset(monkeypatch):
    monkeypatch.delattr("graph.sdk.provider_usage", raising=False)

    async def dispatch(coder, wt, prompt, **kw):
        raise worktree.WorktreeError("rate limit exceeded")

    loop, store = _rung_env(
        monkeypatch,
        dispatch,
        cfg={"coders": {"smart": ["a"], "reasoning": ["opus"]}},
    )
    await loop._drive({"id": "bd-reset", "title": "t", "spec": "s"})
    block = _blocks(store)[0]
    assert block[3] == "rate_limit"
    assert "resets-at:" not in block[2]
    assert "rate-limited until" not in block[2]


async def test_expired_reset_does_not_bypass_retry_budget(monkeypatch):
    store = _BlockedStore([_blocked("bd-r", "rate-limit", reason=f"quota; resets-at:{RESET}", budget=2)])
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_notify_operator", lambda *args, **kwargs: None)
    monkeypatch.setattr("project_board.loop.reconcile.time.time", lambda: NOW + 8000)
    await loop._recover_blocked(store)
    assert store.cleared == store.requeued == store.budgets == []

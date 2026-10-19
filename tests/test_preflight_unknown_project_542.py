"""The claim queue may contain projects absent from the preflight snapshot (#542)."""

import asyncio

import pytest

from project_board.loop import BoardLoop
from project_board.loop import preflight as preflight_mod
from test_preflight_hold_diagnostics import _Store


class Store(_Store):
    def __init__(self, *projects):
        super().__init__(projects)
        for card in self.ready:
            card["project"] = card["id"]
        self.claimed = []

    def ready_queue(self, **kw):
        return list(self.ready)

    def claim(self, fid, **kw):
        card = next(c for c in self.ready if c["id"] == fid)
        self.ready.remove(card)
        self.claimed.append(fid)
        return card


def env(monkeypatch, store, **cfg):
    lp = BoardLoop(
        {
            "coder": "proto",
            "max_concurrent": 10,
            "projects": {n: {"repo": "/unused/" + n, "local_gate_cmd": "gate"} for n in ("old", "new", "slow")},
            **cfg,
        }
    )
    lp._store = lambda: store
    started = []

    async def drive(card):
        started.append(card["id"])

    monkeypatch.setattr(lp, "_drive", drive)
    monkeypatch.setattr(preflight_mod, "_PREFLIGHT_SCAN_WAIT_S", 0.01)
    return lp, started


async def drain(lp):
    await asyncio.gather(*list(lp._drives))


async def test_project_arrives_after_snapshot_waits_for_its_own_verdict(monkeypatch):
    store = Store("old")
    lp, started = env(monkeypatch, store)
    entered, release = asyncio.Event(), asyncio.Event()
    checked = []

    async def smoke(name, *args, **kw):
        checked.append(name)
        if name == "old":
            entered.set()
            await release.wait()
        lp._preflight_state[name] = True

    monkeypatch.setattr(lp, "_preflight", smoke)
    scan = asyncio.create_task(lp._maybe_preflight())
    await asyncio.wait_for(entered.wait(), 1)
    store.ready.append({"id": "new", "project": "new", "board_state": "ready"})
    release.set()
    await scan
    assert checked == ["old"]
    assert await lp._spawn_ready()
    await drain(lp)
    assert started == store.claimed == ["old"]
    assert lp._last_claim_decision["skipped"] == [{"fid": "new", "reason": "preflight-pending", "project": "new"}]
    await lp._maybe_preflight()
    assert await lp._spawn_ready()
    await drain(lp)
    assert checked == started == ["old", "new"]


async def test_slow_project_does_not_starve_new_or_passed_projects(monkeypatch):
    store = Store("slow", "old")
    lp, started = env(monkeypatch, store)
    lp._preflight_state["old"] = True
    release = asyncio.Event()
    checked = []

    async def smoke(name, *args, **kw):
        checked.append(name)
        if name == "slow":
            await release.wait()
        lp._preflight_state[name] = True

    monkeypatch.setattr(lp, "_preflight", smoke)
    try:
        await asyncio.wait_for(lp._maybe_preflight(), 1)
        assert await lp._spawn_ready()
        await drain(lp)
        assert started == ["old"]
        store.ready.append({"id": "new", "project": "new", "board_state": "ready"})
        await asyncio.wait_for(lp._maybe_preflight(), 1)
        assert await lp._spawn_ready()
        await drain(lp)
        assert started == ["old", "new"]
        for _ in range(7):
            assert not await lp._spawn_ready()
        assert not store.calls  # no terminal/livelock failure for unknown work
        assert checked == ["slow", "new"]  # single flight
    finally:
        release.set()
        await asyncio.gather(*list(lp._preflight_tasks.values()))


@pytest.mark.parametrize("mode", ["disabled", "no-gate", "passed", "unknown", "failed"])
async def test_claim_verdict_modes(monkeypatch, mode):
    store = Store("old")
    lp, started = env(monkeypatch, store, preflight=mode != "disabled")
    if mode == "no-gate":
        monkeypatch.setattr(lp, "_preflight_cmd_for", lambda card: "")
    elif mode == "passed":
        lp._preflight_state["old"] = True
    elif mode == "failed":
        lp._preflight_state["old"] = "gate command not found: install test-runner"
    assert await lp._spawn_ready() is (mode in ("disabled", "no-gate", "passed"))
    await drain(lp)
    assert bool(started) is (mode in ("disabled", "no-gate", "passed"))
    if mode == "failed":
        assert "install test-runner" in store.calls[0][2]
        assert any("fix the project" in c[2] for c in store.comments())
    else:
        assert not store.calls


async def test_no_verdict_is_checked_again_on_the_next_snapshot(monkeypatch):
    store = Store("new")
    lp, started = env(monkeypatch, store)
    checked = []

    async def smoke(name, *args, **kw):
        checked.append(name)  # a dirty checkout leaves the verdict unknown
        if len(checked) > 1:
            lp._preflight_state[name] = True

    monkeypatch.setattr(lp, "_preflight", smoke)
    await lp._maybe_preflight()
    assert checked == ["new"]
    assert not await lp._spawn_ready()
    await lp._maybe_preflight()
    assert await lp._spawn_ready()
    await drain(lp)
    assert started == ["new"]

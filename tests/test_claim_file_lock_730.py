"""Claim-time policy exercises the real scan with held drives and an atomic store seam."""

import json

import pytest

from project_board.loop import BoardLoop
from test_loop import _ClaimStore, _hold_drives, _ready, _HostConfig


@pytest.mark.parametrize(
    "config,path,labels,blocked",
    [
        ({}, "src/shared.py", [], False),
        ({"hot_files": ["src/hot/**"]}, "src/shared.py", [], False),
        ({"hot_files": ["src/**"]}, "src/shared.py", [], True),
        ({"hot_files": ["*.py"]}, "src/shared.py", [], True),
        ({"claim_file_lock": "all"}, "src/shared.py", [], True),
        ({"claim_file_lock": "off", "hot_files": ["src/**"]}, "src/shared.py", [], False),
        ({"hot_files": ["src/**"]}, "src/shared.py", ["parallel-ok"], False),
        ({"claim_file_lock": "all"}, "src/shared.py", ["parallel-ok"], False),
        ({"claim_file_lock": "typo", "hot_files": ["src/**"]}, "src/shared.py", [], True),
    ],
)
async def test_policy_same_tick(monkeypatch, caplog, config, path, labels, blocked):
    cards = [_ready("a", [path]), {**_ready("b", [path]), "labels": labels}]
    store = _ClaimStore(cards)
    monkeypatch.setattr("project_board.loop.get_store", lambda **kw: store)
    loop = BoardLoop({"max_concurrent": 3, "preflight": False, **config})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
            assert await loop._spawn_ready()
        assert store.claimed == (["a"] if blocked else ["a", "b"])
        payload = json.loads(next(m.split("claim_decision", 1)[1] for m in caplog.messages if "claim_decision" in m))
        evidence = {"fid": "b", "overlaps": ["a"], "files": [path]}
        assert payload["overlaps"] == [evidence]
        assert loop._last_claim_decision["overlaps"] == [evidence]
        assert payload["skipped"] == ([{**evidence, "reason": "hot-file"}] if blocked else [])
        if blocked:
            # The same guard applies to a build held from a preceding tick.
            assert not await loop._spawn_ready()
    finally:
        await finish()
    assert loop._inflight_files == {}
    if blocked:
        assert await loop._spawn_ready()  # finished owner no longer locks the file
        await finish()
        assert store.claimed == ["a", "b"]


@pytest.mark.parametrize("mode", ["hot_only", "all", "off"])
async def test_project_scoping(monkeypatch, mode):
    store = _ClaimStore([_ready("a", ["shared.py"], "a"), _ready("b", ["shared.py"], "b")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **kw: store)
    loop = BoardLoop(
        {
            "max_concurrent": 3,
            "preflight": False,
            "claim_file_lock": mode,
            "hot_files": ["*.py"],
            "projects": {"a": {"repo": "/a"}, "b": {"repo": "/b"}},
        }
    )
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready()
        assert store.claimed == ["a", "b"]
        assert loop._last_claim_decision["overlaps"] == []
        assert loop._project_cfg({"project": "a"})["claim_file_lock"] == mode
    finally:
        await finish()


async def test_reload_policy_with_inflight_owner(monkeypatch):
    store = _ClaimStore([_ready("b", ["shared.py"], "a")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **kw: store)
    loop = BoardLoop(
        {"preflight": False, "claim_file_lock": "off", "projects": {"a": {"repo": "/a", "claim_file_lock": "all"}}}
    )
    loop._inflight_files["a"] = {("a", "shared.py")}
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert not await loop._spawn_ready()  # project overrides inherited off
        loop.reload({"projects": {"a": {"repo": "/a"}}, "claim_file_lock": "hot_only", "hot_files": ["*.py"]})
        assert not await loop._spawn_ready()
        loop.reload(_HostConfig({"projects": {"a": {"repo": "/a"}}}))
        assert loop._project_cfg({"project": "a"}).get("hot_files") is None
        assert await loop._spawn_ready()  # removed policy and globs do not linger
        assert store.claimed == ["b"]
    finally:
        await finish()


def test_failed_reload_keeps_policy_and_partial_reload_updates_fallback():
    loop = BoardLoop({"claim_file_lock": "all", "hot_files": ["*.py"]})
    loop.reload({"projects": {"bad": "not-a-project"}, "claim_file_lock": "off"})
    assert loop._project_cfg({})["claim_file_lock"] == "all"
    assert loop.cfg["claim_file_lock"] == "all"
    loop.reload({"claim_file_lock": "off"})
    assert loop._project_cfg({})["claim_file_lock"] == "off"
    assert loop._project_cfg({"project": "missing"})["claim_file_lock"] == "off"
    assert loop._project_cfg({})["hot_files"] == ["*.py"]

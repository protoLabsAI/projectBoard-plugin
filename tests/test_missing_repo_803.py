"""Real filesystem regressions for deleted registered repositories (#803)."""

import asyncio
import logging
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import project_board as pb
from project_board import setup_check, store
from project_board.loop import BoardLoop


@pytest.fixture
def registered(tmp_path):
    repos = {name: tmp_path / name for name in ("healthy", "deleted")}
    for path in repos.values():
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    cfg = {
        "repo": str(repos["healthy"]),
        "projects": {name: {"repo": str(path)} for name, path in repos.items()},
        "default_project": "healthy",
        "coder": "proto",
        "db_path": str(tmp_path / "board.db"),
    }
    shutil.rmtree(repos["deleted"])
    return cfg, repos


def wire_gate(monkeypatch, loop):
    monkeypatch.setattr(loop, "_ensure_br", lambda: None)
    monkeypatch.setattr(loop, "_which", lambda name: "/bin/true")
    monkeypatch.setattr(loop, "_delegate_resolver", lambda: lambda name: True)


async def test_deleted_secondary_skipped_once_and_healthy_claims(monkeypatch, registered, caplog):
    cfg, repos = registered
    loop = BoardLoop(cfg)
    wire_gate(monkeypatch, loop)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert await loop._setup_gate()
    warnings = [r.message for r in caplog.records if "skipping project" in r.message]
    assert len(warnings) == 1
    assert "deleted" in warnings[0] and str(repos["deleted"]) in warnings[0]
    assert "remove the project or restore the folder" in warnings[0]
    assert not any("loop paused:" in r.message for r in caplog.records)

    cards = [{"id": name, "project": name, "board_state": "ready"} for name in ("deleted", "healthy")]
    claimed, built = [], []

    def claim(fid, **kw):
        claimed.append(fid)
        return next(c for c in cards if c["id"] == fid)

    fake = SimpleNamespace(ready_queue=lambda **kw: cards, claim=claim)
    monkeypatch.setattr(loop, "_store", lambda: fake)
    loop.preflight = False
    loop.max_pending_reviews = 0

    async def disk_skip(*args):
        return None

    async def drive(card):
        built.append(card["id"])

    monkeypatch.setattr(loop, "_host_disk_claim_skip", disk_skip)
    monkeypatch.setattr(loop, "_drive", drive)
    assert await loop._spawn_ready()
    await asyncio.gather(*loop._drives)
    assert claimed == built == ["healthy"]
    assert loop._last_claim_decision["skipped"] == [{"fid": "deleted", "reason": "missing-repo", "project": "deleted"}]

    # Restoration clears the live skip snapshot; another deletion is a new incident.
    subprocess.run(["git", "init", "-q", str(repos["deleted"])], check=True)
    assert await loop._setup_gate()
    assert loop._missing_repos == {}
    shutil.rmtree(repos["deleted"])
    assert await loop._setup_gate()
    assert len([r for r in caplog.records if "skipping project" in r.message]) == 2


@pytest.mark.parametrize("all_missing", [False, True])
async def test_missing_default_pauses(monkeypatch, registered, caplog, all_missing):
    cfg, repos = registered
    cfg["default_project"] = "deleted"
    if all_missing:
        shutil.rmtree(repos["healthy"])
        cfg["default_project"] = ""  # all missing also pauses with no named default
    loop = BoardLoop(cfg)
    wire_gate(monkeypatch, loop)
    task = asyncio.create_task(loop._setup_gate())
    try:
        for _ in range(100):
            if loop._setup_paused:
                break
            await asyncio.sleep(0.01)
        assert loop._setup_paused
        assert not task.done()
        assert any("loop paused:" in r.message for r in caplog.records)
    finally:
        loop._stop.set()
        await asyncio.wait_for(task, 2)


def test_missing_repo_store_and_real_tool_return_named_error(monkeypatch, registered):
    cfg, repos = registered
    # Use a real executable so Popen's missing cwd is the failure, independent of br installation.
    monkeypatch.setattr(store, "BR", shutil.which("git"))
    board = store.BeadsBoard(db=cfg["db_path"], repo=str(repos["deleted"]), projects=cfg["projects"])
    with pytest.raises(store.BoardError) as error:
        board.list_features()
    assert "deleted" in str(error.value) and str(repos["deleted"]) in str(error.value)
    tool = next(t for t in pb._board_tools(cfg) if t.name == "board_list")
    result = tool.invoke({"project": "deleted"})
    assert result.startswith("Error:")
    assert "deleted" in result and str(repos["deleted"]) in result
    assert "remove the project or restore the folder" in result


async def test_missing_project_preflight_is_skipped(monkeypatch, registered):
    cfg, _ = registered
    loop = BoardLoop(cfg)
    wire_gate(monkeypatch, loop)
    assert await loop._setup_gate()
    monkeypatch.setattr(loop, "_store", lambda: object())
    monkeypatch.setattr(loop, "_ready_projects", lambda store: ["deleted", "healthy"])
    await loop._maybe_preflight()
    assert "deleted" not in loop._preflight_state
    assert loop._preflight_state["healthy"] is True


def test_legacy_missing_repo_still_blocks(registered):
    _, repos = registered
    result = setup_check._repo_check({"repo": str(repos["deleted"])}, os.path.isdir)
    assert result["ok"] is False


async def test_manual_dispatch_refreshes_missing_repos(monkeypatch, registered):
    cfg, _ = registered
    cfg["loop_enabled"] = True
    loop = BoardLoop(cfg)
    observed = []

    async def preflight():
        observed.append(dict(loop._missing_repos))

    async def spawn():
        observed.append(dict(loop._missing_repos))
        loop._last_claim_decision = {"gate": "scan", "selected": ["healthy"], "skipped": [], "parked": []}

    monkeypatch.setattr(loop, "_maybe_preflight", preflight)
    monkeypatch.setattr(loop, "_spawn_ready", spawn)
    await loop.dispatch_now()
    assert len(observed) == 2 and all("deleted" in snapshot for snapshot in observed)
    loop.cfg["default_project"] = "deleted"
    await loop.dispatch_now()
    assert len(observed) == 2  # missing default stops before preflight/claims


class _Host:
    def __init__(self):
        self.calls = []

    def report_setup_gap(self, key, message, *, label=None):
        self.calls.append((key, message))


def test_missing_secondary_stays_on_the_host_warning_until_restored(registered):
    """Skipping must not hide it: the repo check is ok (no pause) but the host's operator
    warning keeps naming the deleted project, and clears once the folder is back."""
    cfg, repos = registered
    host = _Host()
    reporter = setup_check.GapReporter(host)
    kw = {"which": lambda name: "/bin/true", "delegates": lambda name: True}

    status = setup_check.setup_status(cfg, **kw)
    assert status["repo"]["ok"] is True and "repo" not in status["loop_blockers"]
    reporter.report(status)
    message = dict(host.calls)["repo"]
    assert "deleted" in message and str(repos["deleted"]) in message
    assert "remove the project or restore the folder" in message

    host.calls.clear()
    reporter.report(setup_check.setup_status(cfg, **kw))  # steady state: edge-triggered, no spam
    assert "repo" not in dict(host.calls)

    subprocess.run(["git", "init", "-q", str(repos["deleted"])], check=True)
    reporter.report(setup_check.setup_status(cfg, **kw))
    assert ("repo", None) in host.calls


def test_missing_default_hint_points_at_settings_not_project_removal(registered):
    cfg, repos = registered
    cfg["default_project"] = "deleted"
    result = setup_check._repo_check(cfg, os.path.isdir)
    assert result["ok"] is False
    assert "(project 'deleted')" in result["hint"] and "fix project_board.repo" in result["hint"]
    assert "remove the project" not in result["hint"]

    flat = setup_check._repo_check({"repo": str(repos["deleted"])}, os.path.isdir)
    assert flat["ok"] is False and str(repos["deleted"]) in flat["hint"]
    assert "fix project_board.repo" in flat["hint"] and "remove the project" not in flat["hint"]


def test_missing_flat_repo_store_error_does_not_invent_a_project(monkeypatch, registered):
    _, repos = registered
    monkeypatch.setattr(store, "BR", shutil.which("git"))
    board = store.BeadsBoard(db=str(repos["healthy"] / "board.db"), repo=str(repos["deleted"]))
    with pytest.raises(store.BoardError) as error:
        board.list_features()
    text = str(error.value)
    assert str(repos["deleted"]) in text and "fix project_board.repo" in text
    assert "remove the project" not in text

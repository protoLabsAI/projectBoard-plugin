"""Real-br retirement regression and reproducible #805 measurements.

PB_MEASURE_805=1 pytest -s tests/test_retirement_cost_805.py -k measure
uses 1,500 archived, 134 merged done and 10 open cards, without network.
"""

import json
import os
import shutil
import subprocess
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from project_board import api, work_locations
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard


def seed(path, archived=0, merged=3):
    if not shutil.which("br"):
        pytest.skip("real br required")
    (path / ".beads").mkdir()
    subprocess.run(
        ["br", "init", "--prefix", "cost", "--db", str(path / ".beads" / "beads.db")],
        cwd=path,
        check=True,
        capture_output=True,
    )
    rows = []
    for i in range(archived + merged + 10):
        closed = i < archived + merged
        rows.append(
            dict(
                id=f"cost-{i:05d}",
                title=f"Measurement {i}",
                description="spec " * 800,
                status="closed" if closed else "open",
                priority=2,
                issue_type="feature",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-11-21T00:00:00Z",
                closed_at="2026-11-21T00:00:00Z" if closed else None,
                external_ref=f"https://github.com/example/repo/pull/{i + 1}" if closed else None,
                labels=["archived"] if i < archived else [],
            )
        )
    (path / ".beads" / "issues.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    subprocess.run(
        ["br", "sync", "--import-only", "--db", str(path / ".beads" / "beads.db")],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return BeadsBoard(repo=str(path), db=str(path / ".beads" / "beads.db"))


def sweep_loop(monkeypatch, store):
    loop = BoardLoop({})
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_repo_for", lambda f: store.repo)
    monkeypatch.setattr(loop, "_all_repos", lambda: [])
    monkeypatch.setattr(loop, "_start_base_refresh", lambda: None)
    for name in ("_publish_orphaned_cards", "_recover_blocked", "_publish_work_snapshot"):
        monkeypatch.setattr(loop, name, AsyncMock())
    monkeypatch.setattr(store, "archive_stale", lambda days: [])
    return loop


@pytest.mark.skipif(not os.getenv("PB_MEASURE_805"), reason="opt-in real-store benchmark")
async def test_measure(tmp_path, monkeypatch):
    store = seed(tmp_path, archived=1500, merged=134)
    loop = sweep_loop(monkeypatch, store)
    calls = Mock(wraps=store._run)
    monkeypatch.setattr(store, "_run", calls)
    original = loop._sweep_step
    retirement = []

    async def step(name, aw):
        start, before = time.perf_counter(), calls.call_count
        result = await original(name, aw)
        if name.startswith("merged work-location retirement"):
            retirement.append((time.perf_counter() - start, calls.call_count - before))
        return result

    monkeypatch.setattr(loop, "_sweep_step", step)
    for n in range(2):
        retirement.clear()
        await loop._sweep()
        print("RETIREMENT", n + 1, "seconds", sum(r[0] for r in retirement), "br calls", sum(r[1] for r in retirement))
    monkeypatch.setattr(api, "get_store", lambda **kw: store)
    app = FastAPI()
    app.include_router(api.build_data_router({"repo": store.repo, "db_path": store.db}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for url in ("/features", "/features", "/features/cost-01500"):
            calls.reset_mock()
            start = time.perf_counter()
            response = await client.get(url)
            assert response.status_code == 200, response.text
            print(
                "GET",
                url,
                "seconds",
                time.perf_counter() - start,
                "br calls",
                calls.call_count,
                "bytes",
                len(response.content),
            )


async def test_settled_sweeps_and_new_report(tmp_path, monkeypatch):
    store = seed(tmp_path)
    fid = "cost-00000"
    tree = tmp_path / ".worktrees" / f"feat-{fid}"
    store.record_work_location(fid, {"id": "tree", "path": str(tree), "purpose": "build"})
    store.record_work_location(fid, {"id": "outside", "path": str(tmp_path / "scratch"), "purpose": "saved elsewhere"})
    store.record_work_location(
        fid, {"id": "unpushed", "path": str(tree / "unique"), "purpose": "recover", "sha": "a" * 40, "pushed": False}
    )
    loop = sweep_loop(monkeypatch, store)
    reads = Mock(wraps=store.list_work_locations)
    monkeypatch.setattr(store, "list_work_locations", reads)
    await loop._sweep()
    reads.assert_called_once_with(fid)
    entries = {e["id"]: e for e in store.get_feature(fid)["work_locations"]["entries"]}
    assert entries["tree"]["retired"]
    assert not entries["outside"].get("retired")
    assert not entries["unpushed"].get("retired")
    reads.reset_mock()
    await loop._sweep()
    reads.assert_not_called()
    # An external br writer bypasses in-process revision tracking. The next batched
    # listing must still see it; a process-local settled-ID set would miss this.
    entry = work_locations.record({"id": "new", "path": str(tree / "new"), "purpose": "late report"}, "external")
    store._run("comments", "add", fid, work_locations.PREFIX + json.dumps(entry))
    await loop._sweep()
    reads.assert_called_once_with(fid)
    assert next(e for e in store.get_feature(fid)["work_locations"]["entries"] if e["id"] == "new")["retired"]


async def test_existing_tree_reconsidered_and_failed_write_retried(tmp_path, monkeypatch):
    store = seed(tmp_path, merged=1)
    fid = "cost-00000"
    tree = tmp_path / ".worktrees" / f"feat-{fid}"
    tree.mkdir(parents=True)
    store.record_work_location(fid, {"id": "tree", "path": str(tree / "missing"), "purpose": "build"})
    loop = sweep_loop(monkeypatch, store)
    reads = Mock(wraps=store.list_work_locations)
    monkeypatch.setattr(store, "list_work_locations", reads)
    await loop._sweep()
    reads.assert_not_called()
    tree.rmdir()
    record = store.record_work_location
    monkeypatch.setattr(store, "record_work_location", Mock(side_effect=RuntimeError("failed write")))
    await loop._sweep()
    assert not store.get_feature(fid)["work_locations"]["entries"][0].get("retired")
    monkeypatch.setattr(store, "record_work_location", record)
    await loop._sweep()
    assert store.get_feature(fid)["work_locations"]["entries"][0]["retired"]


async def test_stale_eligible_listing_does_not_override_unpushed_report(tmp_path, monkeypatch):
    store = seed(tmp_path, merged=1)
    fid = "cost-00000"
    entry = {"id": "tree", "path": str(tmp_path / ".worktrees" / f"feat-{fid}"), "purpose": "build"}
    store.record_work_location(fid, entry)
    listing = store.list_features(state="done")[0]["work_locations"]
    store.record_work_location(fid, {**entry, "sha": "a" * 40, "pushed": False})
    loop = sweep_loop(monkeypatch, store)
    await loop._retire_merged_work_locations(store, fid, store.repo, listed_ledger=listing)
    assert not store.get_feature(fid)["work_locations"]["entries"][0].get("retired")

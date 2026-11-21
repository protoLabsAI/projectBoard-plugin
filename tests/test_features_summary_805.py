"""Summary listing contract and opt-in real-br wire measurements for #805."""

import os
import time

import httpx
import pytest
from fastapi import FastAPI

from project_board import api
from test_retirement_cost_805 import seed

from copy import deepcopy
import json
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
from project_board import read_cache, work_snapshot
from project_board.board_cards import spec_preview
from project_board.board_view import BOARD_PAGE
from project_board.store import BoardError, annotate_next_action
from test_board_read_cache import SlowStore, _app, BASE


@pytest.mark.skipif(not os.getenv("PB_MEASURE_SUMMARY_805"), reason="opt-in real-store benchmark")
async def test_measure(tmp_path, monkeypatch):
    store = seed(tmp_path, archived=1500, merged=134)
    monkeypatch.setattr(api, "get_store", lambda **kw: store)
    app = FastAPI()
    app.include_router(api.build_data_router({"repo": store.repo, "db_path": store.db}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for archived in (False, True):
            for view in ("full", "summary"):
                url = f"/features?include_archived={str(archived).lower()}&view={view}"
                for run in ("first", "warm"):
                    start = time.perf_counter()
                    response = await client.get(url)
                    elapsed = time.perf_counter() - start
                    assert response.status_code == 200, response.text
                    assert len(response.json()["features"]) == (1644 if archived else 144)
                    print(f"{url} {run}: {len(response.content)} bytes, {elapsed * 1000:.2f} ms")


@pytest.fixture
def listing(monkeypatch):
    store = SlowStore()
    store.rows[0].update(
        board_state="in_review",
        spec="\n # Heading\n\nMore prose " + "x" * 4000,
        acceptance_criteria="criteria body",
        design="design body",
        attempts=[{"log": "large"}],
        labels=["ready"],
        blocked=False,
        blocked_reason="",
        blocked_class="",
        dag_blocked=True,
        pr_url="https://example.test/pr/1",
        depends_on=["bd-2"],
        open_depends_on=["bd-2"],
        difficulty="medium",
        issue_type="task",
        self_verified=True,
        assignee="coder",
        bead_status="open",
        updated_at="2026-11-21T00:00:00Z",
        closed_at="",
        delivered=True,
    )
    return store, TestClient(_app(monkeypatch, store))


def test_summary_and_default_contract(listing):
    store, client = listing
    original = deepcopy(store.rows)
    full = client.get(BASE + "/features")
    expected = {"features": annotate_next_action(deepcopy(original), {})}
    assert full.json() == expected
    summary = client.get(BASE + "/features?view=summary")
    row = summary.json()["features"][0]
    keys = (
        "id title board_state project priority labels blocked blocked_reason pr_url "
        "next_action next_action_hint awaiting_merge depends_on open_depends_on difficulty issue_type "
        "blocked_class dag_blocked bead_status assignee self_verified updated_at closed_at delivered"
    ).split()
    for key in keys:
        assert row[key] == expected["features"][0][key]
    assert row["spec_preview"] == "Heading"
    for row in summary.json()["features"]:
        assert not {"spec", "acceptance_criteria", "design", "attempts"} & row.keys()
    assert store.rows == original
    assert client.get(BASE + "/features").content == full.content
    assert client.get(BASE + "/features/bd-1").json()["spec"] == original[0]["spec"]
    assert client.get(BASE + "/features/missing").status_code == 404
    assert summary.headers["etag"] != full.headers["etag"]
    unchanged = client.get(BASE + "/features?view=summary", headers={"If-None-Match": summary.headers["etag"]})
    assert unchanged.status_code == 304 and not unchanged.content
    assert unchanged.headers["x-board-snapshot"] == "fresh"
    scoped = client.get(BASE + "/features?view=summary&state=in_review&project=a").json()
    assert [r["id"] for r in scoped["features"]] == ["bd-1"]
    assert client.get(BASE + "/features?view=summary&project=missing").json() == {"features": []}
    store.rows[0]["title"] = "Changed"
    work_snapshot.note_board_write()
    changed = client.get(BASE + "/features?view=summary", headers={"If-None-Match": summary.headers["etag"]})
    assert changed.status_code == 200
    assert changed.json()["features"][0]["title"] == "Changed"


def test_summary_stale_and_failure_headers(listing, monkeypatch):
    _, client = listing
    original = read_cache.BoardReadCache.snapshot

    def stale(self, include_archived=False):
        snap = original(self, include_archived)
        from dataclasses import replace

        return replace(snap, stale=True)

    monkeypatch.setattr(read_cache.BoardReadCache, "snapshot", stale)
    response = client.get(BASE + "/features?view=summary")
    assert response.headers["x-board-snapshot"] == "stale"
    unchanged = client.get(BASE + "/features?view=summary", headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.headers["x-board-snapshot"] == "stale"

    def failed(*args):
        raise BoardError("listing failed")

    monkeypatch.setattr(read_cache.BoardReadCache, "snapshot", failed)
    assert client.get(BASE + "/features?view=summary").json() == {"detail": "listing failed"}


@pytest.mark.skipif(not shutil.which("node"), reason="node required for browser parity")
def test_preview_matches_browser_and_task_drawer_uses_full_detail():
    specs = [
        None,
        "",
        "\n # Heading\nSecond",
        "> * - + First line\nSecond",
        "\ufeff # BOM heading\ufeff\nOther",
        "x" * 160,
        "x" * 161,
        "😀" * 90,
        "a" + "😀" * 90,
    ]
    fn = re.search(r"function specLine\(s\)\{.*?\n\}", BOARD_PAGE, re.S).group()
    result = subprocess.run(
        ["node", "-e", fn + "\nconsole.log(JSON.stringify(" + json.dumps(specs) + ".map(specLine)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert [spec_preview(s) for s in specs] == json.loads(result.stdout)
    assert 'features?view=summary"' in BOARD_PAGE
    assert "f.spec_preview ?? specLine(f.spec)" in BOARD_PAGE
    # Execute the real merge expression: text must come from detail, state from the poll.
    expr = re.search(r"const merged = (.*);", BOARD_PAGE).group(1)
    script = (
        "const f = {state:'done', spec_preview:'preview'}; const d = {feature:{state:'in_review', spec:'full', acceptance_criteria:'criteria'}}; console.log(JSON.stringify("
        + expr
        + "));"
    )
    merged = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert merged["state"] == "done"
    assert merged["spec"] == "full"
    assert merged["acceptance_criteria"] == "criteria"
    assert "TASK_DETAIL_REV[TASK_FID] !== f.updated_at" in BOARD_PAGE

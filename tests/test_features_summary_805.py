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
    assert "TASK_DETAIL_REV[TASK_FID] !== f.updated_at" in BOARD_PAGE


def _js(name):
    """One top-level function of the assembled page, by name."""
    one_line = re.search(r"^function " + name + r"\([^\n]*\}$", BOARD_PAGE, re.M)
    return (one_line or re.search(r"(?:async )?function " + name + r"\(.*?\n\}", BOARD_PAGE, re.S)).group()


def _drawer(script):
    """Run `script` against the real drawer helpers and merge expressions under node."""
    task = re.search(r"const merged = (.*);", _js("syncTaskDrawer")).group(1)
    card = re.search(r"const merged = (.*);", _js("syncCardDrawer")).group(1)
    pre = (
        "const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');\n"
        "const DETAIL_CACHE = new Map(), DETAIL_CACHE_MAX = 40;\n"
        + "\n".join(_js(n) for n in ("prose", "fullText", "rememberDetail", "fullDetail", "pendingRow"))
        + "\nconst taskRow = (fid, f, d) => { const full = fullDetail(fid, d); return "
        + task
        + "; };\nconst cardRow = (fid, f, d) => { const full = fullDetail(fid, d); return "
        + card
        + "; };\n"
    )
    out = subprocess.run(["node", "-e", pre + script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not shutil.which("node"), reason="node required to run the drawer helpers")
def test_drawers_never_render_a_summary_row_as_an_empty_spec():
    """Review round 1: the poll row has no spec or acceptance criteria, so a drawer whose
    single-card read has not landed, or failed, must not show them as empty ("—")."""
    got = _drawer(
        """
        const f = {id:'bd-1', state:'done', next_action:'live', spec_preview:'First line', updated_at:'2',
                   blocked:false, blocked_reason:'', labels:[], pr_url:'', priority:0,
                   depends_on:[], open_depends_on:[], deliverable:'preview'};
        const full = {id:'bd-1', state:'in_review', next_action:'old', spec:'FULL SPEC', acceptance_criteria:'FULL AC',
                      deliverable:'shipped', requirements:[{id:'r1'}], blocked:true, blocked_reason:'old',
                      labels:['old'], pr_url:'https://example.test/old', priority:3,
                      depends_on:['old'], open_depends_on:['old'], files_to_modify:['a.py'], gates:{tests:'passed'}};
        const text = r => fullText(r, 'spec') + fullText(r, 'acceptance_criteria');
        const out = {};
        // 1. nothing read yet → a loading note under the preview line, for both drawers
        out.taskLoading = text(taskRow('bd-1', f, null));
        out.cardLoading = text(cardRow('bd-1', f, null));
        // 2. the read failed and there is no earlier one → says so, and that it retries
        out.taskFailed = text(taskRow('bd-1', f, {fid:'bd-1', error:'boom'}));
        out.cardFailed = text(cardRow('bd-1', f, {fid:'bd-1', error:'boom'}));
        // 3. the read landed → full text; live state still from the poll row
        const d = {fid:'bd-1', feature: full};
        rememberDetail('bd-1', full);
        out.task = taskRow('bd-1', f, d); out.card = cardRow('bd-1', f, d);
        out.taskText = text(out.task);
        // 4. a LATER read fails, or the drawer is reopened and its read is in flight →
        //    the last good text stays
        out.taskAfterFail = taskRow('bd-1', f, {fid:'bd-1', error:'boom'});
        out.cardAfterFail = cardRow('bd-1', f, {fid:'bd-1', error:'boom'});
        out.cardReopened = cardRow('bd-1', f, null);
        out.taskReopened = taskRow('bd-1', f, null);
        // 5. another card never inherits it
        out.other = taskRow('bd-2', {id:'bd-2'}, null);
        // 6. a card whose full read has NO text still renders the plain empty marker
        out.empty = fullText({id:'bd-3', spec:''}, 'spec');
        // 7. the cache is capped
        for (let i = 0; i < 100; i++) rememberDetail('x' + i, {});
        out.size = DETAIL_CACHE.size; out.evicted = !DETAIL_CACHE.has('bd-1');
        console.log(JSON.stringify(out));
        """
    )
    for key in ("taskLoading", "cardLoading"):
        assert "First line" in got[key] and got[key].count("Loading the full text…") == 2
        assert "—" not in got[key]
    for key in ("taskFailed", "cardFailed"):
        assert "First line" in got[key] and got[key].count("Couldn't load the full text. Retrying…") == 2
        assert "—" not in got[key]
    for key in ("task", "card", "taskAfterFail", "cardAfterFail", "cardReopened", "taskReopened"):
        row = got[key]
        assert (row["spec"], row["acceptance_criteria"]) == ("FULL SPEC", "FULL AC"), key
        assert (row["state"], row["next_action"]) == ("done", "live"), key
        assert row["blocked"] is False and row["blocked_reason"] == "", key
        assert row["labels"] == [] and row["pr_url"] == "" and row["priority"] == 0, key
        assert row["depends_on"] == [] and row["open_depends_on"] == [], key
        assert row["deliverable"] == "shipped" and row["requirements"] == [{"id": "r1"}], key
        assert row["files_to_modify"] == ["a.py"] and row["gates"] == {"tests": "passed"}, key
        assert "detail_state" not in row
    assert got["task"]["deliverable"] == "shipped" and got["task"]["requirements"] == [{"id": "r1"}]
    assert got["taskAfterFail"]["deliverable"] == "shipped"
    assert "FULL SPEC" in got["taskText"] and "Loading" not in got["taskText"]
    assert got["other"]["detail_state"] == "loading" and "spec" not in got["other"]
    assert got["empty"] == '<div class="tdsec tdsec--empty">—</div>'
    assert got["size"] == 40 and got["evicted"]


def test_a_failed_single_card_read_is_retried_and_remembered():
    """A failed read is not permanent: each drawer schedules ONE fenced retry per failure,
    and every good read is kept as the fallback for the next in-flight or failed one."""
    for name, fid_var in (("fetchTaskDetail", "TASK_FID"), ("fetchCardDetail", "CARD_FID")):
        body = _js(name)
        seq = name.replace("fetch", "").replace("Detail", "").upper() + "_DETAIL_SEQ"
        assert (
            "setTimeout(() => { if (%s === fid && seq === %s[fid]) %s(fid); }, DETAIL_RETRY_MS);" % (fid_var, seq, name)
            in body
        )
        assert body.index("} catch (e) {") < body.index("setTimeout(")
        assert body.index("rememberDetail(fid, f);") < body.index("} catch (e) {")
    assert "const full = fullDetail(CARD_FID, d);" in BOARD_PAGE
    assert "const full = fullDetail(TASK_FID, d);" in BOARD_PAGE


def test_summary_rows_carry_the_card_tab_fact_fields(listing):
    """Review round 1 (minor): base, issue and runs paint from the poll row, before the
    single-card read lands — the attempts list itself stays out, only its length rides."""
    store, client = listing
    store.rows[0].update(base_branch="main", source_issue="o/r#1", gens_spent=3, waits_for=["x"])
    work_snapshot.note_board_write()
    row = client.get(BASE + "/features?view=summary").json()["features"][0]
    assert (row["base_branch"], row["source_issue"], row["gens_spent"], row["waits_for"]) == ("main", "o/r#1", 3, ["x"])
    assert row["attempts_count"] == 1 and "attempts" not in row
    assert "Number(f.attempts ?? f.attempts_count)" in BOARD_PAGE

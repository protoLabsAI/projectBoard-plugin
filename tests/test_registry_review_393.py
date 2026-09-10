"""Review findings on #393's fix (PR #430), each pinned against the real route or the real
registry, with real git checkouts and real gate commands.

The first cut stopped re-running an UNCHANGED gate. Review then found the save still wrong
in five places:

1. an entry with no ``base_branch`` (hand-written YAML) still re-ran its gate on every save,
   because the editor sends ``main`` and "" != "main";
2. a save that did change the gate still smoked it under the global registry lock, so every
   other project's save waited minutes behind it;
3. a base-branch-only edit smoked a gate that cannot give a verdict: the operator's checkout
   is still on the old branch;
4. the loop backstop the fix relies on stranded held projects (tests/test_registry_backstop_393.py);
5. the log claimed "passed on the clean base" for a dirty checkout, and said nothing when a
   save failed after its smoke, was cancelled, or was a DELETE waiting on the lock.

Also here, because a gate-changing save legitimately takes minutes and the fleet proxy gives a
plugin API call 20s: the outcome of every save is readable from GET /projects by the id the
editor sent.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api
from project_board import project_registry as registry
from project_board.projects_view import PROJECTS_PAGE

_LOG = "protoagent.plugins.project_board"
_ROUTE = "/api/plugins/project_board/projects"


def _git_repo(path: Path) -> Path:
    """A checkout sitting cleanly at `main` (one commit), so "at base" means something."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)
    return path.resolve()


def _apply_like_host(cfg):
    def apply_settings(patch):
        section = cfg.plugin_config.setdefault("project_board", {})
        for key, value in (patch.get("project_board") or {}).items():
            if isinstance(value, dict) and isinstance(section.get(key), dict):
                for inner, inner_value in value.items():
                    if inner_value is None:
                        section[key].pop(inner, None)
                    else:
                        section[key][inner] = inner_value
            else:
                section[key] = value
        return True, []

    return apply_settings


def _host(monkeypatch, tmp_path, projects: dict):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(tmp_path / "dev"),
        plugin_config={"project_board": {"projects": projects}},
    )
    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: cfg
    fake_plugins = types.ModuleType("graph.plugins")
    fake_plugins.__path__ = []
    fake_host = types.ModuleType("graph.plugins.host")
    fake_host.HOST = types.SimpleNamespace(apply_settings=_apply_like_host(cfg))
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "graph.plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", fake_host)
    # Locks of this test's own: an asyncio.Lock binds to the first event loop that contends
    # it, and the real ones are process-global.
    monkeypatch.setattr(registry, "_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(registry, "_SMOKE_LOCKS", {}, raising=False)
    monkeypatch.setattr(registry, "_SAVES", {}, raising=False)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return TestClient(app), cfg


def _entry(cfg, name="alpha") -> dict:
    return cfg.plugin_config["project_board"]["projects"][name]


# ── 1 + 3: what the smoke is decided on ────────────────────────────────────────────────


def test_an_entry_with_no_base_branch_is_not_resmoked_by_the_editors_save(monkeypatch, tmp_path):
    """R1: hand-written YAML often omits base_branch (the loop inherits the board default).
    The editor fills in `main` and sends the gate back, and "" != "main" read as a changed
    gate. The red suite ran on every save and refused every one, forever."""
    repo = _git_repo(tmp_path / "dev" / "alpha")
    marker = tmp_path / "gate-ran"
    gate = f'touch "{marker}"; exit 3'
    client, cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "local_gate_cmd": gate}})

    r = client.put(
        f"{_ROUTE}/alpha",
        json={"repo": str(repo), "base_branch": "main", "local_gate_cmd": gate, "repo_conventions": "new rules"},
    )

    assert r.status_code == 200, r.text
    assert not marker.exists()
    assert _entry(cfg)["repo_conventions"] == "new rules" and _entry(cfg)["local_gate_cmd"] == gate


def test_a_base_branch_only_change_does_not_smoke(monkeypatch, tmp_path):
    """R2: the smoke runs in the operator's checkout, which a base-branch edit does not
    switch. It ran the OLD branch's code, read as not-at-base, and could only wait minutes
    to say "no verdict". The loop's preflight owns a base change: the reload resets it,
    and it re-smokes against the new base before anything dispatches."""
    repo = _git_repo(tmp_path / "dev" / "alpha")
    subprocess.run(["git", "branch", "develop"], cwd=repo, check=True)
    marker = tmp_path / "gate-ran"
    gate = f'touch "{marker}"; exit 3'
    client, cfg = _host(
        monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main", "local_gate_cmd": gate}}
    )

    r = client.put(f"{_ROUTE}/alpha", json={"repo": str(repo), "base_branch": "develop", "local_gate_cmd": gate})

    assert r.status_code == 200, r.text
    assert not marker.exists(), "a base-only change must not run the gate against the old branch's checkout"
    assert _entry(cfg)["base_branch"] == "develop"


async def test_a_preserved_gate_moved_to_another_repo_is_smoked_there(monkeypatch, tmp_path):
    """R5: the rule is about the gate the entry will RUN, not only the text a call sends. The
    agent tool sends a blank gate, which keeps the operator's, and moving the project to a
    new checkout then carried that gate there unproven."""
    a = _git_repo(tmp_path / "dev" / "alpha")
    other = _git_repo(tmp_path / "dev" / "other")
    marker = tmp_path / "gate-ran"
    gate = f'touch "{marker}"; exit 3'
    _client, cfg = _host(
        monkeypatch, tmp_path, {"alpha": {"repo": str(a), "base_branch": "main", "local_gate_cmd": gate}}
    )

    out = await registry.build_register_tool({}).ainvoke({"name": "alpha", "repo": str(other)})

    assert out.startswith("Error:") and "failed on the clean base" in out
    assert marker.exists()  # it ran, in the new checkout
    assert _entry(cfg)["repo"] == str(a)  # and the move was refused


# ── 2: the smoke runs outside the registry lock ────────────────────────────────────────


async def test_a_gate_smoke_never_holds_up_another_projects_save(monkeypatch, tmp_path):
    """R4: the smoke ran under the global registry lock, so a conventions edit to ANY other
    project waited out a minutes-long suite. Saves in different checkouts now proceed while
    a smoke runs. Two smokes in ONE checkout still take turns, because two gates in one
    working tree trample each other's caches and build output."""
    a = _git_repo(tmp_path / "dev" / "alpha")
    b = _git_repo(tmp_path / "dev" / "beta")
    _client, cfg = _host(
        monkeypatch,
        tmp_path,
        {
            "alpha": {"repo": str(a), "base_branch": "main", "local_gate_cmd": "exit 0"},
            "beta": {"repo": str(b), "base_branch": "main", "local_gate_cmd": "exit 0"},
            "alpha2": {"repo": str(a), "base_branch": "main"},
        },
    )
    spans = tmp_path / "spans"
    slow = f'echo start >> "{spans}"; sleep 2; echo end >> "{spans}"; exit 0'

    smoking = asyncio.create_task(registry.upsert_project("alpha", str(a), local_gate_cmd=slow))
    await asyncio.sleep(0.4)
    started = time.monotonic()
    await registry.upsert_project("beta", str(b), repo_conventions="v2")
    assert time.monotonic() - started < 1.5, "a save in another checkout waited for alpha's gate"
    assert not smoking.done()

    # A second smoke in alpha's checkout waits its turn instead of running alongside.
    second = asyncio.create_task(registry.upsert_project("alpha2", str(a), local_gate_cmd=slow + " # alpha2"))
    await asyncio.gather(smoking, second)
    assert spans.read_text().split() == ["start", "end", "start", "end"], "two gates ran in one checkout at once"
    assert _entry(cfg, "beta")["repo_conventions"] == "v2"


async def test_a_project_changed_while_its_gate_ran_is_a_409_not_an_overwrite(monkeypatch, tmp_path):
    """With the smoke outside the lock, another save can land on the same project while it
    runs. Applying over it would silently undo that save. The smoking save is refused with a
    conflict to retry, and the other save's change stands."""
    a = _git_repo(tmp_path / "dev" / "alpha")
    client, cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(a), "base_branch": "main"}})

    smoking = asyncio.create_task(registry.upsert_project("alpha", str(a), local_gate_cmd="sleep 1.5; exit 0"))
    await asyncio.sleep(0.4)
    await registry.upsert_project("alpha", str(a), repo_conventions="landed meanwhile")

    with pytest.raises(registry.ProjectRegistryConflict, match="changed by another save"):
        await smoking
    assert _entry(cfg)["repo_conventions"] == "landed meanwhile"
    assert "local_gate_cmd" not in _entry(cfg)


def test_the_route_answers_a_conflict_with_409(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path / "dev" / "alpha")
    client, _cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main"}})

    async def conflicted(*_a, **_k):
        raise registry.ProjectRegistryConflict("project 'alpha' was changed by another save — save again")

    monkeypatch.setattr(registry, "upsert_project", conflicted)
    r = client.put(f"{_ROUTE}/alpha", json={"repo": str(repo)})
    assert r.status_code == 409 and "save again" in r.json()["detail"]


# ── the outcome survives an intermediary that gave up ───────────────────────────────────


def test_every_saves_outcome_is_readable_by_its_request_id(monkeypatch, tmp_path):
    """The fleet proxy answers a plugin API call with 504 after 20s, while the member keeps
    running a minutes-long gate. The editor reads how its save ended from GET /projects,
    matched by the id it sent: saved, or refused with the reason."""
    repo = _git_repo(tmp_path / "dev" / "alpha")
    client, _cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main"}})

    assert (
        client.put(
            f"{_ROUTE}/alpha", json={"repo": str(repo), "local_gate_cmd": "exit 0", "request_id": "r-1"}
        ).status_code
        == 200
    )
    saved = client.get(_ROUTE).json()["saves"]["alpha"]
    assert saved["id"] == "r-1" and saved["state"] == "saved" and saved["finished_at"]

    refused = client.put(f"{_ROUTE}/alpha", json={"repo": str(repo), "local_gate_cmd": "exit 4", "request_id": "r-2"})
    assert refused.status_code == 400
    record = client.get(_ROUTE).json()["saves"]["alpha"]
    assert record["id"] == "r-2" and record["state"] == "refused" and "exit 4" in record["detail"]


def test_the_editor_says_the_gate_is_running_and_recovers_a_save_the_proxy_gave_up_on():
    """The Projects editor sends a request id, says the gate is running when the save will
    run one, and when the connection gives up (502/504 or a dropped request) it reads the
    outcome from GET /projects instead of reporting a failure that has not happened."""
    assert "request_id:requestId" in PROJECTS_PAGE
    assert '"Running the gate…"' in PROJECTS_PAGE
    assert "runs its gate once on the clean base before anything is saved" in PROJECTS_PAGE
    assert "error.status === 502 || error.status === 504" in PROJECTS_PAGE
    assert "data.saves?.[name]" in PROJECTS_PAGE and "save.id !== requestId" in PROJECTS_PAGE


# ── 5: the log says what actually happened ─────────────────────────────────────────────


def test_a_green_gate_on_a_dirty_checkout_is_not_logged_as_a_clean_pass(monkeypatch, tmp_path, caplog):
    """R3: a pass on a checkout carrying the operator's edits proves nothing about the base.
    The preflight treats it as no verdict (#300), and the log said "passed on the clean base"."""
    repo = _git_repo(tmp_path / "dev" / "alpha")
    (repo / "f.txt").write_text("the operator's local edit\n")
    client, _cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main"}})

    with caplog.at_level(logging.INFO, logger=_LOG):
        assert client.put(f"{_ROUTE}/alpha", json={"repo": str(repo), "local_gate_cmd": "exit 0"}).status_code == 200

    assert "passed on the clean base" not in caplog.text
    assert "was NOT at base when it started" in caplog.text and "no verdict on the base" in caplog.text


def test_a_save_that_fails_after_its_gate_passed_is_logged(monkeypatch, tmp_path, caplog):
    """R7: the smoke logged "passed", the host then refused the write, and nothing more was
    logged. To a client that had gone, the edit looked saved."""
    repo = _git_repo(tmp_path / "dev" / "alpha")
    client, _cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main"}})
    sys.modules["graph.plugins.host"].HOST.apply_settings = lambda patch: (False, ["config write: disk full"])

    with caplog.at_level(logging.INFO, logger=_LOG):
        r = client.put(f"{_ROUTE}/alpha", json={"repo": str(repo), "local_gate_cmd": "exit 0"})

    assert r.status_code == 400
    assert "register[alpha]: not saved — config write: disk full" in caplog.text


async def test_a_save_cancelled_mid_smoke_is_logged_and_recorded(monkeypatch, tmp_path, caplog):
    repo = _git_repo(tmp_path / "dev" / "alpha")
    _client, cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": str(repo), "base_branch": "main"}})

    with caplog.at_level(logging.INFO, logger=_LOG):
        save = asyncio.create_task(registry.upsert_project("alpha", str(repo), local_gate_cmd="sleep 30"))
        await asyncio.sleep(0.4)
        save.cancel()
        with pytest.raises(asyncio.CancelledError):
            await save

    assert "register[alpha]: cancelled while running the gate on the clean base — nothing was saved" in caplog.text
    assert registry._SAVES["alpha"]["state"] == "cancelled"
    assert "local_gate_cmd" not in _entry(cfg)


async def test_a_delete_queued_behind_another_change_says_so(monkeypatch, tmp_path, caplog):
    _client, cfg = _host(monkeypatch, tmp_path, {"alpha": {"repo": "/a"}, "beta": {"repo": "/b"}})

    with caplog.at_level(logging.INFO, logger=_LOG):
        async with registry._MUTATION_LOCK:
            queued = asyncio.create_task(registry.delete_project("alpha"))
            for _ in range(100):
                if "waiting for" in caplog.text:
                    break
                await asyncio.sleep(0.02)
            assert "delete[alpha]: waiting for another project change to finish" in caplog.text
        await queued
    assert "alpha" not in cfg.plugin_config["project_board"]["projects"]

"""#393: a Projects-editor save re-ran the project's WHOLE gate, and said nothing about it.

Live on protoEngineer (2026-09-02): a `PUT /projects/protoAgent` that changed only
`repo_conventions` never answered. The editor (and the reporter's curl) round-trips every
field, so the project's unchanged `local_gate_cmd` — `python scripts/gate.py`, i.e. ruff +
lint-imports + the full pytest suite — came back as "gate text this call carries" and was
smoke-run synchronously, under the registry lock, bounded only by the 600s smoke timeout. Four
such saves queued on the lock and ran four back-to-back suites (~3m15s apart in the log). The
clients timed out at 60s/120s; uvicorn writes no access line for a response to a client that
has gone; and the smoke logged its START but never its outcome, so whether the edit landed was
unknowable from anywhere.

The fix: the smoke proves a gate against a checkout, so it runs only when THIS call changes
one of them (the command, the repo, or the base) — an unchanged gate was proven when it was
set, and the loop's preflight re-checks every project's gate after a registry change anyway.
And the edge now logs its outcome, and a call queued behind another project change says so.

These drive the REAL route through FastAPI with a fake host that has the host's merge
semantics, and a real gate command that leaves a marker file when it runs.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api
from project_board import project_registry as registry

_LOG = "protoagent.plugins.project_board"
_ROUTE = "/api/plugins/project_board/projects/alpha"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _apply_like_host(cfg):
    """``apply_settings`` with the host's semantics: a section-member map MERGES and only a
    ``None`` value removes a key (``graph.config_io.apply_updates_to_yaml``)."""

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


def _board(monkeypatch, tmp_path, *, gate: str, conventions: str = "old rules"):
    """A live host with ONE boarded project `alpha` whose explicit gate is ``gate``."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha").resolve()
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={
            "project_board": {
                "projects": {
                    "alpha": {
                        "repo": str(repo),
                        "base_branch": "main",
                        "local_gate_cmd": gate,
                        "repo_conventions": conventions,
                    }
                }
            }
        },
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
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return TestClient(app), cfg, repo


def _entry(cfg) -> dict:
    return cfg.plugin_config["project_board"]["projects"]["alpha"]


def test_a_conventions_only_save_does_not_rerun_the_unchanged_gate(monkeypatch, tmp_path):
    """The incident, exactly: the editor echoes the configured gate back and changes only the
    conventions. The gate is RED on this checkout and leaves a marker when it runs — so the old
    code both ran it (the minutes-long "hang") and let a red suite refuse an unrelated
    conventions update, the very thing upsert's own comment says it must not do."""
    marker = tmp_path / "gate-ran"
    gate = f'touch "{marker}"; exit 3'
    client, cfg, repo = _board(monkeypatch, tmp_path, gate=gate)

    r = client.put(
        _ROUTE,
        json={
            "repo": str(repo),
            "base_branch": "main",
            "local_gate_cmd": gate,  # echoed back unchanged, as the editor always does
            "repo_conventions": "kind = added|changed|fixed|removed|deprecated|security|docs",
        },
    )

    assert r.status_code == 200, r.text
    assert not marker.exists(), "the unchanged gate was re-run by a save that did not change it"
    assert _entry(cfg)["repo_conventions"] == "kind = added|changed|fixed|removed|deprecated|security|docs"
    assert _entry(cfg)["local_gate_cmd"] == gate  # preserved, not cleared


def test_a_save_that_changes_the_gate_or_its_checkout_is_still_smoked(monkeypatch, tmp_path):
    """The rule is "smoke what this call CHANGES", not "never smoke on save" — #261 stands for
    new gate text, and for an old gate pointed at a checkout it was never proven in."""
    ran = tmp_path / "old-gate-ran"
    old_gate = f'touch "{ran}"; exit 0'
    client, cfg, repo = _board(monkeypatch, tmp_path, gate=old_gate)
    body = {"repo": str(repo), "base_branch": "main", "local_gate_cmd": old_gate, "repo_conventions": "v2"}

    # Unchanged gate → no smoke (this half is what the old code got wrong).
    assert client.put(_ROUTE, json=body).status_code == 200
    assert not ran.exists()

    # NEW gate text → smoked on the clean base; a red one refuses and persists nothing.
    new_ran = tmp_path / "new-gate-ran"
    red_gate = f'touch "{new_ran}"; echo the-new-gate-is-broken; exit 3'
    r = client.put(_ROUTE, json={**body, "local_gate_cmd": red_gate, "repo_conventions": "v3"})
    assert r.status_code == 400 and "failed on the clean base" in r.json()["detail"]
    assert new_ran.exists()
    assert _entry(cfg)["local_gate_cmd"] == old_gate and _entry(cfg)["repo_conventions"] == "v2"

    # The SAME gate re-pointed at another checkout → smoked there, before it persists.
    other = _git_repo(tmp_path / "dev" / "alpha-clone").resolve()
    assert client.put(_ROUTE, json={**body, "repo": str(other)}).status_code == 200
    assert ran.exists(), "a gate moved to a checkout it was never proven in must be smoked there"
    assert _entry(cfg)["repo"] == str(other)


def test_the_save_logs_its_outcome_not_just_its_start(monkeypatch, tmp_path, caplog):
    """A client that times out never sees the response, and uvicorn logs no access line for
    it — so the plugin's own log is the only place the outcome can be read. It used to say
    "smoking the gate…" and then nothing, whether the edit landed or was refused."""
    client, cfg, repo = _board(monkeypatch, tmp_path, gate="exit 0")
    body = {"repo": str(repo), "base_branch": "main", "repo_conventions": "v2"}

    with caplog.at_level(logging.INFO, logger=_LOG):
        assert client.put(_ROUTE, json={**body, "local_gate_cmd": "true"}).status_code == 200
    assert "register[alpha]: gate smoke passed on the clean base" in caplog.text
    assert "register[alpha]: updated" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOG):
        assert client.put(_ROUTE, json={**body, "local_gate_cmd": "exit 5"}).status_code == 400
    assert "register[alpha]: gate FAILED on the clean base (exit 5) — refusing" in caplog.text
    assert "register[alpha]: updated" not in caplog.text


async def test_a_change_queued_behind_another_says_so(monkeypatch, tmp_path, caplog):
    """A retry during a long smoke waited on the registry lock in silence, then ran its own
    smoke — the four stacked suites in the incident log. The wait itself is now visible."""
    _client, cfg, repo = _board(monkeypatch, tmp_path, gate="exit 0")
    # A lock of this test's own: an asyncio.Lock binds to the first event loop that
    # CONTENDS it, and the real one is process-global — contending it here would bind it
    # to this test's loop and break the next test that contends it from another.
    lock = asyncio.Lock()
    monkeypatch.setattr(registry, "_MUTATION_LOCK", lock)

    with caplog.at_level(logging.INFO, logger=_LOG):
        async with lock:  # another project change is in flight
            queued = asyncio.create_task(registry.upsert_project("alpha", str(repo), repo_conventions="v2"))
            for _ in range(100):
                if "waiting for" in caplog.text or queued.done():
                    break
                await asyncio.sleep(0.02)
            assert not queued.done(), "the queued change must wait for the lock, not bypass it"
            assert "register[alpha]: waiting for another project change to finish" in caplog.text
        result = await queued

    assert result["project"] == "alpha" and _entry(cfg)["repo_conventions"] == "v2"

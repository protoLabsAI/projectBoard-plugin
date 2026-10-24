"""Host-free contracts for the shared scalar settings boundary."""

import copy
import json
import sys
import types
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, settings_api as settings
from project_board.loop._common import LIVE_KNOBS


@pytest.fixture
def host(monkeypatch):
    live = copy.deepcopy(settings._DEFAULTS)
    saved = []
    monkeypatch.setattr(settings, "_boot_values", None)
    settings.capture_boot_settings(live)
    monkeypatch.setattr(settings, "_live_section", lambda **kw: copy.deepcopy(live))

    def apply(compute):
        patch = compute(types.SimpleNamespace(plugin_config={"project_board": live}))
        saved.append(copy.deepcopy(patch))
        live.update(patch["project_board"])
        return True, []

    stub = types.SimpleNamespace(apply_settings=apply)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", types.SimpleNamespace(HOST=stub))
    return live, saved, stub


def test_allowlist_metadata_and_masking(host):
    live, _, _ = host
    manifest = yaml.safe_load(Path(settings.__file__).with_name("protoagent.plugin.yaml").read_text())
    # `loop_enabled` OUTLIVES its manifest settings entry (#611): the Overview tab owns its
    # switch, so it was dropped from the generic schema UI, but settings_api keeps it in the
    # allowlist by hand so PUT /settings + pending_restart still cover it. Every OTHER key is
    # still derived 1:1 from the manifest.
    assert set(settings.SETTINGS) == {entry["key"] for entry in manifest["settings"]} | {"loop_enabled"}
    for entry in manifest["settings"]:
        for field in ("label", "type", "minimum", "maximum", "options"):
            assert settings.SETTINGS[entry["key"]][field] == entry.get(field)
    assert len(settings.SETTINGS) == 30  # 29 manifest settings + the hand-added loop_enabled.
    live["webhook_secret"] = "never-echo-this"
    settings.capture_boot_settings(live)
    live["npm_token"] = "nor-this"
    snapshot = settings.settings_snapshot()
    assert set(snapshot["settings"]) == set(settings.SETTINGS)
    for key, entry in snapshot["settings"].items():
        assert {"value", "boot_value", "type", "minimum", "maximum", "options", "restart"} <= entry.keys()
        assert entry["restart"] == (key not in LIVE_KNOBS)
    assert snapshot["settings"]["webhook_secret"]["value"] == {"set": True}
    assert snapshot["settings"]["npm_token"]["boot_value"] == {"set": False}
    assert "never-echo-this" not in json.dumps(snapshot)
    assert "nor-this" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "update",
    [
        {"not_a_setting": 1},
        {"max_concurrent": True},
        {"max_concurrent": "2"},
        {"max_concurrent": 1.5},
        {"max_concurrent": 0},
        {"max_concurrent": 65},
        {"auto_merge": 1},
        {"coder": None},
        {"webhook_secret": {}},
        {"merge_method": "bad"},
        [],
    ],
)
async def test_refuses_entire_batch(host, update):
    _, writes, _ = host
    batch = {"coder": "valid", **update} if isinstance(update, dict) else update
    result = await settings.apply_setting_updates(batch, "alice")
    assert not result["ok"] and result["reason"]
    assert result["saved"] == []
    assert writes == []


async def test_saved_pending_revert_and_reboot(host, caplog):
    live, writes, _ = host
    old = live["review_fix_max"]
    with caplog.at_level("INFO"):
        result = await settings.apply_setting_updates({"review_fix_max": old + 1, "npm_token": "secret-value"}, "alice")
    assert result["ok"]
    assert result["saved"] == ["review_fix_max", "npm_token"]
    assert result["restart_required"] == ["review_fix_max", "npm_token"]
    assert {p["key"] for p in result["pending_restart"]} == {"review_fix_max", "npm_token"}
    assert all(p["label"] for p in result["pending_restart"])
    assert len(writes) == 1
    assert writes[0] == {"project_board": {"review_fix_max": old + 1, "npm_token": "secret-value"}}
    assert "alice" in caplog.text and "npm_token" in caplog.text
    assert "secret-value" not in caplog.text + json.dumps(result)
    assert settings.settings_snapshot()["settings"]["review_fix_max"]["boot_value"] == old
    result = await settings.apply_setting_updates({"review_fix_max": old}, "alice")
    assert [p["key"] for p in result["pending_restart"]] == ["npm_token"]
    settings.capture_boot_settings(live)  # New router/process boots with the saved config.
    assert settings.settings_snapshot()["pending_restart"] == []


async def test_every_live_knob_never_pending(host):
    live, _, _ = host
    updates = {}
    for key in LIVE_KNOBS:
        value = live[key]
        updates[key] = not value if type(value) is bool else value + 1 if type(value) is int else "new-coder"
    result = await settings.apply_setting_updates(updates, "alice")
    assert result["ok"]
    assert result["restart_required"] == result["pending_restart"] == []
    assert settings.settings_snapshot()["pending_restart"] == []


async def test_host_refusal_does_not_echo_secrets(host):
    _, writes, stub = host
    stub.apply_settings = lambda compute: (False, ["secret-value"])
    result = await settings.apply_setting_updates({"npm_token": "secret-value"}, "alice")
    assert not result["ok"] and result["saved"] == []
    assert "secret-value" not in json.dumps(result)
    assert not writes


def test_operator_router(host, caplog):
    live, writes, _ = host
    app = FastAPI()

    @app.middleware("http")
    async def subject(request, call_next):
        request.state.subject = "operator-alice"
        return await call_next(request)

    app.include_router(api.build_data_router(live), prefix="/api/plugins/project_board")
    client = TestClient(app)
    url = "/api/plugins/project_board/settings"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()["settings"]) == set(settings.SETTINGS)
    with caplog.at_level("INFO"):
        response = client.put(url, json={"max_concurrent": 3})
    assert response.status_code == 200 and response.json()["saved"] == ["max_concurrent"]
    assert "operator-alice" in caplog.text
    assert client.put(url, json={"max_concurrent": 0}).status_code == 400
    assert len(writes) == 1
    assert client.get("/plugins/project_board/settings").status_code == 404


async def test_secret_rotation_compares_values_not_presence(host):
    live, _, _ = host
    live["webhook_secret"] = "old-secret"
    settings.capture_boot_settings(live)
    result = await settings.apply_setting_updates({"webhook_secret": "new-secret"}, "alice")
    assert [item["key"] for item in result["pending_restart"]] == ["webhook_secret"]
    entry = settings.settings_snapshot()["settings"]["webhook_secret"]
    assert entry["value"] == entry["boot_value"] == {"set": True}


@pytest.mark.parametrize("raises", [False, True])
async def test_host_unavailable_or_exception(host, raises):
    _, writes, stub = host

    def fail(compute):
        raise RuntimeError("sensitive-host-diagnostic")

    stub.apply_settings = fail if raises else None
    result = await settings.apply_setting_updates({"npm_token": "token"}, "alice")
    assert not result["ok"] and result["saved"] == []
    assert "sensitive-host-diagnostic" not in json.dumps(result)
    assert not writes

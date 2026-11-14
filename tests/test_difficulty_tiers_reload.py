"""The difficulty→tier map is CHANGEABLE without a plugin update (bd-l8sqf).

bd-xqypj added the board-level ``difficulty_tiers`` config key and validated it at store
construction. This follow-up makes the mapping fully operable at runtime:

* `r1` it is read on config RELOAD (``POST /api/config`` → ``BoardLoop.reload``) and applies
  to the NEXT dispatch — the shared cached board is reconfigured in place, no restart.
* `r2` a ``projects.<name>.difficulty_tiers`` entry wins over the board-level map for that
  project's cards, and is re-resolved live when the project map reloads.
* `r3` the manifest config schema carries the default map and the Settings page exposes it as
  one picker per difficulty, folded into the one nested key by ``settings_api``.

The reload/dispatch reads go through the real ``get_store`` seam the loop dispatches on (``br``
PATH stubbed, ``_run``/``_require`` faked), so they prove the config value survives the
``(db, repo, base_branch)``-keyed board cache — not just ``BeadsBoard(difficulty_tiers=...)``.
"""

from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest
import yaml

from project_board import settings_api as settings
from project_board import store

DEFAULT_MAP = {"small": "standard", "medium": "standard", "large": "reasoning", "architectural": "advanced"}


def _manifest() -> dict:
    return yaml.safe_load(Path(settings.__file__).with_name("protoagent.plugin.yaml").read_text())


def _feature(fid="bd-1", *, difficulty="medium", project="", labels=None):
    f = {"id": fid, "difficulty": difficulty, "labels": list(labels or [])}
    if project:
        f["project"] = project
    return f


# ── r1: the board-level map is live on reload ────────────────────────────────────


def test_reload_applies_new_difficulty_tiers_to_next_dispatch(monkeypatch):
    """A config reload with a new mapping updates the shared board in place, so the NEXT
    dispatch of a medium card starts on the new tier — the whole point of the card. Proved
    end-to-end through the real get_store the loop's dispatch reads (store.current_tier)."""
    from project_board.loop import BoardLoop

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    monkeypatch.setattr(store, "_BOARDS", {})

    loop = BoardLoop({})  # default map: medium → standard
    board = store.get_store(**loop._store_kw)  # construct + cache the board the loop dispatches on
    monkeypatch.setattr(board, "_run", lambda *a, **k: None)
    monkeypatch.setattr(board, "_require", lambda fid: _feature(fid, difficulty="medium"))
    assert board.current_tier("bd-1") == "standard"

    changed = loop.reload({"difficulty_tiers": {"medium": "reasoning"}})

    assert changed["difficulty_tiers"] == (None, {"medium": "reasoning"})
    assert loop._store() is board  # same cached object, not a rebuild
    assert board.current_tier("bd-1") == "reasoning"  # the next dispatch climbs to Opus first
    assert board.initial_tier("bd-1") == "reasoning"
    assert loop.cfg["difficulty_tiers"] == {"medium": "reasoning"}
    assert loop._store_kw["difficulty_tiers"] == {"medium": "reasoning"}


def test_reload_with_a_malformed_map_keeps_the_current_one(monkeypatch, caplog):
    """A typo in Settings must not stall the loop: a malformed difficulty_tiers logs a warning
    and leaves the running map untouched."""
    from project_board.loop import BoardLoop

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    monkeypatch.setattr(store, "_BOARDS", {})

    loop = BoardLoop({"difficulty_tiers": {"medium": "reasoning"}})
    board = store.get_store(**loop._store_kw)
    monkeypatch.setattr(board, "_run", lambda *a, **k: None)
    monkeypatch.setattr(board, "_require", lambda fid: _feature(fid, difficulty="medium"))

    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert loop.reload({"difficulty_tiers": {"medium": "superbrain"}}) == {}
    assert "difficulty_tiers is malformed" in caplog.text
    assert board.current_tier("bd-1") == "reasoning"  # unchanged
    assert loop.cfg["difficulty_tiers"] == {"medium": "reasoning"}


def test_reconfigure_cached_store_updates_difficulty_tiers_in_place(monkeypatch):
    """The store seam the reload uses: an existing cached board gets the new map, a board that
    was never constructed is reported absent (the loop then builds it from store_kw)."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    board = store.BeadsBoard(repo="/instance")
    key = ("/inst/project_board/.beads/beads.db", "/instance", "main")
    monkeypatch.setitem(store._BOARDS, key, board)
    assert board.difficulty_tiers["medium"] == "standard"

    assert store.reconfigure_cached_store(repo="/instance", difficulty_tiers={"medium": "reasoning"})
    assert board.difficulty_tiers["medium"] == "reasoning"
    assert not store.reconfigure_cached_store(repo="/not-cached", difficulty_tiers={"medium": "advanced"})


def test_difficulty_tiers_reconfigure_leaves_the_project_map_intact(monkeypatch):
    """The two reload knobs are independent: a difficulty_tiers reconfigure must not wipe the
    live project routing map (and the project reload does not touch the tier map)."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    board = store.BeadsBoard(repo="/instance", projects={"web": {"repo": "/web"}}, default_project="web")
    key = ("/inst/project_board/.beads/beads.db", "/instance", "main")
    monkeypatch.setitem(store._BOARDS, key, board)

    assert store.reconfigure_cached_store(repo="/instance", difficulty_tiers={"large": "advanced"})
    assert board.difficulty_tiers["large"] == "advanced"
    assert board.projects == {"web": {"repo": "/web"}}  # untouched
    assert board.default_project == "web"


def test_board_level_reload_reaches_default_project_cards_when_cfg_carried_the_key(monkeypatch):
    """Regression (bd-l8sqf): when the boot cfg carries a flat `difficulty_tiers`, resolve_projects
    lifts that map onto the synthesized IMPLICIT default project. It must NOT be pinned as a
    per-project override, or it would shadow the LIVE board-level map and a reload would never reach
    the default project's cards — nor unlabeled cards, which resolve through that project."""
    from project_board.projects import resolve_projects

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    cfg = {"repo": "/repo", "difficulty_tiers": {"medium": "standard"}}
    b = store.BeadsBoard(
        db=None,
        repo="/repo",
        difficulty_tiers=cfg["difficulty_tiers"],
        projects=resolve_projects(cfg),  # the implicit "default" entry carries difficulty_tiers
        default_project="default",
    )
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    assert b._project_difficulty_tiers == {}  # the implicit project is not a per-project pin

    for require in (
        lambda fid: _feature(fid, difficulty="medium"),  # unlabeled → default project
        lambda fid: _feature(fid, difficulty="medium", project="default"),  # the default project
    ):
        monkeypatch.setattr(b, "_require", require)
        assert b.initial_tier("x") == "standard"

    b.reconfigure_difficulty_tiers({"medium": "reasoning"})  # a live board-level reload

    for require in (
        lambda fid: _feature(fid, difficulty="medium"),
        lambda fid: _feature(fid, difficulty="medium", project="default"),
    ):
        monkeypatch.setattr(b, "_require", require)
        assert b.initial_tier("x") == "reasoning"  # the reload reaches default/unlabeled cards


async def test_api_store_resolves_difficulty_tiers_live_on_the_cached_board(monkeypatch):
    """The data-router store() path resolves `difficulty_tiers` live like project routing (bd-l8sqf):
    a store touch after a reload reconfigures the shared cached board in place, so the /ci escalation
    path starts the next card on the operator's current mapping, not the boot value."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from project_board import api, project_registry

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    monkeypatch.setattr(store, "_BOARDS", {})

    cfg = {"repo": "/instance"}  # boot cfg carries no difficulty_tiers → the default map
    board = store.BeadsBoard(repo="/instance", difficulty_tiers=cfg.get("difficulty_tiers"))
    key = ("/inst/project_board/.beads/beads.db", "/instance", "main")
    monkeypatch.setitem(store._BOARDS, key, board)
    monkeypatch.setattr(board, "_run", lambda *a, **k: None)
    monkeypatch.setattr(board, "get_feature", lambda fid: _feature(fid, difficulty="medium"))
    monkeypatch.setattr(api, "annotate_next_action", lambda feats, _cfg: feats)
    # what the operator saved since boot: medium now starts on reasoning.
    monkeypatch.setattr(project_registry, "_live_section", lambda **kw: {"difficulty_tiers": {"medium": "reasoning"}})
    assert board.difficulty_tiers["medium"] == "standard"

    app = FastAPI()
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/plugins/project_board/features/bd-1")

    assert resp.status_code == 200, resp.text
    assert board.difficulty_tiers["medium"] == "reasoning"  # the cached board picked up the live map


# ── r2: per-project override wins, and is live ───────────────────────────────────


def test_per_project_override_wins_over_board_level(monkeypatch):
    """A project's own difficulty_tiers decides its cards' starting rung; a project without one
    inherits the board-level map; an unlabeled card takes the default project's view."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = store.BeadsBoard(
        db=None,
        repo="/repo",
        difficulty_tiers={"medium": "reasoning"},  # board level: medium → reasoning
        projects={
            "web": {"repo": "/web", "difficulty_tiers": {"medium": "advanced"}},  # wins for web's cards
            "api": {"repo": "/api"},  # inherits the board-level map
        },
        default_project="web",
    )
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)

    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="web"))
    assert b.initial_tier("x") == "advanced"  # web's override

    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="api"))
    assert b.initial_tier("x") == "reasoning"  # board-level (api declared none)

    # web's override names only `medium`; a large card there still falls back to the default.
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="large", project="web"))
    assert b.initial_tier("x") == "reasoning"

    # an unlabeled card takes the default project (web) → its override.
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium"))
    assert b.initial_tier("x") == "advanced"


def test_empty_per_project_override_inherits_and_follows_board_reloads(monkeypatch):
    """Regression (bd-l8sqf): a project declaring an EMPTY `difficulty_tiers: {}` inherits the live
    board-level map rather than being pinned to the resolved default — so it stays out of the
    per-project map and a later board-level reload still reaches its cards (pinning `{}` to the
    default would freeze the project against reloads, contrary to the docs' "inherits" wording)."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = store.BeadsBoard(
        db=None,
        repo="/repo",
        difficulty_tiers={"medium": "reasoning"},  # board level: medium → reasoning
        projects={"web": {"repo": "/web", "difficulty_tiers": {}}},  # empty → inherit, do not pin
        default_project="web",
    )
    assert "web" not in b._project_difficulty_tiers  # empty map is not a per-project pin
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="web"))
    assert b.initial_tier("x") == "reasoning"  # inherits the live board-level map

    b.reconfigure_difficulty_tiers({"medium": "advanced"})  # a live board-level reload
    assert b.initial_tier("x") == "advanced"  # the reload reaches web's cards (would be frozen if pinned)


def test_per_project_override_climbs_the_same_ladder(monkeypatch):
    """The override only moves the STARTING rung; escalation is unchanged, so a per-project
    medium card that fails still advances up TIER_LADDER."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = store.BeadsBoard(
        db=None,
        repo="/repo",
        projects={"web": {"repo": "/web", "difficulty_tiers": {"medium": "standard"}}},
        default_project="web",
    )
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    monkeypatch.setattr(b, "comment", lambda *a, **k: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: _feature(fid, difficulty="medium", project="web"))
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="web", labels=[]))
    assert b.current_tier("x") == "standard"
    assert b.escalate("x", "gate failed") == "reasoning"


def test_reconfigure_projects_reresolves_the_per_project_override(monkeypatch):
    """Editing a project's difficulty_tiers reaches the running board: reconfigure_projects
    re-resolves the per-project map, so the next dispatch honors the new override — no rebuild."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = store.BeadsBoard(db=None, repo="/repo", projects={"web": {"repo": "/web"}}, default_project="web")
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="web"))
    assert b.initial_tier("x") == "standard"  # no override yet → default

    b.reconfigure_projects({"web": {"repo": "/web", "difficulty_tiers": {"medium": "reasoning"}}}, "web")
    assert b.initial_tier("x") == "reasoning"  # live, no rebuild


def test_a_bad_per_project_override_is_rejected_at_construction(monkeypatch):
    """A per-project typo fails fast at load, like the board-level key — not at the dispatch of
    that project's first card."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    with pytest.raises(store.BoardError, match="project 'web' difficulty_tiers"):
        store.BeadsBoard(db=None, repo="/repo", projects={"web": {"repo": "/web", "difficulty_tiers": {"huge": "x"}}})


def test_projects_resolution_carries_difficulty_tiers():
    """`difficulty_tiers` is a per-project setting, so resolve_projects keeps it on the entry
    the store reads (otherwise the override would be silently dropped at config load)."""
    from project_board.projects import is_project_setting, resolve_projects

    assert is_project_setting("difficulty_tiers")
    resolved = resolve_projects({"projects": {"web": {"repo": "/web", "difficulty_tiers": {"medium": "advanced"}}}})
    assert resolved["web"]["difficulty_tiers"] == {"medium": "advanced"}


def test_resolve_projects_rejects_a_bad_per_project_override():
    """Regression (bd-l8sqf): resolve_projects validates a project's difficulty_tiers at config
    RESOLUTION, so the guarded reload/boot regions (api.store(), drive.reload) that resolve the
    routing unit inside a try catch a malformed map and keep the last good one — rather than passing
    it through to board construction/reconfigure, where it would raise out of a live board route."""
    from project_board.projects import resolve_projects

    with pytest.raises(ValueError, match="project 'web' difficulty_tiers"):
        resolve_projects({"projects": {"web": {"repo": "/web", "difficulty_tiers": {"huge": "x"}}}})
    # a VALID override still resolves — the validation is not over-eager.
    ok = resolve_projects({"projects": {"web": {"repo": "/web", "difficulty_tiers": {"medium": "advanced"}}}})
    assert ok["web"]["difficulty_tiers"] == {"medium": "advanced"}


def test_reconfigure_projects_keeps_the_last_good_map_on_a_bad_per_project_override(monkeypatch):
    """Regression (bd-l8sqf): reconfigure_projects validates the per-project tier overrides BEFORE
    mutating any shared attribute, so a malformed projects[].difficulty_tiers leaves the live board
    whole — projects, default_project AND the per-project map — not a new `projects` half-applied
    over a stale tier map. Every board route keeps answering on the last good routing unit."""
    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = store.BeadsBoard(
        db=None,
        repo="/repo",
        projects={"web": {"repo": "/web", "difficulty_tiers": {"medium": "advanced"}}},
        default_project="web",
    )
    assert b._project_difficulty_tiers["web"]["medium"] == "advanced"

    with pytest.raises(store.BoardError, match="project 'web' difficulty_tiers"):
        b.reconfigure_projects({"web": {"repo": "/web2", "difficulty_tiers": {"huge": "x"}}}, "web")

    # nothing applied: the board still routes with the OLD map, not a half-updated one.
    assert b.projects == {"web": {"repo": "/web", "difficulty_tiers": {"medium": "advanced"}}}
    assert b.default_project == "web"
    assert b._project_difficulty_tiers["web"]["medium"] == "advanced"
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", project="web"))
    assert b.initial_tier("x") == "advanced"  # still the last good override


# ── r3: manifest schema + the Settings pickers ───────────────────────────────────


def test_manifest_config_schema_declares_the_default_map():
    assert _manifest()["config"]["difficulty_tiers"] == DEFAULT_MAP
    # The manifest default (the Settings picker's fallback) and the store's DIFFICULTY_TIER
    # constant (its own fallback) must not drift apart — pin them to the same map.
    assert store.DIFFICULTY_TIER == DEFAULT_MAP


def test_manifest_exposes_one_picker_per_difficulty():
    """The Settings page shows difficulty_tiers as a picker per card size, choices = the ladder's
    rungs, each with a one-line help text (the plugin settings UX standard)."""
    entries = {e["key"]: e for e in _manifest()["settings"] if str(e.get("key", "")).startswith("difficulty_tier_")}
    assert set(entries) == {
        "difficulty_tier_small",
        "difficulty_tier_medium",
        "difficulty_tier_large",
        "difficulty_tier_architectural",
    }
    for entry in entries.values():
        assert entry["type"] == "select"
        assert entry["options"] == store.TIER_LADDER  # the configured tier names
        assert entry["description"].strip()  # one-line help text
        assert "restart" not in entry  # live, like br_autofetch


@pytest.fixture
def settings_host(monkeypatch):
    """A host-free Settings boundary over a live config seeded from the manifest defaults."""
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
    return live, saved


def test_pickers_read_from_the_nested_map_and_report_live(settings_host):
    snap = settings.settings_snapshot()["settings"]
    assert snap["difficulty_tier_medium"]["value"] == "standard"
    assert snap["difficulty_tier_large"]["value"] == "reasoning"
    # live-via-reload: never pending-restart, even though they are not scalar LIVE_KNOBS.
    assert snap["difficulty_tier_medium"]["restart"] is False
    assert settings.settings_snapshot()["pending_restart"] == []


async def test_saving_a_picker_folds_into_difficulty_tiers_and_is_live(settings_host):
    live, saved = settings_host
    result = await settings.apply_setting_updates({"difficulty_tier_medium": "reasoning"}, "alice")

    assert result["ok"]
    assert result["restart_required"] == [] and result["pending_restart"] == []
    patched = saved[-1]["project_board"]["difficulty_tiers"]
    assert patched["medium"] == "reasoning"  # the edited difficulty
    assert patched["large"] == "reasoning" and patched["small"] == "standard"  # the others preserved
    assert live["difficulty_tiers"]["medium"] == "reasoning"
    assert settings.settings_snapshot()["settings"]["difficulty_tier_medium"]["value"] == "reasoning"


async def test_a_picker_value_outside_the_ladder_is_refused(settings_host):
    _, saved = settings_host
    result = await settings.apply_setting_updates({"difficulty_tier_small": "superbrain"}, "alice")
    assert not result["ok"] and result["saved"] == []
    assert saved == []  # nothing crossed the host save boundary

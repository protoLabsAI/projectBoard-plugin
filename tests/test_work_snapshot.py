"""The board's contribution to the host's ``<working_state>`` block (protoAgent ADR 0079).

The gap: the host injects the agent's live commitments every turn from four CORE stores, so
the board — the whole job, for a PM agent — was invisible there. An agent could report
itself idle while one of its own cards sat stalled.

The constraint that shapes the design: the host calls a provider INLINE ON EVERY TURN, and
every board read is a ``br`` subprocess. So the provider must never touch the store; the
loop publishes a snapshot on its sweep and the provider returns it from memory. These tests
pin both halves, plus the feature detection that keeps this safe on a host too old to have
the seam at all.
"""

from __future__ import annotations

import project_board
from project_board import work_snapshot


def _feature(fid, state, title="A card", hint=""):
    return {"id": fid, "board_state": state, "title": title, "next_action_hint": hint}


def setup_function():
    work_snapshot.reset()


def teardown_function():
    work_snapshot.reset()


# ── what the snapshot keeps ──────────────────────────────────────────────────


def test_only_live_states_are_published():
    """`done`/`cancelled` are finished and `backlog` has not passed the Ready gate — none of
    them is something the agent is on the hook for right now."""
    work_snapshot.publish(
        [
            _feature("bd-ready", "ready"),
            _feature("bd-prog", "in_progress"),
            _feature("bd-rev", "in_review"),
            _feature("bd-block", "blocked"),
            _feature("bd-done", "done"),
            _feature("bd-cancel", "cancelled"),
            _feature("bd-backlog", "backlog"),
        ]
    )
    assert {i["id"] for i in work_snapshot.provider()} == {"bd-ready", "bd-prog", "bd-rev", "bd-block"}


def test_blocked_cards_sort_first():
    """A card that cannot clear itself is the one the agent most needs to see, and the host
    caps the list — so ordering decides what survives the trim."""
    work_snapshot.publish(
        [
            _feature("bd-r", "ready"),
            _feature("bd-p", "in_progress"),
            _feature("bd-v", "in_review"),
            _feature("bd-b", "blocked"),
        ]
    )
    assert [i["state"] for i in work_snapshot.provider()] == ["blocked", "in_review", "in_progress", "ready"]


def test_the_boards_own_next_action_hint_is_reused_verbatim():
    """Rather than inventing a second phrasing for 'what unsticks this'."""
    work_snapshot.publish([_feature("bd-1", "in_progress", hint="awaiting agent — board_deliver(bd-1, text=…)")])
    assert work_snapshot.provider()[0]["hint"] == "awaiting agent — board_deliver(bd-1, text=…)"


def test_the_snapshot_is_capped():
    work_snapshot.publish([_feature(f"bd-{i}", "ready") for i in range(50)])
    assert len(work_snapshot.provider()) == work_snapshot.MAX_ITEMS


def test_a_malformed_publish_never_raises():
    """The sweep calls this; a bad snapshot must not take the sweep down with it."""
    work_snapshot.publish([{"no": "board_state"}, None, 42])
    assert work_snapshot.provider() == []


def test_one_malformed_row_costs_only_that_row():
    """Per-item, not all-or-nothing. Building the list inside one try meant a single bad row
    aborted the whole update — the good cards alongside it were lost too."""
    work_snapshot.publish([_feature("bd-ok", "ready"), None, 42, _feature("bd-ok2", "blocked")])
    assert [i["id"] for i in work_snapshot.provider()] == ["bd-ok2", "bd-ok"]


def test_a_malformed_publish_does_not_silently_serve_a_STALE_snapshot():
    """The failure mode that made the all-or-nothing version dangerous: `_SNAPSHOT` kept its
    PREVIOUS value, so the agent went on being shown a board that no longer existed with
    nothing to signal it. A publish that finds nothing usable must clear, not freeze."""
    work_snapshot.publish([_feature("bd-old", "ready")])
    assert [i["id"] for i in work_snapshot.provider()] == ["bd-old"]
    work_snapshot.publish([None, 42, {"no": "board_state"}])
    assert work_snapshot.provider() == []


def test_provider_returns_a_copy_so_a_caller_cannot_mutate_the_snapshot():
    work_snapshot.publish([_feature("bd-1", "ready")])
    work_snapshot.provider().clear()
    assert len(work_snapshot.provider()) == 1


def test_reset_stops_a_stopped_board_advertising_work():
    work_snapshot.publish([_feature("bd-1", "ready")])
    work_snapshot.reset()
    assert work_snapshot.provider() == []


# ── the provider contract: no I/O ────────────────────────────────────────────


def test_the_provider_never_touches_the_store(monkeypatch):
    """The property the whole design turns on. It runs inline on EVERY agent turn, and every
    board read is a `br` subprocess — so if this ever reaches the store, every turn pays for
    a subprocess. Fail loudly if someone 'simplifies' it into a live read."""
    from project_board import store as store_mod

    called = []
    # `_run` IS the blocking seam (the `br` subprocess); `get_store` is the only way to
    # reach it. Trip either and the provider is doing I/O on the agent's turn.
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: called.append("get_store"), raising=True)
    monkeypatch.setattr(store_mod.BeadsBoard, "_run", lambda *_a, **_kw: called.append("br"), raising=True)
    work_snapshot.publish([_feature("bd-1", "ready")])
    assert work_snapshot.provider() == [{"id": "bd-1", "title": "A card", "state": "ready", "hint": ""}]
    assert called == []


# ── registration is feature-detected ─────────────────────────────────────────


class _Registry:
    """The host-free registry stand-in `register()` runs against (mirrors the one in
    test_packaging). ``with_seam`` decides whether this host has ADR 0079's work-provider
    seam at all — the whole point of the feature detection under test."""

    def __init__(self, with_seam: bool):
        self.config = {"coder": "proto"}
        self.tools, self.routers, self.surfaces = [], [], []
        self.subagents, self.skill_dirs = [], []
        self.surface_reloads = {}
        self.work_providers = {}
        if with_seam:
            self.register_work_provider = self._register_work_provider

    def register_tool(self, t):
        self.tools.append(t)

    def register_router(self, router, prefix):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None, reload=None):
        self.surfaces.append(name)
        self.surface_reloads[name] = reload

    def register_subagent(self, config):
        self.subagents.append(config)

    def register_skill_dir(self, path):
        self.skill_dirs.append(path)

    def _register_work_provider(self, name, fn, label=""):
        self.work_providers[name] = (fn, label)


def test_register_contributes_the_board_as_a_work_provider():
    """Through the REAL `register()`, not a hand-rolled call — otherwise this asserts only
    that the stand-in works."""
    reg = _Registry(with_seam=True)
    project_board.register(reg)
    fn, label = reg.work_providers["cards"]
    assert fn is work_snapshot.provider
    assert label == "Open board cards"


def test_register_is_a_no_op_on_a_host_without_the_seam():
    """MERGED is not RELEASED: this seam is newer than the plugin's declared host floor, so
    a host predating it must still load the plugin cleanly and simply surface nothing."""
    reg = _Registry(with_seam=False)
    project_board.register(reg)  # must not raise
    assert reg.work_providers == {}
    # and the rest of registration still happened
    assert "project-board-loop" in reg.surfaces

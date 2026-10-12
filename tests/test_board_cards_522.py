"""Board cards in chat (#522): the `board-cards` frame component.

Host-free: the validator (props are POINTERS only), the feature-detected registration
(an older core gets one setup gap and text-only cards), the tool's output (a text summary
plus the component tail), the manifest/route wiring the frame needs, and — under node —
the page logic (tests/node/board_cards.test.cjs: render, refresh scheduling, button set
per state, exact proposal texts, refused/unknown states, the older-core fallback).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import project_board as pb
from project_board import board_cards as bc

ROOT = Path(__file__).resolve().parent.parent

# The core's component-v1 sentinel (graph/components.py) — mirrored so the tail can be
# decoded without a host.
_SENTINEL = "\x1e[component-v1]"


def _encode(component, props):
    return _SENTINEL + json.dumps({"component": component, "props": props}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _reset_emit(monkeypatch):
    monkeypatch.setattr(bc, "EMIT", False)  # registration flips a module global — don't leak it


@pytest.fixture
def emit(monkeypatch):
    """A host that took the kind: EMIT on, and graph.sdk.encode_component available."""
    monkeypatch.setattr(sys.modules["graph.sdk"], "encode_component", _encode, raising=False)
    monkeypatch.setattr(bc, "EMIT", True)


def _split(reply: str):
    text, _, tail = reply.partition("\n" + _SENTINEL)
    return text, (json.loads(tail) if tail else None)


# ── the validator ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "props",
    [
        {"ids": ["bd-a1"]},
        {"ids": ["bd-vbyd.24", "bd-x9"], "title": "Epic #4076"},
        {"ids": [f"bd-{i}" for i in range(20)]},
        {"state": "blocked"},
        {"state": "in_review", "project": "protoAgent"},
        {"project": "my_repo-2"},
        {"state": "done", "title": "x" * 80},
    ],
)
def test_validator_accepts_pointer_props(props):
    assert bc.validate_props(props) is None


@pytest.mark.parametrize(
    "props, why",
    [
        ("nope", "object"),
        ({}, "needs ids"),
        ({"title": "only a title"}, "needs ids"),
        ({"ids": ["bd-a1"], "spec": "card content"}, "unexpected"),
        ({"ids": ["bd-a1"], "cards": [{"id": "bd-a1", "title": "x"}]}, "unexpected"),
        ({"ids": []}, "non-empty"),
        ({"ids": "bd-a1"}, "non-empty list"),
        ({"ids": [f"bd-{i}" for i in range(21)]}, "at most 20"),
        ({"ids": ["bd-a1", "bd-a1"]}, "repeat"),
        ({"ids": ["features/../status"]}, "not a board card id"),
        ({"ids": ["bd-a1/secret"]}, "not a board card id"),
        ({"ids": ["bd-a1?x=1"]}, "not a board card id"),
        ({"ids": ["bd-a..b"]}, "not a board card id"),
        ({"ids": ["nodash"]}, "not a board card id"),
        ({"ids": [7]}, "not a board card id"),
        ({"state": "held"}, "state must be one of"),
        ({"state": "cancelled"}, "state must be one of"),
        ({"project": "../x"}, "project must be"),
        ({"project": "a b"}, "project must be"),
        ({"ids": ["bd-a1"], "state": "blocked"}, "not both"),
        ({"ids": ["bd-a1"], "project": "p"}, "not both"),
        ({"state": "blocked", "title": "x" * 81}, "at most 80"),
        ({"state": "blocked", "title": "two\nlines"}, "one line"),
        ({"state": "blocked", "title": 5}, "one line"),
    ],
)
def test_validator_rejects_everything_else(props, why):
    reason = bc.validate_props(props)
    assert reason is not None and why in reason, reason


def test_build_props_normalizes_tool_arguments():
    assert bc.build_props(["bd-a", " bd-b ", "", "bd-a"]) == {"ids": ["bd-a", "bd-b"]}
    assert bc.build_props("bd-a, bd-b  bd-c") == {"ids": ["bd-a", "bd-b", "bd-c"]}
    assert bc.build_props([], state=" blocked ", project="p", title="  Epic   #1 ") == {
        "state": "blocked",
        "project": "p",
        "title": "Epic #1",
    }
    assert bc.build_props(None) == {}


# ── registration: feature-detected, one setup gap on an older core ─────────────────


class _NewHost:
    """A host with ``register_component(..., frame=, reads=)`` (protoAgent#4143)."""

    def __init__(self):
        self.components, self.calls, self.gaps = {}, [], []

    def register_component(self, name, validator, frame=None, reads=None):
        self.calls.append((name, validator, frame, reads))
        self.components[name] = validator

    def report_setup_gap(self, key, message, **_kw):
        self.gaps.append((key, message))


class _FrameOnlyHost(_NewHost):
    """ADR 0118 D5 without reads — the core before #4143."""

    def register_component(self, name, validator, frame=None):  # noqa: D401 — no reads=
        self.calls.append((name, validator, frame, None))
        self.components[name] = validator


class _NoComponentsHost:
    def __init__(self):
        self.gaps = []

    def report_setup_gap(self, key, message, **_kw):
        self.gaps.append((key, message))


class _RefusingHost(_NewHost):
    """The host log-and-skips (e.g. a frame outside public_paths): nothing is stored."""

    def register_component(self, name, validator, frame=None, reads=None):
        self.calls.append((name, validator, frame, reads))


class _OverPromisingHost(_NewHost):
    def register_component(self, name, validator, **kw):
        if "reads" in kw:
            raise TypeError("unexpected keyword argument 'reads'")


def test_registers_the_frame_kind_with_its_reads():
    host = _NewHost()
    assert bc.register_component(host) is True
    assert bc.EMIT is True
    assert host.calls == [
        ("board-cards", bc.validate_props, "component/board-cards.html", ["cards", "features/*", "status"])
    ]
    assert host.gaps == []


@pytest.mark.parametrize("host_cls", [_FrameOnlyHost, _NoComponentsHost, _OverPromisingHost])
def test_an_older_core_gets_one_setup_gap_and_text_only(host_cls):
    host = host_cls()
    assert bc.register_component(host) is False
    assert bc.EMIT is False
    assert host.gaps == [(bc.SETUP_GAP_KEY, bc.SETUP_GAP_MESSAGE)]
    assert getattr(host, "calls", []) == [], "never registered without reads"


def test_a_refused_registration_is_not_emitted_and_raises_no_gap():
    host = _RefusingHost()
    assert bc.register_component(host) is False
    assert bc.EMIT is False
    assert host.gaps == []


def test_no_setup_gap_seam_still_degrades_quietly():
    class Bare:
        pass

    assert bc.register_component(Bare()) is False


def test_register_wires_the_component_end_to_end(monkeypatch):
    """The real register(): the component lands, and its frame is exposed by the manifest's
    public_paths exactly as the host checks it (graph.plugins.registry._accept_component_frame)."""

    class Host(_NewHost):
        def __init__(self):
            super().__init__()
            self.config = {"coder": "proto"}
            self.tools = []

        def register_tool(self, t):
            self.tools.append(t)

        def register_router(self, *a, **k):
            pass

        def register_surface(self, *a, **k):
            pass

        def register_subagent(self, *a, **k):
            pass

        def register_skill_dir(self, *a, **k):
            pass

    host = Host()
    pb.register(host)
    assert "board-cards" in host.components
    assert host.calls[0][2:] == (bc.FRAME, bc.READS)
    assert "board_show_cards" in {t.name for t in host.tools}
    # The host's frame rule: relative, no '..', and covered by a manifest public path.
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    full = f"/plugins/{manifest['id']}/{bc.FRAME}"
    assert any(full.startswith(p) for p in manifest["public_paths"])
    # …and the public path stays inside the plugin's own namespace, ending in '/'.
    for p in manifest["public_paths"]:
        assert p.startswith(f"/plugins/{manifest['id']}/") and p.endswith("/")


def test_reads_are_well_formed_patterns():
    for r in bc.READS:
        assert not r.startswith("/") and ".." not in r and "?" not in r and "**" not in r


# ── the tool ───────────────────────────────────────────────────────────────────────


class _Store:
    def __init__(self, feats):
        self.feats = {f["id"]: f for f in feats}
        self.shows = []

    def get_feature(self, fid):
        self.shows.append(fid)
        f = self.feats.get(fid)
        return dict(f) if f else None

    def list_features(self, state=None, include_archived=False):
        rows = [dict(f, blocked_reason="") for f in self.feats.values()]  # a listing has no comments
        return [f for f in rows if state is None or f["board_state"] == state]


def _feat(fid, state, **kw):
    return {
        "id": fid,
        "title": f"Card {fid}",
        "board_state": state,
        "blocked": state == "blocked",
        "pr_url": "",
        "project": "protoAgent",
        "labels": [],
        "priority": 2,
        "difficulty": "",
        "spec": "",
        "acceptance_criteria": "",
        "design": "",
        **kw,
    }


@pytest.fixture
def store(monkeypatch):
    s = _Store(
        [
            _feat("bd-1", "in_review", pr_url="https://github.com/o/r/pull/9"),
            _feat("bd-2", "blocked", blocked_reason="gate red on base"),
            _feat("bd-3", "in_progress"),
            _feat("bd-4", "done", project="other"),
        ]
    )
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: s)
    return s


def _tool():
    return {t.name: t for t in pb._board_tools({})}["board_show_cards"]


def test_tool_emits_the_component_plus_a_text_summary(store, emit):
    reply = _tool().invoke({"ids": ["bd-1", "bd-2", "bd-3", "bd-nope"], "title": "Epic #4076"})
    text, payload = _split(reply)
    assert payload == {
        "component": "board-cards",
        "props": {"ids": ["bd-1", "bd-2", "bd-3", "bd-nope"], "title": "Epic #4076"},
    }
    lines = text.splitlines()
    assert lines[0] == "Epic #4076 · 3 open · 1 blocked"
    assert "- bd-1 · Card bd-1 — in review" in lines[1] and "https://github.com/o/r/pull/9" in lines[1]
    assert lines[2] == "- bd-2 · Card bd-2 — blocked (gate red on base)"
    assert lines[3].startswith("- bd-3 · Card bd-3 — building")
    assert lines[-1] == "- bd-nope · not on this board"


def test_tool_query_mode_filters_and_fills_block_reasons(store, emit):
    text, payload = _split(_tool().invoke({"state": "blocked", "project": "protoAgent"}))
    assert payload["props"] == {"state": "blocked", "project": "protoAgent"}
    assert text.splitlines() == [
        "project protoAgent · blocked · 1 open · 1 blocked",
        "- bd-2 · Card bd-2 — blocked (gate red on base)",
    ]
    assert store.shows == ["bd-2"], "the listing's blocked card is read once for its reason"


def test_tool_empty_query(store, emit):
    text, payload = _split(_tool().invoke({"project": "nobody"}))
    assert text.splitlines() == ["project nobody · 0 open", "(no cards match)"]
    assert payload["props"] == {"project": "nobody"}


def test_tool_refuses_bad_props_without_touching_the_board(store, emit):
    assert _tool().invoke({}).startswith("Error: board-cards needs ids")
    assert _tool().invoke({"ids": ["bd-1"], "state": "blocked"}).startswith("Error:")
    assert _tool().invoke({"state": "held"}).startswith("Error: board-cards state")
    assert store.shows == []


def test_tool_on_an_older_core_is_text_only(store, monkeypatch):
    monkeypatch.setattr(bc, "EMIT", False)
    reply = _tool().invoke({"ids": ["bd-1"]})
    assert _SENTINEL not in reply
    assert reply.startswith("Board cards · 1 open")


def test_tool_without_a_host_encoder_is_text_only(store, monkeypatch):
    monkeypatch.setattr(bc, "EMIT", True)
    monkeypatch.delattr(sys.modules["graph.sdk"], "encode_component", raising=False)
    assert _SENTINEL not in _tool().invoke({"ids": ["bd-1"]})


def test_list_and_get_feature_never_attach_cards(store, emit):
    tools = {t.name: t for t in pb._board_tools({})}
    assert _SENTINEL not in tools["board_list"].invoke({})
    assert _SENTINEL not in tools["board_get_feature"].invoke({"feature_id": "bd-1"})


def test_card_row_is_slim():
    row = bc.card_row(
        _feat("bd-1", "in_review", spec="long spec", acceptance_criteria="ac", design="d", next_action="x")
    )
    assert "spec" not in row and "acceptance_criteria" not in row and "design" not in row
    assert row["next_action"] == "x" and row["board_state"] == "in_review"


# ── the routes the frame reads + the page itself ───────────────────────────────────


def test_the_frame_page_and_its_reads_are_served(monkeypatch, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_data_router, build_router

    monkeypatch.setattr("project_board.api.get_store", lambda **_kw: store, raising=False)
    app = FastAPI()
    app.include_router(build_router({}), prefix="/plugins/project_board")
    app.include_router(build_data_router({}), prefix="/api/plugins/project_board")
    c = TestClient(app)

    page = c.get("/plugins/project_board/" + bc.FRAME)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "__CARDS_" not in page.text and "BoardCards.start(window, document)" in page.text
    assert "/_ds/plugin-kit.css" in page.text

    # Every declared read is a served JSON GET under the plugin prefix (the frame reads these).
    slim = c.get("/api/plugins/project_board/cards")
    assert slim.status_code == 200 and slim.headers["content-type"].startswith("application/json")
    rows = slim.json()["features"]
    assert [r["id"] for r in rows] == ["bd-1", "bd-2", "bd-3", "bd-4"]
    assert all("spec" not in r and "design" not in r for r in rows)

    one = c.get("/api/plugins/project_board/features/bd-1")
    assert one.status_code == 200 and one.json()["id"] == "bd-1"
    assert c.get("/api/plugins/project_board/features/bd-missing").status_code == 404
    assert c.get("/api/plugins/project_board/status").status_code == 200


def test_component_assets_ship_in_the_sdist():
    assert "recursive-include component *.html *.css *.js" in (ROOT / "MANIFEST.in").read_text()


def test_the_page_uses_ds_classes_and_tokens_only():
    css = (ROOT / "component" / "board-cards.css").read_text()
    js = (ROOT / "component" / "board-cards.js").read_text()
    assert "#" not in css.split("*/", 1)[1], "no hex colours — --pl-* tokens only"
    for cls in ("pl-card", "pl-badge--", "pl-callout--warning", "pl-btn"):
        assert cls in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_page_logic_under_node():
    r = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "node" / "board_cards.test.cjs")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


def test_the_skill_names_every_proposal_and_its_tool():
    text = (ROOT / "skills" / "showing-board-cards" / "SKILL.md").read_text()
    fm = yaml.safe_load(text.split("---", 2)[1])
    assert fm["name"] == "showing-board-cards"
    assert {"board_show_cards", "board_unblock_feature", "board_get_feature", "board_dispatch"} <= set(fm["tools"])
    for phrase in (
        "Unblock board card bd-…",
        "Why is board card bd-… blocked?",
        "Ask Vera to re-review board card bd-…",
        "Merge board card bd-…",
        "Show the live coder run for board card bd-…",
        "Dispatch board card bd-…",
    ):
        assert phrase in text, phrase
    assert "When NOT to use it" in text

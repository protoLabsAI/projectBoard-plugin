"""The new-card form in chat: the ``board-card-form`` frame component and ``board_card_form``.

Host-free: the props validator (short labels only), the feature-detected registration, the
tool's output (one line plus the component tail, the board's projects in the picker), the
manifest/route wiring that serves the page, a static check that the page never submits a
<form>, and — under node — the page logic (tests/node/board_card_form.test.cjs: the picker
hidden for one project, validation, the exact compact proposal, the send verdicts).
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import project_board as pb
from project_board import board_card_form as bf

ROOT = Path(__file__).resolve().parent.parent
_SENTINEL = "\x1e[component-v1]"  # core's component-v1 sentinel (graph/components.py)


def _encode(component, props):
    return _SENTINEL + json.dumps({"component": component, "props": props}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _reset_emit(monkeypatch):
    monkeypatch.setattr(bf, "EMIT", False)


@pytest.fixture
def emit(monkeypatch):
    monkeypatch.setattr(sys.modules["graph.sdk"], "encode_component", _encode, raising=False)
    monkeypatch.setattr(bf, "EMIT", True)


def _split(reply: str):
    text, _, tail = reply.partition("\n" + _SENTINEL)
    return text, (json.loads(tail) if tail else None)


def _registry(monkeypatch, projects, default=""):
    reg = importlib.import_module(f"{pb.__name__}.project_registry")
    rows = [{"name": n, "repo": f"/r/{n}", "base_branch": "main"} for n in projects]
    monkeypatch.setattr(reg, "project_registry_snapshot", lambda: {"projects": rows, "default_project": default})


def _tool(cfg=None):
    return {t.name: t for t in pb._board_tools(cfg or {})}["board_card_form"]


# ── the validator ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "props",
    [
        {},
        {"projects": ["web", "api"], "project": "api"},
        {"projects": ["only"]},
        {"project": "demo", "title_hint": "Add an undo command", "why_hint": "x" * 160},
    ],
)
def test_validator_accepts_label_props(props):
    assert bf.validate_props(props) is None


@pytest.mark.parametrize(
    "props, why",
    [
        ("nope", "object"),
        ({"spec": "card content"}, "unexpected"),
        ({"title": "x"}, "unexpected"),
        ({"projects": "web"}, "list"),
        ({"projects": ["a b"]}, "not a project name"),
        ({"projects": ["../x"]}, "not a project name"),
        ({"projects": ["a", "a"]}, "repeat"),
        ({"projects": [f"p{i}" for i in range(31)]}, "at most 30"),
        ({"project": "p\n"}, "project must be"),
        ({"projects": ["web"], "project": "api"}, "not one of"),
        ({"title_hint": "two\nlines"}, "one line"),
        ({"why_hint": "x" * 161}, "at most 160"),
        ({"title_hint": 5}, "one line"),
    ],
)
def test_validator_rejects_everything_else(props, why):
    reason = bf.validate_props(props)
    assert reason is not None and why in reason, reason


def test_build_props_picks_the_project_and_clips_hints():
    assert bf.build_props(["web", "api", "web", "bad name"], project="api", default_project="web") == {
        "projects": ["web", "api"],
        "project": "api",
    }
    # an unknown requested project falls back to the default instead of failing the form
    assert bf.build_props(["web", "api"], project="nope", default_project="web")["project"] == "web"
    props = bf.build_props(["web"], title_hint="  Add\n undo  ", why_hint="y" * 400)
    assert props["title_hint"] == "Add undo"
    assert len(props["why_hint"]) == bf.HINT_MAX and props["why_hint"].endswith("…")
    assert bf.validate_props(props) is None
    assert bf.build_props(None) == {}


# ── registration ───────────────────────────────────────────────────────────────────


class _FrameHost:
    def __init__(self):
        self.components, self.calls = {}, []

    def register_component(self, name, validator, frame=None, reads=None):
        self.calls.append((name, validator, frame, reads))
        self.components[name] = validator


class _PreFrameHost(_FrameHost):
    def register_component(self, name, validator):  # ADR 0051 only, no frame=
        self.calls.append((name, validator, None, None))


class _RefusingHost(_FrameHost):
    def register_component(self, name, validator, frame=None, reads=None):
        self.calls.append((name, validator, frame, reads))  # logged-and-skipped: nothing stored


def test_registers_the_frame_kind_without_reads():
    host = _FrameHost()
    assert bf.register_component(host) is True and bf.EMIT is True
    assert host.calls == [("board-card-form", bf.validate_props, "component/board-card-form.html", None)]


@pytest.mark.parametrize("host", [_PreFrameHost(), _RefusingHost(), object()])
def test_an_older_or_refusing_host_is_not_emitted(host):
    assert bf.register_component(host) is False and bf.EMIT is False
    if isinstance(host, _PreFrameHost):
        assert host.calls == [], "never registered without frame="


def test_register_wires_both_components_and_the_tool():
    class Host(_FrameHost):
        config = {"coder": "proto"}

        def __init__(self):
            super().__init__()
            self.tools = []

        def register_tool(self, t):
            self.tools.append(t)

        def __getattr__(self, name):  # every other registry seam: a no-op
            if name.startswith("register_"):
                return lambda *a, **k: None
            raise AttributeError(name)

    host = Host()
    pb.register(host)
    assert {"board-cards", "board-card-form"} <= set(host.components)
    assert "board_card_form" in {t.name for t in host.tools}
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    full = f"/plugins/{manifest['id']}/{bf.FRAME}"
    assert any(full.startswith(p) for p in manifest["public_paths"]), "the frame must be public"


# ── the tool ───────────────────────────────────────────────────────────────────────


def test_tool_emits_the_component_with_the_boards_projects(monkeypatch, emit):
    _registry(monkeypatch, ["web", "api"], default="web")
    text, payload = _split(_tool().invoke({"title_hint": "Add CSV export", "why_hint": "Ops want the runs as a file"}))
    assert payload == {
        "component": "board-card-form",
        "props": {
            "projects": ["web", "api"],
            "project": "web",
            "title_hint": "Add CSV export",
            "why_hint": "Ops want the runs as a file",
        },
    }
    assert text == "Showing the new-card form (project web; picker: web, api)."


def test_tool_preselects_a_named_project(monkeypatch, emit):
    _registry(monkeypatch, ["web", "api"], default="web")
    _, payload = _split(_tool().invoke({"project": '"api"'}))
    assert payload["props"]["project"] == "api"


def test_tool_with_one_project_has_no_picker(monkeypatch, emit):
    """A flat single-repo board: the registry is empty, the implicit project is the one."""
    _registry(monkeypatch, [])
    text, payload = _split(_tool({"repo": "/tmp/x"}).invoke({}))
    assert payload["props"] == {"projects": ["default"], "project": "default"}
    assert "no picker" in text


def test_tool_on_a_host_without_frames_says_ask_in_chat(monkeypatch):
    _registry(monkeypatch, ["web", "api"], default="web")
    reply = _tool().invoke({"title_hint": "x"})
    assert _SENTINEL not in reply
    assert "can't render" in reply and "ONE short message" in reply and "web, api" in reply


def test_tool_without_a_host_encoder_falls_back(monkeypatch):
    _registry(monkeypatch, ["web"])
    monkeypatch.setattr(bf, "EMIT", True)
    monkeypatch.delattr(sys.modules["graph.sdk"], "encode_component", raising=False)
    assert "can't render" in _tool().invoke({})


# ── the page: served, public, no <form> ─────────────────────────────────────────────


def test_the_frame_page_is_served_on_the_public_router():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_router

    app = FastAPI()
    app.include_router(build_router({}), prefix="/plugins/project_board")
    page = TestClient(app).get("/plugins/project_board/" + bf.FRAME)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "__FORM_" not in page.text and "BoardCardForm.start(window, document)" in page.text
    assert "/_ds/plugin-kit.css" in page.text
    assert "function buildMessage(" in page.text, "the JS is inlined"


def test_the_page_never_submits_a_form_and_proposes_through_the_send_bridge():
    """The frame is sandboxed without allow-forms: a <form> submit is blocked and its handler
    never runs. The button must be type=button + click, and the proposal must go out on the
    frame bridge's send path (protoComponent:send, the host's ConfirmChip)."""
    page = bf.FORM_PAGE
    js = (ROOT / "component" / "board-card-form.js").read_text()
    assert "<form" not in page and "<form" not in js
    assert 'type="submit"' not in page and '"submit"' not in js and ".submit(" not in js
    assert 'type="button" class="pl-btn pl-btn--primary" id="cf-create"' in js
    assert 'addEventListener("click"' in js
    assert '"protoComponent:send"' in js
    assert "focus()" not in js and "autofocus" not in js, "a self-focusing frame is quarantined (#4076)"


def test_the_page_uses_ds_classes_and_tokens_only():
    css = (ROOT / "component" / "board-card-form.css").read_text()
    js = (ROOT / "component" / "board-card-form.js").read_text()
    assert "#" not in css.split("*/", 1)[1], "no hex colours — --pl-* tokens only"
    for cls in ("pl-card", "pl-eyebrow", "pl-field__label", "pl-input", "pl-textarea", "pl-select", "pl-btn--primary"):
        assert cls in js


def test_the_message_prefix_matches_the_page():
    js = (ROOT / "component" / "board-card-form.js").read_text()
    assert f'var PREFIX = "{bf.MESSAGE_PREFIX}";' in js


def test_component_assets_ship_in_the_sdist():
    assert "recursive-include component *.html *.css *.js" in (ROOT / "MANIFEST.in").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_page_logic_under_node():
    r = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "node" / "board_card_form.test.cjs")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]

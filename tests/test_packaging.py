"""Packaging + contract tests — the manifest, version coherence, the view page's
four-rules compliance, the planning skill, and that ``register()`` wires cleanly.

These are the cheap guards that catch the whole class of "ships but is subtly
broken" bugs: a manifest field renamed, the pyproject/manifest versions drifting
apart (the version-coherence failure mode), the board page losing its DS-kit /
slug-aware contract, or ``register()`` throwing on load.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _manifest():
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())


# ── the manifest ────────────────────────────────────────────────────────────────


def test_manifest_has_the_required_shape():
    m = _manifest()
    assert m["id"] == "project_board"
    assert m["name"] and m["version"]
    assert m["enabled"] is False  # ships DISABLED — enable is the trust decision
    assert m["config_section"] == "project_board"
    # The config the loop/store/api read must be declared (defaults are truth).
    for key in ("coder", "repo", "base_branch", "loop_enabled", "webhook_secret"):
        assert key in m["config"], f"manifest config missing {key!r}"


def test_manifest_and_pyproject_versions_agree():
    m = _manifest()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert m["version"] == pyproject["project"]["version"], (
        "manifest and pyproject versions drifted — keep them in lockstep (the tag too)"
    )


def test_manifest_view_path_is_public_and_base_safe():
    view = _manifest()["views"][0]
    assert view["id"] == "board"
    assert view["path"] == "/plugins/project_board/board"  # NOT under /api
    assert view["path"].split("/plugins/")[0] == ""  # base derives to "" on the host


# ── the board view page: the plugin-view four rules (ADR 0026/0042) ──────────────


def test_board_page_is_four_rules_compliant():
    from project_board.board_view import BOARD_PAGE

    html = BOARD_PAGE
    # rule 4 — link the DS kit (CSS + the ESM JS), same-origin off the derived base.
    assert "/_ds/plugin-kit.css" in html
    assert 'import(BASE + "/_ds/plugin-kit.js")' in html  # ESM dynamic import
    assert 'type="module"' in html
    # rule 3 — slug-aware base from the page's own path (never a hardcoded /api/…).
    assert 'location.pathname.split("/plugins/")[0]' in html
    # rules 2+3 — gated data via the kit's authed, slug-aware fetch.
    assert "kit.apiFetch" in html
    assert "/api/plugins/project_board/features" in html
    # The kit owns theming + the handshake — no hand-rolled :root map or listener.
    assert ":root{" not in html and ":root {" not in html
    assert 'addEventListener("message"' not in html


# ── the planning skill ──────────────────────────────────────────────────────────


def test_decompose_skill_frontmatter():
    text = (ROOT / "skills" / "decompose-project" / "SKILL.md").read_text()
    fm = yaml.safe_load(text.split("---", 2)[1])
    assert fm["name"] == "decompose-project"
    # It must be able to populate the board and run the per-epic human gate.
    for t in ("board_create_feature", "board_mark_ready", "request_user_input"):
        assert t in fm["tools"], f"decompose skill missing tool {t!r}"


# ── register(): wires the contributions without a host, doesn't throw ────────────


class _Registry:
    def __init__(self):
        self.config = {"coder": "proto"}
        self.tools, self.routers, self.surfaces = [], [], []
        self.subagents, self.skill_dirs = [], []

    def register_tool(self, t):
        self.tools.append(t)

    def register_router(self, router, prefix):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None):
        self.surfaces.append(name)

    def register_subagent(self, config):
        self.subagents.append(config)

    def register_skill_dir(self, path):
        self.skill_dirs.append(path)


def test_register_wires_routers_surface_and_tools():
    import project_board

    reg = _Registry()
    project_board.register(reg)  # must not raise even with no protoAgent host
    # Both prefixes mount (rule 2: the public view + the gated CRUD are split).
    assert "/plugins/project_board" in reg.routers
    assert "/api/plugins/project_board" in reg.routers
    # The background puller is registered as a lifecycle surface.
    assert "project-board-loop" in reg.surfaces
    # The four headless board tools the agent (or A2A) can drive.
    names = {getattr(t, "name", "") for t in reg.tools}
    assert {"board_create_epic", "board_create_feature", "board_mark_ready", "board_list"} <= names

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


def test_lockfile_version_agrees_with_manifest():
    # uv.lock is the third version-bearing file — its project-board entry must move
    # in lockstep with the manifest/pyproject on every bump, or the lock ships stale.
    m = _manifest()
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    board = next(p for p in lock["package"] if p["name"] == "project-board")
    assert board["version"] == m["version"], (
        "uv.lock project-board version drifted from the manifest — re-lock on every bump"
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


# ── the onboarding skill: registration gated on onboarding.enabled (#194) ───────


def test_onboard_skill_gates_registration_on_onboarding():
    """The human-gate form offers `register_project` ONLY when the host's
    `onboarding.enabled` is on — offering it on a disabled host collects an answer
    `board_register_project` then refuses, and the operator's choice silently
    evaporates. The skill must gate the field, carry the unavailable-note when off,
    and lead the completion summary with any refusal (older forms may still answer
    true)."""
    text = (ROOT / "skills" / "onboard-project" / "SKILL.md").read_text()
    fm = yaml.safe_load(text.split("---", 2)[1])
    assert fm["name"] == "onboard-project"
    # It must be able to register (when consented) and run the readiness human gate.
    for t in ("board_register_project", "request_user_input"):
        assert t in fm["tools"], f"onboard skill missing tool {t!r}"

    flat = " ".join(text.split())  # the procedure wraps across lines in the source
    # The field is conditional on the HOST setting, and OMITTED — not just refused — when off.
    assert "`onboarding.enabled`" in flat
    assert "omit the `register_project` field" in flat
    # The exact operator-facing note the form carries in the disabled case.
    note = (
        "Project registration unavailable — enable Settings ▸ Project onboarding "
        "to register repos for filesystem access."
    )
    assert note in flat
    # When enabled, the field is offered as before (no regression).
    assert "`register_project: true/false`" in flat
    # Backward compat: a refusal must LEAD the summary, not be buried in the turn.
    assert "REGISTRATION REFUSED" in flat


# ── register(): wires the contributions without a host, doesn't throw ────────────


class _Registry:
    def __init__(self):
        self.config = {"coder": "proto"}
        self.tools, self.routers, self.surfaces = [], [], []
        self.subagents, self.skill_dirs = [], []
        self.surface_reloads = {}

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


def test_register_wires_the_loop_reload_hook():
    """The surface registers `reload=` (ADR 0018) so a config reload re-applies the
    live concurrency knobs to the RUNNING loop. Without it the host can't tell the
    loop anything after boot and a `max_concurrent` edit silently waits for a restart
    — the exact gap: a one-slot loop serializing a six-project board."""
    import project_board
    from project_board.loop import BoardLoop

    reg = _Registry()
    project_board.register(reg)
    reload_cb = reg.surface_reloads["project-board-loop"]
    assert callable(reload_cb)
    assert getattr(reload_cb, "__func__", None) is BoardLoop.reload


def test_manifest_settings_are_exactly_the_live_knobs():
    """Settings → Plugins renders the manifest's `settings:`; every field there MUST be
    one the running loop re-applies on reload (LIVE_KNOBS), and every live knob must
    be editable there — a field the operator can edit that silently needs a restart
    is worse than no field, and a live knob with no field can't be reached from the
    console. Each must also carry a manifest default (the reload payload is manifest
    defaults ⊕ YAML, so the hook always sees every knob)."""
    from project_board.loop import LIVE_BOOL_KNOBS, LIVE_KNOBS, LIVE_STR_KNOBS

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    fields = {s["key"]: s for s in m["settings"]}
    assert set(fields) == set(LIVE_KNOBS)
    for key, spec in fields.items():
        want = "bool" if key in LIVE_BOOL_KNOBS else ("string" if key in LIVE_STR_KNOBS else "number")
        assert spec["type"] == want, key
        assert spec["label"] and spec["description"], key
        assert key in m["config"], f"{key} has no manifest default"


def test_manifest_min_host_version_covers_the_reload_hook():
    """register_surface(reload=) landed in protoAgent v0.31.0 (#509/#511); an older
    host raises TypeError on the kwarg and the loop never registers."""
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    major, minor, *_ = (int(x) for x in str(m["min_protoagent_version"]).split("."))
    assert (major, minor) >= (0, 31)

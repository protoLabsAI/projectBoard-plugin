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


def test_onboard_skill_registers_with_the_auto_sentinel_never_a_gate_command():
    """The register tool takes no gate command text (hardening) — the skill must
    instruct the literal `gate="auto"` and may name `project_board.local_gate_cmd`
    only as OPERATOR config, never as something the agent passes."""
    text = (ROOT / "skills" / "onboard-project" / "SKILL.md").read_text()
    flat = " ".join(text.split())
    assert 'gate="auto"' in flat
    # No tool-call argument carrying a gate command — the old call shape is gone.
    assert "local_gate_cmd=" not in flat


# ── the release workflow: a thin caller of the reusable plugin-release ────────────


def _release_workflow():
    """Parse .github/workflows/release.yml. YAML 1.1 folds the bare ``on:`` key into
    the boolean ``True`` (the on/off/yes/no quirk); irrelevant here — we assert on
    ``permissions`` and ``jobs``, whose string keys survive."""
    return yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())


def test_release_workflow_calls_the_reusable_plugin_release_at_v2():
    """release.yml is a thin caller of protoLabsAI/release-tools' reusable
    plugin-release workflow — pinned at @v2, not the old @v1 composite action, and
    carrying NO inline release logic (tag/notes/Discord live in the reusable side)."""
    wf = _release_workflow()
    job = wf["jobs"]["release"]
    # It delegates to the reusable workflow, pinned at @v2.
    assert job["uses"] == "protoLabsAI/release-tools/.github/workflows/plugin-release.yml@v2"
    # Org secrets flow through so the reusable side can reach the gateway + Discord.
    assert job["secrets"] == "inherit"
    # A reusable-workflow-calling job has no `steps:` — the inline ritual is gone.
    assert "steps" not in job

    raw = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # The old @v1 composite action must not linger anywhere in the file.
    assert "release-tools@v1" not in raw
    # None of the inline machinery the reusable workflow now owns.
    for gone in ("gh release create", "git tag", "GATEWAY_API_KEY", "DISCORD_RELEASE_WEBHOOK"):
        assert gone not in raw, f"inline release logic leaked: {gone!r}"


def test_release_workflow_keeps_the_repo_specific_fork_guard():
    """The fork guard stays in the caller's `if:` — it's repo-specific and can't live
    in the reusable workflow shared across the fleet."""
    guard = _release_workflow()["jobs"]["release"]["if"]
    assert "github.repository == 'protoLabsAI/projectBoard-plugin'" in guard
    assert "workflow_dispatch" in guard
    assert "chore: release v" in guard


def test_release_workflow_grants_contents_write_to_the_reusable_workflow():
    """The caller must grant `contents: write` — a called workflow can only DOWNGRADE
    the caller's GITHUB_TOKEN, never escalate. Drop this and an org/repo default of
    read-only leaves the reusable workflow unable to create the tag or the release."""
    wf = _release_workflow()
    assert wf["permissions"]["contents"] == "write"


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


def test_no_agent_tool_exposes_an_executable_shaped_parameter():
    """Hardening contract: values persisted into gate config are later run by the
    loop's shell seam, so command text must only ever enter through the operator's
    bearer-gated surface (PUT /projects/{name} / YAML) — never through an agent tool.
    Sweep EVERY agent tool schema (the board tools plus the register tool, which is
    also asserted standalone in case its wiring moves) for parameter names shaped
    like an executable. The register tool's `gate` accepts only the literal "auto"
    discovery sentinel, which carries no command text."""
    import project_board
    from project_board.project_registry import build_register_tool

    def executable_shaped(param: str) -> bool:
        p = param.lower()
        return p in {"command", "args", "cmd", "executable"} or p.endswith("_cmd")

    tools = list(project_board._board_tools({}))
    register_tool = build_register_tool({})
    assert register_tool is not None  # langchain is a test dependency — the guard must cover it
    tools.append(register_tool)
    assert {t.name for t in tools} >= {"board_register_project", "board_create_feature"}  # the sweep swept
    for t in tools:
        for param in t.args:
            assert not executable_shaped(param), (
                f"{t.name}({param}): executable-shaped agent tool input — route it "
                "through the operator config surface instead"
            )
    register = next(t for t in tools if t.name == "board_register_project")
    assert "local_gate_cmd" not in register.args


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


def test_manifest_settings_are_truthful_about_live_vs_restart_apply():
    """Every live knob is exposed without a restart badge; every other exposed
    scalar says restart. Structural and internal tuning are an explicit file-only
    allowlist, so adding a config default forces an expose/hide decision."""
    from project_board.loop import LIVE_BOOL_KNOBS, LIVE_KNOBS, LIVE_STR_KNOBS

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    fields = {s["key"]: s for s in m["settings"]}
    hidden = {
        # Structural/machine routing belongs in the Projects editor or YAML.
        "project",
        "repo",
        "base_branch",
        "worktrees_root",
        "db_path",
        # Low-level retry/timing budgets remain expert file-only tuning.
        "review_run_max",
        "goal_fix_max",
        "local_gate_max",
        "local_gate_timeout_s",
        "coder_solve_test_timeout_s",
        "coder_solve_budget",
        "coder_solve_k",
        "coder_solve_tree_depth",
        "loop_interval_s",
        "coder_timeout_s",
        "merge_poll_interval_s",
        "auto_merge_max",
        "merged_verify_max",
        "health_sweep_interval_s",
    }
    assert set(fields) | hidden == set(m["config"])
    assert set(fields).isdisjoint(hidden)
    assert set(LIVE_KNOBS) <= set(fields)
    for key, spec in fields.items():
        assert bool(spec.get("restart", False)) is (key not in LIVE_KNOBS), key
        assert spec["label"] and spec["description"], key
        assert key in m["config"], f"{key} has no manifest default"
    for key in LIVE_KNOBS:
        want = "bool" if key in LIVE_BOOL_KNOBS else ("string" if key in LIVE_STR_KNOBS else "number")
        assert fields[key]["type"] == want, key


def test_manifest_declares_the_ordered_tabbed_config_contract():
    m = _manifest()
    assert m["settings_tabs"] == [
        {"id": "projects", "label": "Projects", "path": "/plugins/project_board/config/projects"},
        {"id": "automation", "label": "Automation"},
        {"id": "review", "label": "Review & merge"},
        {"id": "security", "label": "Security"},
    ]
    schema_tabs = {t["id"] for t in m["settings_tabs"] if "path" not in t}
    assert {field["tab"] for field in m["settings"]} <= schema_tabs
    assert {field["tab"] for field in m["settings"]} == schema_tabs
    assert next(field for field in m["settings"] if field["key"] == "webhook_secret")["type"] == "secret"


def test_manifest_numeric_settings_have_operator_safe_bounds():
    fields = {field["key"]: field for field in _manifest()["settings"]}
    for key, field in fields.items():
        if field["type"] != "number":
            continue
        assert "minimum" in field and "maximum" in field, key
        assert field["minimum"] <= field["maximum"], key


def test_manifest_min_host_version_covers_tabbed_plugin_config():
    """The tab contract shipped in protoAgent 0.153.2 (#3179/#3180)."""
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    version = tuple(int(x) for x in str(m["min_protoagent_version"]).split("."))
    assert version >= (0, 153, 2)


# ── #316 docs: the task lane's self-verification provenance ──────────────────────


def test_readme_documents_the_task_lane_self_verification_posture():
    """The README must describe the landed #316 field/badge accurately: the task
    Done edge, that self-verification is RECORDED and flagged (never blocked), the
    user-visible provenance posture WITHOUT overclaiming identity/auth, and the
    deliberate API-`operator` vs tool/store-actor default split."""
    flat = " ".join((ROOT / "README.md").read_text().split())

    # The task lane + its Done edge (a task ships a deliverable, verified not merged).
    assert "The task lane" in flat and "deliverables, not PRs" in flat
    assert "Done edge is a verifier's approval" in flat
    assert "verified: <who>" in flat

    # r1: self-verification is allowed and flagged/recorded, NOT blocked.
    assert "flagged, never blocked" in flat
    assert "a `self-verified` label" in flat
    assert "`self_verified: true`" in flat  # the projected field the badge reads
    assert "caution badge" in flat  # the user-visible board surface (S4)

    # r2: provenance posture, NO new identity/authentication guarantee claim.
    assert "not an identity or authentication guarantee" in flat
    assert "does not attest who those actors *really* were" in flat

    # r4: the intentional API(operator) vs tool(store-actor) default distinction.
    assert "store actor" in flat  # the agent-tool (board_verify) default
    assert "NOT the store actor" in flat  # the API default is `operator`, deliberately not the actor
    assert "A console verification is out-of-band by construction" in flat  # why they differ
    assert "An explicit `by` is forwarded verbatim" in flat


def test_loop_retro_skill_calls_out_self_verified_closures():
    """The loop-retro skill must teach the retro to surface self-verified task
    closures — deliverables that closed with no independent verifier — for the human
    report, sourced from `board_list` (board_retro's digest doesn't carry the flag),
    and kept OUT of the coder KG (a governance signal, not a failure class)."""
    text = (ROOT / "skills" / "loop-retro" / "SKILL.md").read_text()
    fm = yaml.safe_load(text.split("---", 2)[1])
    assert fm["name"] == "loop-retro"
    # The projection tool is the honest source for the per-task self_verified flag.
    assert "board_list" in fm["tools"]

    flat = " ".join(text.split())
    # It names the landed field + what a self-verified closure MEANS.
    assert "self_verified: true" in flat
    assert "no independent verifier" in flat
    assert "no second pair of eyes" in flat
    # It goes in the human REPORT, never the coder-facing knowledge graph.
    assert "never the KG" in flat

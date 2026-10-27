"""Planning subagents must not collide with the host's built-in names (#643)."""

from pathlib import Path

import project_board
from _plugin_testkit import FakeRegistry, install_host_stubs

ROOT = Path(__file__).resolve().parent.parent

# Kept explicit so this regression test runs without a protoAgent host installed.
CORE_BUILTIN_NAMES = {
    "researcher",
    "antagonist",
    "verifier",
    "synthesizer",
    "codebase-mapper",
    "review-finder",
    "review-synthesizer",
    "dream",
    "distill",
    "self-improve",
    "coder",
    "skill_writer",
    "friction_triage",
    "plugin-architect",
}


def test_registered_subagents_do_not_shadow_core_builtins():
    install_host_stubs()
    registry = FakeRegistry({"br_autofetch": False}, plugin_id="project_board")
    project_board.register(registry)
    names = {config.name for config in registry.subagents}
    assert names, "Planning subagent registration was skipped"
    assert names.isdisjoint(CORE_BUILTIN_NAMES), names & CORE_BUILTIN_NAMES
    assert {"decompose", "plan_antagonist"} <= names


def test_decompose_skill_dispatches_the_planning_critic():
    skill = (ROOT / "skills" / "decompose-project" / "SKILL.md").read_text()
    assert "task(plan_antagonist, …)" in skill
    assert "task(antagonist," not in skill

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


def test_decomposer_uses_the_current_memory_tool():
    install_host_stubs()
    registry = FakeRegistry({"br_autofetch": False}, plugin_id="project_board")
    project_board.register(registry)
    decomposer = next(config for config in registry.subagents if config.name == "decompose")
    assert "memory_search" in decomposer.tools
    assert "memory_recall" not in decomposer.tools  # removed in core 0.231.0 (ADR 0125)


def test_authoring_skills_require_a_card_specific_dependency_reason():
    rule = (
        "An edge needs a reason: the two cards touch the same file, or one builds on named code "
        "the other adds. Say which on the card. A phase-level 'after X' in a brief or design doc "
        "is not a reason by itself; check it card by card."
    )
    for name in ("board-tool-reference", "decompose-project", "card-from-chat"):
        skill = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert rule in " ".join(skill.split()), name


def test_split_guidance_keeps_independent_halves_parallel():
    skill = (ROOT / "skills" / "board-tool-reference" / "SKILL.md").read_text()
    split_rule = " ".join(skill.split("**Split at four authored files.**", 1)[1].split("- **", 1)[0].split())
    assert (
        "Chain the halves with `depends_on` only if they share a file "
        "or one builds on code the other adds; otherwise leave them parallel."
    ) in split_rule
    assert "two cards chained by `depends_on`" not in split_rule


def test_registered_planning_critic_checks_dependency_necessity():
    install_host_stubs()
    registry = FakeRegistry({"br_autofetch": False}, plugin_id="project_board")
    project_board.register(registry)
    critic = next(config for config in registry.subagents if config.name == "plan_antagonist")
    feature_row = next(
        line for line in critic.system_prompt.splitlines() if line.startswith("| → feature requirements/design/tasks |")
    )
    assert "Are `depends_on` edges declared and correct?" in feature_row
    assert (
        "Is each `depends_on` edge needed (a shared file or named build-on), or needless serialization?" in feature_row
    )

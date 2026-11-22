"""Carding in the background (the `board_card_writer` helper + `card-github-issues` skill).

Carding a list of issues in the lead's own thread filled its context and held its chat: it
read eleven issues and their code before writing one card, then stopped. One helper per issue
does that reading now; the lead keeps duplicates, ordering and release."""

from pathlib import Path

import project_board
import yaml
from _plugin_testkit import FakeRegistry, install_host_stubs

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "card-github-issues" / "SKILL.md"


def _registry():
    install_host_stubs()
    registry = FakeRegistry({"br_autofetch": False}, plugin_id="project_board")
    project_board.register(registry)
    return registry


def _writer(registry):
    return next(c for c in registry.subagents if c.name == "board_card_writer")


def _front_matter():
    return yaml.safe_load(SKILL.read_text().split("---", 2)[1])


def test_the_helper_is_registered_alongside_the_planning_subagents():
    names = {c.name for c in _registry().subagents}
    assert {"board_card_writer", "decompose", "plan_antagonist"} <= names


def test_the_helper_can_write_and_repair_a_card_but_not_release_or_steer_the_board():
    tools = set(_writer(_registry()).tools)
    # What one issue → one card needs.
    assert {"github_get_issue", "github_issue_comments", "read_file", "search_files"} <= tools
    assert {"board_list", "board_create_feature", "board_check_ready", "board_update_feature"} <= tools
    # Release, ordering across issues and recovery need the whole picture: the lead's.
    for held_back in (
        "board_mark_ready",
        "board_dispatch",
        "board_cancel_feature",
        "board_requeue_feature",
        "board_unblock_feature",
        "board_mark_done",
        "board_hold_merge",
        "github_comment",
        "github_set_labels",
        "write_file",
        "task",
        "task_batch",
    ):
        assert held_back not in tools, held_back


def test_every_board_tool_the_helper_lists_is_one_the_plugin_registers():
    """A helper whose tools are not bound on the agent is refused at spawn (core #4472), and a
    misspelt name would silently give it less than it needs."""
    registry = _registry()
    registered = {getattr(t, "name", getattr(t, "__name__", "")) for t in registry.tools}
    listed = {t for t in _writer(registry).tools if t.startswith("board_")}
    assert listed and listed <= registered, listed - registered


def test_the_helper_prompt_carries_the_rules_that_went_wrong_by_hand():
    prompt = _writer(_registry()).system_prompt
    # Reading without carding.
    assert "ten read calls" in prompt
    # The same card twice.
    assert "Look for an existing card first" in prompt and "EXISTING" in prompt
    # An issue closed while it still had slices; a closing line without the `#`.
    assert "Only the LAST one closes the issue" in prompt
    assert "`owner/repo#N`, with the `#`" in prompt
    # The lead orders cards across issues from these lines.
    assert "FILES <id>" in prompt
    assert "Mark a card ready" in prompt  # under "What you never do"


def test_the_skill_names_the_helper_and_the_background_batch():
    fm = _front_matter()
    assert fm["name"] == "card-github-issues"
    assert {"task_batch", "task", "board_list", "board_mark_ready", "board_update_feature"} <= set(fm["tools"])
    # The lead does not card or read issues itself under this skill.
    for not_the_leads in ("board_create_feature", "github_get_issue", "github_issue_comments", "read_file"):
        assert not_the_leads not in fm["tools"], not_the_leads
    body = SKILL.read_text()
    assert "board_card_writer" in body
    assert "`task_batch` with `run_in_background=true`" in body


def test_the_skill_keeps_the_lead_free_and_gives_it_only_whole_picture_work():
    body = SKILL.read_text()
    assert "Reply straight away" in body
    assert "Do not wait for the helpers" in body
    assert "Read an issue body, its comments or source files to write a card yourself." in body
    for kept in ("**Duplicates.**", "**Order.**", "board_mark_ready"):
        assert kept in body, kept


def test_the_skill_states_the_dependency_reason_rule():
    assert "An edge needs that reason" in SKILL.read_text()


def test_the_description_routes_a_single_described_card_to_card_from_chat():
    assert "card-from-chat" in _front_matter()["description"]

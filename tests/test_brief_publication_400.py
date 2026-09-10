"""The coder's brief must match who publishes the work (#400).

The brief said both "You cannot run shell commands (edit-only)" and "Push the branch if you
can" — contradicting itself at exactly the step that publishes the work. bd-ezs7's coder
reported it could not run the gate or push, which was true and was never its job. The
contract is edit-only: the loop commits what the coder leaves, runs the pre-PR checks when a
gate is configured, pushes the branch and opens the PR. So the brief must not hand the coder
any part of that — and must not promise a check the loop will not run.
"""

from __future__ import annotations

from project_board.loop import BoardLoop

_CARD = {
    "id": "bd-1",
    "title": "Add a thing",
    "repo": "/repo",
    "base_branch": "main",
    "spec": "do the thing",
    "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
    "files_to_modify": ["a.py"],
}


def _rules(cfg: dict) -> str:
    return BoardLoop(cfg)._build_prompt(_CARD).split("## Rules", 1)[1]


def test_the_brief_never_hands_the_coder_a_publishing_step():
    rules = _rules({})
    assert "push the branch if you can" not in rules.lower(), "the brief still asks the coder to push"
    assert "Do NOT commit or push" in rules
    assert "the loop commits whatever you left here" in rules and "and pushes the branch" in rules
    assert "do NOT open a PR (draft or otherwise) — the loop opens it" in rules  # #207 still holds


def test_the_brief_promises_the_pre_pr_checks_only_when_a_gate_runs_them():
    gated = _rules({"local_gate_cmd": "make check"})
    assert "runs the repo's pre-PR checks and pushes the branch" in gated
    assert "The tests you write run in those checks and in CI" in gated

    ungated = _rules({"local_gate_cmd": ""})
    assert "pre-PR checks" not in ungated, "the brief promised a check no gate will run"
    assert "The tests you write run in CI" in ungated

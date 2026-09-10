"""The coder's brief must match who publishes the work (#400).

The brief said both "You cannot run shell commands (edit-only)" and "Push the branch if you
can" — contradicting itself at exactly the step that publishes the work. bd-ezs7's coder
reported it could not run the gate or push, which was true and was never its job. The
contract is edit-only: the loop commits what the coder leaves, runs the pre-PR checks,
pushes the branch and opens the PR. So the brief must not hand the coder any part of that.
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


def _rules() -> str:
    return BoardLoop({})._build_prompt(_CARD).split("## Rules", 1)[1]


def test_the_brief_never_hands_the_coder_a_publishing_step():
    rules = _rules()
    assert "push the branch if you can" not in rules.lower(), "the brief still asks the coder to push"
    assert "Do NOT commit or push" in rules


def test_the_brief_says_the_loop_publishes():
    rules = _rules()
    assert "the loop commits whatever you left here" in rules
    assert "runs the repo's pre-PR checks and pushes the branch" in rules
    assert "do NOT open a PR (draft or otherwise) — the loop opens it" in rules  # #207 still holds

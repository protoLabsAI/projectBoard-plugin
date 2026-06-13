"""Loop tests — config parsing, the coder prompt, and the drive state machine.

``_drive`` is the only thing that moves a feature forward (``done`` is the merge
webhook's job). These tests stub the store (``loop.get_store``), the worktree
helpers (``worktree.create_worktree`` / ``dispatch_coder`` / ``open_pr`` /
``remove_worktree``), and the delegate lookup, then assert the transitions: a
clean build → ``open_review``; an empty diff with a single coder → ``flag_blocked``;
an unconfigured coder → ``flag_blocked`` before any worktree is created.
"""

from __future__ import annotations

from project_board import worktree
from project_board.loop import BoardLoop


class FakeLoopStore:
    def __init__(self):
        self.calls = []

    def current_tier(self, fid):
        return "fast"

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.calls.append(("flag_blocked", fid, reason))
        return {"id": fid}

    def names(self):
        return [c[0] for c in self.calls]


FEATURE = {
    "id": "bd-1",
    "title": "Add a thing",
    "repo": "/repo",
    "base_branch": "main",
    "spec": "do the thing",
    "design": "",
    "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
    "files_to_modify": ["a.py", "b.py"],
}


# ── config parsing ──────────────────────────────────────────────────────────────


def test_config_defaults():
    loop = BoardLoop({})
    assert loop.coder_name == "proto" and loop.reviewer_name == "quinn"
    assert loop.review_dispatch is False
    assert loop.interval == 30 and loop.enabled is False
    assert loop.escalation_on is False  # no coders map → single-coder mode


def test_escalation_on_with_two_distinct_coders():
    loop = BoardLoop({"coders": {"fast": "proto", "smart": "proto-smart"}})
    assert loop.escalation_on is True


# ── the coder prompt (ProtoMaker discipline: name the files, demand the diff) ────


def test_build_prompt_is_imperative_and_lists_the_files():
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Add a thing" in prompt
    assert "do the thing" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt
    assert "WHEN x THE SYSTEM SHALL y" in prompt
    assert "make all the edits here, now" in prompt.lower()


# ── _drive: the state machine ───────────────────────────────────────────────────


async def _drive_with(monkeypatch, *, open_pr, coder=object()):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions."""
    store = FakeLoopStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: coder)
    await loop._drive(FEATURE)
    return loop, store


async def test_drive_opens_review_on_a_clean_build(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._active is None  # a completed drive leaves nothing to reap


async def test_drive_blocks_on_an_empty_diff_with_a_single_coder(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    # No escalation ladder (single coder) → a capability failure blocks immediately.
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._active is None


async def test_drive_blocks_when_the_coder_is_not_configured(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not be reached")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, coder=None)
    assert store.names() == ["flag_blocked"]  # blocked before any worktree work

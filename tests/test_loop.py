"""Loop tests — config parsing, the coder prompt, and the drive state machine.

``_drive`` is the only thing that moves a feature forward (``done`` is the merge
webhook's job). These tests stub the store (``loop.get_store``), the worktree
helpers (``worktree.create_worktree`` / ``dispatch_coder`` / ``open_pr`` /
``remove_worktree``), and the delegate lookup, then assert the transitions: a
clean build → ``open_review``; an empty diff with a single coder → ``flag_blocked``;
an unconfigured coder → ``flag_blocked`` before any worktree is created.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from project_board import coder_seam
from project_board import store as store_mod
from project_board import worktree
import project_board.loop as loop_mod
from project_board.loop import (
    _MERGED_VERIFIED_SHA_LEN,
    BoardLoop,
    _ci_failure_reason,
    _inject_source_issue_line,
    _pr_body,
    _resolve_gate_cmd,
    _source_issue,
    _source_issue_still_open,
)
from project_board.store import BeadsBoard, BoardError


class FakeLoopStore:
    def __init__(self):
        self.calls = []
        self.gens_spent = {}  # fid -> cumulative gens (record_gens_spent)

    def current_tier(self, fid):
        return "fast"

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.calls.append(("flag_blocked", fid, reason))
        return {"id": fid}

    def record_attempt(self, fid, *, tier, outcome):
        self.calls.append(("record_attempt", fid, tier, outcome))
        return {"id": fid}

    def record_gens_spent(self, fid, n):
        self.gens_spent[fid] = self.gens_spent.get(fid, 0) + n
        return {"id": fid}

    def record_verified_candidate(self, fid, *, branch, sha, worktree):
        self.calls.append(("record_verified", fid, branch, sha, worktree))
        return {"id": fid}

    def clear_verified_candidate(self, fid):
        self.calls.append(("clear_verified", fid))
        return {"id": fid}

    def cancel_feature(self, fid, reason=""):
        self.calls.append(("cancel_feature", fid, reason))
        return {"id": fid}

    def set_requirements(self, fid, items):
        self.calls.append(("set_requirements", fid, items))
        return {"id": fid, "requirements": items}

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


async def _noop_coro():
    """Async no-op — used to patch BoardLoop._run in startup-log tests."""


# ── config parsing ──────────────────────────────────────────────────────────────


def test_config_defaults():
    loop = BoardLoop({})
    # v0.42.0: NO default coder — unset is "" (a setup-preflight gap), not "proto".
    assert loop.coder_name == "" and loop.reviewer_name == "quinn"
    assert loop.review_dispatch is False
    assert loop.interval == 30 and loop.enabled is False
    assert loop.escalation_on is False  # no coders map → single-coder mode
    assert loop.max_concurrent == 1  # serial by default
    assert loop.merge_poll is True and loop.merge_poll_interval == 60


def test_escalation_on_with_two_distinct_coders():
    loop = BoardLoop({"coders": {"fast": "proto", "smart": "proto-smart"}})
    assert loop.escalation_on is True


def test_max_concurrent_floors_at_one():
    assert BoardLoop({"max_concurrent": 0}).max_concurrent == 1
    assert BoardLoop({"max_concurrent": 4}).max_concurrent == 4


# ── reload(): the live concurrency knobs land on the RUNNING loop ────────────────


class _HostConfig:
    """Shape of what the host hands a surface's reload hook: a LangGraphConfig whose
    `.plugin_config[section]` is the resolved (manifest defaults ⊕ YAML) section."""

    def __init__(self, section):
        self.plugin_config = {"project_board": section}


def test_reload_applies_every_live_knob_and_returns_the_diff():
    loop = BoardLoop({"max_concurrent": 1, "max_pending_reviews": 5, "max_concurrent_sessions": 0})
    changed = loop.reload(_HostConfig({"max_concurrent": 6, "max_pending_reviews": 12, "max_concurrent_sessions": 2}))
    assert changed == {"max_concurrent": (1, 6), "max_pending_reviews": (5, 12), "max_concurrent_sessions": (0, 2)}
    assert (loop.max_concurrent, loop.max_pending_reviews, loop.max_concurrent_sessions) == (6, 12, 2)
    # Idempotent: the same payload again is a no-op diff.
    assert (
        loop.reload(_HostConfig({"max_concurrent": 6, "max_pending_reviews": 12, "max_concurrent_sessions": 2})) == {}
    )


def test_reload_accepts_a_bare_section_dict_or_a_wrapped_one():
    loop = BoardLoop({})
    assert loop.reload({"max_concurrent": 3}) == {"max_concurrent": (1, 3)}
    assert loop.reload({"project_board": {"max_concurrent": 4}}) == {"max_concurrent": (3, 4)}
    assert loop.reload(None) == {} and loop.reload("nonsense") == {}


def test_reload_floors_like_the_constructor_and_keeps_bad_values(caplog):
    """A Settings typo must not stall the loop: a non-integer keeps the current knob
    (with a warning); a below-floor value floors exactly as construction does; a
    payload missing the key leaves that knob alone."""
    loop = BoardLoop({"max_concurrent": 2, "max_pending_reviews": 5})
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert loop.reload({"max_concurrent": "lots"}) == {}
    assert loop.max_concurrent == 2
    assert "is malformed" in caplog.text
    assert loop.reload({"max_concurrent": 0, "max_pending_reviews": -3}) == {
        "max_concurrent": (2, 1),
        "max_pending_reviews": (5, 0),
    }
    assert loop.reload({"max_pending_reviews": 7}) == {"max_pending_reviews": (0, 7)}
    assert loop.max_concurrent == 1  # untouched by a payload without the key


def test_reload_flips_auto_merge_and_rejects_garbage():
    loop = BoardLoop({})
    assert loop.auto_merge is False
    assert loop.reload({"auto_merge": True}) == {"auto_merge": (False, True)}
    assert loop.reload({"auto_merge": "false"}) == {"auto_merge": (True, False)}  # YAML string spelling
    assert loop.reload({"auto_merge": "maybe"}) == {}  # malformed → keep
    assert loop.auto_merge is False


def test_reload_only_touches_live_knobs():
    loop = BoardLoop({"coder_timeout_s": 1800, "loop_interval_s": 30, "coders": {"smart": "a"}})
    assert loop.reload({"coder_timeout_s": 5, "loop_interval_s": 1, "coders": {"smart": "b"}}) == {}
    assert loop.coder_timeout == 1800 and loop.interval == 30 and loop.coders == {"smart": "a"}


def test_reload_applies_coder_live_and_keeps_cfg_in_step():
    """v0.42.0 (review on #212): `coder` is a live string knob — the drive resolves
    `self.coder_name` per attempt, and the setup preflight reads `self.cfg`, so a
    Settings save that names the coder clears the gap on the RUNNING loop."""
    loop = BoardLoop({"coder": ""})
    assert loop.reload({"coder": "  proto "}) == {"coder": ("", "proto")}
    assert loop.coder_name == "proto" and loop.cfg["coder"] == "proto"
    assert loop.reload({"coder": "proto"}) == {}  # steady
    assert loop.reload({"coder": ""}) == {"coder": ("proto", "")}  # can be unset live too
    assert loop.coder_name == "" and loop.cfg["coder"] == ""


def test_max_mode_n_parsing():
    assert BoardLoop({}).max_mode_n == 1  # off by default
    assert BoardLoop({"max_mode_n": 5}).max_mode_n == 5
    assert BoardLoop({"max_mode_n": 0}).max_mode_n == 1  # floors at 1 (never < 1)


# ── the coder prompt (ProtoMaker discipline: name the files, demand the diff) ────


def test_build_prompt_is_imperative_and_lists_the_files():
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Add a thing" in prompt
    assert "do the thing" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt
    assert "WHEN x THE SYSTEM SHALL y" in prompt
    assert "make all the edits here, now" in prompt.lower()


def test_build_prompt_requires_tests():
    """The coder's definition of done includes writing tests — the #897 lesson:
    a feature merged testless because nothing in the prompt or gate mandated it."""
    prompt = BoardLoop({})._build_prompt(FEATURE).lower()
    assert "automated tests" in prompt
    assert "definition of done" in prompt
    assert "rejected before the pr opens" in prompt


def test_build_prompt_asks_for_a_clean_pr_summary_not_raw_reasoning():
    """The coder's reply feeds the PR body through `_pr_body`, which keeps only the
    `## Summary` section — so the prompt must ask for one explicitly, or every PR
    falls back to the bare no-summary template."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "final message becomes the pr description" in prompt.lower()
    assert "do not narrate your process" in prompt.lower()


def test_queue_review_feedback_reaches_the_next_prompt():
    """AC (bd-171): a /review bounce stashed via queue_review_feedback rides the SAME
    prompt path as an in-loop CI/review bounce — _build_prompt drains it into
    _ci_feedback, leads the prompt with it, and clears the one-shot pending entry."""
    from project_board.loop import _PENDING_FEEDBACK, queue_review_feedback

    _PENDING_FEEDBACK.clear()
    loop = BoardLoop({})
    queue_review_feedback("bd-1", "the auth check is missing a null guard")  # FEATURE id is bd-1
    prompt = loop._build_prompt(FEATURE)
    assert "REJECTED" in prompt  # the previous-attempt-rejected block fires
    assert "null guard" in prompt  # the findings text reached the dispatch prompt
    assert "bd-1" not in _PENDING_FEEDBACK  # drained one-shot
    assert loop._ci_feedback.get("bd-1")  # promoted into the per-run feedback lever


def test_queue_review_feedback_ignores_blank_findings():
    from project_board.loop import _PENDING_FEEDBACK, queue_review_feedback

    _PENDING_FEEDBACK.clear()
    queue_review_feedback("bd-9", "   ")
    assert "bd-9" not in _PENDING_FEEDBACK  # nothing to carry back


# ── repo standing gate files (#108) ──────────────────────────────────────────────


def test_gate_files_config_defaults_empty():
    """`project_board.gate_files` is per-repo, default empty (this repo has no
    CHANGELOG, so it declares none). Accepts a list or a comma/space string,
    de-duplicated with order preserved (mirrors env_passthrough)."""
    assert BoardLoop({}).gate_files == []
    assert BoardLoop({"gate_files": []}).gate_files == []
    assert BoardLoop({"gate_files": ["CHANGELOG.md", "docs/api.json"]}).gate_files == [
        "CHANGELOG.md",
        "docs/api.json",
    ]
    # string form: comma- or whitespace-separated, same result
    assert BoardLoop({"gate_files": "CHANGELOG.md, docs/api.json"}).gate_files == ["CHANGELOG.md", "docs/api.json"]
    assert BoardLoop({"gate_files": "CHANGELOG.md docs/api.json"}).gate_files == ["CHANGELOG.md", "docs/api.json"]
    # dedup, order preserved; blanks dropped
    assert BoardLoop({"gate_files": ["a.py", "a.py", "  ", "b.py"]}).gate_files == ["a.py", "b.py"]


def test_build_prompt_appends_repo_gate_files():
    """AC (#108): the prompt-assembly seam appends `project_board.gate_files` as a
    SEPARATE block — repo-wide obligations a tight `files_to_modify` would suppress,
    which a card author can't enumerate per repo."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md", "docs/openapi.json"]})
    prompt = loop._build_prompt(FEATURE)
    assert "Repo standing gate files" in prompt
    assert "- CHANGELOG.md" in prompt and "- docs/openapi.json" in prompt
    # the card's own files still appear — gate files are additive, not a replacement
    assert "- a.py" in prompt and "- b.py" in prompt


def test_build_prompt_omits_gate_files_block_when_none_configured():
    """Default (empty) → no block at all, so a repo that declares none (e.g. this one,
    no CHANGELOG) gets the unchanged prompt."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Repo standing gate files" not in prompt


def test_gate_files_are_not_ledger_items_nor_files_to_modify():
    """Composes with #113: gate files are a prompt-assembly ADDITION, not ledger items
    and not merged into the feature's `files_to_modify` — the block is distinct from
    the `## Files to create / modify` list and the `## Requirements ledger`."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md"]})
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    prompt = loop._build_prompt(feature)
    # gate file lands under its own heading, NOT under Files to create / modify
    # ([0] is the file list itself — everything up to the next `##` heading)
    files_section = prompt.split("## Files to create / modify")[1].split("##")[0]
    assert "CHANGELOG.md" not in files_section
    assert "- a.py" in files_section  # sanity: this IS the file-list section
    # the standing block does not leak into / become a ledger requirement line
    ledger_section = prompt.split("## Requirements ledger")[1]
    assert "CHANGELOG.md" not in ledger_section
    # …and the feature's own files_to_modify was never mutated by prompt assembly
    assert feature["files_to_modify"] == ["a.py", "b.py"]


# ── repo conventions (#108) ──────────────────────────────────────────────────────

_CONVENTIONS = (
    "- CI runs `ruff check . && ruff format --check .` — lint and format must pass.\n"
    "- Every PR must include a `changelog.d/<issue>.<kind>.md` fragment.\n"
    "- `plugins/docs/nav.json` is GENERATED — never edit it directly.\n"
    "- If a convention named here does not exist in the repo, STOP and say so."
)


def test_repo_conventions_config_defaults_empty():
    """`project_board.repo_conventions` is per-repo free-text markdown, default empty
    (backwards-compatible). Read verbatim — no parsing, unlike `gate_files`."""
    assert BoardLoop({}).repo_conventions == ""
    assert BoardLoop({"repo_conventions": ""}).repo_conventions == ""
    assert BoardLoop({"repo_conventions": _CONVENTIONS}).repo_conventions == _CONVENTIONS
    # a None in config coerces to "" rather than exploding on .strip() downstream
    assert BoardLoop({"repo_conventions": None}).repo_conventions == ""


def test_build_prompt_injects_repo_conventions():
    """AC (#108): when set, the conventions ride the dispatch prompt verbatim as a
    distinct `## Repo conventions` block — the standing RULES a card author can't
    restate per-card (what CI runs, required fragment formats, generated files)."""
    loop = BoardLoop({"repo_conventions": _CONVENTIONS, "gate_files": ["CHANGELOG.md"]})
    prompt = loop._build_prompt(FEATURE)
    assert "## Repo conventions" in prompt
    assert _CONVENTIONS in prompt  # injected verbatim, not reformatted
    # sits AFTER the gate-files block (its natural neighbour — both repo-wide, not per-card)
    assert prompt.index("## Repo conventions") > prompt.index("Repo standing gate files")
    # the card's own task/files are untouched — the block is additive
    assert "- a.py" in prompt and "do the thing" in prompt


def test_build_prompt_omits_repo_conventions_when_empty():
    """Default (empty/absent) → no block at all, so a repo that declares no conventions
    gets the unchanged prompt (backwards-compatible)."""
    assert "## Repo conventions" not in BoardLoop({})._build_prompt(FEATURE)
    # whitespace-only is treated as empty too
    assert "## Repo conventions" not in BoardLoop({"repo_conventions": "   \n  "})._build_prompt(FEATURE)


def test_is_test_path_classification():
    """The deterministic gate's path classifier — what counts as a test vs code."""
    from project_board.loop import _is_code_path, _is_test_path

    for p in ("tests/test_inbox.py", "test_x.py", "inbox/foo_test.py", "conftest.py", "web/x.test.tsx"):
        assert _is_test_path(p), p
    for p in ("inbox/store.py", "README.md", "config.yaml"):
        assert not _is_test_path(p), p
    assert _is_code_path("inbox/store.py") and _is_code_path("web/x.tsx")
    assert not _is_code_path("README.md") and not _is_code_path("config.yaml")


def test_format_cmd_parsed_from_config():
    assert BoardLoop({}).format_cmd == ""  # off by default
    assert BoardLoop({"format_cmd": "ruff check --fix ."}).format_cmd == "ruff check --fix ."


async def test_run_fixups_noop_when_unset(monkeypatch):
    """No format_cmd → _run_fixups must not shell out (it's the pre-PR auto-fix hook)."""
    loop = BoardLoop({})
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    await loop._run_fixups("/wt")
    assert not shelled


# ── pre-PR local gate (bd-xbh) ───────────────────────────────────────────────────


def test_local_gate_config_parsed():
    assert BoardLoop({}).local_gate_cmd == ""  # off by default
    assert BoardLoop({}).local_gate_max == 2
    loop = BoardLoop({"local_gate_cmd": "ruff check .", "local_gate_max": 1})
    assert loop.local_gate_cmd == "ruff check ." and loop.local_gate_max == 1


async def test_run_local_gate_noop_when_unset(monkeypatch):
    """No local_gate_cmd → never shells out."""
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    assert await BoardLoop({})._run_local_gate("/wt") is None
    assert not shelled


async def test_run_local_gate_passes_and_captures_failure(tmp_path):
    """Exit 0 → None (pass); non-zero → captured output for the coder."""
    assert await BoardLoop({"local_gate_cmd": "exit 0"})._run_local_gate(str(tmp_path)) is None
    out = await BoardLoop({"local_gate_cmd": "echo boom 1>&2; exit 1"})._run_local_gate(str(tmp_path))
    assert out is not None and "boom" in out


async def test_run_local_gate_signal_kill_is_no_verdict_not_a_failure(tmp_path, caplog):
    """A gate process killed by a signal (member shutdown delivering SIGTERM, an operator
    kill, the OOM killer) has no verdict — it must degrade to pass like a timeout does,
    not surface as "gate FAILED" with a half-finished pytest transcript. Before this a
    restart that landed mid merged-state gate flag_blocked a feature whose PR CI was
    fully green (2026-08-20, bd-k2j)."""
    # The shell kills ITSELF with SIGTERM after emitting partial output → returncode -15.
    cmd = "echo 'tests/x.py ....... [ 13%]'; kill -TERM $$"
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        out = await BoardLoop({"local_gate_cmd": cmd})._run_local_gate(str(tmp_path))
    assert out is None
    assert "killed by signal 15" in caplog.text


async def test_run_local_gate_degrades_to_pass_on_launch_error(monkeypatch):
    """A gate that can't even spawn must not block — it degrades to pass (CI gates)."""

    async def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _boom)
    assert await BoardLoop({"local_gate_cmd": "anything"})._run_local_gate("/wt") is None


# ── _drive: the state machine ───────────────────────────────────────────────────


async def _drive_with(
    monkeypatch, *, open_pr, coder=object(), dispatch=None, cfg=None, gate=None, judge=None, seed=None, feature=None
):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions.

    ``judge`` stubs ``_judge_candidates`` (Max-Mode best-of-N); ``seed`` is a callable
    run on the loop before the drive (e.g. to pre-seed _ci_feedback for a CI-bounce test)."""
    store = FakeLoopStore()
    store.creates = []  # fids create_worktree was called for (a goal-fix retry reuses, so won't re-create)
    store.removes = []  # worktrees remove_worktree was called for
    store.reaps = []  # fids reap_feature_worktree was called for (Max-Mode loser teardown)
    store.promotes = []  # (src_wt, src_branch, fid) the Max-Mode winner was promoted with
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        store.creates.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _default_dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return None

    async def _reap(repo, root, fid):
        store.reaps.append(fid)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees"):
        store.promotes.append((src_wt, src_branch, fid))
        return ("/wt/feat-" + fid, "feat/" + fid)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch or _default_dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    loop = BoardLoop(cfg or {"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: coder)
    if gate is not None:
        monkeypatch.setattr(loop, "_run_local_gate", gate)
    if judge is not None:
        monkeypatch.setattr(loop, "_judge_candidates", judge)
    if seed is not None:
        seed(loop)
    await loop._drive(feature if feature is not None else FEATURE)
    return loop, store


async def test_drive_opens_review_on_a_clean_build(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._inflight == {}  # a completed drive leaves nothing to reap


async def test_drive_pr_body_is_the_summary_not_the_raw_stream(monkeypatch):
    """open_pr must receive `_pr_body`'s output, never the coder's raw reply (#56)."""
    bodies = []

    async def _open_pr(wt, branch, *, base, title, body):
        bodies.append(body)
        return "https://example/pr/9"

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "I first looked at loop.py.\nLet me wire it up.\n## Summary\n\n- Wired the helper\n"

    await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    assert bodies == ["## Summary\n\n- Wired the helper"]


# ── Max-Mode: N parallel candidates → judge → promote winner → ship (#21) ────────


async def test_drive_max_mode_fans_out_and_ships_the_winner(monkeypatch):
    """max_mode_n=3 → 3 candidate worktrees built + dispatched in parallel, the judge
    picks one, the winner is promoted to the canonical name, the losers are reaped, and
    ONLY the winner's PR opens (on the canonical branch)."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append((wt, branch))
        return "https://example/pr/7"

    dispatched = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(wt)
        return f"reply from {wt}"

    async def _judge(feature, base, worktrees):
        assert len(worktrees) == 3  # the judge sees every candidate
        return 2  # candidate index 2 wins

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
    )
    # Three candidate worktrees, suffixed so none collides with the canonical name.
    assert store.creates == ["bd-1.c0", "bd-1.c1", "bd-1.c2"]
    assert len(dispatched) == 3  # all three coders ran
    # The winner (c2) is promoted to canonical; the two losers are reaped (winner is not).
    assert store.promotes == [("/wt/feat-bd-1.c2", "feat/bd-1.c2", "bd-1")]
    assert set(store.reaps) == {"bd-1.c0", "bd-1.c1"}
    # Only the winner's PR opens, on the canonical branch.
    assert opened == [("/wt/feat-bd-1", "feat/bd-1")]
    assert ("open_review", "bd-1", "https://example/pr/7") in store.calls
    assert loop._inflight == {}


async def test_drive_max_mode_all_empty_reaps_all_and_blocks(monkeypatch):
    """Every candidate empty → judge returns None → all candidates reaped, no PR, and
    the feature blocks (NoChangesError, single coder with no ladder)."""

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("no PR should open when every candidate is empty")

    async def _judge(feature, base, worktrees):
        return None  # nothing to ship

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
    )
    assert set(store.reaps) == {"bd-1.c0", "bd-1.c1", "bd-1.c2"}  # every candidate torn down
    assert store.promotes == []  # nothing promoted
    assert "flag_blocked" in store.names()


async def test_drive_max_mode_skips_fanout_on_a_carried_forward_fix(monkeypatch):
    """A re-dispatch carrying _ci_feedback (a CI bounce / goal-fix / gate-fix) FIXES the
    existing diff with ONE coder — Max-Mode must not re-fan-out N candidates."""

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/9"

    async def _judge(feature, base, worktrees):
        raise AssertionError("the judge must not run on a single-candidate carried-forward fix")

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
        seed=lambda lp: lp._ci_feedback.__setitem__("bd-1", "CI failed: lint"),
    )
    assert store.creates == ["bd-1"]  # one canonical worktree, NOT N suffixed candidates
    assert store.promotes == [] and store.reaps == []
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_drive_local_gate_failure_redispatches_then_opens(monkeypatch):
    """A pre-PR gate failure re-dispatches the SAME tier with the output injected,
    REUSING the worktree (one create), then opens the PR once the gate passes."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    gate_seq = iter(["FAILED tests/test_config.py::golden - boom", None])

    async def _gate(wt, feature=None):
        return next(gate_seq)

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 2},
    )
    assert len(prompts) == 2  # initial + 1 gate-fix re-dispatch
    assert store.creates == ["bd-1"]  # keep-worktree → only one worktree created
    assert "boom" in prompts[1]  # the gate output was carried into the retry prompt
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._gate_fix_attempts.get("bd-1", 0) == 0  # budget reset once the PR opened


async def test_drive_local_gate_exhausted_opens_pr_anyway(monkeypatch):
    """A persistent gate failure opens the PR after local_gate_max tries — never
    blocks (CI + the ci-fix budget are the backstop)."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/2"

    async def _gate(wt, feature=None):
        return "still red"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 1},
    )
    assert len(prompts) == 2  # initial + 1 (local_gate_max) then opens anyway
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)  # never blocked


async def test_drive_blocks_on_an_empty_diff_with_a_single_coder(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    # No diff + no tool activity ⇒ empty_result (#198): one same-tier retry, then
    # blocked for triage (single coder — there's no ladder to consult anyway).
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


# ── empty_result: a completed dispatch with no diff and no tool activity (#198) ──


def test_empty_result_max_config():
    assert BoardLoop({}).empty_result_max == 2
    assert BoardLoop({"empty_result_max": 3}).empty_result_max == 3
    assert BoardLoop({"empty_result_max": 0}).empty_result_max == 1  # floored — 0 could never block


async def test_drive_classifies_empty_result_and_records_the_stop_reason(monkeypatch):
    """No worktree diff + no tool-call activity ⇒ the attempt is `empty_result`, its
    own failure class (#198) — recorded on the attempt with the ACP adapter's
    stop-reason so the retro and the monitor drawer can show WHY the coder produced
    nothing. After empty_result_max (default 2) occurrences the feature blocks with
    a reason naming the class and the evidence."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        # The gen buffer exists (dispatch_coder_tapped ran progress_begin before
        # falling back to worktree.dispatch_coder in a host-free test env); simulate
        # the tap stashing the adapter's stop-reason. NO tool events — the coder
        # connected but never executed.
        coder_seam.progress_stop_reason("bd-1", 1, "refusal")
        return ""

    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits vs base — nothing to PR")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    # Every empty occurrence is recorded on the attempt, class + stop-reason included.
    attempts = [c for c in store.calls if c[0] == "record_attempt"]
    assert len(attempts) == 2 and len(prompts) == 2  # blocked after N=2, not before
    assert all("empty_result" in a[3] for a in attempts)
    assert all("stop_reason=refusal" in a[3] for a in attempts)
    # The block reason names the failure class and the evidence.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    assert reason.startswith("empty_result")
    assert "empty coder reply — no diff, no tool calls" in reason
    assert "stop_reason=refusal" in reason
    assert loop._inflight == {} and loop._empty_results == {}


async def test_drive_empty_result_does_not_escalate_the_tier(monkeypatch):
    """empty_result must NOT climb the coders ladder (#198): a stronger model can't
    fix "connecting but not executing". Same-tier retry, then Blocked — escalate()
    is never called even with a full ladder configured."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        return ""

    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    assert store.escalated == []  # the ladder was never consulted
    assert len(dispatches) == 2  # same-tier retry, then blocked
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and "empty coder reply — no diff, no tool calls" in blocked[0][2]


async def test_drive_no_diff_with_tool_activity_still_escalates(monkeypatch):
    """The existing capability class is untouched (#198 narrows only the truly-empty
    case): a no-diff dispatch that DID run tools still climbs the ladder."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # Real tool activity in the gen buffer — the coder executed, just shipped nothing.
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "a.py"}})
        return "I read the files but made no edits"

    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # fast → smart (one climb), smart is the top → blocked as a capability failure.
    assert [e[0] for e in store.escalated] == ["bd-1", "bd-1"]
    assert len(dispatches) == 2
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and "empty coder reply" not in blocked[0][2]


async def test_drive_empty_result_retry_recovers_and_resets_the_count(monkeypatch):
    """One empty occurrence then a real diff: the same-tier retry ships normally and
    the empty-result count resets (a later empty attempt starts a fresh window)."""
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.NoChangesError("coder produced no commits")
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls
    assert "flag_blocked" not in store.names()
    assert loop._empty_results == {}  # reset once the PR opened


# ── source-issue closed guard (#166) ────────────────────────────────────────────


async def test_source_issue_still_open_no_raw():
    """Empty source_issue → True (no gh call, proceed)."""
    assert await _source_issue_still_open("", "/wt") is True


async def test_source_issue_still_open_open_state(monkeypatch):
    """gh reports 'open' → True (proceed)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 0, "open\n", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_closed_state(monkeypatch):
    """gh reports 'closed' → False (skip PR)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 0, "closed\n", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is False


async def test_source_issue_still_open_gh_nonzero_fail_open(monkeypatch):
    """gh exits non-zero (e.g. 404) → True (fail-open)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 1, "", "not found"

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_gh_raises_fail_open(monkeypatch):
    """gh raises (e.g. WorktreeError timeout) → True (fail-open)."""

    async def _fake_gh(*args, cwd, timeout=60):
        raise worktree.WorktreeError("timed out")

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_full_url(monkeypatch):
    """Full GitHub issue URL is parsed correctly."""

    async def _fake_gh(*args, cwd, timeout=60):
        assert "repos/acme/lib/issues/7" in args
        return 0, "closed", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("https://github.com/acme/lib/issues/7", "/wt")
    assert result is False


async def test_source_issue_still_open_bare_number_resolves_slug(monkeypatch):
    """A bare issue number resolves the slug from the worktree remote."""
    called_with_slug = []

    async def _fake_slug(*, cwd):
        return "myorg/myrepo"

    async def _fake_gh(*args, cwd, timeout=60):
        called_with_slug.append(args)
        assert "repos/myorg/myrepo/issues/5" in args
        return 0, "open", ""

    monkeypatch.setattr(worktree, "repo_slug", _fake_slug)
    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("#5", "/wt")
    assert result is True
    assert called_with_slug  # gh was called


async def test_source_issue_still_open_bare_number_unresolvable_fail_open(monkeypatch):
    """Bare number with unresolvable slug → True (fail-open, no gh call)."""
    gh_called = []

    async def _fake_slug(*, cwd):
        return ""  # couldn't resolve

    async def _fake_gh(*args, cwd, timeout=60):
        gh_called.append(args)
        return 0, "closed", ""

    monkeypatch.setattr(worktree, "repo_slug", _fake_slug)
    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("#99", "/wt")
    assert result is True
    assert gh_called == []  # never called gh when slug unresolvable


async def test_drive_skips_pr_when_source_issue_closed(monkeypatch):
    """When source_issue is closed at PR-open time, no PR is created and the
    card is cancelled (cancel_feature is called with the supersede reason)."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append((wt, branch))
        return "https://example/pr/99"

    async def _closed(si_raw, cwd):
        return False

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _closed)
    feature = dict(FEATURE, source_issue="owner/repo#42")
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert opened == []  # no PR opened
    assert "cancel_feature" in store.names()
    assert "open_review" not in store.names()
    cancel_calls = [c for c in store.calls if c[0] == "cancel_feature"]
    assert any("superseded" in c[2] for c in cancel_calls)
    assert loop._inflight == {}


async def test_drive_opens_pr_when_source_issue_still_open(monkeypatch):
    """When source_issue is still open, the PR opens normally."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append("https://example/pr/1")
        return opened[-1]

    async def _still_open(si_raw, cwd):
        return True

    async def _slug(*, cwd):
        return "owner/repo"

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _still_open)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    feature = dict(FEATURE, source_issue="owner/repo#42")
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert len(opened) == 1
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert "cancel_feature" not in store.names()


# ── coder.solve() board seam (ADR 0064 P2) ───────────────────────────────────────


def test_coder_solve_config_defaults():
    loop = BoardLoop({})
    assert loop.coder_solve is True  # opt-OUT valve; the real gate is coder_seam
    assert loop.coder_solve_test_cmd == ""  # no local_gate_cmd to fall back to either
    assert loop.coder_solve_budget == 6
    assert loop.coder_solve_k == 3
    assert loop.coder_solve_tree_depth == 2
    assert loop.coder_solve_test_timeout == 300
    assert loop.max_concurrent_sessions == 0  # default: unlimited within the k budget


def test_max_concurrent_sessions_config():
    assert BoardLoop({"max_concurrent_sessions": 1}).max_concurrent_sessions == 1
    assert BoardLoop({"max_concurrent_sessions": 4}).max_concurrent_sessions == 4
    assert BoardLoop({"max_concurrent_sessions": -1}).max_concurrent_sessions == 0  # floors at 0


async def test_coder_solve_startup_log_notes_peak_sessions_when_k_gt_1(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 3, "max_concurrent": 2, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert peak_lines, "expected a startup INFO line noting peak concurrent ACP sessions"
    assert "2" in peak_lines[0] and "3" in peak_lines[0] and "6" in peak_lines[0]


async def test_coder_solve_startup_log_silent_when_k_is_one(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 1, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert not peak_lines, "no peak-sessions log expected when coder_solve_k=1"


async def test_coder_solve_startup_log_notes_cap_when_max_concurrent_sessions_set(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 3, "max_concurrent_sessions": 1, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert peak_lines
    assert "capped at 1" in peak_lines[0]


def test_coder_solve_test_cmd_falls_back_to_local_gate_cmd():
    assert BoardLoop({"local_gate_cmd": "pytest -q"}).coder_solve_test_cmd == "pytest -q"
    loop = BoardLoop({"local_gate_cmd": "pytest -q", "coder_solve_test_cmd": "pytest tests/unit -q"})
    assert loop.coder_solve_test_cmd == "pytest tests/unit -q"  # explicit wins over the fallback


def test_use_coder_solve_requires_the_opt_out_flag_plus_the_seam_gate(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())  # pretend `coder` is installed
    on = BoardLoop({"local_gate_cmd": "pytest -q"})
    assert on._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is True
    assert on._use_coder_solve({"acceptance_criteria": ""}) is False  # no oracle → degrade

    off = BoardLoop({"local_gate_cmd": "pytest -q", "coder_solve": False})
    assert off._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False  # opted out


def test_use_coder_solve_false_when_coder_plugin_unavailable(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: None)  # coder plugin absent/disabled
    loop = BoardLoop({"local_gate_cmd": "pytest -q"})
    assert loop._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False


def test_use_coder_solve_false_without_a_test_command(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    loop = BoardLoop({})  # no local_gate_cmd, no coder_solve_test_cmd
    assert loop._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False


async def _pass_gate(wt, feature=None):
    """A stand-in for `_run_local_gate` — pass immediately. These tests set
    `local_gate_cmd` (needed as the coder_solve_test_cmd fallback) but the drive's
    fake worktree paths don't exist on disk, so the REAL gate would just shell out
    against a bogus cwd; stub it rather than rely on that degrading to a pass."""
    return None


async def test_drive_uses_coder_solve_when_available_and_records_gens(monkeypatch):
    """coder available + acceptance present + a test command → the solve path runs
    INSTEAD of the single delegate_to(acp) shot, and gens-spent lands on the feature
    via store.record_gens_spent (so portfolio_rollup can read it)."""
    from project_board import coder_seam

    seen = {}

    async def _fake_dispatch(
        *,
        task,
        coder,
        repo,
        base,
        root,
        fid,
        dispatch_timeout,
        test_cmd,
        test_timeout,
        budget,
        k,
        tree_depth,
        record_gens=None,
        fusion_delegate=None,
        fusion_k=2,
        files_to_modify=None,
        fusion_max_file_chars=None,
        env_passthrough=(),
        tier="",
        record_verified=None,
        commit_message="",
        max_concurrent_sessions=0,
    ):
        seen["fid"] = fid
        seen["test_cmd"] = test_cmd
        seen["task"] = task
        seen["env_passthrough"] = env_passthrough
        seen["tier"] = tier
        seen["commit_message"] = commit_message
        record_gens(4)
        # dispatch() calls this at the verify boundary (#91) — the loop must have
        # threaded a recorder that lands the record on THIS feature's bead.
        record_verified(f"feat/{fid}", "abc123", f"/wt/feat-{fid}")
        return (f"/wt/feat-{fid}", f"feat/{fid}", "[coder.solve rung=best-of-k gens=4] solved")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _fake_dispatch)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/42"

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}, gate=_pass_gate
    )
    assert seen["fid"] == "bd-1" and seen["test_cmd"] == "pytest -q"
    assert "Add a thing" in seen["task"]  # the same built prompt, not a different one
    assert seen["commit_message"] == "feat: Add a thing"  # the verified commit keeps the PR title
    assert store.gens_spent.get("bd-1") == 4
    # The verify-boundary salvage record (#91) landed on the bead via the store.
    assert ("record_verified", "bd-1", "feat/bd-1", "abc123", "/wt/feat-bd-1") in store.calls
    assert ("open_review", "bd-1", "https://example/pr/42") in store.calls
    assert store.creates == []  # solve()'s own per-candidate worktrees replaced the single create


async def test_drive_skips_fusion_for_a_dispatch_when_files_are_oversized(monkeypatch, tmp_path):
    """Fusion can't tool-call and returns whole-file replacements — an oversized
    file must gate BEFORE dispatch (fusion_delegate=None for that dispatch), not
    get attempted and risk a silently truncated rewrite. The ladder still runs
    (greedy/best-of-k/tree-search), it just skips the fusion rung."""
    from project_board import coder_seam

    (tmp_path / "big.py").write_text("x" * 1000)
    seen = {}

    async def _fake_dispatch(*, fusion_delegate=None, **kw):
        seen["fusion_delegate"] = fusion_delegate
        return (f"/wt/feat-{kw['fid']}", f"feat/{kw['fid']}", "[coder.solve rung=greedy gens=1] solved")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _fake_dispatch)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/42"

    feature = {**FEATURE, "repo": str(tmp_path), "files_to_modify": ["big.py"]}
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={
            "coder": "proto",
            "local_gate_cmd": "pytest -q",
            "coder_solve_fusion_delegate": "fusion-model",
            "coder_solve_fusion_max_file_chars": 10,
        },
        gate=_pass_gate,
        feature=feature,
    )
    assert seen["fusion_delegate"] is None  # gated out before dispatch, not attempted


async def test_drive_falls_back_to_single_shot_without_acceptance_criteria(monkeypatch):
    """Honest degrade: even with the coder plugin available and a test command
    configured, a feature with NO acceptance criteria takes today's single
    delegate_to(acp) shot — never a silent best-of-k."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run without acceptance criteria")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    feature = dict(FEATURE, acceptance_criteria="")
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={"coder": "proto", "local_gate_cmd": "pytest -q"},
        feature=feature,
        gate=_pass_gate,
    )
    assert store.creates == ["bd-1"]  # the plain single-worktree path ran
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls


async def test_drive_falls_back_to_single_shot_when_coder_plugin_unavailable(monkeypatch):
    """Honest degrade: acceptance criteria + a test command present, but `coder`
    itself isn't installed/enabled — still the single shot, never a fake ladder."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: None)

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run when the coder plugin is unavailable")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/2"

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}, gate=_pass_gate
    )
    assert store.creates == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls


async def test_drive_falls_back_to_single_shot_without_a_test_command(monkeypatch):
    """Honest degrade: `coder` available + acceptance present, but NO test command
    configured (no coder_solve_test_cmd, no local_gate_cmd) — no runnable oracle, so
    the single shot runs rather than fake grounding."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run with no runnable test command")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, cfg={"coder": "proto"})  # no test cmd anywhere
    assert store.creates == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


async def test_drive_coder_solve_exhausted_blocks_like_a_capability_failure(monkeypatch):
    """A SolveExhausted (no candidate passed the acceptance tests) is treated exactly
    like NoChangesError/CoderTimeout — blocked immediately with no ladder configured."""
    from project_board import coder_seam

    async def _exhausted(**kw):
        raise coder_seam.SolveExhausted("coder.solve exhausted after 6 generation(s) (rung=best-partial): 1/3 failing")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _exhausted)

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not run — no candidate passed")

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}
    )
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


async def test_drive_coder_solve_skipped_on_a_carried_forward_ci_bounce(monkeypatch):
    """A CI-bounce re-dispatch (signalled by _ci_feedback) fixes the EXISTING diff
    with the single coder — coder.solve must not re-fan-out on that retry, same rule
    as Max-Mode."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run on a carried-forward re-dispatch")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/9"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={"coder": "proto", "local_gate_cmd": "pytest -q"},
        seed=lambda lp: lp._ci_feedback.__setitem__("bd-1", "CI failed: lint"),
        gate=_pass_gate,
    )
    assert store.creates == ["bd-1"]  # the plain single-worktree path ran, not solve()
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_drive_max_mode_wins_precedence_over_coder_solve_when_both_configured(monkeypatch):
    """A board already running Max-Mode (`max_mode_n>1`) must keep fanning out N
    candidates and judging, NOT silently switch to coder.solve's ladder, even once
    the `coder` plugin becomes importable and every one of
    coder_seam.should_use_solve's gates (acceptance criteria + a runnable test
    command) is satisfied. Pins the fix for the precedence bug: coder.solve only
    preempts Max-Mode when max_mode_n<=1. (Uses `coder_solve_test_cmd`, not
    `local_gate_cmd`, to satisfy the test-command gate without also flipping
    Max-Mode's OWN candidate-selection strategy from judge to execution-grounded —
    that's an orthogonal knob this test isn't about.)"""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())  # coder plugin available

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run — Max-Mode has precedence when max_mode_n>1")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/11"

    async def _judge(feature, base, worktrees):
        assert len(worktrees) == 3  # Max-Mode's fan-out ran, not solve()
        return 0

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3, "coder_solve_test_cmd": "pytest -q"},
    )
    assert store.creates == ["bd-1.c0", "bd-1.c1", "bd-1.c2"]  # Max-Mode's candidates, not solve()'s
    assert ("open_review", "bd-1", "https://example/pr/11") in store.calls


# ── goal-verification gate (MiMo-borrowed; opt-in `goal_verify`) ─────────────────


async def test_goal_verify_pass_opens_the_pr(monkeypatch):
    async def _ok(self, feature, wt, base, coder_reply=""):
        return None  # PASS — no gap

    monkeypatch.setattr(BoardLoop, "_verify_goal", _ok)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/9"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "goal_verify": True})
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_goal_verify_gap_retries_same_tier_then_opens(monkeypatch):
    """A goal-verify gap (e.g. missing tests) re-dispatches the SAME coder with the
    gap carried into the prompt — and opens the PR once the coder fixes it."""
    calls = {"n": 0}

    async def _verify(self, feature, wt, base, coder_reply=""):
        calls["n"] += 1
        return "missing tests for the new behavior" if calls["n"] == 1 else None  # gap once, then PASS

    monkeypatch.setattr(BoardLoop, "_verify_goal", _verify)
    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/77"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert ("open_review", "bd-1", "https://example/pr/77") in store.calls  # opened after the retry
    assert len(dispatched) == 2  # initial + 1 same-tier re-dispatch
    # keep-worktree: the retry REUSES the worktree (impl intact) — created once, never removed
    assert store.creates == ["bd-1"]  # NOT re-created for the retry
    assert store.removes == []  # not wiped between attempts
    assert "ALREADY in this worktree" in dispatched[1] and "missing tests" in dispatched[1]  # add-to-existing feedback
    assert loop._goal_fix_attempts.get("bd-1") is None  # reset once the gate passes


async def test_goal_verify_gap_exhausts_retries_then_blocks(monkeypatch):
    """A persistent gap exhausts goal_fix_max same-tier retries, then blocks — no PR."""

    async def _gap(self, feature, wt, base, coder_reply=""):
        return "AC #1 unmet: multiply() missing"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _gap)
    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append(True)
        return "https://example/pr/x"

    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(prompt)
        return "reply"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert not opened  # the gate stopped the PR from being opened
    assert len(dispatched) == 3  # initial + goal_fix_max (2) same-tier retries
    assert store.creates == ["bd-1"]  # keep-worktree: created ONCE, reused across both retries
    assert "flag_blocked" in store.names()  # then blocked for triage
    assert "open_review" not in store.names()


async def test_goal_verify_off_by_default_skips_the_gate(monkeypatch):
    called = []

    async def _spy(self, feature, wt, base):
        called.append(True)
        return "would fail if invoked"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _spy)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)  # default cfg → off
    assert not called  # the gate is never invoked when goal_verify is off
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


async def test_verify_goal_requires_a_test_deterministically(monkeypatch):
    """The gate is path-based — no LLM, no diff. A code change with no test file → gap;
    with a test → pass; docs/config-only → pass. Immune to diff truncation (the bug that
    made the old LLM verifier false-reject tests that sorted past the cap)."""
    loop = BoardLoop({"goal_verify": True})

    def _git_listing(names):
        async def _git(wt, *args, timeout=60):
            # `add -A` → empty; `diff --cached --name-only` → the changed-file list
            return (0, names if "--name-only" in args else "", "")

        return _git

    # code changed, NO test → gap
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ngraph/config.py"))
    gap = await loop._verify_goal(FEATURE, "/wt", "main")
    assert gap and "no test" in gap.lower()

    # code changed WITH a test → pass (this is the case the old verifier wrongly blocked)
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ntests/test_inbox.py"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # code changed, no test, but the coder declared NO_TEST_NEEDED → pass (escape hatch)
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py"))
    reply = "Pure rename refactor.\nNO_TEST_NEEDED: behavior unchanged, covered by existing tests"
    assert await loop._verify_goal(FEATURE, "/wt", "main", reply) is None
    # ...but without the declaration, the same change is still a gap
    assert await loop._verify_goal(FEATURE, "/wt", "main", "I changed inbox/store.py") is not None

    # docs/config only → pass (no code change → no test required)
    monkeypatch.setattr(worktree, "_git", _git_listing("README.md\ndocs/x.md\nconfig.yaml"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # empty diff → None (open_pr's NoChangesError job, not the gate's)
    monkeypatch.setattr(worktree, "_git", _git_listing(""))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None


async def test_verify_goal_fails_open_when_no_criteria(monkeypatch):
    loop = BoardLoop({"goal_verify": True})
    # No acceptance_criteria → gate must not even shell out / call the model.
    assert await loop._verify_goal({"id": "x", "acceptance_criteria": ""}, "/wt", "main") is None


async def test_drive_blocks_when_the_coder_is_not_configured(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not be reached")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, coder=None)
    assert store.names() == ["flag_blocked"]  # blocked before any worktree work


# ── _drive: failure classification + backoff (no real sleeps) ───────────────────


async def _no_sleep(_delay):
    return None


async def test_drive_retries_a_transient_failure_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.WorktreeError("git push failed: connection reset by peer")
        return "https://example/pr/1"

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert calls["n"] == 2  # one transient retry, then success
    assert "flag_blocked" not in store.names()
    assert loop._inflight == {}


async def test_drive_blocks_after_exhausting_transient_retries(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 503 service unavailable")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 3  # transient policy = 3 attempts, then Blocked
    assert loop._inflight == {}


async def test_drive_blocks_immediately_on_a_terminal_failure(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 403 forbidden — bad credential")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 1  # auth is terminal → no retry


# ── _drive: the stuck-coder watchdog (CoderTimeout) ─────────────────────────────


async def test_drive_blocks_on_a_coder_timeout_not_transient_retried(monkeypatch):
    calls = {"n": 0}

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        raise worktree.CoderTimeout("coder timed out after 1800s")

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not run after a coder timeout")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    # A timeout matches "timed out" in classify (transient), but it's a CAPABILITY
    # failure → it must NOT be transient-retried: blocked after a single attempt.
    assert calls["n"] == 1
    assert "flag_blocked" in store.names()
    assert loop._inflight == {}


class _EscalatingStore(FakeLoopStore):
    """A store that hands out one climb (fast→smart) then None (ladder top), so a
    _drive test can exercise the timeout-escalation path end to end."""

    def __init__(self, tiers):
        super().__init__()
        self._tiers = list(tiers)
        self.escalated = []

    def escalate(self, fid, reason):
        self.escalated.append((fid, reason))
        return self._tiers.pop(0) if self._tiers else None


async def test_drive_carries_timeout_context_into_the_escalated_prompt(monkeypatch):
    """A CoderTimeout that climbs the tier ladder must NOT hand the stronger model a
    byte-identical prompt (#146): the escalated dispatch leads with the ring buffer's
    timeout context — elapsed time, that no diff was produced, and the last tool/thought
    tail — injected via `_ci_feedback` so it rides the normal prompt-building path."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    # Deterministic gen clock: the timed-out gen runs 0.0s→1800.0s (elapsed 1800.0),
    # every later _monotonic() reads freeze at the tail value.
    ticks = iter([0.0, 1800.0])
    monkeypatch.setattr(coder_seam, "_monotonic", lambda: next(ticks, 1800.0))

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        if len(prompts) == 1:
            # Simulate live activity the tap would have recorded, then time out. The
            # gen buffer already exists (dispatch_coder_tapped called progress_begin
            # before falling back to worktree.dispatch_coder in a host-free test env).
            coder_seam.progress_thought("bd-1", 1, "still mapping the dispatch flow in loop.py")
            coder_seam.progress_tool(
                "bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "loop.py"}}
            )
            raise worktree.CoderTimeout("coder timed out after 1800s")
        return "the escalated coder's reply"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # It escalated once (fast→smart), redispatched, and shipped a clean PR.
    assert [e[0] for e in store.escalated] == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert len(prompts) == 2

    original, escalated = prompts
    # r1: the escalated prompt is NOT byte-identical to the one that timed out.
    assert escalated != original
    # The original carried no timeout context (nothing had failed yet).
    assert "TIMED OUT" not in original
    # r2: the escalated prompt names the elapsed time, the no-diff outcome, and the
    # last tool + thought tail mined from the progress ring buffer.
    assert "TIMED OUT" in escalated
    assert "1800.0s" in escalated
    assert "produced NO diff" in escalated
    assert "Read" in escalated and "loop.py" in escalated
    assert "still mapping the dispatch flow" in escalated
    # r3: it arrived via `_ci_feedback`, so it rides the standard rejected-attempt block.
    assert "previous attempt was REJECTED" in escalated
    assert "still mapping the dispatch flow" in loop._ci_feedback.get("bd-1", "")


# ── concurrency: _spawn_ready claims up to max_concurrent ────────────────────────


class _ClaimStore:
    """A peekable ready queue + atomic claim(fid), mirroring the store API _spawn_ready
    now uses. Records claims so a test can prove the caps/gates stop the puller."""

    def __init__(self, features, in_review=0):
        self._features = [dict(f) for f in features]
        self._in_review = in_review
        self.claimed = []
        self.last_relaxed = None

    def ready_queue(self, relaxed=False):
        self.last_relaxed = relaxed
        return [f for f in self._features if f["id"] not in self.claimed]

    def claim(self, fid, assignee=""):
        if fid in self.claimed:
            return None
        self.claimed.append(fid)
        return next((f for f in self._features if f["id"] == fid), None)

    def list_features(self, state=None):
        return [{"id": f"rev-{i}"} for i in range(self._in_review)] if state == "in_review" else []


def _ready(fid, files, project=""):
    return {"id": fid, "board_state": "ready", "files_to_modify": files, "project": project}


async def _hold_drives(loop, monkeypatch):
    """Replace _drive with a coroutine that blocks, so spawned tasks stay 'running'.
    Returns a finalizer the test calls to release + await them."""
    release = asyncio.Event()

    async def _hold(feature):
        await release.wait()

    monkeypatch.setattr(loop, "_drive", _hold)

    async def _finish():
        release.set()
        await asyncio.gather(*loop._drives, return_exceptions=True)

    return _finish


async def test_spawn_ready_claims_up_to_max_concurrent(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert loop._spawn_ready() is True
        assert len(loop._drives) == 2  # capped at max_concurrent
        assert store.claimed == ["bd-1", "bd-2"]  # stopped claiming once full
    finally:
        await finish()


async def test_reload_raises_the_cap_for_the_next_claim_scan_without_a_restart(monkeypatch):
    """The bug this fixes: six projects' worth of ready cards behind a one-slot loop,
    and a max_concurrent edit that only landed after a restart. After reload() the
    very next _spawn_ready claims up to the NEW cap on the SAME running loop."""
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        assert store.claimed == ["bd-1"]  # serial: one slot
        assert loop.reload(_HostConfig({"max_concurrent": 3})) == {"max_concurrent": (1, 3)}
        loop._spawn_ready()
        assert store.claimed == ["bd-1", "bd-2", "bd-3"]  # both extra slots filled this tick
        assert len(loop._drives) == 3
    finally:
        await finish()


async def test_reload_lowering_the_cap_never_kills_a_drive(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        assert len(loop._drives) == 2
        loop.reload({"max_concurrent": 1})
        assert len(loop._drives) == 2  # in-flight builds keep running…
        assert loop._spawn_ready() is False  # …the loop just stops claiming until under the cap
        assert store.claimed == ["bd-1", "bd-2"]
    finally:
        await finish()


async def test_spawn_ready_is_false_when_nothing_ready(monkeypatch):
    store = _ClaimStore([])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    assert loop._spawn_ready() is False
    assert loop._drives == set()


async def test_spawn_ready_skips_a_file_conflicting_candidate(monkeypatch):
    # bd-1 + bd-2 both touch shared.py; bd-3 touches other.py.
    store = _ClaimStore([_ready("bd-1", ["shared.py"]), _ready("bd-2", ["shared.py"]), _ready("bd-3", ["other.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 3})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        # bd-1 claimed; bd-2 deferred (overlaps bd-1's file); bd-3 claimed (disjoint).
        assert store.claimed == ["bd-1", "bd-3"]
        # #197: guard keys are (project, path); unstamped cards resolve to "default".
        assert loop._inflight_files == {"bd-1": {("default", "shared.py")}, "bd-3": {("default", "other.py")}}
    finally:
        await finish()


async def test_spawn_ready_respects_the_review_wip_limit(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])], in_review=5)  # already at the cap
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2, "max_pending_reviews": 5})
    assert loop._spawn_ready() is False
    assert store.claimed == []  # paused: too many PRs await review


async def test_drive_done_releases_its_files(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1})

    async def _quick(feature):
        return None

    monkeypatch.setattr(loop, "_drive", _quick)
    loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callbacks run
    assert loop._inflight_files == {}  # files released when the drive finished
    assert loop._drives == set()


async def test_spawn_ready_logs_the_claim_decision_with_skip_reason(monkeypatch, caplog):
    """#124: when a lower-priority card claims ahead of a higher one, the single
    per-tick claim_decision line must name the selected fid AND why the higher card
    was passed over — the evidence to tell the hot-file guard from a lost claim race.
    Mirrors the caplog pattern in test_store.test_run_logs_the_retry_count_on_final_success."""
    # bd-hi is FIRST in the ready queue (higher priority) but collides on shared.py with
    # an in-flight build; bd-lo is disjoint and gets claimed ahead of it.
    store = _ClaimStore([_ready("bd-hi", ["shared.py"]), _ready("bd-lo", ["other.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    # #197: guard keys are (project, path); unstamped cards resolve to "default".
    loop._inflight_files = {"bd-live": {("default", "shared.py")}}  # an in-flight build owns shared.py
    finish = await _hold_drives(loop, monkeypatch)
    try:
        with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
            loop._spawn_ready()
    finally:
        await finish()
    assert store.claimed == ["bd-lo"]  # the lower-priority card claimed ahead of bd-hi
    lines = [m for m in caplog.messages if "claim_decision" in m]
    assert len(lines) == 1  # exactly one structured line per tick
    payload = json.loads(lines[0].split("claim_decision", 1)[1])  # parseable without log grepping
    assert payload["selected"] == ["bd-lo"]  # the selected fid is recorded
    skip = {s["fid"]: s for s in payload["skipped"]}
    assert skip["bd-hi"]["reason"] == "hot-file"  # the passed-over card's reason…
    assert skip["bd-hi"]["overlaps"] == ["bd-live"]  # …names the in-flight build it collides with
    assert skip["bd-hi"]["files"] == ["shared.py"]


async def test_spawn_ready_logs_a_claim_race_skip(monkeypatch, caplog):
    """#124: a candidate whose claim() returns None (lost the atomic-claim race) is
    recorded with a distinct, parseable reason — not conflated with the hot-file guard."""

    class _RacingStore(_ClaimStore):
        def claim(self, fid, assignee=""):
            if fid == "bd-hi":
                return None  # someone else won the claim race for the higher card
            return super().claim(fid, assignee=assignee)

    store = _RacingStore([_ready("bd-hi", ["a.py"]), _ready("bd-lo", ["b.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
            loop._spawn_ready()
    finally:
        await finish()
    lines = [m for m in caplog.messages if "claim_decision" in m]
    assert len(lines) == 1
    payload = json.loads(lines[0].split("claim_decision", 1)[1])
    assert payload["selected"] == ["bd-lo"]
    skip = {s["fid"]: s for s in payload["skipped"]}
    assert skip["bd-hi"]["reason"] == "claim-race"


# ── the PR reconcile (terminal-edge fallback) ───────────────────────────────────


class _ReconcileStore:
    def __init__(self, in_review):
        self._in_review = in_review
        self.merged = []
        self.blocked = []

    def list_features(self, state=None):
        return self._in_review if state == "in_review" else []

    def record_merge(self, *, pr_url):
        self.merged.append(pr_url)
        return {"id": "x", "board_state": "done"}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))


async def test_reconcile_drives_merged_to_done_and_closed_to_blocked(monkeypatch):
    store = _ReconcileStore(
        [
            {"id": "bd-merged", "pr_url": "https://example/pr/1"},
            {"id": "bd-closed", "pr_url": "https://example/pr/2"},
            {"id": "bd-open", "pr_url": "https://example/pr/3"},
            {"id": "bd-nopr", "pr_url": ""},  # no PR → skipped entirely
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    states = {
        "https://example/pr/1": "MERGED",
        "https://example/pr/2": "CLOSED",
        "https://example/pr/3": "OPEN",
    }

    async def _pr_state(url, *, cwd="."):
        return states[url]

    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ("passing", "")  # the OPEN PR's CI is green → left in review

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    await BoardLoop({})._reconcile_prs()
    assert store.merged == ["https://example/pr/1"]  # merged → done
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed-unmerged → blocked
    assert set(reaped) == {"bd-merged", "bd-closed"}  # both terminal states reap; open kept


# ── the CI-feedback edge (closed-loop verify) ────────────────────────────────────


class _CiStore:
    def __init__(self, feature, escalate_tiers=None):
        self._feature = feature
        self.requeued = []
        self.blocked = []
        self.escalated = []
        self._escalate_tiers = list(escalate_tiers or [])

    def list_features(self, state=None):
        return [self._feature] if state == "in_review" else []

    def record_merge(self, *, pr_url):
        return None

    def requeue(self, fid):
        self.requeued.append(fid)
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))

    def escalate(self, fid, reason):
        self.escalated.append((fid, reason))
        return self._escalate_tiers.pop(0) if self._escalate_tiers else None


async def _stub_ci_worktree(monkeypatch, *, ci, diff="- a\n+ b"):
    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ci() if callable(ci) else ci

    async def _pr_diff(url, *, cwd=".", max_chars=4000):
        return diff

    async def _reap(repo, root, fid):
        return None

    async def _merge_state(url, *, cwd="."):
        return "CLEAN"  # not BEHIND/DIRTY → auto-rebase no-ops, leaving the CI path under test

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "pr_diff", _pr_diff)
    monkeypatch.setattr(worktree, "pr_merge_state", _merge_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)


async def test_reconcile_ci_bounces_failing_pr_then_blocks(monkeypatch):
    """No coder ladder (single coder) → bounded same-tier retry capped by ci_fix_max."""
    store = _CiStore({"id": "bd-ci", "pr_url": "https://example/pr/9"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(
        monkeypatch, ci=("failing", "Failing checks:\n- Web E2E: FAILURE\n\nFailing log:\nelement not found")
    )

    loop = BoardLoop({"ci_fix_max": 2})  # no `coders` → escalation_on is False
    assert not loop.escalation_on
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert store.blocked == [] and store.escalated == []
    assert "element not found" in loop._ci_feedback["bd-ci"]
    assert loop._ci_fix_attempts["bd-ci"] == 2
    # cap=2 exhausted → blocked, no further requeue.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert [b[0] for b in store.blocked] == ["bd-ci"]


async def test_reconcile_ci_escalates_through_tiers_then_blocks(monkeypatch):
    """With a coder ladder AND no same-tier budget (ci_fix_max=0), each CI failure
    climbs a tier (stronger model) carrying the prior diff; the top tier failing →
    Blocked (the ladder is the bound)."""
    store = _CiStore({"id": "bd-esc", "pr_url": "https://example/pr/7"}, escalate_tiers=["smart", "reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Tests: FAILURE"), diff="- old\n+ new")

    loop = BoardLoop({"coders": {"fast": "a", "smart": "b", "reasoning": "c"}, "ci_fix_max": 0})
    assert loop.escalation_on
    # CI failures climb tiers (escalate), requeue, NOT blocked, carrying the prior diff.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [e[0] for e in store.escalated] == ["bd-esc", "bd-esc"]
    assert store.blocked == []
    assert "- old" in loop._ci_prior_diff["bd-esc"]
    # top tier exhausted (escalate → None) → blocked.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [b[0] for b in store.blocked] == ["bd-esc"]


async def test_reconcile_ci_spends_same_tier_budget_before_escalating(monkeypatch):
    """With a ladder AND ci_fix_max>0, a CI failure first spends same-tier fix
    attempts (cheap nits — lint, a golden-map update) before climbing a model tier,
    and the per-tier budget RESETS at the new rung. Without this, a one-line F841
    burned reasoning→opus and then blocked."""
    store = _CiStore({"id": "bd-b", "pr_url": "https://example/pr/5"}, escalate_tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Lint: F841 unused variable"))

    loop = BoardLoop({"coders": {"smart": "a", "reasoning": "b"}, "ci_fix_max": 2})
    assert loop.escalation_on

    # First two failures: same-tier CI-fix (requeue), no escalation.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b"]
    assert store.escalated == []
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted → escalate ONE tier and reset the per-tier budget.
    await loop._reconcile_prs()
    assert [e[0] for e in store.escalated] == ["bd-b"]
    assert store.requeued == ["bd-b", "bd-b", "bd-b"]
    assert loop._ci_fix_attempts.get("bd-b", 0) == 0  # fresh budget at the new rung

    # The new rung gets its own same-tier attempts before the ladder is exhausted.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b", "bd-b", "bd-b", "bd-b"]
    assert [e[0] for e in store.escalated] == ["bd-b"]  # still just the one climb
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted again → escalate returns None (ladder top) → blocked.
    await loop._reconcile_prs()
    assert [b[0] for b in store.blocked] == ["bd-b"]


async def test_reconcile_ci_leaves_passing_and_pending_in_review(monkeypatch):
    store = _CiStore({"id": "bd-ok", "pr_url": "https://example/pr/8"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    statuses = iter([("pending", ""), ("passing", "")])
    await _stub_ci_worktree(monkeypatch, ci=lambda: next(statuses))

    await BoardLoop({})._reconcile_prs()  # pending → leave
    await BoardLoop({})._reconcile_prs()  # passing → leave
    assert store.requeued == [] and store.blocked == []


# ── the merged/closed guard: never bounce a PR that already left review (bd-1zp) ──


async def test_reconcile_ci_skips_pr_that_left_review_since_the_poll(monkeypatch):
    """Lagged-poll guard (Test C): a PR that merged/closed between the top-level state
    read and the CI reconcile is bailed on BEFORE the CI rollup is even read — a CI fix
    must never dispatch against a PR that is no longer OPEN."""
    store = _CiStore({"id": "bd-gone", "pr_url": "https://example/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    ci_reads = []

    async def _pr_state(url, *, cwd="."):
        return "MERGED"  # merged out from under the poll

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        ci_reads.append(url)
        return ("failing", "Failing checks:\n- Tests: FAILURE")

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)

    loop = BoardLoop({"ci_fix_max": 2})
    await loop._reconcile_ci(store, "bd-gone", "https://example/pr/1", ".")
    assert ci_reads == []  # bailed on state before ever reading CI
    assert store.requeued == [] and store.blocked == [] and store.escalated == []


async def test_reconcile_prs_closed_pr_blocks_and_never_runs_ci(monkeypatch):
    """Test D: a CLOSED PR is driven to blocked by the merge poll, and the CI reconcile
    is never invoked against it — the closed edge, not a CI-fix re-dispatch, handles it."""
    store = _ReconcileStore([{"id": "bd-closed", "pr_url": "https://example/pr/1"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_state(url, *, cwd="."):
        return "CLOSED"

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({})
    ci_called = []

    async def _ci_spy(store, fid, pr_url, repo):
        ci_called.append(fid)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)

    await loop._reconcile_prs()
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed → blocked for triage
    assert ci_called == []  # the CI reconcile never ran against the closed PR


def test_build_prompt_injects_ci_feedback_and_prior_diff():
    loop = BoardLoop({})
    feature = {"id": "bd-ci", "title": "T", "spec": "do it", "acceptance_criteria": "AC", "files_to_modify": ["a.py"]}
    assert "previous attempt was REJECTED" not in loop._build_prompt(feature)  # none stored → no block
    loop._ci_feedback["bd-ci"] = "Failing checks:\n- Web E2E: FAILURE\nelement not found"
    loop._ci_prior_diff["bd-ci"] = "--- a/x.tsx\n+++ b/x.tsx\n+ bad code"
    prompt = loop._build_prompt(feature)
    assert "previous attempt was REJECTED" in prompt
    assert "element not found" in prompt
    assert "bad code" in prompt  # the prior diff is carried forward


# ── CI failure reason (sharpen the retro signal) ─────────────────────────────────


def test_ci_failure_reason_keeps_the_classifiable_error_not_the_header():
    assert _ci_failure_reason("") == "checks red"
    # checks-only (no log excerpt) → the failing check names, not "Failing checks:"
    r = _ci_failure_reason("Failing checks:\n- Python tests: FAILURE\n- Lint: FAILURE")
    assert "Python tests" in r and "Lint" in r and "Failing checks:" not in r
    # with a log excerpt → the SPECIFIC error survives so the retro can bucket it
    summary = (
        "Failing checks:\n- Python tests: FAILURE\n\n"
        "Failing log (truncated):\n"
        "    def test_golden(): ...\n"
        "E   AssertionError: golden field map is out of sync\n"
        "FAILED tests/test_config_roundtrip.py::test_golden - golden field map is out of sync\n"
    )
    r = _ci_failure_reason(summary)
    assert "golden field map" in r  # the classifiable signal is preserved
    assert "Failing checks:" not in r and len(r) <= 500


# ── PR body: the coder's summary, never the raw stream (#56) ─────────────────────


def test_pr_body_keeps_from_the_last_summary_heading():
    """Narration before the summary is dropped, and an EARLY `## Summary` line in the
    narration doesn't truncate the real section — the LAST heading wins."""
    raw = (
        "I first looked at loop.py.\n"
        "## Summary\n(placeholder — I'll finish this at the end)\n"
        "Let me edit the tests.\n"
        "## Summary\n\n- Added the helper\n- Wired the call site\n"
    )
    body = _pr_body(raw, FEATURE)
    assert body.startswith("## Summary")
    assert "Added the helper" in body and "Wired the call site" in body
    assert "I first looked" not in body and "Let me edit" not in body and "placeholder" not in body


def test_pr_body_without_a_summary_uses_the_template_not_the_raw_stream():
    raw = "step one: read the file\nstep two: edit it\nall done"
    body = _pr_body(raw, FEATURE)
    assert FEATURE["title"] in body and FEATURE["id"] in body
    assert "See the diff for details." in body
    assert "step one" not in body and "all done" not in body
    assert FEATURE["title"] in _pr_body("", FEATURE)  # empty reply → same template


def test_pr_body_drops_control_marker_lines():
    raw = "## Summary\n\nDid the thing.\n  NO_TEST_NEEDED: covered by the existing golden\nMore detail.\n"
    body = _pr_body(raw, FEATURE)
    assert "NO_TEST_NEEDED" not in body
    assert "Did the thing." in body and "More detail." in body


def test_pr_body_caps_at_4000_chars():
    raw = "narration first\n## Summary\n\n" + "x" * 9000
    body = _pr_body(raw, FEATURE)
    assert len(body) <= 4000 and body.startswith("## Summary")


# ── KG lessons injected into the coder prompt (flywheel read half) ───────────────


def test_kg_lessons_config_defaults():
    loop = BoardLoop({})
    assert loop.kg_lessons is True and loop.kg_lessons_k == 3 and loop.kg_lessons_domain == "loop-lessons"
    assert BoardLoop({"kg_lessons": False}).kg_lessons is False


async def test_fetch_kg_lessons_disabled_never_queries(monkeypatch):
    called = []

    async def _search(*a, **k):
        called.append(1)
        return [{"preview": "x"}]

    monkeypatch.setattr("graph.sdk.knowledge_search", _search)
    assert await BoardLoop({"kg_lessons": False})._fetch_kg_lessons(FEATURE) == ""
    assert not called


async def test_fetch_kg_lessons_formats_hits_and_scopes_query(monkeypatch):
    captured = {}

    async def _search(query, *, k=5, domain=None):
        captured.update(query=query, k=k, domain=domain)
        return [{"preview": "golden-map: also update settings_schema.FIELDS"}, {"content": "F841: no unused vars"}]

    monkeypatch.setattr("graph.sdk.knowledge_search", _search)
    out = await BoardLoop({"kg_lessons_k": 2})._fetch_kg_lessons(FEATURE)
    assert "- golden-map: also update settings_schema.FIELDS" in out
    assert "- F841: no unused vars" in out
    assert captured["domain"] == "loop-lessons" and captured["k"] == 2
    assert "Add a thing" in captured["query"] and "a.py" in captured["query"]  # title + files


async def test_fetch_kg_lessons_empty_or_error_returns_empty(monkeypatch):
    async def _empty(*a, **k):
        return []

    monkeypatch.setattr("graph.sdk.knowledge_search", _empty)
    assert await BoardLoop({})._fetch_kg_lessons(FEATURE) == ""

    async def _boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr("graph.sdk.knowledge_search", _boom)
    assert await BoardLoop({})._fetch_kg_lessons(FEATURE) == ""  # error → best-effort ""


def test_build_prompt_injects_lessons_block_only_when_present():
    loop = BoardLoop({})
    assert "Known gotchas for this area" not in loop._build_prompt(FEATURE)
    p = loop._build_prompt(FEATURE, lessons="- always update the golden map")
    assert "Known gotchas for this area" in p and "always update the golden map" in p


# ── auto-rebase on conflict (bd-2gu) ─────────────────────────────────────────────


def _aret(val):
    async def _f(*a, **k):
        return val

    return _f


def test_auto_rebase_config_defaults():
    assert BoardLoop({}).auto_rebase is True  # defaults to merge_poll (True)
    assert BoardLoop({"merge_poll": False}).auto_rebase is False
    assert BoardLoop({"auto_rebase": False}).auto_rebase is False
    assert BoardLoop({}).rebase_fix_max == 1


async def test_maybe_rebase_skips_when_not_behind_or_dirty(monkeypatch):
    """CLEAN / BLOCKED(checks) / UNKNOWN → not the rebase's job; never touches git."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("CLEAN"))
    rebased = []
    monkeypatch.setattr(worktree, "rebase_onto_base", lambda *a, **k: rebased.append(1))
    store = _CiStore({"id": "bd-1"})
    assert await BoardLoop({"coder": "proto"})._maybe_rebase(store, FEATURE, "pr", "/repo") is False
    assert not rebased


async def test_maybe_rebase_behind_does_clean_rebase_no_coder(monkeypatch):
    """BEHIND → a clean rebase + force-push; no requeue, no block, no coder."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("BEHIND"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("clean", "")))
    store = _CiStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto"})
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == [] and store.blocked == []
    assert loop._rebase_attempts.get("bd-1", 0) == 0


async def test_maybe_rebase_conflict_redispatches_then_blocks(monkeypatch):
    """DIRTY + a real conflict → re-dispatch the coder (requeue, conflicting file in
    the feedback) up to rebase_fix_max, then Block for a manual rebase."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("DIRTY"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("conflict", "graph/x.py")))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    store = _CiStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto", "rebase_fix_max": 1})
    # 1st conflict → re-dispatch, carrying the conflicting file into the feedback.
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == ["bd-1"]
    assert "graph/x.py" in loop._ci_feedback["bd-1"]
    assert loop._rebase_attempts["bd-1"] == 1
    # budget (1) exhausted → block, no second requeue.
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == ["bd-1"]
    assert [b[0] for b in store.blocked] == ["bd-1"]


async def test_maybe_rebase_infra_error_is_noop(monkeypatch):
    """A fetch/push/worktree error degrades to no-op (next poll retries) — no block."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("BEHIND"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("error", "fetch failed")))
    store = _CiStore({"id": "bd-1"})
    assert await BoardLoop({"coder": "proto"})._maybe_rebase(store, FEATURE, "pr", "/repo") is False
    assert store.requeued == [] and store.blocked == []


async def test_reconcile_prs_rebase_acts_skips_ci(monkeypatch):
    """An OPEN PR the rebase handled skips the CI reconcile this pass (a rebase
    force-pushes + re-runs CI, so the stale head's CI would be thrown away)."""
    store = _CiStore({"id": "bd-1", "pr_url": "https://e/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = BoardLoop({"merge_poll": True})
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(True))
    await loop._reconcile_prs()
    assert ci == []  # rebase acted → CI reconcile skipped


async def test_reconcile_prs_no_rebase_runs_ci(monkeypatch):
    """An OPEN PR the rebase left alone still gets the CI reconcile."""
    store = _CiStore({"id": "bd-1", "pr_url": "https://e/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = BoardLoop({"merge_poll": True})
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    await loop._reconcile_prs()
    assert ci == [1]  # nothing to rebase → CI reconcile runs


# ── merged-state verify: re-verify the verdict when base moves (#131) ────────────


class _VerifyStore(_CiStore):
    def __init__(self, feature):
        super().__init__(feature)
        self.verified = []

    def record_merged_verified(self, fid, sha):
        self.verified.append((fid, sha))
        return {"id": fid}


def _vloop(**cfg):
    base = {"coder": "proto", "local_gate_cmd": "pytest -q"}
    base.update(cfg)
    return BoardLoop(base)


async def test_verify_merged_state_noop_without_a_gate(monkeypatch):
    """No local_gate_cmd → nothing to verify the merged state WITH; never touches git."""
    calls = []
    monkeypatch.setattr(worktree, "origin_head_sha", lambda *a, **k: calls.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto"})  # no gate configured
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert not calls and store.verified == [] and store.blocked == []


async def test_verify_merged_state_current_stamp_is_noop(monkeypatch):
    """Stamp == current origin/<base> → the verdict is current; no worktree, no gate run."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc123"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    feature = {"id": "bd-1", "labels": ["merged-verified:abc123"]}
    assert await _vloop()._verify_merged_state(store, feature, "pr", "/repo") is False
    assert not built and store.verified == [] and store.blocked == []


async def test_verify_merged_state_sha_read_failure_is_noop(monkeypatch):
    """A git hiccup reading origin/<base> degrades to no-op — next poll retries."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(""))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    assert await _vloop()._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert not built and store.blocked == []


async def test_verify_merged_state_base_moved_green_gate_stamps_and_stays(monkeypatch):
    """Base moved + green gate on the merged state → stamp the sha, card STAYS in
    review (non-blocking default), throwaway worktree removed."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/repo/.worktrees/.verify-feat-bd-1")))
    removed = []

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    gates = []

    async def _gate(wt, feature=None):
        gates.append(wt)
        return None  # green

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    feature = {"id": "bd-1", "labels": ["merged-verified:oldsha"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert gates == ["/repo/.worktrees/.verify-feat-bd-1"]
    assert removed == gates  # throwaway removed even on success
    assert store.verified == [("bd-1", "def456")]
    assert store.requeued == [] and store.blocked == []


async def test_verify_merged_state_red_gate_blocks(monkeypatch):
    """A CLEAN gate FAILURE on the merged state is the ONLY blocking outcome — the
    PR merges clean but the result is broken; the reason carries the gate output."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret("1 failed: test_x"))
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is True
    assert [b[0] for b in store.blocked] == ["bd-1"]
    assert "merged state" in store.blocked[0][1] and "1 failed: test_x" in store.blocked[0][1]
    assert store.verified == []  # a red verdict is never stamped current
    assert reaped == ["bd-1"]


async def test_verify_merged_state_budget_bounds_reverification(monkeypatch):
    """merged_verify_max bounds re-verification: a base that moves repeatedly stops
    burning gate runs — the stale stamp stays visible, the card stays in review."""
    shas = iter(["aaa", "bbb", "ccc"])

    async def _sha(repo, ref):
        return next(shas)

    monkeypatch.setattr(worktree, "origin_head_sha", _sha)
    built = []

    async def _build(repo, branch, sha, root=".worktrees"):
        built.append(sha)
        return ("merged", "/wt")

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))
    # 1st move (no stamp yet) → verify + stamp against aaa.
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.verified == [("bd-1", "aaa")]
    # base moves again (bbb, then ccc) but the budget (1) is spent → no more gate
    # runs, no block — the stale stamp is the adjudicator's signal.
    feature = {"id": "bd-1", "labels": ["merged-verified:aaa"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == ["aaa"]
    assert store.verified == [("bd-1", "aaa")] and store.blocked == []


async def test_verify_merged_state_conflict_and_error_are_noops(monkeypatch):
    """An infra error retries next poll; a merge conflict is the DIRTY/rebase edge's
    job — neither blocks, stamps, nor burns the verify budget."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc"))
    outcomes = iter([("error", "fetch failed"), ("conflict", "x.py"), ("merged", "/wt")])

    async def _build(repo, branch, sha, root=".worktrees"):
        return next(outcomes)

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))
    feature = {"id": "bd-1", "labels": []}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # error
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # conflict
    assert store.verified == [] and store.blocked == []
    # budget untouched → the third call actually verifies and stamps.
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert store.verified == [("bd-1", "abc")]


async def test_verify_merged_state_stamps_the_short_sha(monkeypatch):
    """The stamp is the SHORT sha (12 chars): `merged-verified:` + 40 = 56 blew beads'
    50-char label cap, so #132's write always died VALIDATION_FAILED — 12 chars (28
    total) fits (#135)."""
    full_sha = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha
    assert len(full_sha) == 40
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(full_sha))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.verified == [("bd-1", full_sha[:_MERGED_VERIFIED_SHA_LEN])]
    # the stamp read back the next poll matches the same-width truncation → current,
    # so nothing rebuilds (the read-back comparison and the write use the SAME width).
    feature = {"id": "bd-1", "labels": [f"merged-verified:{full_sha[:_MERGED_VERIFIED_SHA_LEN]}"]}
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert not built  # current → no rebuild


async def test_verify_merged_state_stamp_write_failure_never_aborts_or_burns_budget(monkeypatch):
    """A BoardError stamping the verified sha is optional bookkeeping (#135): it must
    NOT propagate — the caller's CI/merge reconciliation still has to run — and must NOT
    spend the re-verify budget on a write that never landed; next poll re-verifies."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456abc789"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))

    class _BoomStore(_VerifyStore):
        def record_merged_verified(self, fid, sha):
            raise BoardError("`br update` failed: VALIDATION_FAILED")

    store = _BoomStore({"id": "bd-1"})
    loop = _vloop(rebase_fix_max=3)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # does not raise, does not block…
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.blocked == []
    # …and the budget is untouched — a write that can't land isn't a re-verify attempt.
    assert loop._merged_verify_attempts.get("bd-1", 0) == 0


async def test_verify_merged_state_budget_zero_is_unlimited(monkeypatch):
    """merged_verify_max=0 ⇒ every base move re-verifies (the auto_merge posture for a
    busy base: a held card must never park on a stale stamp)."""
    loop = _vloop(merged_verify_max=0)
    assert loop.merged_verify_max == 0
    # Exhaustion branch unreachable: attempts far past any budget still verify.
    loop._merged_verify_attempts["bd-1"] = 50
    ran = []

    async def _head(repo, ref):
        return "f" * 40

    async def _mst(repo, branch, base_sha, root="."):
        ran.append(base_sha)
        return ("ok", "/wt")

    async def _gate(wt, feature=None):
        return None

    async def _rm(*a, **k):
        return None

    monkeypatch.setattr(worktree, "origin_head_sha", _head)
    monkeypatch.setattr(worktree, "merged_state_worktree", _mst)
    monkeypatch.setattr(worktree, "remove_worktree", _rm)
    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    store = _VerifyStore({"id": "bd-1"})
    feature = {"id": "bd-1", "labels": ["merged-verified:000000000000"]}
    await loop._verify_merged_state(store, feature, "https://x/pull/1", "/repo")
    assert ran == ["f" * 40]
    assert store.verified == [("bd-1", "f" * 12)]


async def test_verify_merged_state_absent_stamp_is_never_reported_stale(monkeypatch, caplog):
    """With the budget spent and NO stamp ever written (every write failed), the exhaustion
    log must not claim a 'stale stamp' that doesn't exist — that false report is the very
    class of bug #132 was built to prevent (#135)."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc123def456"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    loop._merged_verify_attempts["bd-1"] = 1  # budget already spent, stamp write never landed
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    joined = "\n".join(caplog.messages)
    assert "no merged-verified stamp was ever written" in joined
    assert "leaving the stale merged-verified stamp" not in joined
    assert not built  # budget spent → never even builds the merged worktree
    assert store.verified == [] and store.blocked == []


@pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — integration label round-trip needs it",
)
async def test_verify_merged_state_stamp_lands_through_real_br(monkeypatch, tmp_path):
    """Integration (#135): the merged-verified stamp must LAND through the ACTUAL `br`
    label path with a real 40-char sha. `merged-verified:` + 40 = 56 > beads' 50-char
    cap, so the pre-#135 write died VALIDATION_FAILED and #132's promised stamp never
    existed — a gap the fake-`record_merged_verified` unit tests could never catch. A
    REAL BeadsBoard writes the (short) stamp here; we assert the label is on the bead."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    f = board.create_feature("Verify stamp", spec="s", acceptance_criteria="WHEN x THE SYSTEM SHALL y")
    fid = f["id"]
    full_sha = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha
    assert len(full_sha) == 40
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(full_sha))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", str(tmp_path / "wt"))))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # base moved (no stamp yet) + green gate → stamp the short sha through real `br`.
    assert await loop._verify_merged_state(board, board.get_feature(fid), "pr", str(tmp_path)) is False
    labels = board.get_feature(fid).get("labels") or []
    stamps = [l for l in labels if l.startswith("merged-verified:")]
    assert stamps == [f"merged-verified:{full_sha[:_MERGED_VERIFIED_SHA_LEN]}"]  # it LANDED
    assert all(len(l) <= 50 for l in labels)  # and every label fits beads' cap


async def test_reconcile_prs_verify_block_skips_ci(monkeypatch):
    """The merged-state verify rides the OPEN branch after the rebase; when it
    blocks (red gate on the merged state) the CI reconcile is skipped."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop()
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    monkeypatch.setattr(loop, "_verify_merged_state", _aret(True))
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    await loop._reconcile_prs()
    assert ci == []  # blocked on the merged state → CI reconcile skipped


async def test_reconcile_prs_verify_green_still_runs_ci(monkeypatch):
    """A verify that did NOT block (green gate, or nothing to do) leaves the rest
    of the pass — the CI reconcile — untouched."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop()
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    monkeypatch.setattr(loop, "_verify_merged_state", _aret(False))
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    await loop._reconcile_prs()
    assert ci == [1]


async def test_reconcile_prs_auto_rebase_off_skips_verify(monkeypatch):
    """The verify extends the auto_rebase mechanism — the same flag gates it."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop(auto_rebase=False, ci_poll=False)
    called = []
    monkeypatch.setattr(loop, "_verify_merged_state", lambda *a, **k: called.append(1))
    await loop._reconcile_prs()
    assert called == []


async def test_reconcile_merged_clears_merged_verify_attempts(monkeypatch):
    """The merged terminal edge drops the per-run verify counter with the rest."""
    store = _ReconcileStore([{"id": "bd-m", "pr_url": "https://e/pr/1"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("MERGED"))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    loop = BoardLoop({})
    loop._merged_verify_attempts["bd-m"] = 2
    await loop._reconcile_prs()
    assert "bd-m" not in loop._merged_verify_attempts


async def test_maybe_reconcile_is_rate_limited(monkeypatch):
    loop = BoardLoop({"merge_poll": True, "merge_poll_interval_s": 60})
    calls = []

    async def _reconcile():
        calls.append(1)

    monkeypatch.setattr(loop, "_reconcile_prs", _reconcile)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])

    await loop._maybe_reconcile()  # first → reconciles
    await loop._maybe_reconcile()  # immediately → rate-limited
    clock["t"] += 61
    await loop._maybe_reconcile()  # interval elapsed → reconciles again
    assert len(calls) == 2


async def test_merge_poll_off_never_reconciles(monkeypatch):
    loop = BoardLoop({"merge_poll": False})
    called = []
    monkeypatch.setattr(loop, "_reconcile_prs", lambda: called.append(1))
    await loop._maybe_reconcile()
    assert called == []  # disabled → never reconciles


# ── crash recovery on boot ──────────────────────────────────────────────────────


class _RecoverStore:
    def __init__(self, in_progress):
        self._in_progress = in_progress
        self.calls = []

    def list_features(self, state=None):
        return self._in_progress if state == "in_progress" else []

    def raw_features_with_comments(self, states=("done", "blocked")):
        return []  # no blocked cards → the boot preflight release is a no-op

    def get_feature(self, fid):
        return {"id": fid}  # no verified_sha → the salvage check declines cleanly

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def requeue(self, fid):
        self.calls.append(("requeue", fid))


async def test_recover_adopts_an_open_pr_else_resets_to_ready(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        return "https://example/pr/1" if branch == "feat/bd-1" else ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()
    # bd-1 already had a PR (crash between open_pr and open_review) → adopt → in_review.
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    # bd-2 has no PR → reset to ready for a clean rebuild.
    assert ("requeue", "bd-2") in store.calls


async def test_recover_is_resilient_to_a_per_feature_error(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        if branch == "feat/bd-1":
            raise RuntimeError("gh exploded")
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()  # must not raise
    # bd-1 errored and was skipped; bd-2 still recovered.
    assert ("requeue", "bd-2") in store.calls
    assert all(c[1] != "bd-1" for c in store.calls)


# ── crash salvage: resume a verified candidate instead of rebuilding (#91) ──────


class _SalvageStore:
    """A recovery store whose one in_progress feature carries a verified-candidate
    record (the `verified:<sha>` label projected as ``verified_sha``)."""

    def __init__(self, feature):
        self.feature = feature
        self.calls = []

    def list_features(self, state=None):
        return [{"id": self.feature["id"]}] if state == "in_progress" else []

    def raw_features_with_comments(self, states=("done", "blocked")):
        return []  # no blocked cards → the boot preflight release is a no-op

    def get_feature(self, fid):
        return self.feature

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def requeue(self, fid):
        self.calls.append(("requeue", fid))

    def clear_verified_candidate(self, fid):
        self.calls.append(("clear_verified", fid))


def _salvage_git(*, head="abc123", branch="feat/bd-1"):
    """A ``worktree._git`` fake answering the salvage probes (HEAD sha + branch)."""

    async def _git(wt, *args, timeout=60):
        if args == ("rev-parse", "HEAD"):
            return (0, head + "\n", "")
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return (0, branch + "\n", "")
        return (0, "", "")

    return _git


async def _recover_salvage(monkeypatch, tmp_path, *, make_wt=True, head="abc123", gate_out=None):
    """Run boot recovery over ONE in_progress feature with a recorded verified sha
    of ``abc123``. Returns (store, promoted, opened, gates) for the assertions."""
    feature = {
        "id": "bd-1",
        "title": "Add a thing",
        "repo": str(tmp_path),
        "base_branch": "main",
        "verified_sha": "abc123",
    }
    store = _SalvageStore(feature)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "_git", _salvage_git(head=head))
    if make_wt:
        (tmp_path / ".worktrees" / "feat-bd-1").mkdir(parents=True)

    promoted = []

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees"):
        promoted.append((src_wt, src_branch, fid))
        return (src_wt, src_branch)  # already canonical → the real one no-ops too

    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append((wt, branch, base, title))
        return "https://example/pr/91"

    monkeypatch.setattr(worktree, "open_pr", _open_pr)

    loop = BoardLoop({"repo": str(tmp_path)})
    gates = []

    async def _gate(wt, feature=None):
        gates.append(wt)
        return gate_out

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    await loop._recover()
    return store, promoted, opened, gates


async def test_recover_salvages_a_verified_candidate(monkeypatch, tmp_path):
    """A crash between verify and open_pr: the recorded candidate's worktree exists,
    its branch+sha match, and the gate passes NOW → resume at promote → fixups →
    gate → open_pr → in_review. No re-solve, no rebuild-fresh requeue."""
    store, promoted, opened, gates = await _recover_salvage(monkeypatch, tmp_path)
    assert promoted and promoted[0][1:] == ("feat/bd-1", "bd-1")  # resumed at promote
    assert gates  # the gate re-ran on the candidate now
    assert opened and opened[0][1] == "feat/bd-1" and opened[0][3] == "feat: Add a thing"
    assert ("open_review", "bd-1", "https://example/pr/91") in store.calls
    assert ("clear_verified", "bd-1") in store.calls  # the record's window closed
    assert ("requeue", "bd-1") not in store.calls  # never fell through to rebuild


async def test_recover_salvage_worktree_gone_rebuilds_fresh(monkeypatch, tmp_path):
    """The record exists but the worktree dir is gone → doubt → today's rebuild-fresh
    path (requeue), with no PR opened off a missing tree."""
    store, promoted, opened, _gates = await _recover_salvage(monkeypatch, tmp_path, make_wt=False)
    assert ("requeue", "bd-1") in store.calls
    assert not promoted and not opened
    assert ("clear_verified", "bd-1") in store.calls  # stale record dropped


async def test_recover_salvage_sha_mismatch_rebuilds_fresh(monkeypatch, tmp_path):
    """The worktree exists but its HEAD is not the recorded sha (someone/something
    moved it since verify) → doubt → rebuild fresh, never ship the drifted tree."""
    store, promoted, opened, _gates = await _recover_salvage(monkeypatch, tmp_path, head="0ther5ha")
    assert ("requeue", "bd-1") in store.calls
    assert not promoted and not opened


async def test_recover_salvage_gate_failing_now_rebuilds_fresh(monkeypatch, tmp_path):
    """Worktree+branch+sha all check out, but the pre-PR gate FAILS on the candidate
    now (base moved, env changed) → doubt → rebuild fresh, no PR."""
    store, _promoted, opened, gates = await _recover_salvage(monkeypatch, tmp_path, gate_out="FAILED tests: boom")
    assert gates  # the gate did run against the candidate
    assert not opened  # ...and its failure stopped the salvage before open_pr
    assert ("requeue", "bd-1") in store.calls
    assert ("clear_verified", "bd-1") in store.calls


async def test_recover_salvage_open_pr_error_falls_back_to_rebuild(monkeypatch, tmp_path):
    """ANY error inside the salvage (here: open_pr blowing up) must degrade to the
    rebuild-fresh path, never crash recovery or strand the feature in_progress."""
    feature = {"id": "bd-1", "title": "T", "repo": str(tmp_path), "base_branch": "main", "verified_sha": "abc123"}
    store = _SalvageStore(feature)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "_git", _salvage_git())
    (tmp_path / ".worktrees" / "feat-bd-1").mkdir(parents=True)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees"):
        return (src_wt, src_branch)

    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    async def _boom_pr(wt, branch, *, base, title, body):
        raise worktree.WorktreeError("gh exploded")

    monkeypatch.setattr(worktree, "open_pr", _boom_pr)
    loop = BoardLoop({"repo": str(tmp_path)})

    async def _gate(wt, feature=None):
        return None

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    await loop._recover()  # must not raise
    assert ("requeue", "bd-1") in store.calls
    assert all(c[0] != "open_review" for c in store.calls)  # nothing pretended a PR opened


# ── periodic health sweep ───────────────────────────────────────────────────────


class _SweepStore:
    def __init__(self, in_progress=(), features=None):
        self._in_progress = list(in_progress)
        self._features = features or {}  # fid -> board_state
        self.requeued = []
        self.archive_windows = []  # archive_after_days of each archive pass (#115)

    def list_features(self, state=None):
        return [{"id": f} for f in self._in_progress] if state == "in_progress" else []

    def requeue(self, fid):
        self.requeued.append(fid)

    def open_review(self, fid, *, pr_url):
        pass

    def get_feature(self, fid):
        st = self._features.get(fid)
        return {"id": fid, "board_state": st} if st else None

    def archive_stale(self, archive_after_days=7):
        self.archive_windows.append(archive_after_days)
        return []


async def test_sweep_reconciles_in_progress_with_no_live_drive(monkeypatch):
    store = _SweepStore(in_progress=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    loop = BoardLoop({})
    loop._inflight_files = {"bd-2": {"a.py"}}  # bd-2 has a live drive → skip
    await loop._sweep()
    assert store.requeued == ["bd-1"]  # bd-1 (no PR, no drive) reset; bd-2 left alone


async def test_sweep_reaps_orphaned_worktrees(monkeypatch):
    store = _SweepStore(features={"bd-done": "done", "bd-cancelled": "cancelled", "bd-rev": "in_review"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(
        worktree, "list_feature_worktrees", lambda repo, root: ["bd-done", "bd-cancelled", "bd-rev", "bd-gone"]
    )
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    await BoardLoop({})._sweep()
    # Terminal (done/cancelled) + missing feature → reaped; in_review keeps its worktree
    # (a CI-fail re-dispatch still pushes to the same PR). Cancelled is the crash backstop
    # for the terminal-edge reap in api._cancel (#109).
    assert set(reaped) == {"bd-done", "bd-cancelled", "bd-gone"}


async def test_sweep_treats_candidate_worktrees_by_parent_feature(monkeypatch):
    """A leftover `.gN`/`.cN` candidate worktree is NOT a feature id (bd-1cp.g1) — the
    sweep must resolve its PARENT feature's state (#91): parent done/gone → the
    candidate is reaped (by its FULL worktree id, so the right dir+branch go); parent
    with a live drive → left alone. And the store is never asked for the raw candidate
    id — that lookup was the old warning-spam-every-sweep path."""
    store = _SweepStore(features={"bd-done": "done", "bd-live": "in_progress"})
    looked_up = []
    orig_get = store.get_feature

    def _spy(fid):
        looked_up.append(fid)
        return orig_get(fid)

    store.get_feature = _spy
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(
        worktree, "list_feature_worktrees", lambda repo, root: ["bd-done.g1", "bd-done.c2", "bd-gone.g3", "bd-live.g1"]
    )
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    loop = BoardLoop({})
    loop._inflight_files = {"bd-live": {"a.py"}}  # bd-live's drive is live → its candidates stay
    await loop._sweep()
    assert set(reaped) == {"bd-done.g1", "bd-done.c2", "bd-gone.g3"}  # full worktree ids
    assert looked_up and all("." not in fid for fid in looked_up)  # never the raw candidate id


def test_archive_window_config_default_and_override():
    assert BoardLoop({}).archive_after_days == 7  # the #115 default window
    assert BoardLoop({"archive_after_days": 30}).archive_after_days == 30


async def test_sweep_runs_the_archive_pass_with_the_configured_window(monkeypatch):
    """The archive pass (#115) rides the existing health sweep — no scheduler of its
    own — and hands the store the configured window."""
    store = _SweepStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    await BoardLoop({"archive_after_days": 3})._sweep()
    assert store.archive_windows == [3.0]


async def test_sweep_survives_an_archive_pass_failure(monkeypatch):
    """The archive pass is best-effort: a store error there must not break the sweep
    (the self-heal halves already ran) or escape into the loop."""
    store = _SweepStore(in_progress=["bd-1"])

    def _boom(archive_after_days=7):
        raise RuntimeError("br unavailable")

    store.archive_stale = _boom
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    await BoardLoop({})._sweep()  # must not raise
    assert store.requeued == ["bd-1"]  # the reconcile half still did its job


async def test_maybe_sweep_is_rate_limited(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 300})
    calls = []

    async def _sweep():
        calls.append(1)

    monkeypatch.setattr(loop, "_sweep", _sweep)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])
    await loop._maybe_sweep()  # first → sweeps
    await loop._maybe_sweep()  # immediately → rate-limited
    clock["t"] += 301
    await loop._maybe_sweep()  # interval elapsed → sweeps again
    assert len(calls) == 2


async def test_sweep_off_when_interval_zero(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 0})
    called = []
    monkeypatch.setattr(loop, "_sweep", lambda: called.append(1))
    await loop._maybe_sweep()
    assert called == []  # disabled → never sweeps


# ── dependency gate (merge vs review) ───────────────────────────────────────────


def test_dep_gate_config_defaults_to_merge():
    assert BoardLoop({}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "merge"}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "review"}).relaxed_gate is True


async def test_spawn_ready_passes_the_dep_gate_to_ready_queue(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"dep_gate": "review", "max_concurrent": 1})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        assert store.last_relaxed is True  # the relaxed gate reaches ready_queue
    finally:
        await finish()


# ── max-mode best-of-N judge (#21) ───────────────────────────────────────────────


async def test_judge_candidates_returns_the_model_pick(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        # distinct non-empty diff per worktree so every candidate competes
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _judge(prompt, *, system=None, model_name=None):
        assert "WHEN x THE SYSTEM SHALL y" in prompt  # acceptance criteria reach the judge
        return "Candidate 1 is the most complete."

    monkeypatch.setattr("graph.sdk.complete", _judge)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_none_when_all_empty(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run when there is nothing to judge")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) is None


async def test_judge_candidates_single_nonempty_skips_the_model(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        if args[0] == "diff" and wt == "/wt/b":
            return (0, "real diff", "")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run for a single candidate")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_fails_open_to_first_when_judge_errors(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _err(prompt, *, system=None, model_name=None):
        raise RuntimeError("model offline")

    monkeypatch.setattr("graph.sdk.complete", _err)
    # both candidates non-empty → first non-empty index wins when the judge dies
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 0


# ── execution-grounded candidate selection (ADR 0064) ────────────────────────────


def _git_nonempty_for(nonempty_wts):
    """A worktree._git stub: name-only diff is non-empty only for the given worktrees."""

    async def _git(wt, *args, timeout=60):
        if args and args[0] == "diff":
            return (0, ("solution.py" if wt in nonempty_wts else ""), "")
        return (0, "", "")

    return _git


async def test_select_candidate_prefers_passing_gate(monkeypatch):
    """With a gate, the candidate whose gate PASSES wins even if the judge would pick another."""
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 3})
    wts = ["/c0", "/c1", "/c2"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))  # all have a diff

    async def gate(wt, feature=None):
        return None if wt == "/c2" else "boom"  # only c2 passes

    async def judge(*a, **k):
        return 0  # the judge would (wrongly) pick c0 — must be overridden

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 2


async def test_select_candidate_judges_only_among_passing(monkeypatch):
    """Multiple candidates pass → the judge breaks the tie among the PASSING set only."""
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 3})
    wts = ["/c0", "/c1", "/c2"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        return None if wt in ("/c0", "/c2") else "boom"  # c0 + c2 pass, c1 fails

    async def judge(feature, base, sub):
        assert sub == ["/c0", "/c2"]  # judge sees only the passing candidates
        return 1  # picks the 2nd of the sublist → original index 2

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 2


async def test_select_candidate_falls_back_to_judge_when_none_pass(monkeypatch):
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 2})
    wts = ["/c0", "/c1"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        return "boom"  # none pass

    async def judge(feature, base, sub):
        assert sub == wts  # judges over ALL candidates
        return 1

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 1


async def test_select_candidate_no_gate_uses_judge_and_never_runs_gate(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})  # no local_gate_cmd
    wts = ["/c0", "/c1"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        raise AssertionError("the gate must not run when local_gate_cmd is unset")

    async def judge(*a, **k):
        return 0

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 0


async def test_select_candidate_none_when_no_diff(monkeypatch):
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 2})
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set()))  # all empty

    async def gate(wt, feature=None):
        raise AssertionError("no diffs → nothing to gate")

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    assert await loop._select_candidate(FEATURE, "main", ["/c0", "/c1"]) is None


# ── the blocking review gate (plan M5): bounce / budget / exhaustion ─────────────


class _GateStore(FakeLoopStore):
    """FakeLoopStore + the review-gate surface (sub-state labels, requeue, lookup)."""

    def __init__(self):
        super().__init__()
        self.review_states = []  # (label, note) history
        self.state = "in_review"

    def set_review_substate(self, fid, label, note=""):
        self.calls.append(("set_review_substate", fid, label))
        self.review_states.append((label, note))
        return {"id": fid}

    def requeue(self, fid):
        self.calls.append(("requeue", fid))
        self.state = "ready"
        return {"id": fid}

    def get_feature(self, fid):
        return {"id": fid, "board_state": self.state}


def _inject_fake_findings(monkeypatch):
    """Stand in for the HOST's graph.review.findings (absent in this suite) — the
    ADR 0077 contract _review_gate imports lazily. Parses any fenced/bare JSON
    array into finding-shaped objects."""
    import json as _json
    import types as _types
    from dataclasses import dataclass, field

    @dataclass
    class _Finding:
        file: str = ""
        line: int = 0
        severity: str = "minor"
        category: str = ""
        claim: str = ""
        evidence: str = ""
        verdict: str = ""
        note: str = field(default="")

        def to_dict(self):
            from dataclasses import asdict

            return asdict(self)

    def parse_findings(text):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            items = _json.loads(text[start : end + 1])
        except _json.JSONDecodeError:
            return []
        return [
            _Finding(**{k: v for k, v in it.items() if k in _Finding.__dataclass_fields__})
            for it in items
            if isinstance(it, dict) and it.get("claim")
        ]

    def render_findings_markdown(findings, title="Review findings"):
        return f"## {title}\n" + "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.claim}" for f in findings)

    mod = _types.ModuleType("graph.review.findings")
    mod.parse_findings = parse_findings
    mod.render_findings_markdown = render_findings_markdown
    pkg = _types.ModuleType("graph")
    sub = _types.ModuleType("graph.review")
    pkg.review = sub
    sub.findings = mod
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "graph", pkg)
    monkeypatch.setitem(_sys.modules, "graph.review", sub)
    monkeypatch.setitem(_sys.modules, "graph.review.findings", mod)


def _gate_loop(monkeypatch, output, cfg=None):
    """A review_gate loop whose review-workflow run returns ``output`` (None = the
    run could not happen, with the no-runner-no-reviewer reason) and whose PR-diff
    fetch is stubbed."""
    loop = BoardLoop({"review_gate": True, **(cfg or {})})

    async def _run(fid, pr_url):
        if output is None:
            return None, "no workflow runner available and no reviewer configured"
        return output, None

    async def _diff(pr_url, cwd="."):
        return "diff --git a/x b/x"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    return loop


def test_review_gate_config():
    loop = BoardLoop({})
    assert loop.review_gate is False and loop.review_workflow == "code-review" and loop.review_fix_max == 2
    assert BoardLoop({"review_gate": True, "review_fix_max": 0}).review_fix_max == 0
    assert BoardLoop({"review_workflow": " my-review "}).review_workflow == "my-review"


_BLOCKER = '[{"file": "a.py", "line": 3, "severity": "blocker", "claim": "drops data", "evidence": "x", "verdict": "confirmed"}]'
_MINOR = '[{"file": "a.py", "line": 3, "severity": "nit", "claim": "naming", "evidence": "x", "verdict": "confirmed"}]'
_REFUTED = (
    '[{"file": "a.py", "line": 3, "severity": "blocker", "claim": "drops data", "evidence": "x", "verdict": "refuted"}]'
)


async def test_review_gate_bounces_with_findings_in_the_retry_prompt(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, f"brief…\n```json\n{_BLOCKER}\n```")
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert ("requeue", "bd-1") in store.calls
    # sub-state walked pending → changes-requested, findings recorded on the bead
    assert store.review_states[0][0] == "review-pending"
    assert store.review_states[-1][0] == "changes-requested"
    assert "drops data" in store.review_states[-1][1]
    # the retry prompt carries the findings + the reviewed diff (the CI-bounce levers)
    assert "REQUESTED CHANGES" in loop._ci_feedback["bd-1"]
    assert "drops data" in loop._ci_feedback["bd-1"]
    assert loop._ci_prior_diff["bd-1"].startswith("diff --git")
    assert loop._review_fix_attempts["bd-1"] == 1
    # and the injected feedback lands in the next build prompt
    prompt = loop._build_prompt({**FEATURE})
    assert "REQUESTED CHANGES" in prompt and "drops data" in prompt


async def test_review_gate_clean_and_nonblocking_findings_pass(monkeypatch):
    _inject_fake_findings(monkeypatch)
    for output in ("clean.\n```json\n[]\n```", _MINOR, _REFUTED):
        store = _GateStore()
        loop = _gate_loop(monkeypatch, output)
        await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
        assert ("requeue", "bd-1") not in store.calls
        assert not any(c[0] == "flag_blocked" for c in store.calls)
        assert store.review_states[-1][0] == "review-clean"  # the POSITIVE verdict the merge edge keys off
        assert "bd-1" not in loop._ci_feedback


async def test_review_gate_exhausted_budget_blocks_never_merges_silently(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, _BLOCKER, cfg={"review_fix_max": 1})
    loop._review_fix_attempts["bd-1"] = 1  # budget already spent
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "needs human review" in blocked[0][2]
    assert ("requeue", "bd-1") not in store.calls
    assert "bd-1" not in loop._review_fix_attempts  # budget cleared with the block


async def test_review_gate_unrunnable_leaves_pending_for_the_reconcile_retry(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, None)  # no runner + no reviewer
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert store.review_states == [("review-pending", "")]  # left pending — retried next poll
    assert ("requeue", "bd-1") not in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)


def test_parse_pr_url():
    from project_board.loop import _parse_pr_url

    assert _parse_pr_url("https://github.com/protoLabsAI/protoContent/pull/421") == (
        "421",
        "protoLabsAI/protoContent",
    )
    assert _parse_pr_url("https://example.com/not-a-pr") == ("", "")


# ── fail-closed gate + delta re-review carry (ADR 0078 Phase A2) ─────────────────


def test_review_gate_config_run_max():
    assert BoardLoop({}).review_run_max == 3
    assert BoardLoop({"review_run_max": 0}).review_run_max == 1  # floor: at least one try


async def test_review_gate_partial_panel_is_not_a_review(monkeypatch):
    """A workflow result with failed steps must NOT be judged — the gate treats it
    as unreviewed (fail closed): review-pending stays, no requeue, no block."""
    _inject_fake_findings(monkeypatch)
    import sys as _sys
    import types as _types

    calls = []

    async def _runner(name, inputs):
        calls.append(inputs)
        return {"output": "clean.\n```json\n[]\n```", "steps": {}, "failed": ["find_crossfile"]}

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)

    store = _GateStore()
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)  # no reviewer fallback
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert calls, "the runner must have been invoked"
    # Fail closed: the clean-looking partial output was NOT judged.
    assert store.review_states == [("review-pending", "")]
    assert ("requeue", "bd-1") not in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    assert loop._review_run_failures["bd-1"] == 1


async def test_review_gate_unrunnable_escalates_after_run_max(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, None, cfg={"review_run_max": 2})
    loop._review_run_failures["bd-1"] = 1  # one prior unrunnable attempt
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "operator attention" in blocked[0][2]
    # #180: the block reason carries the ACTUAL cause, not the generic three-hypothesis text
    assert "no workflow runner available and no reviewer configured" in blocked[0][2]
    assert "bd-1" not in loop._review_run_failures
    # #181: review-pending is PRESERVED through the block (never cleared) so an
    # operator unblock re-arms the gate on the next reconcile poll — cleared, the
    # feature would sit in_review indistinguishable from a clean review.
    assert store.review_states == [("review-pending", "")]
    assert ("set_review_substate", "bd-1", None) not in store.calls


class _RoundTripStore(_GateStore):
    """_GateStore + the reconcile surface — one feature whose labels/state mirror
    what set_review_substate / flag_blocked / an operator unblock would do."""

    _SUBSTATES = ("review-pending", "changes-requested", "review-clean")

    def __init__(self):
        super().__init__()
        self.labels = []
        self.pr_url = "https://github.com/o/r/pull/9"

    def set_review_substate(self, fid, label, note=""):
        self.labels = [l for l in self.labels if l not in self._SUBSTATES]
        if label:
            self.labels.append(label)
        return super().set_review_substate(fid, label, note)

    def flag_blocked(self, fid, reason):
        self.state = "blocked"
        return super().flag_blocked(fid, reason)

    def unblock(self, fid):
        self.state = "in_review"

    def list_features(self, state=None):
        if state == "in_review" and self.state == "in_review":
            return [{"id": "bd-1", "pr_url": self.pr_url, "labels": list(self.labels), "repo": "/repo"}]
        return []

    def get_feature(self, fid):
        return {"id": fid, "board_state": self.state, "labels": list(self.labels)}


async def test_review_gate_unrunnable_block_then_unblock_rearms_the_gate(monkeypatch):
    """The full #181 round trip: gate exhausts review_run_max → blocked WITH
    review-pending kept → the label is inert while blocked (no reconcile) →
    operator unblocks → the next reconcile poll sees review-pending and re-runs
    the gate."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    # merge_poll off → ci_poll/auto_rebase default off, so the reconcile exercises
    # ONLY the review-gate resume edge.
    loop = BoardLoop({"review_gate": True, "review_run_max": 1, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return None, "stub: unrunnable"  # no runner, dead run, or partial panel

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    # Round 1: the only allowed attempt fails → blocked, review-pending intact.
    await loop._review_gate(store, "bd-1", store.pr_url, "/repo")
    assert store.state == "blocked"
    assert "review-pending" in store.labels

    # Blocked features aren't reconciled — the label is inert, the gate stays quiet.
    await loop._reconcile_prs()
    assert runs == [store.pr_url]

    # Operator fixes the environment and unblocks → back in_review with the label
    # still set — the next poll re-arms the gate.
    store.unblock("bd-1")
    await loop._reconcile_prs()
    assert runs == [store.pr_url, store.pr_url]


async def test_review_gate_in_flight_is_not_rearmed_by_the_reconcile(monkeypatch):
    """#205: the drive's gate is RUNNING (pending label set, panel mid-flight) when
    the reconcile poll comes round and sees review-pending on an in_review card.
    That is not an interrupted gate — the reconcile must NOT start a second panel
    on the same head: exactly one workflow run, one verdict, one bounce increment."""
    import asyncio as _asyncio

    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []
    started, release = _asyncio.Event(), _asyncio.Event()

    async def _run(fid, pr_url):
        runs.append(pr_url)
        started.set()
        await release.wait()  # hold the panel open — minutes in real life
        return f"brief…\n```json\n{_BLOCKER}\n```", None

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _diff(url, *, cwd=".", max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    # The drive's gate starts and parks inside the panel with review-pending set.
    gate = _asyncio.create_task(loop._review_gate(store, "bd-1", store.pr_url, "/repo"))
    await started.wait()
    assert "review-pending" in store.labels and "bd-1" in loop._review_inflight

    # The reconcile poll fires mid-gate: the resume edge sees the pending label …
    await loop._reconcile_prs()
    # … and does nothing — no second panel.
    assert runs == [store.pr_url]

    # The first gate lands its own verdict: ONE bounce, budget 1 (was 2 — 1/2 → 2/2
    # with no fix between — when the duplicate run raced it).
    release.set()
    await gate
    assert runs == [store.pr_url]
    assert loop._review_fix_attempts["bd-1"] == 1
    assert "changes-requested" in store.labels
    assert "bd-1" not in loop._review_inflight  # guard released for the real re-review

    # A gate that actually DIED (restart: empty set, label still pending) is still
    # resumed by the reconcile — the edge keeps its job.
    store.state = "in_review"
    store.labels = ["review-pending"]
    release.set()
    await loop._reconcile_prs()
    assert runs == [store.pr_url, store.pr_url]


async def test_review_gate_passes_prior_findings_on_the_next_run(monkeypatch):
    """Round 1 findings ride into round 2's workflow inputs (delta re-review)."""
    _inject_fake_findings(monkeypatch)
    import sys as _sys
    import types as _types

    seen_inputs = []

    async def _runner(name, inputs):
        seen_inputs.append(dict(inputs))
        return {"output": f"```json\n{_BLOCKER}\n```", "steps": {}, "failed": []}

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)

    async def _diff(pr_url, cwd="."):
        return "diff --git a/x b/x"

    monkeypatch.setattr(worktree, "pr_diff", _diff)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_fix_max": 5})
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "prior_findings" not in seen_inputs[0]  # first pass — nothing to carry
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "prior_findings" in seen_inputs[1]
    assert "drops data" in seen_inputs[1]["prior_findings"]


# ── surfaced unrunnable-gate causes (#180) ───────────────────────────────────────


def _inject_runtime_runner(monkeypatch, runner):
    """Stand in for the HOST's runtime.state.STATE (the workflows plugin's published
    runner) — ``runner=None`` models the plugin being disabled."""
    import sys as _sys
    import types as _types

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)


def _inject_fake_adapters(monkeypatch, dispatch):
    """Stand in for the HOST's plugins.delegates.adapters (absent in this suite) so
    the reviewer-fallback path can be exercised."""
    import sys as _sys
    import types as _types

    adapters = _types.ModuleType("plugins.delegates.adapters")
    adapters.ADAPTERS = {"a2a": _types.SimpleNamespace(dispatch=dispatch)}
    plugins = _types.ModuleType("plugins")
    delegates = _types.ModuleType("plugins.delegates")
    plugins.delegates = delegates
    delegates.adapters = adapters
    monkeypatch.setitem(_sys.modules, "plugins", plugins)
    monkeypatch.setitem(_sys.modules, "plugins.delegates", delegates)
    monkeypatch.setitem(_sys.modules, "plugins.delegates.adapters", adapters)


async def test_run_review_workflow_reason_no_runner_no_reviewer(monkeypatch):
    """#180 (a) — the live incident: workflows plugin disabled (no STATE.workflow_run)
    and no reviewer resolves. The reason names BOTH missing dependencies."""
    _inject_runtime_runner(monkeypatch, None)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert why == "no workflow runner available and no reviewer configured"


async def test_run_review_workflow_reason_names_failed_steps(monkeypatch):
    """#180 (b): a partial panel fails closed with the failed step NAMES in the reason."""

    async def _runner(name, inputs):
        return {"output": "clean.\n```json\n[]\n```", "steps": {}, "failed": ["find_crossfile", "judge"]}

    _inject_runtime_runner(monkeypatch, _runner)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "failed step(s)" in why and "find_crossfile" in why and "judge" in why


async def test_run_review_workflow_reason_includes_workflow_exception(monkeypatch):
    """#180 (c): a dying workflow with no reviewer fallback — the reason carries the
    exception message and says the fallback was missing."""

    async def _runner(name, inputs):
        raise RuntimeError("panel exploded")

    _inject_runtime_runner(monkeypatch, _runner)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "call failed" in why and "panel exploded" in why
    assert "no reviewer fallback configured" in why


async def test_run_review_workflow_reason_includes_reviewer_exception(monkeypatch):
    """#180 (c), reviewer flavor: no runner, the a2a dispatch raises — the reason
    names the reviewer and carries the exception message."""
    _inject_runtime_runner(monkeypatch, None)

    async def _dispatch(reviewer, msg):
        raise RuntimeError("a2a unreachable")

    _inject_fake_adapters(monkeypatch, _dispatch)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: object())
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "reviewer" in why and "call failed" in why and "a2a unreachable" in why


async def test_review_gate_retry_and_block_name_the_actual_cause(monkeypatch, caplog):
    """End-to-end through the REAL _run_review_workflow: the retry warning AND the
    eventual block reason say exactly what is missing — the operator no longer
    correlates plugin toggles with generic gate warnings in the server log (#180)."""
    _inject_fake_findings(monkeypatch)
    _inject_runtime_runner(monkeypatch, None)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_run_max": 2})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "no workflow runner available and no reviewer configured" in caplog.text  # the retry warning
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")  # exhausts run_max
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "no workflow runner available and no reviewer configured" in blocked[0][2]
    assert "runner missing" not in blocked[0][2]  # the generic three-hypothesis text is gone


async def test_review_gate_block_reason_names_failed_steps(monkeypatch):
    """A gate exhausted by partial panels blocks with the failed step named (#180 (b))."""
    _inject_fake_findings(monkeypatch)

    async def _runner(name, inputs):
        return {"output": "clean", "steps": {}, "failed": ["find_crossfile"]}

    _inject_runtime_runner(monkeypatch, _runner)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_run_max": 1})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "find_crossfile" in blocked[0][2]


async def test_boot_warns_when_review_gate_has_no_runner(monkeypatch, caplog):
    """#180: review_gate=True with neither a workflow runner nor a reviewer → ONE
    loud actionable WARNING at loop start naming the missing dependencies."""
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())
    _inject_runtime_runner(monkeypatch, None)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
        loop.start()
    if loop._task:
        loop._task.cancel()
    warn = [r.message for r in caplog.records if "review_gate is on but no review runner available" in r.message]
    assert warn, "expected a boot WARNING when the review gate cannot run"
    assert "workflows plugin disabled" in warn[0] and "fail closed" in warn[0]


async def test_boot_warning_silent_when_a_runner_or_reviewer_resolves(monkeypatch, caplog):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        # a live workflow runner → no warning
        _inject_runtime_runner(monkeypatch, lambda name, inputs: None)
        loop = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
        loop.start()
        if loop._task:
            loop._task.cancel()
        # no runner but a reviewer resolves → no warning either
        _inject_runtime_runner(monkeypatch, None)
        loop2 = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop2, "_resolve_delegate", lambda n, t: object())
        loop2.start()
        if loop2._task:
            loop2._task.cancel()
        # review_gate off → the preflight is skipped entirely
        loop3 = BoardLoop({"loop_enabled": True})
        loop3.start()
        if loop3._task:
            loop3._task.cancel()
    assert "review_gate is on but no review runner available" not in caplog.text


# ── per-feature project resolution (#90 slice 2) ─────────────────────────────────

# A board serving several repos: the flat top-level keys are the INSTANCE defaults;
# the `projects:` map carries each repo's own execution surface. The tests below prove
# a feature resolves repo/gate/coders/solve-knobs from ITS project, not the instance.
_MULTI_CFG = {
    "repo": "/instance/repo",
    "base_branch": "main",
    "local_gate_cmd": "instance-gate",
    "coders": {"fast": "instance-coder"},
    "default_project": "board-plugin",
    "projects": {
        "board-plugin": {
            "repo": "/repos/board-plugin",
            "base_branch": "develop",
            "local_gate_cmd": "ruff check .",
            "coders": {"fast": "bp-fast", "smart": "bp-smart"},
            "gate_files": ["CHANGELOG.md"],
            "repo_conventions": "CI runs ruff",
            "coder_solve_budget": 9,
        },
        "other": {"repo": "/repos/other", "local_gate_cmd": "make test"},
    },
}


def test_feature_resolves_repo_gate_and_coders_from_its_project():
    # r1/r7: a labeled feature resolves its repo, base, gate and coders from the project
    # config — overriding the instance default the store stamped on `feature["repo"]`.
    lp = BoardLoop(_MULTI_CFG)
    feat = {"id": "bd-9", "project": "board-plugin", "repo": "/instance/repo", "base_branch": "main"}
    assert lp._repo_for(feat) == "/repos/board-plugin"  # project repo, NOT the stamped instance one
    assert lp._base_branch_for(feat) == "develop"
    assert lp._local_gate_cmd_for(feat) == "ruff check ."
    assert lp._coders_for(feat) == {"fast": "bp-fast", "smart": "bp-smart"}
    # …and the instance defaults are genuinely DIFFERENT — proving we didn't read them.
    assert lp.local_gate_cmd == "instance-gate"
    assert lp.coders == {"fast": "instance-coder"}
    assert lp._store_kw["repo"] == "/instance/repo"


def test_unlabeled_feature_resolves_to_the_default_project():
    # r8 back-compat: a feature with no project label falls back to the default project.
    lp = BoardLoop(_MULTI_CFG)  # default_project = board-plugin
    feat = {"id": "bd-x"}  # no `project`
    assert lp._project_cfg(feat)["name"] == "board-plugin"
    assert lp._repo_for(feat) == "/repos/board-plugin"
    assert lp._coders_for(feat) == {"fast": "bp-fast", "smart": "bp-smart"}


def test_back_compat_no_projects_map_is_a_single_implicit_project():
    # r8: absent a `projects:` map, one implicit "default" project is synthesized from
    # the flat keys — every feature resolves to the instance repo/gate/coders as before.
    lp = BoardLoop({"repo": "/solo", "local_gate_cmd": "make gate", "coders": {"fast": "solo"}})
    assert lp._default_project == "default"
    feat = {"id": "bd-1"}  # unlabeled
    assert lp._repo_for(feat) == "/solo"
    assert lp._local_gate_cmd_for(feat) == "make gate"
    assert lp._coders_for(feat) == {"fast": "solo"}


def test_build_prompt_uses_the_features_project_gate_files_and_conventions():
    # r4: the dispatch prompt carries the FEATURE's project gate_files + repo_conventions.
    lp = BoardLoop(_MULTI_CFG)
    prompt = lp._build_prompt({**FEATURE, "project": "board-plugin"})
    assert "CHANGELOG.md" in prompt  # board-plugin's gate_files
    assert "CI runs ruff" in prompt  # board-plugin's repo_conventions
    # a project that declares NEITHER (and no instance-level default) gets no blocks.
    p2 = lp._build_prompt({**FEATURE, "project": "other"})
    assert "Repo standing gate files" not in p2
    assert "## Repo conventions" not in p2


def test_coder_solve_settings_resolve_per_project():
    # r5: the solve() search knobs + verifier command resolve from the feature's project.
    lp = BoardLoop(_MULTI_CFG)
    s = lp._coder_solve_settings({"id": "bd-9", "project": "board-plugin"})
    assert s["budget"] == 9  # board-plugin's coder_solve_budget
    assert s["test_cmd"] == "ruff check ."  # its gate doubles as the solve verifier
    # a project with no coder_solve_budget override falls back to the instance default.
    other = lp._coder_solve_settings({"id": "bd-o", "project": "other"})
    assert other["budget"] == lp.coder_solve_budget


async def test_drive_builds_in_the_features_project_repo(monkeypatch):
    # r1 end-to-end: the drive creates the worktree + opens the PR in the project's repo.
    captured = {}
    store = FakeLoopStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        captured["repo"], captured["base"] = repo, base
        return (f"/wt/feat-{fid}", f"feat/{fid}")

    async def _dispatch(*a, **k):
        return "the coder reply"

    async def _open_pr(wt, branch, *, base, title, body):
        captured["pr_base"] = base
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    loop = BoardLoop(
        {
            "coder": "proto",
            "coder_solve": False,  # force the single-shot path (no solve ladder)
            "repo": "/instance/repo",
            "projects": {"board-plugin": {"repo": "/repos/board-plugin", "base_branch": "develop"}},
        }
    )
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive({**FEATURE, "project": "board-plugin"})
    assert captured["repo"] == "/repos/board-plugin"  # NOT /instance/repo
    assert captured["base"] == "develop" and captured["pr_base"] == "develop"
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls


# ── gate preflight (fail-closed: never start work a broken gate can't accept) ─────


class _PreflightStore(FakeLoopStore):
    """FakeLoopStore + the ready list, ready_queue, claim, and clear_blocked the
    per-project preflight hold/release + _spawn_ready use. ``ready`` is a list of fids
    (default project) or ``(fid, project)`` tuples so a test can spread ready work
    across projects (#90)."""

    def __init__(self, ready):
        super().__init__()
        self._ready = []
        for r in ready:
            fid, project = r if isinstance(r, tuple) else (r, "")
            f = {"id": fid, "board_state": "ready", "blocked": False, "files_to_modify": []}
            if project:
                f["project"] = project
            self._ready.append(f)

    def list_features(self, state=None):
        return [dict(f) for f in self._ready] if state == "ready" else []

    def ready_queue(self, relaxed=False):
        return [dict(f) for f in self._ready]

    def claim(self, fid, assignee=""):
        for f in self._ready:
            if f["id"] == fid:
                self.calls.append(("claim", fid))
                return dict(f)
        return None

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))
        return {"id": fid}


class _FakeProc:
    def __init__(self, rc, out=b""):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


def test_preflight_config_defaults():
    assert BoardLoop({}).preflight is True  # on by default
    assert BoardLoop({})._preflight_state == {}  # per-project dict, empty = nothing checked
    assert BoardLoop({"preflight": False}).preflight is False


async def test_preflight_noop_when_no_gate(monkeypatch):
    # No local_gate_cmd → nothing to smoke → the project is treated as runnable.
    lp = BoardLoop({"preflight": True})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True


async def test_preflight_passes_when_gate_exits_zero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True


async def test_preflight_fails_closed_on_nonzero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state["default"], str)
    assert "tsc: not found" in lp._preflight_state["default"]


async def test_preflight_fails_closed_when_gate_cannot_launch(monkeypatch):
    # The exact case this exists for: the gate binary isn't installed.
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        raise FileNotFoundError("pnpm")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state["default"], str)
    assert "could not run" in lp._preflight_state["default"]


def test_spawn_ready_holds_all_work_when_preflight_failed(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1: tsc: not found"}  # simulate a failed preflight
    store = _PreflightStore(ready=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    spawned = lp._spawn_ready()

    assert spawned is False  # dispatched nothing — every ready card is in the held project
    blocked = {c[1]: c[2] for c in store.calls if c[0] == "flag_blocked"}
    assert set(blocked) == {"bd-1", "bd-2"}  # both held, visibly
    assert all("preflight" in reason.lower() for reason in blocked.values())


async def test_preflight_recovery_releases_holds(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1"}  # previously failed
    lp._preflight_held = {"default": {"bd-1", "bd-2"}}  # and it held these
    # A recheck of a KNOWN-failed preflight is throttled by (monotonic() - _last_preflight);
    # put the last check far enough back that the recheck fires regardless of the absolute
    # monotonic value (a fresh CI container's clock can be < the 60s throttle window).
    lp._last_preflight = {"default": -10_000.0}
    # No ready work left (the held cards dropped out of `ready`) — recovery must STILL
    # fire for a project still marked failed, else the holds never release.
    store = _PreflightStore(ready=[])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _shell(*a, **k):
        return _FakeProc(0)  # gate now passes

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    assert lp._preflight_state["default"] is True
    assert lp._preflight_held == {}  # the project's hold set was cleared and dropped
    assert {c[1] for c in store.calls if c[0] == "clear_blocked"} == {"bd-1", "bd-2"}


async def test_spawn_ready_holds_only_the_failed_project(monkeypatch):
    """Per-project isolation (#90, r2/r6): preflight failed for project A, but a ready
    feature in project B is still claimed + driven this tick."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build", "max_concurrent": 2})
    lp._preflight_state = {"proj-a": "gate exited 1: tsc missing", "proj-b": True}
    # bd-a builds in the broken project A; bd-b in the healthy project B.
    store = _PreflightStore(ready=[("bd-a", "proj-a"), ("bd-b", "proj-b")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    finish = await _hold_drives(lp, monkeypatch)
    try:
        spawned = lp._spawn_ready()
        assert spawned is True  # project B's feature was dispatched despite A's broken gate
        assert len(lp._drives) == 1  # ONLY B — A is held
        claims = [c[1] for c in store.calls if c[0] == "claim"]
        assert claims == ["bd-b"]  # A never claimed; B claimed
        blocked = {c[1] for c in store.calls if c[0] == "flag_blocked"}
        assert blocked == {"bd-a"}  # only the failed project's card is held
    finally:
        await finish()


async def test_preflight_isolation_a_fails_b_passes(monkeypatch):
    """_maybe_preflight smokes each project's gate independently (#90): project A's gate
    fails-closed while project B's passes — the two states never cross-contaminate."""
    lp = BoardLoop(
        {
            "preflight": True,
            "projects": {
                "proj-a": {"repo": "/repos/a", "local_gate_cmd": "gate-a"},
                "proj-b": {"repo": "/repos/b", "local_gate_cmd": "gate-b"},
            },
        }
    )
    store = _PreflightStore(ready=[("bd-a", "proj-a"), ("bd-b", "proj-b")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _shell(cmd, **k):
        return _FakeProc(1, b"gate-a: boom") if cmd == "gate-a" else _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    assert isinstance(lp._preflight_state["proj-a"], str)  # A held closed
    assert "boom" in lp._preflight_state["proj-a"]
    assert lp._preflight_state["proj-b"] is True  # B runnable — unaffected by A


# ── boot recovery of orphaned preflight holds (#186) ────────────────────────────


_PREFLIGHT_HOLD_COMMENT = "blocked: gate preflight failed — the coder environment can't run the gate: tsc: not found"


class _BootPreflightStore(_PreflightStore):
    """_PreflightStore + the boot-recovery reads (#186): `_recover`'s in_progress scan
    (inherited — [] for any non-ready state) and the blocked-card scan WITH comment
    history (``raw_features_with_comments``)."""

    def __init__(self, ready=(), blocked=()):
        super().__init__(list(ready))
        self._blocked = list(blocked)  # raw `br` dicts: {"id", "comments"}

    def raw_features_with_comments(self, states=("done", "blocked")):
        return [dict(f) for f in self._blocked] if "blocked" in states else []


async def test_recover_unblocks_orphaned_preflight_holds(monkeypatch):
    """#186 (r1): `_preflight_held` dies with the process, so a restart orphans the
    previous loop's holds — the cards are blocked (not ready), invisible to both
    `_ready_projects` and a fresh `_preflight_state`, and would stay held forever.
    Boot recovery clear_blocked's every card whose CURRENT block reason carries the
    preflight marker — and ONLY those."""
    store = _BootPreflightStore(
        blocked=[
            {"id": "bd-1", "comments": [_PREFLIGHT_HOLD_COMMENT]},
            # `br` comments can be dict-shaped — the text field must parse too
            {"id": "bd-2", "comments": [{"text": _PREFLIGHT_HOLD_COMMENT}]},
            # blocked for an unrelated reason (operator/CI block) → never touched
            {"id": "bd-3", "comments": ["blocked: CI failed: pytest exploded"]},
            # once preflight-held but later re-blocked for another reason: the LAST
            # `blocked:` comment is the current reason → not a preflight orphan
            {"id": "bd-4", "comments": [_PREFLIGHT_HOLD_COMMENT, "blocked: coder timeout"]},
            # the reverse — an old unrelated block, currently preflight-held → released
            {"id": "bd-5", "comments": ["blocked: coder timeout", _PREFLIGHT_HOLD_COMMENT]},
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await BoardLoop({})._recover()
    cleared = {c[1] for c in store.calls if c[0] == "clear_blocked"}
    assert cleared == {"bd-1", "bd-2", "bd-5"}


async def test_recover_cleared_card_reheld_when_gate_still_broken(monkeypatch):
    """#186 (r2): boot releases the orphaned hold, and the first tick re-checks — a
    STILL-broken gate re-holds the card one tick later (fail-closed survives the
    restart; nothing is ever dispatched against the broken gate)."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    store = _BootPreflightStore(
        ready=["bd-1"],  # clear_blocked put the card back in ready
        blocked=[{"id": "bd-1", "comments": [_PREFLIGHT_HOLD_COMMENT]}],
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await lp._recover()
    assert ("clear_blocked", "bd-1") in store.calls  # boot released the orphan

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")  # STILL broken

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()  # the first tick smokes the gate again…
    assert isinstance(lp._preflight_state["default"], str)
    spawned = lp._spawn_ready()  # …and the claim scan re-holds instead of dispatching
    assert spawned is False
    blocked = {c[1]: c[2] for c in store.calls if c[0] == "flag_blocked"}
    assert set(blocked) == {"bd-1"}  # re-held, visibly, one tick after boot
    assert blocked["bd-1"].startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX)


def test_hold_reason_carries_the_recovery_marker(monkeypatch):
    """#186 (r3): the hold path stamps the SAME module constant the boot scan matches
    on — a drifted prefix would orphan every future hold across restarts."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1: tsc: not found"}
    store = _PreflightStore(ready=["bd-1"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp._hold_ready_for_preflight()
    reasons = [c[2] for c in store.calls if c[0] == "flag_blocked"]
    assert reasons and all(r.startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX) for r in reasons)


# ── auto gate resolution (_resolve_gate_cmd) ────────────────────────────────────


def _write(p, name, body):
    f = p / name
    f.write_text(body)
    return f


def test_resolve_gate_explicit_command_passes_through(tmp_path):
    # An explicit gate is never rewritten, even if the repo declares a ci script.
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm test"}}')
    assert _resolve_gate_cmd("pytest -q", str(tmp_path)) == "pytest -q"


def test_resolve_gate_blank_stays_gateless(tmp_path):
    # Blank still means "no gate" — auto must be opt-in, never inferred from blank.
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm test"}}')
    assert _resolve_gate_cmd("", str(tmp_path)) == ""
    assert _resolve_gate_cmd("  ", str(tmp_path)) == ""


def test_resolve_gate_auto_prefers_declared_ci_script(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm typecheck && pnpm -r test", "test": "x"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run ci"


def test_resolve_gate_auto_falls_to_check_then_verify(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"check": "x", "verify": "y"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run check"
    _write(tmp_path, "package.json", '{"scripts": {"verify": "y"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run verify"


def test_resolve_gate_auto_convention_fallback_when_no_entrypoint(tmp_path):
    # A node repo with no ci/check/verify → the --if-present standard-checks superset.
    _write(tmp_path, "package.json", '{"scripts": {"test": "vitest run"}}')
    got = _resolve_gate_cmd("auto", str(tmp_path))
    assert got == (
        f"{loop_install()} && pnpm -r --if-present typecheck && pnpm -r --if-present build && pnpm -r --if-present test"
    )


def test_resolve_gate_auto_prefers_gate_over_ci_script(tmp_path):
    # A complex-CI repo declares a dedicated fast `gate` alongside a heavy `ci`;
    # the coder must gate on `gate`, not the whole `ci`.
    _write(tmp_path, "package.json", '{"scripts": {"gate": "x", "ci": "everything"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run gate"


def test_resolve_gate_auto_reads_makefile_ci_target(tmp_path):
    _write(tmp_path, "Makefile", "build:\n\tgo build ./...\nci:\n\tgo test ./...\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "make ci"


def test_resolve_gate_auto_makefile_gate_beats_ci(tmp_path):
    # Python/Go/Rust path: `make gate` (fast slice) wins over a heavy `make ci`.
    _write(tmp_path, "Makefile", "ci:\n\tpytest -q -m 'integration'\ngate:\n\truff check . && pytest -q\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "make gate"


def test_resolve_gate_auto_justfile_check_target(tmp_path):
    _write(tmp_path, "justfile", "default:\n\techo hi\ncheck:\n\tcargo test\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "just check"


def test_resolve_gate_auto_unrecognized_repo_is_gateless(tmp_path):
    # Nothing recognized → "" (fail-open, gateless) rather than a wrong guess.
    _write(tmp_path, "README.md", "# a repo with no known toolchain")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == ""


def test_resolve_gate_auto_malformed_package_json_falls_through(tmp_path):
    # Broken package.json must not crash construction — treat as no scripts → fallback.
    _write(tmp_path, "package.json", "{ not valid json ")
    got = _resolve_gate_cmd("auto", str(tmp_path))
    assert got.startswith(loop_install()) and "--if-present" in got


def loop_install():
    from project_board.loop import _PNPM_INSTALL

    return _PNPM_INSTALL


# ── source-issue → PR "Fixes #N" line (#93) ─────────────────────────────────────


def test_source_issue_prefers_field_then_scans_description():
    """An explicit `source_issue` field wins (full URL / owner-repo#n / bare number);
    otherwise the FIRST GitHub issue URL in the feature text is used."""
    assert _source_issue({"source_issue": "https://github.com/acme/widgets/issues/8"}) == ("acme/widgets", 8)
    assert _source_issue({"source_issue": "other/repo#12"}) == ("other/repo", 12)
    assert _source_issue({"source_issue": "#4"}) == ("", 4)
    assert _source_issue({"source_issue": "4"}) == ("", 4)
    # No field → the FIRST issue URL in the text (spec here) wins over the later one.
    feat = {"spec": "per https://github.com/acme/widgets/issues/1 then https://github.com/acme/widgets/issues/2"}
    assert _source_issue(feat) == ("acme/widgets", 1)
    assert _source_issue({"spec": "no issue linked here"}) is None


def test_inject_same_repo_appends_fixes_and_is_idempotent():
    body = "## Summary\n\n- did the thing"
    out = _inject_source_issue_line(body, "acme/widgets", 7, "acme/widgets")
    assert out == body + "\n\nFixes #7"
    # A same-repo closing reference already present suppresses (no duplicate).
    assert _inject_source_issue_line(out, "acme/widgets", 7, "acme/widgets") == out
    assert _inject_source_issue_line("Closes #7 already", "acme/widgets", 7, "acme/widgets") == "Closes #7 already"


def test_inject_cross_repo_appends_refs_link():
    body = "## Summary\n\n- did it"
    out = _inject_source_issue_line(body, "other/repo", 9, "acme/widgets")
    assert out == body + "\n\nRefs https://github.com/other/repo/issues/9"


def test_inject_url_dedup_is_word_bounded():
    """FIX-NOW item 1: the URL suppression must be \\b-bounded — issues/12 is a substring
    of issues/123, so a body that only references #123 must NOT suppress a #12 line."""
    body = "See https://github.com/acme/widgets/issues/123 for context."
    out = _inject_source_issue_line(body, "acme/widgets", 12, "acme/widgets")
    assert out.endswith("Fixes #12")  # the 123 URL is not a match for #12
    # The EXACT-issue URL already present does suppress (a real duplicate reference).
    exact = "Already linked https://github.com/acme/widgets/issues/12 here."
    assert _inject_source_issue_line(exact, "acme/widgets", 12, "acme/widgets") == exact


def test_inject_cross_repo_shorthand_does_not_suppress():
    """FIX-NOW item 2: a bare `Fixes #42` can't name another repo's issue, so cross-repo
    it must NOT suppress the Refs line — only a full-URL match may."""
    body = "Unrelated: Fixes #42 in this repo."
    out = _inject_source_issue_line(body, "other/repo", 42, "acme/widgets")
    assert out.endswith("Refs https://github.com/other/repo/issues/42")
    # The cross-repo URL already present DOES suppress (no duplicate link).
    linked = "Refs https://github.com/other/repo/issues/42 already."
    assert _inject_source_issue_line(linked, "other/repo", 42, "acme/widgets") == linked


def test_inject_bare_number_is_same_repo_even_when_target_unknown():
    # A bare number (slug "") is same-repo by construction — Fixes #n even if the target
    # repo couldn't be resolved (repo_slug failed open); never a Refs to an empty slug.
    assert _inject_source_issue_line("body", "", 5, "") == "body\n\nFixes #5"


def test_inject_unknown_target_degrades_to_refs_for_a_slugged_issue():
    # repo_slug failed open (target ""): a slugged issue can't be confirmed same-repo, so
    # the safe degrade is a Refs link, never a possibly-wrong Fixes that auto-closes here.
    out = _inject_source_issue_line("body", "acme/widgets", 3, "")
    assert out == "body\n\nRefs https://github.com/acme/widgets/issues/3"


async def test_repo_slug_fails_open_on_gh_error(monkeypatch):
    """FIX-NOW item 3: worktree.repo_slug must fail OPEN — a raising _gh (WorktreeError /
    timeout) yields "" rather than propagating; a non-zero rc also yields ""."""

    async def _boom(*args, cwd, timeout=60):
        raise worktree.WorktreeError("gh repo view timed out after 60s")

    monkeypatch.setattr(worktree, "_gh", _boom)
    assert await worktree.repo_slug(cwd="/repo") == ""

    async def _rc1(*args, cwd, timeout=60):
        return (1, "", "not a gh repo")

    monkeypatch.setattr(worktree, "_gh", _rc1)
    assert await worktree.repo_slug(cwd="/repo") == ""

    async def _ok(*args, cwd, timeout=60):
        return (0, "acme/widgets\n", "")

    monkeypatch.setattr(worktree, "_gh", _ok)
    assert await worktree.repo_slug(cwd="/repo") == "acme/widgets"


async def test_drive_injects_fixes_line_into_the_pr_body(monkeypatch):
    """End-to-end: a feature carrying a same-repo source issue gets a `Fixes #n` line
    appended to the body the loop hands to open_pr (the coder stays out of the loop)."""
    bodies = []

    async def _open_pr(wt, branch, *, base, title, body):
        bodies.append(body)
        return "https://example/pr/1"

    async def _slug(*, cwd):
        return "acme/widgets"

    monkeypatch.setattr(worktree, "repo_slug", _slug)
    feature = {**FEATURE, "source_issue": "https://github.com/acme/widgets/issues/7"}
    _loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert bodies and bodies[0].endswith("Fixes #7")


# ── #113: the requirement ledger — parse dispositions, carry in prompt, gate the PR ──


def test_parse_requirements_reply_reads_done_and_declined_lines():
    from project_board.loop import _parse_requirements_reply

    reply = (
        "I did the work.\n"
        "## Requirements\n"
        "- r1: done\n"
        "- r2: declined — dicts not reachable through SqliteSaver+pickle\n"
        "r3 - done\n"
        "not a disposition line\n"
        "## Summary\n\n- r4: done (this is PAST the section — must not parse)\n"
    )
    assert _parse_requirements_reply(reply) == [
        {"id": "r1", "status": "done"},
        {"id": "r2", "status": "declined", "decline_reason": "dicts not reachable through SqliteSaver+pickle"},
        {"id": "r3", "status": "done"},
    ]


def test_parse_requirements_reply_keeps_the_last_heading_and_tolerates_no_section():
    from project_board.loop import _parse_requirements_reply

    reply = "## Requirements\n- r1: done\nnarration…\n## Requirements\n- r2: declined: not applicable\n"
    assert _parse_requirements_reply(reply) == [{"id": "r2", "status": "declined", "decline_reason": "not applicable"}]
    assert _parse_requirements_reply("no section here") == []
    assert _parse_requirements_reply("") == []


def test_parse_requirements_reply_ignores_open_and_junk_statuses():
    from project_board.loop import _parse_requirements_reply

    reply = "## Requirements\n- r1: open\n- r2: wontfix\n- r3: done\n"
    assert _parse_requirements_reply(reply) == [{"id": "r3", "status": "done"}]


def test_build_prompt_carries_the_requirement_ledger_with_statuses():
    feature = {
        **FEATURE,
        "requirements": [
            {"id": "r1", "text": "restore dict tolerance", "status": "open"},
            {"id": "r2", "text": "update CHANGELOG.md", "status": "done"},
        ],
    }
    prompt = BoardLoop({})._build_prompt(feature)
    assert "Requirements ledger" in prompt
    assert "`r1` [open] restore dict tolerance" in prompt
    assert "`r2` [done] update CHANGELOG.md" in prompt  # statuses ride along on re-dispatch
    assert "## Requirements" in prompt  # the reporting contract is named
    assert "SILENCE IS NOT" in prompt  # silence is not disposition
    # no ledger → no block (the prose-AC prompt is unchanged)
    assert "Requirements ledger" not in BoardLoop({})._build_prompt(FEATURE)


async def test_drive_requirement_gate_bounces_open_items_then_opens(monkeypatch):
    """An undisposed item bounces the build back (same tier, keep-worktree) with the
    open list injected; once every item is done/declined the PR opens. The loop —
    not the coder — writes the dispositions back to the bead each round."""
    prompts = []
    replies = iter(
        [
            "did some of it\n## Requirements\n- r1: done\n",  # r2 silent → stays open → bounce
            "## Requirements\n- r2: declined — repo has no CHANGELOG\n## Summary\n\n- done\n",
        ]
    )

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return next(replies)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/3"

    feature = {
        **FEATURE,
        "requirements": [
            {"id": "r1", "text": "restore dict tolerance", "status": "open"},
            {"id": "r2", "text": "update CHANGELOG.md", "status": "open"},
        ],
    }
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch, feature=feature)
    assert len(prompts) == 2  # initial + 1 ledger bounce
    assert store.creates == ["bd-1"]  # keep-worktree: the impl is not thrown away
    assert "r2" in prompts[1] and "update CHANGELOG.md" in prompts[1]  # the open list re-injected
    # the loop wrote dispositions back each round; the final ledger is fully closed
    writes = [c for c in store.calls if c[0] == "set_requirements"]
    assert len(writes) == 2
    final = writes[-1][2]
    assert {i["id"]: i["status"] for i in final} == {"r1": "done", "r2": "declined"}
    assert final[1]["decline_reason"] == "repo has no CHANGELOG"  # a decline with a reason is a valid close
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls
    assert loop._req_fix_attempts.get("bd-1", 0) == 0  # budget reset once the PR opened


async def test_drive_requirement_gate_exhausted_blocks_never_opens(monkeypatch):
    """The hard invariant: a feature cannot reach in_review with open items — an
    exhausted bounce budget is a capability failure (block with a single coder),
    NEVER a PR with unaddressed requirements (unlike the local gate's fail-open)."""

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "no requirements section at all"

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("no PR may open while ledger items are open")

    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "restore dict tolerance", "status": "open"}]}
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        feature=feature,
        cfg={"coder": "proto", "goal_fix_max": 0},
    )
    assert "open_review" not in store.names()
    blocked = next(c for c in store.calls if c[0] == "flag_blocked")
    assert "requirements unresolved" in blocked[2] and "r1" in blocked[2]


async def test_drive_without_a_ledger_never_touches_set_requirements(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/4"

    _loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)  # FEATURE has no ledger
    assert "set_requirements" not in store.names()
    assert ("open_review", "bd-1", "https://example/pr/4") in store.calls


# ── shutdown-aware dispatch guards (#149) ──────────────────────────────────────


async def test_drive_shutdown_suppresses_coder_timeout_escalation(monkeypatch):
    """A dispatch killed during host shutdown must NOT escalate and must NOT write a
    tier label or block the feature (r1/r3/r4/r5). The feature stays in_progress so
    boot recovery (_recover → _reconcile_orphan) handles it on the next start."""

    class _EscStore(FakeLoopStore):
        def __init__(self):
            super().__init__()
            self.escalated = []

        def escalate(self, fid, reason):
            self.escalated.append((fid, reason))
            return "smart"  # would climb a tier without the shutdown guard

    store = _EscStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        raise worktree.CoderTimeout("coder killed during shutdown")

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr must not be reached when shutting down")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    # Escalation is ON (two distinct coders) so without the guard this would escalate.
    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    loop._shutting_down = True  # simulate shutdown in progress
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    await loop._drive(FEATURE)

    assert store.escalated == []  # no tier escalation (no tier label written)
    assert "flag_blocked" not in store.names()  # feature stays in_progress
    assert loop._inflight == {}  # cleanup still runs


async def test_preflight_cancelled_by_shutdown_does_not_produce_failure(monkeypatch):
    """A preflight cancelled during host shutdown must NOT set _preflight_state to a
    failure string (r2/r6): no PREFLIGHT FAILED log, no HOLDING verdict. The state
    stays at its pre-call value (None = unchecked) so the next boot re-runs cleanly."""
    loop = BoardLoop({"local_gate_cmd": "ruff check .", "preflight": True})
    loop._shutting_down = True  # simulate shutdown in progress

    async def _cancelled(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _cancelled)

    await loop._preflight("default", "ruff check .", "/repo")

    # The project's state must remain unset (unchecked) — never a failure string that
    # would hold work.
    assert loop._preflight_state.get("default") is None
    assert not isinstance(loop._preflight_state.get("default"), str)


# ── the auto-merge edge: green + current + reviewed + not held → gh pr merge ──────


class _MergeStore:
    def __init__(self, feature):
        self.feature = dict(feature)
        self.comments = []

    def get_feature(self, fid):
        return dict(self.feature)

    def _comment(self, fid, text):
        self.comments.append((fid, text))


def _reviewed(**over):
    f = {
        "id": "bd-1",
        "board_state": "in_review",
        "blocked": False,
        "labels": ["in-review", "review-clean", "merged-verified:abcdef123456"],
        "pr_url": "https://github.com/o/r/pull/1",
    }
    f.update(over)
    return f


def _merge_env(monkeypatch, *, head="abcdef123456" + "0" * 28, mss="CLEAN", merge_ok=True):
    calls = {"merge": []}

    async def _head(repo, ref):
        return head

    async def _mss(pr_url, *, cwd="."):
        return mss

    async def _merge(pr_url, *, method="squash", cwd="."):
        calls["merge"].append((pr_url, method, cwd))
        return (merge_ok, "" if merge_ok else "Pull request is not mergeable: required status check pending")

    async def _state(pr_url, *, cwd="."):
        return "MERGED" if (merge_ok or calls.get("landed_anyway")) else "OPEN"

    async def _delete(repo, branch):
        calls.setdefault("deleted", []).append(branch)
        return True

    monkeypatch.setattr(worktree, "origin_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_merge_state", _mss)
    monkeypatch.setattr(worktree, "merge_pr", _merge)
    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "delete_remote_branch", _delete)
    return calls


async def test_auto_merge_merges_when_every_gate_is_green_and_current(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert calls["merge"] == [("https://github.com/o/r/pull/1", "squash", "/repo")]


async def test_auto_merge_is_off_by_default_and_never_called(monkeypatch):
    loop = BoardLoop({})
    assert loop.auto_merge is False  # opt-in: a pre-upgrade board keeps parking PRs for its adjudicator


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"labels": ["in-review", "review-clean", "merged-verified:abcdef123456", "merge-hold"]}, "merge-hold"),
        ({"labels": ["in-review", "review-pending", "merged-verified:abcdef123456"]}, "review in progress"),
        ({"labels": ["in-review", "changes-requested", "merged-verified:abcdef123456"]}, "changes requested"),
        ({"labels": ["in-review", "merged-verified:abcdef123456"]}, "no review-clean verdict"),
        ({"labels": ["in-review", "review-clean", "merged-verified:000000000000"]}, "merged-verified stamp"),
        ({"labels": ["in-review", "review-clean"]}, "merged-verified stamp (none)"),
        ({"blocked": True}, "blocked"),
        ({"board_state": "in_progress"}, "state=in_progress"),
    ],
)
async def test_auto_merge_holds_on_each_board_side_blocker(monkeypatch, over, reason):
    calls = _merge_env(monkeypatch)
    # A local gate is what writes the merged-verified stamp — the currency rows
    # above only mean something on a board that has one (#209).
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "local_gate_cmd": "pytest -q"})
    store = _MergeStore(_reviewed(**over))
    why = await loop._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
    assert any(reason in w for w in why), why
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []


@pytest.mark.parametrize("mss", ["BLOCKED", "UNSTABLE", "BEHIND", "DIRTY", "UNKNOWN", "DRAFT", ""])
async def test_auto_merge_requires_github_clean(monkeypatch, mss):
    """Only GitHub's CLEAN — required checks + branch protection satisfied, not draft,
    mergeable. UNSTABLE (a non-required check red) is deliberately NOT a merge."""
    calls = _merge_env(monkeypatch, mss=mss)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []


async def test_auto_merge_without_review_gate_needs_no_review_label(monkeypatch):
    """review_gate off ⇒ there is no review verdict to wait for (the fleet pipeline
    reviews on its own); CI + verdict currency still gate."""
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1


async def test_auto_merge_without_a_local_gate_does_not_wait_for_a_stamp_nobody_writes(monkeypatch):
    """#209: `local_gate_cmd` blank (the default) ⇒ `_verify_merged_state` never
    stamps `merged-verified:` — so the merge edge must not require it, or auto_merge
    is unreachable on a default board (review-clean + CI-green cards sat in_review
    forever on orbisEngineer with `merged-verified stamp (none)` at debug level).
    CI + GitHub CLEAN are the gates."""
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})  # auto_rebase defaults ON, no local gate
    assert loop.auto_rebase and not loop.local_gate_cmd
    store = _MergeStore(_reviewed(labels=["in-review", "review-clean"]))  # no stamp — nothing ever writes one
    assert (
        await loop._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
        == []
    )
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1
    # …and WITH a local gate the stamp is still required (the #131 contract is intact).
    gated = BoardLoop({"auto_merge": True, "review_gate": True, "local_gate_cmd": "pytest -q"})
    why = await gated._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
    assert any("merged-verified stamp (none)" in w for w in why), why


async def test_auto_merge_without_auto_rebase_skips_the_currency_check(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "auto_rebase": False})
    store = _MergeStore(_reviewed(labels=["in-review", "review-clean"]))  # no stamp — auto_rebase never writes one
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1


async def test_auto_merge_refusal_retries_then_gives_up_on_the_bead_never_blocks(monkeypatch, caplog):
    calls = _merge_env(monkeypatch, merge_ok=False)
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "auto_merge_max": 2})
    store = _MergeStore(_reviewed())
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
        # exhausted: no third gh call, one give-up comment, and the feature is NOT blocked
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert len(calls["merge"]) == 2
    assert len(store.comments) == 1 and "auto-merge gave up after 2 attempt(s)" in store.comments[0][1]
    assert "gave up" in caplog.text
    assert store.feature["blocked"] is False


async def test_auto_merge_trusts_pr_state_over_gh_exit_code(monkeypatch):
    """gh can exit non-zero AFTER the merge landed (the --delete-branch local-branch
    trap, 2026-08-20) or lose a race to a concurrent merge — the PR's state is the
    verdict, and a landed merge must not spend the retry budget."""
    calls = _merge_env(monkeypatch, merge_ok=False)
    calls["landed_anyway"] = True
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert "bd-1" not in loop._auto_merge_failures
    assert calls["deleted"] == ["feat/bd-1"]  # remote cleanup rides the success path


async def test_auto_merge_deletes_the_remote_branch_after_a_clean_merge(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert calls["deleted"] == ["feat/bd-1"]


async def test_auto_merge_honours_merge_method(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False, "merge_method": "rebase"})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo")
    assert calls["merge"][0][1] == "rebase"


class _BlockedReconcileStore(_ReconcileStore):
    """A merged-but-blocked card (#196): the reconciler must poll blocked features'
    PRs and drive MERGED to done, but never touch CLOSED/OPEN ones (their blocked
    reason is deliberate)."""

    def __init__(self, in_review, blocked):
        super().__init__(in_review)
        self._blocked = blocked

    def list_features(self, state=None):
        if state == "blocked":
            return self._blocked
        return super().list_features(state)


async def test_reconcile_prs_promotes_a_blocked_card_whose_pr_merged(monkeypatch):
    """#196 (protoEngineer friction 2026-08-19): bd-2ti/bd-q1i had MERGED PRs but sat
    in blocked forever because the reconcile scanned in_review only."""
    store = _BlockedReconcileStore(
        [],
        [{"id": "bd-blk", "pr_url": "https://example/pr/7", "board_state": "blocked"}],
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_state(url, *, cwd="."):
        return "MERGED"

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({})
    await loop._reconcile_prs()
    assert store.merged == ["https://example/pr/7"]
    assert store.blocked == []  # never re-flagged


async def test_reconcile_prs_leaves_a_blocked_card_with_an_open_or_closed_pr_alone(monkeypatch):
    """The blocked scan takes ONLY the merged edge: a CLOSED PR must not rewrite the
    card's deliberate blocked reason, and the OPEN gates must not run against it."""
    for pr_state_val in ("OPEN", "CLOSED"):
        store = _BlockedReconcileStore(
            [],
            [{"id": "bd-blk", "pr_url": "https://example/pr/7", "board_state": "blocked"}],
        )
        monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

        async def _pr_state(url, *, cwd="."):
            return pr_state_val

        monkeypatch.setattr(worktree, "pr_state", _pr_state)
        loop = BoardLoop({})
        ci_called = []

        async def _ci_spy(store_, fid, pr_url, repo):
            ci_called.append(fid)

        monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
        await loop._reconcile_prs()
        assert store.merged == [] and store.blocked == [] and ci_called == [], pr_state_val


async def test_spawn_ready_does_not_collide_same_filename_across_projects(monkeypatch):
    """#197 (protoEngineer friction 2026-08-20): every plugin repo carries PROTO.md —
    two doc-touching cards in DIFFERENT projects must both claim; only a same-project
    overlap defers."""
    store = _ClaimStore(
        [
            _ready("bd-ke7", ["PROTO.md"], project="discord"),
            _ready("bd-qjd", ["PROTO.md"], project="promptlab"),
            _ready("bd-dup", ["PROTO.md"], project="discord"),
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 3})
    monkeypatch.setattr(loop, "_project_name", lambda f: f.get("project") or "")
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        # cross-project twin claims; the SAME-project twin defers.
        assert store.claimed == ["bd-ke7", "bd-qjd"]
        assert loop._inflight_files == {
            "bd-ke7": {("discord", "PROTO.md")},
            "bd-qjd": {("promptlab", "PROTO.md")},
        }
    finally:
        await finish()

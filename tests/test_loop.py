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

from project_board import worktree
from project_board.loop import BoardLoop, _ci_failure_reason, _resolve_gate_cmd


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

    def record_gens_spent(self, fid, n):
        self.gens_spent[fid] = self.gens_spent.get(fid, 0) + n
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
    assert loop.max_concurrent == 1  # serial by default
    assert loop.merge_poll is True and loop.merge_poll_interval == 60


def test_escalation_on_with_two_distinct_coders():
    loop = BoardLoop({"coders": {"fast": "proto", "smart": "proto-smart"}})
    assert loop.escalation_on is True


def test_max_concurrent_floors_at_one():
    assert BoardLoop({"max_concurrent": 0}).max_concurrent == 1
    assert BoardLoop({"max_concurrent": 4}).max_concurrent == 4


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
    """The coder's final reply is used VERBATIM as the PR body (loop.py's
    `open_pr(..., body=(result or "")[:4000])`) with no post-processing — so an
    un-briefed coder narrating its whole thought process ships that straight into
    the PR description. The prompt must say so explicitly and ask for a short,
    clean summary instead."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "final message becomes the pr description" in prompt.lower()
    assert "do not narrate your process" in prompt.lower()


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

    async def _default_dispatch(c, wt, prompt, *, timeout=None):
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

    async def _dispatch(c, wt, prompt, *, timeout=None):
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

    async def _dispatch(c, wt, prompt, *, timeout=None):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    gate_seq = iter(["FAILED tests/test_config.py::golden - boom", None])

    async def _gate(wt):
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

    async def _dispatch(c, wt, prompt, *, timeout=None):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/2"

    async def _gate(wt):
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
    # No escalation ladder (single coder) → a capability failure blocks immediately.
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


# ── coder.solve() board seam (ADR 0064 P2) ───────────────────────────────────────


def test_coder_solve_config_defaults():
    loop = BoardLoop({})
    assert loop.coder_solve is True  # opt-OUT valve; the real gate is coder_seam
    assert loop.coder_solve_test_cmd == ""  # no local_gate_cmd to fall back to either
    assert loop.coder_solve_budget == 6
    assert loop.coder_solve_k == 3
    assert loop.coder_solve_tree_depth == 2
    assert loop.coder_solve_test_timeout == 300


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


async def _pass_gate(wt):
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
    ):
        seen["fid"] = fid
        seen["test_cmd"] = test_cmd
        seen["task"] = task
        record_gens(4)
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
    assert store.gens_spent.get("bd-1") == 4
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

    async def _disp(c, wt, prompt, *, timeout=None):
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

    async def _disp(c, wt, prompt, *, timeout=None):
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

    async def _dispatch(c, wt, prompt, *, timeout=None):
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


def _ready(fid, files):
    return {"id": fid, "board_state": "ready", "files_to_modify": files}


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
        assert loop._inflight_files == {"bd-1": {"shared.py"}, "bd-3": {"other.py"}}
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


# ── periodic health sweep ───────────────────────────────────────────────────────


class _SweepStore:
    def __init__(self, in_progress=(), features=None):
        self._in_progress = list(in_progress)
        self._features = features or {}  # fid -> board_state
        self.requeued = []

    def list_features(self, state=None):
        return [{"id": f} for f in self._in_progress] if state == "in_progress" else []

    def requeue(self, fid):
        self.requeued.append(fid)

    def open_review(self, fid, *, pr_url):
        pass

    def get_feature(self, fid):
        st = self._features.get(fid)
        return {"id": fid, "board_state": st} if st else None


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
    store = _SweepStore(features={"bd-done": "done", "bd-rev": "in_review"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: ["bd-done", "bd-rev", "bd-gone"])
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    await BoardLoop({})._sweep()
    # done + missing feature → reaped; in_review keeps its worktree (CI-fail re-dispatch).
    assert set(reaped) == {"bd-done", "bd-gone"}


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

    async def gate(wt):
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

    async def gate(wt):
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

    async def gate(wt):
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

    async def gate(wt):
        raise AssertionError("the gate must not run when local_gate_cmd is unset")

    async def judge(*a, **k):
        return 0

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 0


async def test_select_candidate_none_when_no_diff(monkeypatch):
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 2})
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set()))  # all empty

    async def gate(wt):
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
    run could not happen) and whose PR-diff fetch is stubbed."""
    loop = BoardLoop({"review_gate": True, **(cfg or {})})

    async def _run(fid, pr_url):
        return output

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
        assert store.review_states[-1][0] is None  # sub-state cleared
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
    assert "bd-1" not in loop._review_run_failures


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


# ── gate preflight (fail-closed: never start work a broken gate can't accept) ─────


class _PreflightStore(FakeLoopStore):
    """FakeLoopStore + the ready-list and clear_blocked the preflight hold/release use."""

    def __init__(self, ready):
        super().__init__()
        self._ready = [{"id": f, "blocked": False} for f in ready]

    def list_features(self, state=None):
        return list(self._ready) if state == "ready" else []

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
    assert BoardLoop({})._preflight_state is None
    assert BoardLoop({"preflight": False}).preflight is False


async def test_preflight_noop_when_no_gate():
    # No local_gate_cmd → nothing to smoke → treated as runnable, never shells out.
    lp = BoardLoop({"preflight": True})
    await lp._maybe_preflight()
    assert lp._preflight_state is True


async def test_preflight_passes_when_gate_exits_zero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._store_kw = {"repo": "/repo"}

    async def _shell(*a, **k):
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert lp._preflight_state is True


async def test_preflight_fails_closed_on_nonzero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._store_kw = {"repo": "/repo"}

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state, str)
    assert "tsc: not found" in lp._preflight_state


async def test_preflight_fails_closed_when_gate_cannot_launch(monkeypatch):
    # The exact case this exists for: the gate binary isn't installed.
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._store_kw = {"repo": "/repo"}

    async def _shell(*a, **k):
        raise FileNotFoundError("pnpm")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state, str)
    assert "could not run" in lp._preflight_state


def test_spawn_ready_holds_all_work_when_preflight_failed(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = "gate exited 1: tsc: not found"  # simulate a failed preflight
    store = _PreflightStore(ready=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    spawned = lp._spawn_ready()

    assert spawned is False  # dispatched nothing
    blocked = {c[1]: c[2] for c in store.calls if c[0] == "flag_blocked"}
    assert set(blocked) == {"bd-1", "bd-2"}  # both held, visibly
    assert all("preflight" in reason.lower() for reason in blocked.values())


async def test_preflight_recovery_releases_holds(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._store_kw = {"repo": "/repo"}
    lp._preflight_state = "gate exited 1"  # previously failed
    lp._preflight_held = {"bd-1", "bd-2"}  # and it held these
    store = _PreflightStore(ready=[])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _shell(*a, **k):
        return _FakeProc(0)  # gate now passes

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    assert lp._preflight_state is True
    assert lp._preflight_held == set()
    assert {c[1] for c in store.calls if c[0] == "clear_blocked"} == {"bd-1", "bd-2"}


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


def test_resolve_gate_auto_reads_makefile_ci_target(tmp_path):
    _write(tmp_path, "Makefile", "build:\n\tgo build ./...\nci:\n\tgo test ./...\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "make ci"


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

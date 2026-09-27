"""#456: the gate preflight ran twice at once and never finished a slow gate.
#459: coder.solve() judged every candidate by a gate slower than its own timeout.
protoAgent#3692: a deleted `coders` rung delegate paused the whole loop.

The live evidence behind all three, from protoEngineer's board on 2026-09-26/27:

* two `scripts/gate.py` preflights in the same checkout, seven seconds apart (the tick and
  an on-demand `board_dispatch`), each ending "timed out (600s) — indeterminate, allowing
  dispatch" after a 12-minute gate had made every dispatch wait ten minutes;
* bd-7aun spending 15 generations on "acceptance tests timed out after 300s", being
  blocked as `transient`, and being auto-unblocked to spend them again;
* every card stopping when the `fable` delegate was deleted, including cards that would
  have run on `opus`.

The single-flight and timeout-breaker paths are exercised with REAL processes. A mocked
spawn could only show the test's own idea of concurrency, and a mocked timeout could only
show its own idea of what the verdict text says.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from project_board import coder_seam, health, setup_check, worktree
from project_board.failures import ORACLE_TIMEOUT_CLASS
from project_board.loop import BoardLoop
from project_board.loop._common import _SELF_HEALING_BLOCKS
from test_loop import FEATURE, _drive_with, _EscalatingStore, _rung_env

posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises a POSIX /bin/sh")

_AC = "WHEN x THE SYSTEM SHALL y"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> Path:
    """A real git repo with one commit on `main`, and a pinned repo-local identity."""
    _git("init", "-b", "main", str(path))
    for k, v in (("user.email", "board-test@localhost"), ("user.name", "Board Test"), ("commit.gpgsign", "false")):
        _git("-C", str(path), "config", k, v)
    (path / "README.md").write_text("base\n")
    _git("-C", str(path), "add", "-A")
    _git("-C", str(path), "commit", "-m", "base")
    return path


def _commit(path: Path, name: str, text: str = "x\n") -> str:
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    _git("-C", str(path), "add", "-A")
    _git("-C", str(path), "commit", "-m", f"add {name}")
    return _git("-C", str(path), "rev-parse", "HEAD")


def _loop(cfg: dict, projects=("default",)) -> BoardLoop:
    """A loop whose ready scan names ``projects``, with no store behind it."""
    lp = BoardLoop({"coder": "proto", **cfg})
    lp._store = lambda: object()
    lp._ready_projects = lambda _store: list(projects)
    return lp


async def _clean(*_a, **_k):
    return ""


# ── the two new worktree seams, against real git ────────────────────────────────────


async def test_checkout_head_sha_reads_the_checked_out_commit(tmp_path):
    repo = _repo(tmp_path / "r")
    assert await worktree.checkout_head_sha(str(repo)) == _git("-C", str(repo), "rev-parse", "HEAD")
    moved = _commit(repo, "a.py")
    assert await worktree.checkout_head_sha(str(repo)) == moved
    assert await worktree.checkout_head_sha(str(tmp_path / "not-a-repo")) == ""  # unknown, never an error


async def test_changed_paths_lists_commits_edits_and_new_files_but_not_the_boards_own(tmp_path):
    """A candidate may commit its work or leave it uncommitted. Both must pick the same
    oracle. The board's scratch and its node_modules link never count as changes."""
    origin = tmp_path / "origin.git"
    seed = _repo(tmp_path / "seed")
    _git("init", "--bare", str(origin))
    _git("-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    _git("-C", str(seed), "remote", "add", "origin", str(origin))
    _git("-C", str(seed), "push", "-u", "origin", "main")
    tree = tmp_path / "clone"
    _git("clone", str(origin), str(tree))
    for k, v in (("user.email", "t@localhost"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git("-C", str(tree), "config", k, v)
    assert await worktree.changed_paths(str(tree), "main") == []

    _commit(tree, "apps/web/src/a.ts")  # committed since the fork
    (tree / "README.md").write_text("edited\n")  # tracked, uncommitted
    (tree / "docs").mkdir()
    (tree / "docs" / "new.md").write_text("new\n")  # untracked
    (tree / ".proto").mkdir()
    (tree / ".proto" / "notes.md").write_text("scratch\n")  # the coder's scratch
    (tmp_path / "nm").mkdir()
    os.symlink(tmp_path / "nm", tree / "node_modules")  # the board's link

    assert sorted(await worktree.changed_paths(str(tree), "main")) == [
        "README.md",
        "apps/web/src/a.ts",
        "docs/new.md",
    ]
    assert await worktree.changed_paths(str(tmp_path / "nowhere"), "main") is None


# ── #456: single-flight, per-commit cache, slow gate ────────────────────────────────


@posix_only
async def test_two_concurrent_preflights_spawn_one_gate_process(tmp_path, monkeypatch):
    """The live shape: the tick and a `board_dispatch` both preflight a never-checked
    project at the same moment. One real gate process runs, and both callers get its
    verdict."""
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    runs = tmp_path / "runs"
    gate = f"echo run >> {runs}; sleep 1"
    lp = _loop({"repo": str(tmp_path), "local_gate_cmd": gate})

    spawned = []
    real_spawn = worktree.spawn_shell

    async def _counting_spawn(cmd, **kw):
        spawned.append(cmd)
        return await real_spawn(cmd, **kw)

    monkeypatch.setattr(worktree, "spawn_shell", _counting_spawn)
    await asyncio.wait_for(asyncio.gather(lp._maybe_preflight(), lp._maybe_preflight()), timeout=30)

    assert runs.read_text().splitlines() == ["run"], "two preflights ran the gate twice"
    assert spawned == [gate]
    assert lp._preflight_state["default"] is True
    assert lp._preflight_tasks == {}  # the finished run left the in-flight map


async def test_a_run_for_a_changed_command_is_not_shared(tmp_path, monkeypatch):
    """A registry save that changes a project's gate resets its verdict. The new command
    must get its own run, not the answer to the old one still in flight."""
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    lp = _loop({"repo": str(tmp_path)})
    old = lp._start_preflight("default", "sleep 1", str(tmp_path), "main")
    assert lp._start_preflight("default", "sleep 1", str(tmp_path), "main") is old  # same command: shared
    new = lp._start_preflight("default", "true", str(tmp_path), "main")
    assert new is not old
    await asyncio.wait_for(asyncio.gather(old, new), timeout=20)


@posix_only
async def test_a_pass_stands_for_its_commit_and_the_next_commit_rechecks_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    repo = _repo(tmp_path / "r")
    runs = tmp_path / "runs"
    lp = _loop({"repo": str(repo), "local_gate_cmd": f"echo run >> {runs}"})

    await lp._maybe_preflight()
    first = _git("-C", str(repo), "rev-parse", "HEAD")
    assert lp._preflight_sha["default"] == first and len(runs.read_text().splitlines()) == 1

    for _ in range(3):  # same commit: every later pass is a cache hit
        await lp._maybe_preflight()
    assert len(runs.read_text().splitlines()) == 1

    second = _commit(repo, "a.py")  # the checkout moved
    await lp._maybe_preflight()  # returns at once: the re-check runs in the background
    task = lp._preflight_tasks.get("default")
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    assert len(runs.read_text().splitlines()) == 2
    assert lp._preflight_sha["default"] == second and lp._preflight_state["default"] is True


@posix_only
async def test_a_background_recheck_that_goes_red_holds_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    repo = _repo(tmp_path / "r")
    lp = _loop({"repo": str(repo), "local_gate_cmd": "test ! -e broken || { echo tsc: not found; exit 1; }"})
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True

    _commit(repo, "broken")
    await lp._maybe_preflight()
    await asyncio.wait_for(asyncio.shield(lp._preflight_tasks["default"]), timeout=30)
    assert "tsc: not found" in lp._preflight_state["default"]
    assert "tsc: not found" in health.preflight_snapshot()["held"]["default"]


@posix_only
async def test_a_gate_slower_than_the_preflight_timeout_runs_once_per_commit_and_warns_once(
    tmp_path, monkeypatch, caplog
):
    """#456's other half: a 12-minute gate against a 600 s timeout made every dispatch wait
    the full timeout for "indeterminate". Now it is measured once, warned about once,
    shown on /status and in setup, and not re-run until the checkout moves."""
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    repo = _repo(tmp_path / "r")
    runs = tmp_path / "runs"
    lp = _loop({"repo": str(repo), "local_gate_cmd": f"echo run >> {runs}; sleep 30", "preflight_timeout_s": 0.5})

    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        await asyncio.wait_for(lp._maybe_preflight(), timeout=20)
        for _ in range(3):
            await lp._maybe_preflight()

    assert len(runs.read_text().splitlines()) == 1, "a known-slow gate was re-run on the same commit"
    assert lp._preflight_state["default"] is True  # indeterminate → dispatch allowed, as before
    assert lp._gate_seconds["default"] == (0.5, True)  # a lower bound for the solve guard (#459)
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "preflight_cmd" in r.getMessage()]
    assert len(warnings) == 1
    slow = health.preflight_snapshot()["slow"]["default"]
    assert slow["timeout_s"] == 0.5 and "sleep 30" in slow["cmd"]
    assert "can't finish inside preflight_timeout_s" in setup_check.setup_status({"repo": str(repo)})["preflight_hint"]

    _commit(repo, "a.py")  # the base moved: one more measurement, no second warning
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        await lp._maybe_preflight()
        await asyncio.wait_for(asyncio.shield(lp._preflight_tasks["default"]), timeout=20)
    assert len(runs.read_text().splitlines()) == 2
    assert len([r for r in caplog.records if r.levelname == "WARNING" and "preflight_cmd" in r.getMessage()]) == 1


@posix_only
async def test_preflight_cmd_is_smoked_instead_of_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    ran = []
    real_spawn = worktree.spawn_shell

    async def _spy(cmd, **kw):
        ran.append(cmd)
        return await real_spawn(cmd, **kw)

    monkeypatch.setattr(worktree, "spawn_shell", _spy)
    lp = _loop(
        {
            "projects": {
                "web": {"repo": str(tmp_path), "local_gate_cmd": "exit 1", "preflight_cmd": "true"},
                "api": {"repo": str(tmp_path), "local_gate_cmd": "true"},
            }
        },
        projects=("web", "api"),
    )
    await lp._maybe_preflight()
    assert ran == ["true", "true"]  # web smoked its preflight_cmd, api fell back to its gate
    assert lp._preflight_state == {"web": True, "api": True}


def test_preflight_cmd_resolution():
    flat = BoardLoop({"local_gate_cmd": "make gate", "preflight_cmd": "make lint"})
    assert flat._preflight_cmd_for({}) == "make lint"
    assert BoardLoop({"local_gate_cmd": "make gate"})._preflight_cmd_for({}) == "make gate"
    # a board-wide preflight_cmd never runs in a project that has its own gate
    multi = BoardLoop(
        {
            "preflight_cmd": "make lint",
            "projects": {"a": {"repo": "/a", "local_gate_cmd": "pytest"}, "b": {"repo": "/b"}},
        }
    )
    assert multi._preflight_cmd_for({"project": "a"}) == "pytest"
    assert multi._preflight_cmd_for({"project": "b"}) == "make lint"


async def test_stop_cancels_a_preflight_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    lp = _loop({"repo": str(tmp_path), "local_gate_cmd": "sleep 30"})
    task = lp._start_preflight("default", "sleep 30", str(tmp_path), "main")
    await asyncio.sleep(0.2)
    await asyncio.wait_for(lp.stop(), timeout=20)
    assert task.done()
    assert lp._preflight_state.get("default") is None  # shutdown gives no verdict


# ── #459: the oracle guard, path-scoped oracles, the timeout breaker ─────────────────


def _solve_on(monkeypatch):
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())


def test_an_unwinnable_gate_fallback_turns_solve_off_for_the_project(monkeypatch, caplog):
    _solve_on(monkeypatch)
    lp = BoardLoop({"local_gate_cmd": "python scripts/gate.py", "coder_solve_test_timeout_s": 300})
    card = {"acceptance_criteria": _AC}
    assert lp._use_coder_solve(card) is True  # nothing measured yet

    lp._gate_seconds["default"] = (600.0, True)  # the preflight timed out at 600 s
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert lp._use_coder_solve(card) is False
        assert lp._use_coder_solve(card) is False
    assert len([r for r in caplog.records if "coder.solve() is OFF" in r.getMessage()]) == 1  # warned once
    assert "at least 600s" in lp._oracle_unwinnable["default"]
    assert "default" in health.preflight_snapshot()["unwinnable_oracle"]


def test_a_measured_gate_inside_the_budget_keeps_solve_on(monkeypatch):
    _solve_on(monkeypatch)
    lp = BoardLoop({"local_gate_cmd": "pytest -q", "coder_solve_test_timeout_s": 300})
    lp._gate_seconds["default"] = (45.0, False)
    assert lp._use_coder_solve({"acceptance_criteria": _AC}) is True


def test_an_explicit_oracle_or_a_paths_map_is_never_second_guessed(monkeypatch):
    """The guard is for the FALLBACK only. An oracle the operator chose is theirs to size,
    and the timeout breaker still covers it."""
    _solve_on(monkeypatch)
    for extra in (
        {"coder_solve_test_cmd": "python scripts/gate.py"},
        {"coder_solve_test_paths": {"apps/web/**": "npx vitest run", "**": "gate"}},
    ):
        lp = BoardLoop({"local_gate_cmd": "python scripts/gate.py", **extra})
        lp._gate_seconds["default"] = (600.0, True)
        assert lp._use_coder_solve({"acceptance_criteria": _AC}) is True, extra


def test_a_paths_map_alone_is_a_runnable_oracle(monkeypatch):
    _solve_on(monkeypatch)
    lp = BoardLoop({"coder_solve_test_paths": {"apps/web/**": "npx vitest run"}})
    assert lp._use_coder_solve({"acceptance_criteria": _AC}) is True
    assert lp._coder_solve_settings({})["test_paths"] == [("apps/web/**", "npx vitest run")]


def test_parse_test_paths_shapes_and_the_gate_keyword():
    as_map = coder_seam.parse_test_paths({"apps/web/**": "vitest", "docs/**": "", "**": "gate"}, gate_cmd="make gate")
    assert as_map == [("apps/web/**", "vitest"), ("docs/**", ""), ("**", "make gate")]
    as_list = coder_seam.parse_test_paths([{"a/**": "x"}, ["b/**", "skip"]], gate_cmd="")
    assert as_list == [("a/**", "x"), ("b/**", "")]
    assert coder_seam.parse_test_paths({"**": "gate"}, gate_cmd="") == []  # nothing to run
    assert coder_seam.parse_test_paths("nonsense") == []


def test_select_oracle_first_match_wins_and_several_run_in_map_order():
    paths = [("apps/web/**", "WEB"), ("docs/**", ""), ("scripts/*.py", "PY"), ("**", "GATE")]
    web_only, _ = coder_seam.select_oracle(paths, ["apps/web/src/a.ts", "apps/web/b.tsx"], "DEFAULT")
    assert web_only == ["WEB"]  # `**` is never reached for a file `apps/web/**` matched
    both, note = coder_seam.select_oracle(paths, ["server/x.py", "apps/web/a.ts"], "DEFAULT")
    assert both == ["WEB", "GATE"] and "matched apps/web/**, **" in note
    docs, _ = coder_seam.select_oracle(paths, ["docs/a.md"], "DEFAULT")
    assert docs == []  # every file hit a skip entry
    no_catchall = [("apps/web/**", "WEB")]
    assert coder_seam.select_oracle(no_catchall, ["x.py", "apps/web/a"], "DEFAULT")[0] == ["WEB", "DEFAULT"]
    assert coder_seam.select_oracle(no_catchall, [], "DEFAULT")[0] == ["DEFAULT"]  # changed nothing
    assert coder_seam.select_oracle(no_catchall, None, "DEFAULT")[0] == ["DEFAULT"]  # git couldn't say


@posix_only
async def test_compose_oracle_runs_every_command_and_fails_if_any_does(tmp_path):
    cmd = coder_seam.compose_oracle(["cd sub 2>/dev/null; echo one; exit 1", "pwd; echo two"])
    (tmp_path / "sub").mkdir()
    proc = await worktree.spawn_shell(cmd, cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    text = out.decode()
    assert proc.returncode != 0
    assert "one" in text and "two" in text  # the second ran after the first failed
    assert f"{tmp_path}\n" in text  # and the first command's `cd` did not leak into it


@dataclass
class _Verdict:
    passed: bool
    total: int = 0
    failed: int = 0
    failing: list = field(default_factory=list)
    output: str = ""


def _adapter(tmp_path, **kw) -> coder_seam._WorktreeSolveAdapter:
    return coder_seam._WorktreeSolveAdapter(
        repo=str(tmp_path),
        base="main",
        root=".worktrees",
        fid="bd-7aun",
        coder=object(),
        dispatch_timeout=None,
        verdict_cls=_Verdict,
        **kw,
    )


@posix_only
async def test_two_timed_out_candidates_trip_the_breaker_with_real_processes(tmp_path):
    """bd-7aun's 15 generations, cut to two. Each candidate really runs the oracle and
    really times out. The second raises OracleTimeout, a SolveExhausted, so every existing
    handler still sees a solve failure, but one the loop can tell apart."""
    a, b = tmp_path / "g1", tmp_path / "g2"
    a.mkdir()
    b.mkdir()
    ad = _adapter(tmp_path, test_cmd="sleep 30", test_timeout=0.5)

    first = await asyncio.wait_for(ad.verify(str(a)), timeout=20)
    assert first.passed is False and first.output == "acceptance tests timed out after 0s"
    with pytest.raises(coder_seam.OracleTimeout) as caught:
        await asyncio.wait_for(ad.verify(str(b)), timeout=20)
    assert isinstance(caught.value, coder_seam.SolveExhausted)
    assert caught.value.test_cmd == "sleep 30" and "coder_solve_test_timeout_s" in str(caught.value)


@posix_only
async def test_a_single_timeout_among_real_failures_does_not_trip_it(tmp_path):
    a, b = tmp_path / "g1", tmp_path / "g2"
    a.mkdir()
    b.mkdir()
    (b / "fast").write_text("")
    ad = _adapter(tmp_path, test_cmd="test -e fast && exit 1 || sleep 30", test_timeout=0.5)
    assert (await asyncio.wait_for(ad.verify(str(a)), timeout=20)).passed is False  # timed out
    assert (await asyncio.wait_for(ad.verify(str(b)), timeout=20)).passed is False  # a plain red: no raise


@posix_only
async def test_the_adapter_runs_the_commands_the_changed_paths_pick(tmp_path, monkeypatch):
    async def _changed(tree, base=""):
        return ["apps/web/a.ts"]

    monkeypatch.setattr(worktree, "changed_paths", _changed)
    wt = tmp_path / "g1"
    wt.mkdir()
    ad = _adapter(
        tmp_path,
        test_cmd="echo SLOW-GATE; exit 1",
        test_timeout=10,
        test_paths=[("apps/web/**", "echo WEB-TESTS"), ("**", "echo SLOW-GATE; exit 1")],
    )
    verdict = await ad.verify(str(wt))
    assert verdict.passed is True and "WEB-TESTS" in verdict.output and "SLOW-GATE" not in verdict.output


async def test_an_oracle_timeout_blocks_at_once_without_climbing_or_self_healing(monkeypatch):
    """No tier climb (a stronger model times out the same way), and a class the sweep
    never clears on its own. Before, the message's "timed out" made it `transient`."""

    async def _timed_out(**kw):
        raise coder_seam.OracleTimeout(
            "acceptance oracle cannot finish: 2 candidates ran out the 300s coder_solve_test_timeout_s",
            test_cmd=kw["test_cmd"],
            timeout=300,
        )

    _solve_on(monkeypatch)
    monkeypatch.setattr(coder_seam, "dispatch", _timed_out)

    async def _open_pr(*_a, **_k):
        raise AssertionError("no PR for an unverified build")

    store = _EscalatingStore(tiers=["reasoning", "opus"])
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        store=store,
        cfg={"coder": "proto", "coders": {"smart": "a", "reasoning": "b"}, "local_gate_cmd": "python scripts/gate.py"},
    )
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][3] == ORACLE_TIMEOUT_CLASS
    assert store.escalated == []
    assert ORACLE_TIMEOUT_CLASS not in _SELF_HEALING_BLOCKS
    # the oracle was the gate fallback, so the project's later cards skip solve()
    assert "ran out the 300s" in loop._oracle_unwinnable["default"]
    assert loop._use_coder_solve(dict(FEATURE)) is False


# ── protoAgent#3692: a deleted rung delegate degrades instead of pausing ─────────────


async def test_a_rung_with_a_deleted_delegate_runs_on_its_live_sibling_then_the_base_coder(monkeypatch, caplog):
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")  # ends the drive

    cfg = {"coder": "opus", "coders": {"smart": ["fable", "sonnet"], "reasoning": ["fable"], "opus": ["opus"]}}
    loop, store = _rung_env(monkeypatch, _dispatch, cfg=cfg)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None if name == "fable" else name)
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        await loop._drive({"id": "bd-1", "title": "t", "spec": "s"})
    assert seen[0] == "sonnet"  # the live sibling at the same rung
    assert not any("not configured" in c[2] for c in store.calls if c[0] == "flag_blocked")
    assert loop._live_rung("reasoning", ["fable"], loop._coders_for({})) == ["opus"]  # whole rung gone → base coder
    assert len([r for r in caplog.records if "'fable'" in r.getMessage() and "smart" in r.getMessage()]) == 1


def test_a_rung_gone_with_no_base_coder_falls_to_the_nearest_live_rung():
    lp = BoardLoop({"coders": {"smart": "a", "reasoning": "fable", "opus": "c"}})
    lp._resolve_delegate = lambda name, expect: None if name == "fable" else name
    assert lp._live_rung("reasoning", ["fable"], lp._coders_for({})) == ["c"]  # stronger first
    lp._resolve_delegate = lambda name, expect: None if name in ("fable", "c") else name
    assert lp._live_rung("reasoning", ["fable"], lp._coders_for({})) == ["a"]  # then weaker
    lp._resolve_delegate = lambda name, expect: None
    assert lp._live_rung("reasoning", ["fable"], lp._coders_for({})) == ["fable"]  # nothing: the block names it


# ── #467 review: regressions ─────────────────────────────────────────────────────────


def _clone_with_origin(tmp_path, seed_files=()) -> Path:
    """A seed repo pushed to a bare origin, and a clone of it with a local identity."""
    origin = tmp_path / "origin.git"
    seed = _repo(tmp_path / "seed")
    for name in seed_files:
        _commit(seed, name, "def f(): pass\n")
    _git("init", "--bare", str(origin))
    _git("-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    _git("-C", str(seed), "remote", "add", "origin", str(origin))
    _git("-C", str(seed), "push", "-u", "origin", "main")
    tree = tmp_path / "clone"
    _git("clone", str(origin), str(tree))
    for k, v in (("user.email", "t@localhost"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git("-C", str(tree), "config", k, v)
    return tree


async def test_R1_a_rename_lists_its_source_so_the_source_paths_tests_run(tmp_path):
    """`git mv src/core.py docs/core.md` with `{docs/**: skip, src/**: pytest}` used to list
    only docs/core.md, so the candidate passed with no test at all."""
    tree = _clone_with_origin(tmp_path, ["src/core.py"])
    (tree / "docs").mkdir()
    _git("-C", str(tree), "mv", "src/core.py", "docs/core.md")
    _git("-C", str(tree), "commit", "-qm", "mv")
    changed = await worktree.changed_paths(str(tree), "main")
    assert sorted(changed) == ["docs/core.md", "src/core.py"]
    paths = coder_seam.parse_test_paths({"docs/**": "skip", "src/**": "pytest"})
    assert coder_seam.select_oracle(paths, changed, "gate-cmd")[0] == ["pytest"]


async def test_R2_changed_paths_with_no_base_uses_the_remote_default_branch(tmp_path):
    tree = _clone_with_origin(tmp_path)
    _commit(tree, "src/a.py")  # committed, so a HEAD-only diff would miss it
    assert await worktree.changed_paths(str(tree), "") == ["src/a.py"]


def test_R3_the_oracle_guard_is_re_evaluated_not_sticky(monkeypatch):
    """One cold first preflight used to switch solve() off for the life of the process."""
    _solve_on(monkeypatch)
    lp = BoardLoop({"local_gate_cmd": "pytest", "coder_solve_test_timeout_s": 300})
    card = {"acceptance_criteria": _AC}
    lp._record_gate_seconds("default", 400.0, False)  # cold first preflight
    assert lp._use_coder_solve(card) is False and "default" in lp._oracle_unwinnable
    lp._record_gate_seconds("default", 60.0, False)  # the next commit measured fast
    assert lp._use_coder_solve(card) is True and "default" not in lp._oracle_unwinnable


def test_R3b_a_tripped_breaker_lifts_when_the_budget_is_raised_or_the_gate_measures_under_it(monkeypatch):
    _solve_on(monkeypatch)
    card = {"acceptance_criteria": _AC}
    lp = BoardLoop({"local_gate_cmd": "pytest", "coder_solve_test_timeout_s": 300})
    lp._oracle_tripped["default"] = (300.0, 10.0)
    assert lp._use_coder_solve(card) is False
    lp._record_gate_seconds("default", 90.0, False)  # a fresh measurement, under budget
    assert lp._use_coder_solve(card) is True
    raised = BoardLoop({"local_gate_cmd": "pytest", "coder_solve_test_timeout_s": 900})
    raised._oracle_tripped["default"] = (300.0, 10.0)  # tripped under the old, smaller budget
    assert raised._use_coder_solve(card) is True


@posix_only
async def test_R4_a_timeout_never_releases_a_project_held_for_a_red_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    repo = _repo(tmp_path / "r")
    lp = _loop({"repo": str(repo), "local_gate_cmd": "echo red; exit 1", "preflight_timeout_s": 1})
    await lp._preflight("default", "echo red; exit 1", str(repo), "main")
    assert isinstance(lp._preflight_state["default"], str)
    await asyncio.wait_for(lp._preflight("default", "echo red; sleep 5; exit 1", str(repo), "main"), timeout=20)
    assert isinstance(lp._preflight_state["default"], str)  # still held
    assert "default" not in lp._preflight_sha  # not cached: the throttled re-check keeps running


@posix_only
async def test_R5_a_superseded_run_cannot_overwrite_the_new_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    repo = _repo(tmp_path / "r")
    lp = _loop({"repo": str(repo)})
    old = lp._start_preflight("default", "sleep 1; echo oldred; exit 1", str(repo), "main")
    new = lp._start_preflight("default", "true", str(repo), "main")
    await asyncio.wait_for(asyncio.gather(old, new), timeout=20)
    assert lp._preflight_state["default"] is True  # the new command's green stands


def test_R6_a_missing_top_rung_climbs_to_the_nearest_rung_not_the_base_coder():
    lp = BoardLoop({"coder": "base", "coders": {"smart": "base", "reasoning": "mid", "opus": "top"}})
    lp._resolve_delegate = lambda name, expect: None if name == "top" else name
    assert lp._live_rung("opus", ["top"], lp._coders_for({})) == ["mid"]
    lp._resolve_delegate = lambda name, expect: None if name in ("mid", "top") else name
    assert lp._live_rung("reasoning", ["mid"], lp._coders_for({})) == ["base"]  # nothing stronger live


def test_R6b_the_fallback_warning_is_logged_once(caplog):
    lp = BoardLoop({"coder": "base", "coders": {"smart": "base", "reasoning": "mid", "opus": "top"}})
    lp._resolve_delegate = lambda name, expect: None if name == "top" else name
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        for _ in range(3):
            lp._live_rung("opus", ["top"], lp._coders_for({}))
    assert len([r for r in caplog.records if "dispatching 'mid' instead" in r.getMessage()]) == 1
    assert len([r for r in caplog.records if "names delegate 'top'" in r.getMessage()]) == 1


@posix_only
async def test_concurrent_best_of_k_timeouts_count_as_one_round(tmp_path):
    """k copies of a heavy gate verified at once can all time out from contention alone.
    That is one round. The breaker needs a later, separate run to time out too."""
    wts = []
    for i in range(4):
        wt = tmp_path / f"g{i}"
        wt.mkdir()
        wts.append(str(wt))
    ad = _adapter(tmp_path, test_cmd="sleep 30", test_timeout=0.5)
    verdicts = await asyncio.wait_for(asyncio.gather(*(ad.verify(w) for w in wts[:3])), timeout=20)
    assert all(v.passed is False for v in verdicts)  # three timeouts, one round: no block
    with pytest.raises(coder_seam.OracleTimeout, match="in 2 separate rounds"):
        await asyncio.wait_for(ad.verify(wts[3]), timeout=20)


async def test_an_all_skip_pass_is_loud(tmp_path, monkeypatch, caplog):
    async def _changed(tree, base=""):
        return ["docs/a.md"]

    monkeypatch.setattr(worktree, "changed_paths", _changed)
    wt = tmp_path / "g1"
    wt.mkdir()
    ad = _adapter(tmp_path, test_cmd="exit 1", test_timeout=10, test_paths=[("docs/**", "")])
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        verdict = await ad.verify(str(wt))
    assert verdict.passed is True and verdict.total == 0 and "passed without tests" in verdict.output
    assert any("PASSED WITHOUT TESTS" in r.getMessage() for r in caplog.records)

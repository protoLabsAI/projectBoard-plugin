"""Missing oracle commands stop search and preserve the unjudged candidate (#542)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board import coder_seam, worktree
from project_board.failures import PREFLIGHT_HOLD_CLASS
from test_loop import FEATURE, FakeLoopStore, _drive_with
from project_board.loop import BoardLoop
from test_oracle_timeout_keep_tree import _FakeBudget, _FakeResult, _FakeVerdict, _fake_coder, _repo


@pytest.mark.parametrize(
    "code,output",
    [
        (127, ""),
        (9009, "'missing-tool' is not recognized as an internal or external command"),
        (1, "sh: 1: missing-tool: not found"),
        (1, "zsh: command not found: missing-tool"),
        (1, "/bin/sh: /gone/.venv/bin/python: No such file or directory"),
        (127, "bash: line 1: missing-tool: command not found"),
    ],
)
async def test_first_missing_command_stops_dispatch_and_keeps_candidate(tmp_path, monkeypatch, code, output):
    repo = _repo(tmp_path / "repo")
    generated = _fake_coder(monkeypatch)
    calls = []

    async def spawn(cmd, **kwargs):
        calls.append(cmd)

        async def communicate():
            return output.encode(), None

        return SimpleNamespace(returncode=code, communicate=communicate)

    monkeypatch.setattr(worktree, "spawn_shell", spawn)

    async def solve(task, *, generate, verify, **kwargs):
        for _ in range(6):
            candidate = await generate(task)
            await verify(candidate)
        pytest.fail("missing oracle must abort solve before another generation")

    verified = []
    with pytest.raises(coder_seam.OracleMissingCommand) as caught:
        await coder_seam.dispatch(
            task="implement",
            coder=object(),
            repo=repo,
            base="main",
            root=".worktrees",
            fid="bd-1",
            dispatch_timeout=None,
            test_cmd="missing-tool --check",
            test_timeout=10,
            budget=6,
            k=2,
            tree_depth=2,
            _solve=solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
            record_verified=lambda *args: verified.append(args),
        )
    exc = caught.value
    assert len(generated) == 1
    assert calls == ["missing-tool --check"]
    assert exc.returncode == code and exc.output == output
    assert exc.test_cmd == "missing-tool --check"
    assert "infrastructure" in str(exc)
    assert exc.kept and exc.kept[0] == generated[0]
    assert Path(exc.kept[0], "impl.py").exists()
    assert verified == []


async def test_passing_oracle_with_stale_diagnostic_publishes(tmp_path, monkeypatch):
    """The slice-1 regression an adversarial review caught: a PASSING oracle (exit 0) whose
    merged output carries a `command not found` line from an EARLIER optional step with a
    working shell fallback must NOT be classified as a missing oracle. The command ran and
    the acceptance tests passed, so dispatch promotes and publishes the candidate — it never
    raises OracleMissingCommand and never aborts solve."""
    repo = _repo(tmp_path / "repo")
    generated = _fake_coder(monkeypatch)
    verdicts = []

    async def spawn(cmd, **kwargs):
        async def communicate():
            # Exit 0, but the output still carries a stale diagnostic from an optional
            # pre-step whose shell fallback succeeded. The real test run then passed.
            return b"bash: line 1: optional-tool: command not found\nusing fallback\nall tests passed", None

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(worktree, "spawn_shell", spawn)

    async def solve(task, *, generate, verify, **kwargs):
        candidate = await generate(task)
        verdict = await verify(candidate)
        verdicts.append(verdict)
        return _FakeResult(candidate, True, "best-of-k", 1, 1, verdict)

    canon_wt, canon_branch, _text = await coder_seam.dispatch(
        task="implement",
        coder=object(),
        repo=repo,
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="run-tests",
        test_timeout=10,
        budget=6,
        k=2,
        tree_depth=2,
        _solve=solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert len(generated) == 1
    assert verdicts and verdicts[0].passed
    assert Path(canon_wt, "impl.py").exists()


async def test_ordinary_test_failure_continues_search(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    generated = _fake_coder(monkeypatch)

    async def solve(task, *, generate, verify, **kwargs):
        for _ in range(2):
            candidate = await generate(task)
            verdict = await verify(candidate)
            assert not verdict.passed
        return _FakeResult(None, False, "best-of-k", 2, 2, verdict)

    with pytest.raises(coder_seam.SolveExhausted):
        await coder_seam.dispatch(
            task="implement",
            coder=object(),
            repo=repo,
            base="main",
            root=".worktrees",
            fid="bd-1",
            dispatch_timeout=None,
            test_cmd="exit 1",
            test_timeout=10,
            budget=6,
            k=2,
            tree_depth=2,
            _solve=solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
    assert len(generated) == 2
    assert all(not Path(wt).exists() for wt in generated)


async def test_drive_blocks_infrastructure_without_escalation_or_pr(monkeypatch):
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    dispatched = []
    kept = []

    async def dispatch(**kwargs):
        dispatched.append(kwargs)
        exc = coder_seam.OracleMissingCommand(
            test_cmd="env missing-tool --check",
            returncode=127,
            output="sh: 1: missing-tool: not found",
            candidate_wt="/candidate",
        )
        exc.kept = ("/candidate", "feat/bd-1.g1")
        raise exc

    async def no_pr(*args, **kwargs):
        pytest.fail("missing oracle must never publish")

    async def keep(store, fid, repo, wt, branch, **kwargs):
        assert kwargs["missing_command"] is True
        kept.append((wt, branch))

    async def discard(*args, **kwargs):
        pytest.fail("missing oracle must preserve work")

    def seed(loop):
        assert loop.escalation_on
        loop._keep_tree_for_salvage = keep
        loop._discard_tree = discard

    monkeypatch.setattr(coder_seam, "dispatch", dispatch)
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=no_pr,
        seed=seed,
        cfg={
            "coder": "proto",
            "local_gate_cmd": "env missing-tool --check",
            "coders": {"fast": "proto-fast", "smart": "proto-smart"},
        },
        feature={**FEATURE, "acceptance_criteria": "WHEN x THE SYSTEM SHALL y"},
    )
    blocks = [call for call in store.calls if call[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][3] == PREFLIGHT_HOLD_CLASS
    reason = blocks[0][2]
    assert f"project {loop._project_name(FEATURE)!r}" in reason and "infrastructure" in reason
    assert "env missing-tool --check" in reason
    assert "missing-tool (not on PATH)" in reason
    assert "local_gate_cmd" in reason
    assert len(dispatched) == 1
    assert kept == [("/candidate", "feat/bd-1.g1")]
    assert not any(call[0] in ("escalate", "open_review") for call in store.calls)


async def test_recovery_comment_names_missing_command_not_timeout(tmp_path):
    store = FakeLoopStore()
    loop = BoardLoop({"coder": "proto"})
    await loop._keep_tree_for_salvage(store, "bd-1", str(tmp_path), str(tmp_path), "feat/bd-1.g1", missing_command=True)
    comments = [call[2] for call in store.calls if call[0] == "comment"]
    assert len(comments) == 1
    assert "preflight-hold" in comments[0] and "unverified" in comments[0]
    assert str(tmp_path) in comments[0] and "local_gate_cmd" in comments[0]
    assert "timed out" not in comments[0] and "oracle-timeout" not in comments[0]

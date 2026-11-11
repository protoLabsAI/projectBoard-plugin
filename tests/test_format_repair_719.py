"""#719: a formatting-only verify failure repairs the EXISTING candidate in place.

bd-j5qr2: generation 1 was behaviorally correct, but its gate chained
``ruff check && ruff format --check && pytest`` and failed on formatting alone, so pytest
never ran and solve() spent g2 + g3 (40+ minutes) on fresh implementations.

With ``coder_solve_format_check_cmd`` + ``coder_solve_format_fix_cmd`` configured, the
format stage runs before the oracle; a format-only failure is repaired by the trusted
formatter in the candidate's own worktree (bounded), then the FULL oracle runs — the
behavioral tests included. These tests drive the REAL ``coder_seam.dispatch`` / adapter
over REAL git worktrees and REAL subprocesses (a tiny formatter script stands in for
ruff), so the guard is exercised by real processes, not a mocked spawn.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from project_board import coder_seam
from project_board.loop import BoardLoop
from test_oracle_timeout_keep_tree import _FakeBudget, _FakeResult, _FakeVerdict

pytestmark = pytest.mark.skipif(os.name == "nt", reason="exercises real git worktrees + POSIX shells")

PY = shlex.quote(sys.executable)

# The "formatter": a file is formatted when no line carries trailing whitespace.
#   check   — exit 1 listing every unformatted *.py file
#   fix     — strip trailing whitespace from every *.py file
#   fix-bad — a formatter that ALSO rewrites a file outside the candidate's change
#   fix-noop — a formatter that "succeeds" but changes nothing (never converges)
#   fix-fail — a formatter that errors out
_FMT = """
import pathlib, sys
mode = sys.argv[1]
files = sorted(p for p in pathlib.Path(".").rglob("*.py") if ".git" not in p.parts and p.name != "fmt.py")
bad = [p for p in files if any(line != line.rstrip() for line in p.read_text().splitlines())]
if mode == "check":
    for p in bad:
        print(f"Would reformat: {p}")
    sys.exit(1 if bad else 0)
if mode == "fix-fail":
    print("formatter crashed: internal error")
    sys.exit(2)
if mode == "fix-noop":
    sys.exit(0)
for p in bad:
    p.write_text("\\n".join(line.rstrip() for line in p.read_text().splitlines()) + "\\n")
if mode == "fix-bad":
    pathlib.Path("README.md").write_text("rewritten by an overreaching formatter\\n")
    pathlib.Path("stray.py").write_text("x = 1\\n")
"""

# The behavioral test: impl.VALUE must equal 42. Writes a marker so a test can prove the
# behavioral stage actually RAN (not merely that the verdict says passed).
_TESTS = """
import pathlib, sys
sys.path.insert(0, ".")
import impl
pathlib.Path(".tests-ran").write_text("yes")
print("TESTS RAN")
sys.exit(0 if impl.VALUE == 42 else 1)
"""

CHECK = f"{PY} fmt.py check"
FIX = f"{PY} fmt.py fix"
# The gate the incident had: format check chained before the behavioral tests.
ORACLE = f"{PY} fmt.py check && {PY} run_tests.py"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", str(path))
    for k, v in (("user.email", "board-test@localhost"), ("user.name", "Board Test"), ("commit.gpgsign", "false")):
        _git("-C", str(path), "config", k, v)
    (path / "README.md").write_text("base\n")
    (path / "fmt.py").write_text(_FMT)
    (path / "run_tests.py").write_text(_TESTS)
    (path / ".gitignore").write_text(".tests-ran\n")
    _git("-C", str(path), "add", "-A")
    _git("-C", str(path), "commit", "-m", "base")
    os.makedirs(path / ".worktrees", exist_ok=True)
    return str(path)


UNFORMATTED_CORRECT = "VALUE = 42   \n\n\ndef helper():   \n    return VALUE\n"
UNFORMATTED_WRONG = "VALUE = 41   \n"
FORMATTED_CORRECT = "VALUE = 42\n\n\ndef helper():\n    return VALUE\n"


def _coder(monkeypatch, contents: list[str]) -> list[str]:
    """Each generation writes the next entry of ``contents`` as ``impl.py``."""
    seen: list[str] = []

    async def _tapped(coder, wt, prompt, *, fid=None, gen=0, tier="", timeout=None, env_passthrough=(), **_kw):
        coder_seam.progress_begin(fid, gen, tier)
        seen.append(wt)
        Path(wt, "impl.py").write_text(contents[min(len(seen), len(contents)) - 1])
        return f"## Summary\n\n- candidate {gen}"

    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", _tapped)
    return seen


async def _ladder(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
    """The real ladder's shape: greedy, then k-1 best-of-k alternatives on a failure."""
    spent = 0
    verdict = None
    for _ in range(min(budget.total, k)):
        candidate = await generate(task, feedback=None)
        spent += 1
        verdict = await verify(candidate)
        if verdict.passed:
            return _FakeResult(candidate, True, "greedy" if spent == 1 else "best-of-k", spent, spent, verdict)
    return _FakeResult(None, False, "best-of-k", spent, spent, verdict, note="no candidate passed")


async def _dispatch(repo: str, fid: str, **kw):
    gens: list[int] = []
    defaults = dict(
        task="implement",
        coder=object(),
        repo=repo,
        base="main",
        root=".worktrees",
        fid=fid,
        dispatch_timeout=None,
        test_cmd=ORACLE,
        test_timeout=60,
        budget=6,
        k=3,
        tree_depth=0,
        format_check_cmd=CHECK,
        format_fix_cmd=FIX,
        record_gens=gens.append,
        _solve=_ladder,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    defaults.update(kw)
    return await coder_seam.dispatch(**defaults), gens


def _adapter(repo: str, **kw) -> coder_seam._WorktreeSolveAdapter:
    defaults = dict(
        repo=repo,
        base="main",
        root=".worktrees",
        fid="bd-f",
        coder=object(),
        dispatch_timeout=None,
        test_cmd=ORACLE,
        test_timeout=60,
        verdict_cls=_FakeVerdict,
        format_check_cmd=CHECK,
        format_fix_cmd=FIX,
        progress_fid="bd-f",
    )
    defaults.update(kw)
    return coder_seam._WorktreeSolveAdapter(**defaults)


def _stages(fid: str) -> list[str]:
    return [(g.get("verify") or {}).get("stages", "") for g in coder_seam.progress_snapshot(fid)["gens"]]


# ── the incident: format-only defect → zero fresh candidates, full verification runs ───


async def test_format_only_defect_is_repaired_in_place_and_spends_no_generation(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    coder_seam.progress_new_run("bd-j5qr2")
    seen = _coder(monkeypatch, [UNFORMATTED_CORRECT, "VALUE = 42\n", "VALUE = 42\n"])

    (wt, _branch, text), gens = await _dispatch(repo, "bd-j5qr2")

    assert len(seen) == 1, "a format-only failure must not dispatch a fresh implementation"
    assert gens == [1]
    # The candidate's implementation is preserved (only reformatted) and was promoted.
    assert Path(wt, "impl.py").read_text() == FORMATTED_CORRECT
    assert text.endswith("candidate 1")
    # The FULL oracle ran after the repair — the behavioral stage executed.
    assert Path(wt, ".tests-ran").exists()
    stages = _stages("bd-j5qr2")[0]
    assert "format check failed" in stages and "repairing with" in stages
    assert stages.endswith("format check passed → full verification ran")


async def test_without_the_stage_configured_behavior_is_unchanged(tmp_path, monkeypatch):
    """Both commands blank (the default) ⇒ no stage: the chained oracle fails g1 on
    formatting and the ladder generates alternatives exactly as before."""
    repo = _repo(tmp_path / "repo")
    seen = _coder(monkeypatch, [UNFORMATTED_CORRECT, "VALUE = 42\n"])

    (wt, _b, _t), gens = await _dispatch(repo, "bd-off", format_check_cmd="", format_fix_cmd="")

    assert len(seen) == 2 and gens == [2]
    assert Path(wt, "impl.py").read_text() == "VALUE = 42\n"


async def test_a_clean_candidate_is_not_touched(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    coder_seam.progress_new_run("bd-clean")
    _coder(monkeypatch, ["VALUE = 42\n"])
    adapter = _adapter(repo, fid="bd-clean", progress_fid="bd-clean")
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert verdict.passed and adapter.format_repairs == 0
    assert adapter._stages_by_wt[wt] == "format check passed → full verification ran"


# ── real failures stay failures ────────────────────────────────────────────────────────


async def test_behavioral_failure_after_clean_format_is_a_real_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, ["VALUE = 41\n"])
    adapter = _adapter(repo)
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert not verdict.passed
    assert "TESTS RAN" in verdict.output
    assert adapter.format_repairs == 0


async def test_mixed_format_and_behavioral_failure_repairs_then_fails_on_the_tests(tmp_path, monkeypatch):
    """Format AND behavior wrong: the format is repaired, the full oracle runs, and the
    candidate fails on its real behavioral defect — never auto-passed."""
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, [UNFORMATTED_WRONG])
    adapter = _adapter(repo)
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert not verdict.passed
    assert "TESTS RAN" in verdict.output, "the behavioral stage must run after the format repair"
    assert adapter.format_repairs == 1
    assert Path(wt, "impl.py").read_text() == "VALUE = 41\n"  # repaired format, real defect kept


async def test_mixed_failure_in_the_ladder_generates_a_new_candidate(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    seen = _coder(monkeypatch, [UNFORMATTED_WRONG, UNFORMATTED_CORRECT])

    (wt, _b, _t), gens = await _dispatch(repo, "bd-mixed")

    assert len(seen) == 2 and gens == [2]  # the behavioral failure DOES cost a generation
    assert Path(wt, "impl.py").read_text() == FORMATTED_CORRECT


# ── a repair that cannot be trusted leaves the candidate exactly as written ─────────────


async def test_nonconverging_repair_is_bounded_and_restores_the_candidate(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, [UNFORMATTED_CORRECT])
    adapter = _adapter(repo, format_fix_cmd=f"{PY} fmt.py fix-noop", format_repair_attempts=2)
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert not verdict.passed
    assert adapter.format_repairs == 2  # bounded by format_repair_attempts
    assert "not converging" in verdict.output
    assert "behavioral tests: not run" in verdict.output
    assert not Path(wt, ".tests-ran").exists(), "tests must not be reported or run as passed"
    assert Path(wt, "impl.py").read_text() == UNFORMATTED_CORRECT
    assert adapter._stages_by_wt[wt].endswith("behavioral tests not run")


async def test_formatter_that_errors_fails_the_stage_and_restores(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, [UNFORMATTED_CORRECT])
    adapter = _adapter(repo, format_fix_cmd=f"{PY} fmt.py fix-fail")
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert not verdict.passed
    assert "exited 2" in verdict.output and "formatter crashed" in verdict.output
    assert Path(wt, "impl.py").read_text() == UNFORMATTED_CORRECT


async def test_formatter_touching_files_outside_the_change_is_reverted(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, [UNFORMATTED_CORRECT])
    adapter = _adapter(repo, format_fix_cmd=f"{PY} fmt.py fix-bad")
    wt = await adapter.generate("implement")

    verdict = await adapter.verify(wt)

    assert not verdict.passed
    assert "outside the candidate's change" in verdict.output
    assert "README.md" in verdict.output and "stray.py" in verdict.output
    # Everything put back: the base file, no stray file, the candidate's own bytes.
    assert Path(wt, "README.md").read_text() == "base\n"
    assert not Path(wt, "stray.py").exists()
    assert Path(wt, "impl.py").read_text() == UNFORMATTED_CORRECT
    assert not Path(wt, ".tests-ran").exists()


# ── missing tools are infrastructure faults: keep the candidate, spend nothing ──────────


@pytest.mark.parametrize("which", ["check", "fix"])
async def test_missing_format_tool_stops_solve_and_keeps_the_candidate(tmp_path, monkeypatch, which):
    repo = _repo(tmp_path / "repo")
    seen = _coder(monkeypatch, [UNFORMATTED_CORRECT, "VALUE = 42\n"])
    missing = "definitely-not-a-formatter-719 --check"
    kw = {"format_check_cmd": missing} if which == "check" else {"format_fix_cmd": missing}

    with pytest.raises(coder_seam.OracleMissingCommand) as caught:
        await _dispatch(repo, f"bd-miss-{which}", **kw)

    exc = caught.value
    assert exc.test_cmd == missing
    assert len(seen) == 1, "a missing tool must not spend another generation"
    assert exc.kept and exc.kept[0] == seen[0]
    assert Path(exc.kept[0], "impl.py").read_text() == UNFORMATTED_CORRECT


# ── restart: a re-verify of an already-repaired candidate is idempotent ────────────────


async def test_reverifying_a_repaired_candidate_after_restart_needs_no_repair(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _coder(monkeypatch, [UNFORMATTED_CORRECT])
    first = _adapter(repo)
    wt = await first.generate("implement")
    assert (await first.verify(wt)).passed and first.format_repairs == 1

    # A fresh adapter (the process restarted) judges the same tree: the repair already
    # landed in the candidate's own files, so the check passes and nothing re-runs.
    second = _adapter(repo)
    second._gen_by_wt[wt] = 1
    verdict = await second.verify(wt)

    assert verdict.passed and second.format_repairs == 0
    assert Path(wt, "impl.py").read_text() == FORMATTED_CORRECT


# ── config ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,want", [(None, 1), ("x", 1), (0, 0), (2, 2), (99, 3), (-4, 0)])
def test_repair_attempts_are_bounded(raw, want):
    assert coder_seam.clamp_format_repair_attempts(raw) == want


def test_format_stage_settings_resolve_per_project_as_a_pair():
    lp = BoardLoop(
        {
            "coder_solve_format_check_cmd": "ruff format --check .",
            "coder_solve_format_fix_cmd": "ruff format .",
            "projects": {
                "a": {"repo": "/tmp/a"},
                "b": {"repo": "/tmp/b", "coder_solve_format_check_cmd": "npx prettier --check ."},
                "c": {"repo": "/tmp/c", "coder_solve_format_repair_attempts": 7},
            },
        }
    )
    a = lp._coder_solve_settings({"project": "a"})
    b = lp._coder_solve_settings({"project": "b"})
    c = lp._coder_solve_settings({"project": "c"})
    assert (a["format_check_cmd"], a["format_fix_cmd"], a["format_repair_attempts"]) == (
        "ruff format --check .",
        "ruff format .",
        1,
    )
    # A project naming one command owns the pair: no other repo's formatter leaks in.
    assert (b["format_check_cmd"], b["format_fix_cmd"]) == ("npx prettier --check .", "")
    assert c["format_repair_attempts"] == 3


def test_format_stage_is_off_by_default():
    s = BoardLoop({})._coder_solve_settings({})
    assert s["format_check_cmd"] == "" and s["format_fix_cmd"] == ""
    assert not _adapter("/nowhere", format_check_cmd="", format_fix_cmd="").format_stage_enabled
    assert not _adapter("/nowhere", format_repair_attempts=0).format_stage_enabled


def test_a_long_check_listing_keeps_the_not_run_signal_in_every_tail(tmp_path):
    """#722 review: the summary is the LAST thing in the output, so the 1500-char tails the
    live monitor and sibling feedback keep still say the behavioral tests did not run."""
    adapter = _adapter(_repo(tmp_path / "repo"))
    verdict = adapter._format_failed(str(tmp_path), "format check failed", "not converging", "x.py\n" * 5000)
    for tail in (verdict.output, verdict.output[-1500:]):
        assert "behavioral tests: not run" in tail and "not converging" in tail


def test_progress_stages_before_a_verdict_read_as_checking_not_failed():
    """#722 review: while a repair runs the gen's verify dict holds only `stages`. The drawer
    must show it as in progress and render the stage trail, not a red "failed" row."""
    js = (Path(__file__).resolve().parent.parent / "view" / "board.js").read_text(encoding="utf-8")
    assert 'typeof g.verify.passed === "boolean"' in js
    assert '"checking"' in js and "g.verify.stages" in js

"""A preflight hold says WHICH step of the gate failed and WHAT it printed.

designSystem (2026-10-11): cards ds-n8d, ds-tqo and ds-rf6 sat ``blocked:preflight-hold``
reading only "gate preflight failed — the coder environment can't run the gate: Exit status 1".
The project's gate is ``pnpm install --frozen-lockfile && pnpm -r typecheck && pnpm -r test``.
The hold stamped the LAST line of the output, which for a pnpm chain is its sign-off, and the
full tail went only to the daemon log. The agent has no shell, so only an operator could
find out which of the three steps broke, or why.

Now:

- the preflight marks each step of a plain ``&&`` chain (the chain and its exit status are
  unchanged), so the failure names its step;
- the card's ``blocked_reason`` carries that step and the most telling output line, not the
  sign-off;
- each held card gets ONE comment per distinct failure with the step, the exit status and
  the output's last lines, secrets redacted;
- ``/status`` (``preflight.failures``), ``board_get_feature`` (``preflight_failure``) and
  ``board_dispatch`` (``held["blocked:preflight-hold"].preflight_failures``) serve the same
  detail on demand.

The end-to-end cases run a REAL shell against a REAL ``br`` board, like
test_preflight_missing_command_3585.py: a mocked spawn would only prove the test's own
idea of what ``sh`` prints.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

import project_board as pb
import project_board.loop as loop_mod
from project_board import health, work_snapshot, worktree
from project_board import store as store_mod
from project_board.failures import PREFLIGHT_HOLD_CLASS
from project_board.loop import BoardLoop
from project_board.loop.preflight import (
    PREFLIGHT_TAIL_LINES,
    _and_chain_steps,
    _failing_step,
    _informative_line,
    _mark_steps,
    _output_tail,
    _preflight_hold_comment,
    _redact_gate_output,
)
from project_board.store import BeadsBoard

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises a POSIX /bin/sh")

_AC = "- WHEN x THE SYSTEM SHALL y"
_DS_GATE = "pnpm install --frozen-lockfile && pnpm -r typecheck && pnpm -r test"
# What `pnpm -r typecheck` prints when one package's typecheck fails: the error, pnpm's
# own failure line naming the package and script, then the sign-off the hold used to stamp.
_PNPM_RECURSIVE_FAIL = """\
Scope: 4 of 5 workspace projects
packages/core typecheck$ tsc --noEmit
packages/core typecheck: src/tokens.ts(12,7): error TS2322: Type 'string' is not assignable to type 'number'.
packages/core typecheck: Failed
/repo/packages/core:
 ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @ds/core@0.4.1 typecheck: `tsc --noEmit`
Exit status 1"""


# ── the steps of a gate ───────────────────────────────────────────────────────────────


@posix_only
@pytest.mark.parametrize(
    ("cmd", "steps"),
    [
        (_DS_GATE, ["pnpm install --frozen-lockfile", "pnpm -r typecheck", "pnpm -r test"]),
        ("ruff check . && pytest -q 2>&1", ["ruff check .", "pytest -q 2>&1"]),
        ("echo 'a && b' && make test", ["echo 'a && b'", "make test"]),
        ("CI=1 pnpm test|tee out.log && true", ["CI=1 pnpm test|tee out.log", "true"]),
    ],
)
def test_a_plain_and_chain_splits_into_its_steps(cmd, steps):
    assert _and_chain_steps(cmd) == steps


@posix_only
@pytest.mark.parametrize(
    "cmd",
    [
        "pnpm -r test",  # one step: nothing to mark
        "a || b",
        "a && b || c",
        "a; b",
        "(cd x && make) && make test",
        "{ a; } && b",
        "a && $(b)",
        "a && `b`",
        "a & b",
        "a && b\nc",
        "cat <<EOF && b",
        "echo 'unterminated && b",
        "a && && b",
    ],
)
def test_anything_but_a_plain_and_chain_runs_unmarked(cmd):
    assert _and_chain_steps(cmd) == []
    assert _mark_steps(cmd) == (cmd, [])


@posix_only
def test_the_marked_chain_reports_the_step_it_stopped_in():
    run, steps = _mark_steps(_DS_GATE)
    assert run.count("printf") == 3 and run.endswith("&& pnpm -r test")
    out = "[pb-preflight] step 1/3: pnpm install --frozen-lockfile\nLockfile is up to date\n"
    out += "[pb-preflight] step 2/3: pnpm -r typecheck\n" + _PNPM_RECURSIVE_FAIL
    assert _failing_step(out, steps) == (2, 3, "pnpm -r typecheck")
    assert _failing_step(out, []) is None  # an unmarked run names no step


# ── the line that says what broke ─────────────────────────────────────────────────────


def test_the_hold_line_is_the_pnpm_failure_not_its_exit_status():
    assert _informative_line(_PNPM_RECURSIVE_FAIL).startswith("ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @ds/core@0.4.1")


@pytest.mark.parametrize(
    ("output", "want"),
    [
        (
            ' ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with "frozen-lockfile" because pnpm-lock.yaml is '
            "not up to date with packages/core/package.json\n\nExit status 1",
            "ERR_PNPM_OUTDATED_LOCKFILE",
        ),
        (
            "Error: Cannot find module '/repo/node_modules/typescript/bin/tsc'\n  code: 'MODULE_NOT_FOUND'\n"
            " ELIFECYCLE  Command failed with exit code 1.",
            "Error: Cannot find module '/repo/node_modules/typescript/bin/tsc'",
        ),
        ("FAILED tests/test_x.py::test_y - assert 1 == 2\n1 failed in 0.2s", "FAILED tests/test_x.py"),
        ("sh: 1: tsc: not found\ngate command not found: tsc (not on PATH) — fix …", "gate command not found: tsc"),
        ("building…\nall good until here", "all good until here"),
        ("Exit status 1", "Exit status 1"),  # nothing better to say: the line itself
    ],
)
def test_the_hold_line_picks_the_most_telling_line(output, want):
    assert _informative_line(output).startswith(want)


# ── what the comment shows, and what it never shows ───────────────────────────────────


# Built by concatenation so the source never holds a literal token shape: a reviewer's
# diff redactor (protoreview) rewrites such literals, closing quote included, and then
# reads the test as a syntax error. The runtime values still exercise the redactor.
_FAKE = "abcdefghijklmnopqrstuvwxyz0123456789"
_NPM = "npm" + "_" + _FAKE
_GHP = "gh" + "p_" + _FAKE + "AB"
_SKK = "s" + "k-" + _FAKE.upper()
_JWT = "ey" + "J" + "hbGciOiJIUzI1NiJ9" + ".payload" + ".signature"
_SECRET_LINES = [
    "NPM_TOKEN=" + _NPM,
    "//registry.npmjs.org/:_auth" + "Token=s3cr3tvalue",
    "fetching https://bot:" + "hunter2pass" + "@git.example.com/repo.git",
    "token " + _GHP + " in env",
    "Authorization: " + "Bearer " + _JWT,
    "OPENAI key " + _SKK,
]


@pytest.mark.parametrize("secret_line", _SECRET_LINES)
def test_gate_output_is_redacted(secret_line):
    out = _redact_gate_output("step\n" + secret_line + "\nExit status 1")
    assert "[REDACTED]" in out
    for secret in (_NPM[:12], "s3cr3tvalue", "hunter2pass", _GHP[:10], _JWT[:8], _SKK[:9]):
        assert secret not in out


def test_the_tail_is_bounded():
    text = "\n".join(f"line {i} " + "x" * 400 for i in range(200))
    tail = _output_tail(text).splitlines()
    assert len(tail) <= PREFLIGHT_TAIL_LINES
    assert tail[-1].startswith("line 199 ") and all(len(row) <= 300 for row in tail)
    assert sum(len(row) + 1 for row in tail) <= 4000


def test_the_comment_is_not_a_block_reason():
    """Boot recovery (#186) reads the LAST `blocked:` comment as the card's current reason;
    the diagnostics comment must never be mistaken for one."""
    body = _preflight_hold_comment(
        "ds",
        {"step": "pnpm -r test", "step_index": 3, "steps": 3, "exit_code": 1, "tail": "boom", "cmd": _DS_GATE},
    )
    assert not body.lower().startswith("blocked:")
    assert "step 3/3 `pnpm -r test` failed (exit 1)" in body and "boom" in body


# ── the hold, its comment and the dedupe, without `br` ────────────────────────────────


class _Store:
    def __init__(self, ready):
        self.ready = [{"id": fid, "board_state": "ready", "blocked": False} for fid in ready]
        self.calls = []

    def list_features(self, state=None):
        return [dict(f) for f in self.ready] if state == "ready" else []

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", fid, reason))
        self.ready = [f for f in self.ready if f["id"] != fid]

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))

    def comments(self):
        return [c for c in self.calls if c[0] == "comment"]


class _Proc:
    def __init__(self, rc, out):
        self.returncode, self._out, self.pid = rc, out, 0

    async def communicate(self):
        return self._out, b""


@pytest.fixture
def fake(monkeypatch):
    async def _clean(*_a, **_k):
        return ""

    async def _sha(*_a, **_k):
        return ""

    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    monkeypatch.setattr(worktree, "checkout_head_sha", _sha)
    outputs = []

    async def _shell(cmd, **_k):
        rc, out = outputs.pop(0)
        return _Proc(rc, out)

    monkeypatch.setattr(worktree, "spawn_shell", _shell)
    return outputs


async def test_one_comment_per_distinct_failure(fake, monkeypatch):
    store = _Store(["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp = BoardLoop({"local_gate_cmd": "pnpm -r typecheck"})

    fake.append((1, _PNPM_RECURSIVE_FAIL.encode()))
    await lp._preflight("default", "pnpm -r typecheck", "/repo")
    lp._hold_ready_for_preflight()
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert {c[1] for c in blocked} == {"bd-1", "bd-2"}
    assert all("ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL" in c[2] and not c[2].endswith("Exit status 1") for c in blocked)
    assert {c[1] for c in store.comments()} == {"bd-1", "bd-2"}
    assert "packages/core typecheck: Failed" in store.comments()[0][2]

    # The same failure on the next re-check (timings differ): no new comment.
    fake.append((1, b"Scope: 4 of 5 (took 3.1s)\n" + _PNPM_RECURSIVE_FAIL.encode()))
    await lp._preflight("default", "pnpm -r typecheck", "/repo")
    lp._hold_ready_for_preflight()
    assert len(store.comments()) == 2

    # A DIFFERENT failure: the held cards get the new output.
    fake.append((1, b"Error: Cannot find module 'left-pad'\nExit status 1"))
    await lp._preflight("default", "pnpm -r typecheck", "/repo")
    lp._hold_ready_for_preflight()
    newest = store.comments()[2:]
    assert {c[1] for c in newest} == {"bd-1", "bd-2"} and "left-pad" in newest[0][2]

    # Recovery releases the holds and forgets the comments, so a later failure is told again.
    fake.append((0, b""))
    await lp._preflight("default", "pnpm -r typecheck", "/repo")
    assert lp._preflight_commented == {} and "default" not in lp._preflight_detail


async def test_a_failed_comment_is_retried_and_never_breaks_the_hold(fake, monkeypatch):
    store = _Store(["bd-1"])
    boom = {"n": 1}

    def _comment(fid, text):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("br stalled")
        store.calls.append(("comment", fid, text))

    store.comment = _comment
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp = BoardLoop({"local_gate_cmd": "make test"})
    fake.append((2, b"make: *** [test] Error 2"))
    await lp._preflight("default", "make test", "/repo")
    lp._hold_ready_for_preflight()
    assert [c[1] for c in store.calls if c[0] == "flag_blocked"] == ["bd-1"]  # held all the same
    assert store.comments() == []
    lp._hold_ready_for_preflight()  # next tick
    assert [c[1] for c in store.comments()] == ["bd-1"]


async def test_status_serves_the_failure_redacted(fake, monkeypatch):
    store = _Store(["bd-1"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp = BoardLoop({"local_gate_cmd": "pnpm -r test", "preflight": True})
    fake.append((1, ("GH_TOKEN=" + _GHP + "\n" + _PNPM_RECURSIVE_FAIL).encode()))
    await lp._preflight("default", "pnpm -r test", "/repo")
    lp._publish_preflight_health()

    failure = health.preflight_snapshot()["failures"]["default"]
    assert failure["exit_code"] == 1 and failure["headline"].startswith("ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL")
    assert _GHP[:10] not in failure["tail"] and _GHP[:10] not in health.preflight_snapshot()["held"]["default"]
    assert "key" not in failure  # the dedupe key is internal

    fake.append((0, b""))
    await lp._preflight("default", "pnpm -r test", "/repo")
    lp._publish_preflight_health()
    assert health.preflight_snapshot()["failures"] == {}  # a passing project shows nothing


# ── end to end: a real shell, a real board ────────────────────────────────────────────


@pytest.fixture
def board(tmp_path, monkeypatch):
    b = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: b)
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: b)

    async def _clean(*_a, **_k):
        return ""

    # The dirt probe shells git; tmp_path is no repo. A clean base is the case under test.
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)
    work_snapshot.reset()
    yield b
    work_snapshot.reset()


def _ready(board: BeadsBoard, repo, title: str, path: str) -> str:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    board.mark_ready(fid)
    return fid


def _fake_pnpm(tmp_path):
    """A `pnpm` on PATH: `install` succeeds, `-r typecheck` fails the way pnpm does, and
    `-r test` leaves a file behind so the test can tell whether it ran."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pnpm"
    fail = _PNPM_RECURSIVE_FAIL.replace("'", "'\\''")
    script.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  'install --frozen-lockfile') echo 'Lockfile is up to date, resolution step is skipped'; exit 0 ;;\n"
        f"  '-r typecheck') printf '%s\\n' '{fail}'; exit 1 ;;\n"
        f"  '-r test') touch '{tmp_path}/tests-ran'; exit 0 ;;\n"
        "esac\n"
        "exit 3\n"
    )
    script.chmod(0o755)
    return bindir


@posix_only
@requires_br
async def test_the_designsystem_gate_names_its_failing_step_on_the_card(board, tmp_path, monkeypatch):
    bindir = _fake_pnpm(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    fid = _ready(board, tmp_path, "any card", "a.py")
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": _DS_GATE})

    await lp._maybe_preflight()
    lp._hold_ready_for_preflight()

    # The gate means what it meant: step 2 failed, so step 3 never ran, and the project is held.
    assert not (tmp_path / "tests-ran").exists()
    reason = lp._preflight_state["default"]
    assert isinstance(reason, str) and "[pb-preflight]" not in reason

    f = board.get_feature(fid)
    assert f["blocked"] and f["blocked_class"] == PREFLIGHT_HOLD_CLASS
    assert f["blocked_reason"].startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX)
    assert "step 2/3 `pnpm -r typecheck` failed: ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL" in f["blocked_reason"]
    assert not f["blocked_reason"].rstrip().endswith("Exit status 1")

    comments = [c for c in board.feature_comments(fid) if c.startswith("gate preflight output")]
    assert len(comments) == 1
    assert "step 2/3 `pnpm -r typecheck` failed (exit 1)" in comments[0]
    assert "error TS2322" in comments[0] and "Exit status 1" in comments[0]

    # A re-check with the same failure adds nothing to the card.
    lp._last_preflight["default"] = -10_000.0
    await lp._maybe_preflight()
    lp._hold_ready_for_preflight()
    assert len([c for c in board.feature_comments(fid) if c.startswith("gate preflight output")]) == 1

    # The agent can fetch the same detail on demand.
    get = {t.name: t for t in pb._board_tools({})}["board_get_feature"]
    out = json.loads(get.invoke({"feature_id": fid}))
    assert out["blocked_class"] == PREFLIGHT_HOLD_CLASS
    assert out["preflight_failure"]["step"] == "pnpm -r typecheck"
    assert "error TS2322" in out["preflight_failure"]["tail"]

    record = {"outcome": "empty-queue", "detail": "", "dispatched": []}
    await lp._explain_held(record)
    group = record["held"][f"blocked:{PREFLIGHT_HOLD_CLASS}"]
    assert group["ids"] == [fid]
    assert group["preflight_failures"]["default"]["step_index"] == 2


@posix_only
@requires_br
async def test_the_marked_chain_keeps_the_gates_verdict(board, tmp_path):
    """A green chain still passes, and a chain whose FIRST step fails says so."""
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": "true && echo fine"})
    await lp._preflight("default", "true && echo fine", board.repo)
    assert lp._preflight_state["default"] is True

    await lp._preflight("default", "false && echo never", board.repo)
    reason = lp._preflight_state["default"]
    assert reason.startswith("failing step 1/2: false")
    assert "never" not in reason
    assert lp._preflight_detail["default"]["exit_code"] == 1


def test_reviewer_name_is_coerced_and_stripped_like_coder():
    assert BoardLoop({"reviewer": "  vera \n"}).reviewer_name == "vera"
    assert BoardLoop({}).reviewer_name == "quinn"
    assert BoardLoop({"reviewer": ""}).reviewer_name == ""  # blank stays blank: no review dispatch
    assert BoardLoop({"reviewer": None}).reviewer_name == ""


def test_status_resyncs_the_gap_reporter_and_serves_the_preflight_failure(monkeypatch):
    """GET /status re-reports the setup preflight through the shared ``gap_reporter`` (the
    host warning can clear on a board whose loop is off), and in the same response serves
    the held project's gate-preflight failure detail."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board import api

    class _Reporter:
        def __init__(self):
            self.reports = []

        def report(self, setup):
            self.reports.append(setup)

    class _EmptyStore:
        def list_features(self, **_kw):
            return []

    detail = {"cmd": _DS_GATE, "step": "pnpm -r typecheck", "step_index": 2, "steps": 3, "exit_code": 1}
    detail.update(headline="ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL", tail=_PNPM_RECURSIVE_FAIL, key="k")
    health.publish_preflight({"ds": "failing step 2/3: pnpm -r typecheck\n…"}, {}, failures={"ds": detail})
    rep = _Reporter()
    monkeypatch.setattr(api, "get_store", lambda **_kw: _EmptyStore())
    app = FastAPI()
    app.include_router(api.build_data_router({}, gap_reporter=rep), prefix="/api/plugins/project_board")
    try:
        body = TestClient(app).get("/api/plugins/project_board/status").json()
    finally:
        health.publish_preflight({}, {})
    assert len(rep.reports) == 1 and rep.reports[0] == body["setup"]
    assert body["held_projects"] == ["ds"]
    failure = body["preflight"]["failures"]["ds"]
    assert failure["step"] == "pnpm -r typecheck" and failure["exit_code"] == 1 and "key" not in failure


# ── review of #523: a stable failure key, `[[ ]]`, restarts, redaction ─────────────────

_NPM8_FAIL = """\
> ds-site@1.0.0 build /repo
> webpack --mode production

ERROR in ./src/index.js
Module not found: Error: Can't resolve './missing' in '/repo/src'
npm ERR! code ELIFECYCLE
npm ERR! errno 2
npm ERR! ds-site@1.0.0 build: `webpack --mode production`
npm ERR! Exit status 2
npm ERR!
npm ERR! Failed at the ds-site@1.0.0 build script.
npm ERR! This is probably not a problem with npm. There is likely additional logging output above.

npm ERR! A complete log of this run can be found in:
npm ERR!     /root/.npm/_logs/{stamp}-debug.log"""

_NPM10_FAIL = """\
ERROR in ./src/index.js {took}
Module not found: Error: Can't resolve './missing' in '/repo/src'
npm error Lifecycle script `build` failed with error:
npm error code 2
npm error path /repo
npm error workspace ds-site@1.0.0
npm error location /repo
npm error command failed
npm error command sh -c webpack --mode production
npm error A complete log of this run can be found in: /root/.npm/_logs/{stamp}-debug-0.log"""

_JEST_FAIL = """\
FAIL src/tokens.test.ts ({took} s)
  ● tokens › resolves the brand colour
    expect(received).toBe(expected)
Tests: 1 failed, 41 passed, 42 total
Time: {took} s"""

_PYTEST_FAIL = """\
FAILED tests/test_x.py::test_y - assert <Foo object at {addr}> == 3
1 failed, 9 passed in {took}s"""


def _stamp(i: int) -> dict:
    return {
        "stamp": f"2026-10-11T18_{i:02d}_33_{100 + i}Z",
        "took": f"{3 + i}.{i}",
        "addr": f"0x7f{i:02x}a3b9c0d0",
    }


@pytest.mark.parametrize(
    ("template", "headline"),
    [
        (_NPM8_FAIL, "npm ERR! Failed at the ds-site@1.0.0 build script."),
        (_NPM10_FAIL, "npm error Lifecycle script `build` failed with error:"),
        (_JEST_FAIL, "FAIL src/tokens.test.ts"),
        (_PYTEST_FAIL, "FAILED tests/test_x.py::test_y"),
    ],
)
async def test_an_unchanged_failure_comments_once_per_card_across_many_rechecks(fake, monkeypatch, template, headline):
    """The review measured 30 comments in 10 re-checks with 3 cards: npm's timestamped
    log path won the headline, so the key changed every minute. Ten re-checks of one
    failure, with timestamps, durations and addresses varying, now give ONE comment per
    card, and the headline names the failure, never the log file."""
    store = _Store(["bd-1", "bd-2", "bd-3"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp = BoardLoop({"local_gate_cmd": "npm run build"})
    keys = set()
    for i in range(10):
        fake.append((1, template.format(**_stamp(i)).encode()))
        await lp._preflight("default", "npm run build", "/repo")
        lp._hold_ready_for_preflight()
        keys.add(lp._preflight_detail["default"]["key"])
        assert lp._preflight_detail["default"]["headline"].startswith(headline)
        assert "_logs/" not in lp._preflight_detail["default"]["headline"]
    assert len(keys) == 1
    assert sorted(c[1] for c in store.comments()) == ["bd-1", "bd-2", "bd-3"]
    blocked = [c[2] for c in store.calls if c[0] == "flag_blocked"]
    assert all("_logs/" not in r and "A complete log" not in r for r in blocked)


def test_npm_boilerplate_never_wins_the_headline():
    out = _NPM8_FAIL.format(**_stamp(1)).replace("npm ERR! Failed at the ds-site@1.0.0 build script.\n", "")
    assert _informative_line(out).startswith("npm ERR! ds-site@1.0.0 build:")


@posix_only
def test_a_double_bracket_test_is_never_split():
    cmd = "[[ -d . && -f /etc/hosts ]] && echo ok"
    assert _and_chain_steps(cmd) == []
    assert _mark_steps(cmd) == (cmd, [])


@posix_only
@requires_br
async def test_a_double_bracket_gate_still_passes(board):
    """Split inside `[[ ]]`, the marked command exited 2 on bash's /bin/sh and held a
    healthy project. Unmarked, it runs exactly as written. (`/bin/sh` without `[[` support
    fails the original too, so the verdict is compared against the original.)"""
    import subprocess

    cmd = "[[ -d . && -f /etc/hosts ]] && echo ok"
    want = subprocess.run(["/bin/sh", "-c", cmd], cwd=board.repo, capture_output=True).returncode
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": cmd})
    await lp._preflight("default", cmd, board.repo)
    assert (lp._preflight_state["default"] is True) == (want == 0)


async def test_a_restart_does_not_repeat_the_comment(fake, monkeypatch):
    """Boot recovery releases the holds and the first red re-check re-holds them; the
    comment state was in memory, so every restart re-posted the same output. The key is
    now the comment's last line, and a fresh loop reads it back before posting."""
    store = _Store(["bd-1"])
    store.feature_comments = lambda fid: [c[2] for c in store.calls if c[0] == "comment" and c[1] == fid]
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    first = BoardLoop({"local_gate_cmd": "pnpm -r typecheck"})
    fake.append((1, _PNPM_RECURSIVE_FAIL.encode()))
    await first._preflight("default", "pnpm -r typecheck", "/repo")
    first._hold_ready_for_preflight()
    assert len(store.comments()) == 1
    assert store.comments()[0][2].rstrip().endswith(first._preflight_detail["default"]["key"])

    store.ready = [{"id": "bd-1", "board_state": "ready", "blocked": False}]  # boot recovery released it
    second = BoardLoop({"local_gate_cmd": "pnpm -r typecheck"})
    fake.append((1, b"Scope: 4 of 5 (took 9.9s)\n" + _PNPM_RECURSIVE_FAIL.encode()))
    await second._preflight("default", "pnpm -r typecheck", "/repo")
    second._hold_ready_for_preflight()
    assert len([c for c in store.calls if c[0] == "flag_blocked"]) == 2  # re-held, as before
    assert len(store.comments()) == 1  # but the output is not posted again

    # A different failure after the restart is posted.
    fake.append((1, b"Error: Cannot find module 'left-pad'\nExit status 1"))
    await second._preflight("default", "pnpm -r typecheck", "/repo")
    second._hold_ready_for_preflight()
    assert len(store.comments()) == 2


_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "IBAAKCAQEA\nabcdef\n-----END " + "RSA PRIVATE KEY-----"
_MORE_SECRETS = [
    ('{"pass' + 'word": "hunter2pass"}', "hunter2pass"),
    ('"author' + 'ization": "Bea' + 'rer abcdefghijkl"', "abcdefghijkl"),
    ("mysql --pass" + "word hunter2pass -h db", "hunter2pass"),
    ("token " + "ey" + "JhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxIn0" + ".c2lnbmF0dXJl" + " in header", "c2lnbmF0dXJl"),
    ("key:\n" + _PEM + "\nnext line", "MIIEow"),
    ("stripe " + "sk" + "_live_" + "abcdefghijklmnop", "abcdefghijklmnop"),
    ("gitlab " + "gl" + "pat-" + "abcdefghijklmnopqrstu", "abcdefghijklmnopqrstu"),
    ("postgres://user:" + "p@ss" + "@db.example.com/app", "p@ss"),
    ("SECRET_" + 'KEY="a b c"', "a b c"),
]


@pytest.mark.parametrize(("line", "secret"), _MORE_SECRETS)
def test_more_secret_shapes_are_redacted(line, secret):
    out = _redact_gate_output("step\n" + line + "\nExit status 1")
    assert secret not in out and "[REDACTED" in out
    assert "next line" in out or "next line" not in line  # a PEM block is cut, not the rest


@pytest.mark.parametrize(
    "prose",
    [
        "TokenExpiredError: jwt expired",
        "TokenizerError: unexpected character",
        "Missing env var GITHUB_TOKEN: set it in .env",
        "assert token == 'abc'",
    ],
)
def test_prose_that_mentions_a_token_is_left_readable(prose):
    assert _redact_gate_output(prose) == prose

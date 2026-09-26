"""protoLabsAI/protoAgent#3585: a gate whose command does not exist held every card `terminal`.

The protoAgent project's ``local_gate_cmd`` pinned an absolute interpreter,
``<checkout>/.venv/bin/python scripts/gate.py``. With that venv gone the preflight's shell
answered ``/bin/sh: …/.venv/bin/python: No such file or directory`` (exit 127), and the hold
stamped every ready card ``blocked-class:terminal``, "needs a human, never clears". Neither
half was true: the card was fine (the project's gate command was not), and the loop lifts a
preflight hold by itself the moment the gate runs again.

Now the hold carries its own ``preflight-hold`` class, and a gate that exits "not found"
says which command is missing and what to fix. The guard is exercised with a REAL shell
and a REAL `br` board: a mocked spawn would only prove the test's own idea of what ``sh``
prints.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

import project_board.loop as loop_mod
from project_board import work_snapshot, worktree
from project_board import store as store_mod
from project_board.failures import PREFLIGHT_HOLD_CLASS
from project_board.loop import BoardLoop
from project_board.loop import drive as drive_mod
from project_board.loop._common import _missing_gate_command_hint
from project_board.store import BeadsBoard

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises a POSIX /bin/sh")

_AC = "- WHEN x THE SYSTEM SHALL y"


async def _clean(*_a, **_k):
    return ""


@pytest.fixture
def board(tmp_path, monkeypatch):
    b = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: b)
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: b)
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


# ── the hint: which command, and what to fix ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "missing"),
    [
        ("/bin/sh: /x/.venv/bin/python: No such file or directory", "/x/.venv/bin/python"),
        ("/bin/sh: line 1: /x/.venv/bin/python: No such file or directory", "/x/.venv/bin/python"),
        ("sh: 1: tsc: not found", "tsc"),
        ("bash: pnpm: command not found", "pnpm"),
        ("zsh: command not found: pnpm", "pnpm"),
    ],
)
def test_the_hint_names_the_command_the_shell_could_not_find(output, missing):
    hint = _missing_gate_command_hint("protoAgent", "irrelevant", output)
    assert hint.startswith(f"gate command not found: {missing} ")
    assert "fix project 'protoAgent''s local_gate_cmd" in hint


def test_the_hint_falls_back_to_the_commands_own_first_word():
    hint = _missing_gate_command_hint("p", "/gone/.venv/bin/python scripts/gate.py", "")
    assert "/gone/.venv/bin/python (absolute path, now missing)" in hint
    assert "(not on PATH)" in _missing_gate_command_hint("p", "nosuchtool --flag", "")


def test_the_hint_fits_the_line_the_hold_stamps_on_the_card():
    """The hold stamps the reason's LAST line, cut at 200 characters."""
    hint = _missing_gate_command_hint(
        "p" * 128, "", "/bin/sh: /" + "d/" * 200 + ".venv/bin/python: No such file or directory"
    )
    assert len(hint) <= 200 and "/.venv/bin/python (absolute path" in hint


# ── end to end: a real shell, a real board ────────────────────────────────────────────


@posix_only
@requires_br
async def test_a_gate_on_a_missing_interpreter_holds_as_preflight_hold_and_names_it(board, tmp_path):
    fid = _ready(board, tmp_path, "any card", "a.py")
    python = tmp_path / "gone-checkout" / ".venv" / "bin" / "python"
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": f"{python} scripts/gate.py"})

    await lp._maybe_preflight()
    lp._hold_ready_for_preflight()

    reason = lp._preflight_state["default"]
    assert isinstance(reason, str), "a gate that cannot run must still fail closed"
    last = reason.splitlines()[-1]
    assert last.startswith("gate command not found: ") and "(absolute path, now missing)" in last
    assert str(python)[-40:] in last  # a long path keeps its telling tail
    f = board.get_feature(fid)
    assert f["blocked"] and f["blocked_class"] == PREFLIGHT_HOLD_CLASS, f["blocked_class"]
    assert f["blocked_reason"].startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX)
    assert "gate command not found:" in f["blocked_reason"] and "venv/bin/python" in f["blocked_reason"]


@posix_only
@requires_br
async def test_the_hold_still_lifts_itself_once_the_interpreter_is_back(board, tmp_path):
    fid = _ready(board, tmp_path, "any card", "a.py")
    python = tmp_path / "venv" / "bin" / "python"
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": f"{python} -c 'pass'"})
    await lp._maybe_preflight()
    lp._hold_ready_for_preflight()
    assert board.get_feature(fid)["blocked_class"] == PREFLIGHT_HOLD_CLASS

    python.parent.mkdir(parents=True)
    python.symlink_to(os.path.realpath(sys.executable))  # the venv is recreated
    lp._last_preflight["default"] = -10_000.0  # past the re-check throttle
    await lp._maybe_preflight()

    f = board.get_feature(fid)
    assert lp._preflight_state["default"] is True
    assert not f["blocked"] and f["blocked_class"] == ""


@posix_only
@requires_br
async def test_the_sweep_neither_requeues_a_preflight_hold_nor_calls_it_stuck(board, tmp_path, monkeypatch):
    """The preflight owns the release. The sweep tells the operator (the environment needs
    them) without claiming the card will never clear, and never requeues it into a re-hold."""
    fid = _ready(board, tmp_path, "any card", "a.py")
    lp = BoardLoop({"coder": "proto", "repo": board.repo, "local_gate_cmd": "/gone/.venv/bin/python scripts/gate.py"})
    await lp._maybe_preflight()
    lp._hold_ready_for_preflight()

    alerts: list = []
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    monkeypatch.setattr(lp, "_notify_operator", lambda f, text, *, incident="": alerts.append((f, text)))
    await lp._sweep()

    f = board.get_feature(fid)
    assert f["blocked"] and f["blocked_class"] == PREFLIGHT_HOLD_CLASS  # untouched by the sweep
    texts = [t for (who, t) in alerts if who == fid]
    assert texts and "held by its project's gate preflight" in texts[0]
    assert "will not clear itself" not in texts[0]


# ── what the board says moves the card ────────────────────────────────────────────────


def _held_row(fid="bd-1"):
    return {
        "id": fid,
        "title": "t",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": PREFLIGHT_HOLD_CLASS,
        "blocked_reason": "gate preflight failed — the coder environment can't run the gate: …",
    }


def test_the_dispatch_record_names_the_environment_not_a_human_unblock():
    step = drive_mod._held_summary([_held_row()])[f"blocked:{PREFLIGHT_HOLD_CLASS}"]["next"]
    assert "local_gate_cmd" in step and "releases the hold itself" in step
    assert "board_unblock_feature" not in step


def test_the_work_snapshot_hint_says_the_hold_lifts_with_the_gate():
    class _Cards:
        def live_cards(self):
            return [_held_row()]

    work_snapshot.reset()
    try:
        BoardLoop({})._take_work_snapshot(_Cards())
        hint = {c["id"]: c["hint"] for c in work_snapshot.provider()}["bd-1"]
    finally:
        work_snapshot.reset()
    assert hint.startswith("held until fix the project's gate environment")
    assert "needs a human" not in hint


async def test_a_gate_that_cannot_launch_names_its_command_only_when_the_checkout_exists(tmp_path, monkeypatch):
    """`create_subprocess_shell` raises FileNotFoundError for a missing shell or a missing
    cwd alike. Naming the gate command for a checkout that is not there would send the
    operator after the wrong fix."""
    monkeypatch.setattr(worktree, "base_checkout_dirt", _clean)

    async def _shell(*_a, **_k):
        raise FileNotFoundError("sh")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    lp = BoardLoop({"local_gate_cmd": "nosuchtool --check"})
    await lp._preflight("p", "nosuchtool --check", str(tmp_path))
    assert lp._preflight_state["p"].splitlines()[-1].startswith("gate command not found: nosuchtool (not on PATH)")

    await lp._preflight("q", "nosuchtool --check", str(tmp_path / "gone"))
    assert "gate command not found" not in lp._preflight_state["q"]
    assert lp._preflight_state["q"].startswith("gate command could not run")

"""A fresh worktree installs its OWN dependencies when the project names a `setup_cmd`.

REAL processes and a real filesystem, no mocks: what matters is where an install
actually writes. `link_node_modules` symlinks the repo checkout's `node_modules` into
every tree, so an install run through that link would write into the checkout every
other tree shares — the test proves the link is replaced first and the checkout is
untouched.
"""

from __future__ import annotations

import os
import sys

import pytest

from project_board import worktree

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")

_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _linked_tree(tmp_path):
    """A checkout with installed deps, and a worktree linked to them the way
    `link_node_modules` leaves it: root and a workspace package both symlinked."""
    checkout = tmp_path / "checkout"
    for rel in ("node_modules/pkg", "apps/web/node_modules/pkg"):
        (checkout / rel).mkdir(parents=True)
    wt = tmp_path / "wt"
    (wt / "apps/web").mkdir(parents=True)
    os.symlink(checkout / "node_modules", wt / "node_modules")
    os.symlink(checkout / "apps/web/node_modules", wt / "apps/web/node_modules")
    return checkout, wt


async def test_no_setup_cmd_touches_nothing(tmp_path):
    checkout, wt = _linked_tree(tmp_path)
    assert await worktree.prepare_worktree(str(wt), "  ", env=_ENV, timeout=5) == ""
    assert os.path.islink(wt / "node_modules")  # an unset command leaves the borrowed deps


async def test_the_install_writes_into_the_tree_not_the_shared_checkout(tmp_path):
    """Without unlinking first, `mkdir -p node_modules` follows the link and the install
    lands in the checkout — every other worktree's deps change under it."""
    checkout, wt = _linked_tree(tmp_path)
    cmd = "mkdir -p node_modules apps/web/node_modules && touch node_modules/ok apps/web/node_modules/ok"

    assert await worktree.prepare_worktree(str(wt), cmd, env=_ENV, timeout=10) == ""

    for rel in ("node_modules", "apps/web/node_modules"):
        assert not os.path.islink(wt / rel), f"{rel} is still the checkout's link"
        assert (wt / rel / "ok").is_file()
        assert not (checkout / rel / "ok").exists(), f"the install wrote into the checkout's {rel}"
        assert (checkout / rel / "pkg").is_dir()  # and the checkout's own deps are intact


async def test_a_real_node_modules_dir_is_the_trees_own_and_kept(tmp_path):
    wt = tmp_path / "wt"
    (wt / "node_modules/pkg").mkdir(parents=True)
    assert await worktree.prepare_worktree(str(wt), "true", env=_ENV, timeout=5) == ""
    assert (wt / "node_modules/pkg").is_dir()


async def test_it_runs_in_the_worktree(tmp_path):
    assert await worktree.prepare_worktree(str(tmp_path), "pwd > here.txt", env=_ENV, timeout=5) == ""
    assert os.path.realpath((tmp_path / "here.txt").read_text().strip()) == os.path.realpath(tmp_path)


async def test_it_gets_the_env_it_is_handed_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("PB_SETUP_LEAK", "1")
    cmd = 'echo "$PB_SETUP_MARK ${PB_SETUP_LEAK:-none}" > env.txt'
    assert await worktree.prepare_worktree(str(tmp_path), cmd, env={**_ENV, "PB_SETUP_MARK": "x"}, timeout=5) == ""
    assert (tmp_path / "env.txt").read_text().strip() == "x none"


async def test_a_failed_install_is_a_reason_not_an_exception(tmp_path):
    reason = await worktree.prepare_worktree(
        str(tmp_path), "echo 'ERR! lockfile out of sync' >&2; exit 3", env=_ENV, timeout=5
    )
    assert reason.startswith("setup_cmd exited 3")
    assert "lockfile out of sync" in reason


async def test_the_reason_keeps_only_the_tail_of_a_long_error(tmp_path):
    reason = await worktree.prepare_worktree(
        str(tmp_path), "i=0; while [ $i -lt 400 ]; do echo line$i >&2; i=$((i+1)); done; exit 1", env=_ENV, timeout=10
    )
    assert "line399" in reason and "line0\n" not in reason
    assert len(reason) < 700

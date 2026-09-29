"""#472: every coder-dispatch bound counts the time the machine spends asleep.

``asyncio.wait_for`` runs on ``time.monotonic()``, which stops while a Mac sleeps, so a
1800s ``coder_timeout_s`` set during a DarkWake held for hours of real time. Each outer bound
now goes through ``worktree.coder_bound``, which calls the host's ``infra.clock.wait_for``
(protoAgent 0.185.0). Sleep can't be produced in a unit test, so these tests prove the
routing: every dispatch path hands its bound to ``infra.clock.wait_for`` and maps the
timeout it raises to ``CoderTimeout``. The clock itself is covered by the host's suite.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml
from test_coder_seam import (
    _FakeAcpClient,
    _FakeAdapter,
    _FakeCoder,
    _inject_task_adapters,
    _install_pre_c1_host,
)
from test_worktree import _Coder, _inject_fake_delegates

from project_board import coder_seam, worktree

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clock(monkeypatch):
    """Replace ``infra.clock.wait_for`` with a recorder whose deadline has always passed:
    it drops the awaitable the way a fired bound does and raises ``TimeoutError``."""
    calls: list = []

    async def _expired(aw, timeout, *, slice_s=None):
        calls.append(timeout)
        if asyncio.iscoroutine(aw):
            aw.close()
        else:
            aw.cancel()
        raise TimeoutError

    monkeypatch.setattr(sys.modules["infra.clock"], "wait_for", _expired)
    return calls


async def test_coder_bound_hands_a_configured_timeout_to_the_host_clock(clock):
    async def _work():
        return "never"

    with pytest.raises(TimeoutError):
        await worktree.coder_bound(_work(), 1800)
    assert clock == [1800]


@pytest.mark.parametrize("timeout", [None, 0])
async def test_coder_bound_without_a_timeout_is_unbounded_and_skips_the_clock(clock, timeout):
    async def _work():
        return "built"

    assert await worktree.coder_bound(_work(), timeout) == "built"
    assert clock == []


async def test_coder_bound_returns_the_result_through_the_host_clock():
    async def _work():
        return "built"

    assert await worktree.coder_bound(_work(), 5) == "built"


async def test_the_public_seam_dispatch_is_bounded_by_the_host_clock(clock):
    coder_seam._progress.clear()

    async def _seam(delegate, prompt, *, timeout=None, **_cbs):
        return "never"

    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_coder_tapped(
            _FakeCoder(), "/wt", "x", fid="bd-472a", gen=1, timeout=1800, _dispatch_tapped=_seam
        )
    assert clock == [1800]


async def test_the_legacy_tap_dispatch_is_bounded_by_the_host_clock(clock, monkeypatch):
    coder_seam._progress.clear()
    _install_pre_c1_host(monkeypatch, _FakeAcpClient())
    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_coder_tapped(_FakeCoder(), "/wt", "x", fid="bd-472b", gen=1, timeout=1800)
    assert clock == [1800]


async def test_the_untapped_dispatch_is_bounded_by_the_host_clock(clock, monkeypatch):
    class _Acp:
        async def dispatch(self, scoped, prompt, timeout=None):
            return "never"

        async def teardown(self, scoped):
            pass

    _inject_fake_delegates(monkeypatch, _Acp())
    with pytest.raises(worktree.CoderTimeout):
        await worktree.dispatch_coder(_Coder(), "/wt", "do it", timeout=1800)
    assert clock == [1800]


async def test_a_task_dispatch_is_bounded_by_the_host_clock(clock, monkeypatch):
    a2a = _FakeAdapter()
    _inject_task_adapters(monkeypatch, acp=_FakeAdapter(), a2a=a2a)
    delegate = type("D", (), {"type": "a2a", "name": "a2a-agent"})()
    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_task(delegate, "x", timeout=1800)
    assert clock == [1800]
    assert a2a.torn_down == [delegate]


async def test_a_coroutine_self_dispatch_is_bounded_by_the_host_clock(clock):
    async def invoke(prompt, session_id):
        return "never"

    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_self(invoke, "p", "s", timeout=1800)
    assert clock == [1800]


async def test_a_synchronous_self_dispatch_is_bounded_by_the_host_clock_and_still_drained(clock):
    """The worker thread can't be cancelled, so the bound waits on a shield: the fired
    deadline cancels only the shield, and the thread still runs to completion before the
    timeout surfaces (the #311 drain)."""
    import threading

    release = threading.Event()
    finished = threading.Event()

    def invoke(prompt, session_id):
        release.wait(5)
        finished.set()
        return "late"

    task = asyncio.ensure_future(coder_seam.dispatch_self(invoke, "p", "s", timeout=1800))
    await asyncio.sleep(0.05)
    assert clock == [1800]
    assert not task.done(), "the timeout must not surface while the worker is still running"
    release.set()
    with pytest.raises(worktree.CoderTimeout):
        await task
    assert finished.is_set()


def test_the_manifest_floor_carries_the_sleep_aware_clock():
    """``infra.clock`` shipped in protoAgent 0.185.0 (#3724); there is no fallback for older hosts."""
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    version = tuple(int(x) for x in str(m["min_protoagent_version"]).split("."))
    assert version >= (0, 185, 0)


def test_the_double_matches_the_host_signature():
    """The suite's ``infra.clock`` double (tests/conftest.py) must take the host's arguments.
    Needs a protoAgent checkout (``PB_PROTOAGENT_SRC``), like the host-apply conformance check."""
    src_root = os.environ.get("PB_PROTOAGENT_SRC", "").strip()
    path = Path(src_root, "infra", "clock.py") if src_root else None
    if path is None or not path.is_file():
        pytest.skip("set PB_PROTOAGENT_SRC to a protoAgent checkout to run the host conformance check")
    (fn,) = [
        n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "wait_for"
    ]
    assert [a.arg for a in fn.args.args] == ["aw", "timeout"]
    assert [a.arg for a in fn.args.kwonlyargs] == ["slice_s"]

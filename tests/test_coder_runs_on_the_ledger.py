"""A board coder run is on the host's delegation ledger — live while it works.

Josh, 10-25: projectManager's codex "is not lighting up or anything in org chart" while
the board used it. The board reached the delegate adapter directly, around the host's
one recording funnel, so its runs left no live edge and no row. `sdk.delegation`
(protoAgent #4512) is that funnel for a plugin; the board wraps every coder and task
dispatch in it, and a host without it dispatches exactly as before.
"""

from __future__ import annotations

import contextlib
import sys
import types
from dataclasses import dataclass, field

import pytest

from project_board import coder_seam, worktree


@dataclass
class _Coder:
    name: str = "codex"
    type: str = "acp"
    workdir: str = ""
    manage_git: bool = True
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Result:
    reply: str
    usage: dict | None = None
    plan: list | None = None
    stop_reason: str | None = None
    dead_end: str | None = None


@pytest.fixture
def ledger(monkeypatch):
    """A host `graph.sdk` whose `delegation` behaves like the real funnel: the edge is
    open for the block and closes with ok / failed. Returns the event log."""
    log: list = []

    @contextlib.contextmanager
    def delegation(**kw):
        log.append(("open", kw))
        try:
            yield types.SimpleNamespace(cost_usd=None)
        except BaseException as exc:
            log.append(("close", "failed", type(exc).__name__))
            raise
        log.append(("close", "ok"))

    sdk = types.ModuleType("graph.sdk")
    sdk.delegation = delegation
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    return log


async def test_a_coder_run_is_live_on_the_ledger_and_labelled_by_its_card(ledger):
    coder_seam.note_card("bd-rwnf", "Empty question body from #4506")
    seen_open = []

    async def _seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        seen_open.append([e[0] for e in ledger])  # what the ledger held mid-run
        return _Result(reply="done")

    out = await coder_seam.dispatch_coder_tapped(
        _Coder(), "/wt/a", "fix it", fid="bd-rwnf", gen=1, _dispatch_tapped=_seam
    )

    assert out == "done"
    assert seen_open == [["open"]]  # live while the coder works, so the chart lights it up
    (_, opened), closed = ledger
    assert opened == {
        "to_kind": "acp",
        "to_name": "codex",
        "to_instance": "",
        "what": "bd-rwnf: Empty question body from #4506",
        "origin": "project_board",
    }
    assert closed == ("close", "ok")


async def test_a_failed_coder_run_is_recorded_as_failed_and_still_raises(ledger):
    async def _seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        raise worktree.WorktreeError("coder crashed")

    with pytest.raises(worktree.WorktreeError):
        await coder_seam.dispatch_coder_tapped(_Coder(), "/wt/b", "x", fid="bd-b", gen=1, _dispatch_tapped=_seam)
    assert ledger[-1] == ("close", "failed", "WorktreeError")


async def test_a_task_dispatch_is_recorded_under_the_card_its_drive_noted(ledger, monkeypatch):
    class _Adapter:
        async def dispatch(self, delegate, prompt, *, timeout=None):
            return "the deliverable"

        async def teardown(self, delegate):
            pass

    class DelegateError(Exception):
        pass

    mod = types.ModuleType("plugins.delegates.adapters")
    mod.ADAPTERS = {"a2a": _Adapter(), "acp": _Adapter()}
    mod.DelegateError = DelegateError
    monkeypatch.setitem(sys.modules, "plugins.delegates.adapters", mod)

    coder_seam.note_card("bd-task", "Write the ADR")
    delegate = types.SimpleNamespace(type="a2a", name="designSystem", url="http://127.0.0.1:7874/a2a")
    assert await coder_seam.dispatch_task(delegate, "write it") == "the deliverable"
    (_, opened), _closed = ledger
    assert (opened["to_kind"], opened["to_name"], opened["what"]) == ("a2a", "designSystem", "bd-task: Write the ADR")
    assert opened["to_instance"] == "http://127.0.0.1:7874/a2a"


async def test_a_host_without_the_seam_dispatches_as_before(monkeypatch):
    sdk = types.ModuleType("graph.sdk")  # an older host: no `delegation`
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)

    async def _seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        return _Result(reply="ok")

    assert (
        await coder_seam.dispatch_coder_tapped(_Coder(), "/wt/c", "x", fid="bd-c", gen=1, _dispatch_tapped=_seam)
        == "ok"
    )


def test_the_card_title_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(coder_seam, "_CARD_TITLES_MAX", 3)
    monkeypatch.setattr(coder_seam, "_CARD_TITLES", type(coder_seam._CARD_TITLES)())
    for i in range(5):
        coder_seam.note_card(f"bd-{i}", f"card {i}")
    assert list(coder_seam._CARD_TITLES) == ["bd-2", "bd-3", "bd-4"]

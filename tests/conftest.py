"""Test bootstrap + shared fixtures.

The plugin is a multi-module *package* whose modules use relative imports
(``from .store import …``, ``from . import worktree``) — exactly how the host
loads it (under a synthetic ``protoagent_plugin_<id>`` package). So the suite
can't import the modules as top-level files; it registers the repo as a package
named ``project_board`` (path = repo root) and imports through it
(``from project_board.store import …``). Executing ``__init__.py`` is safe — it
only *defines* ``register``/tools at import time (the host-only imports live
inside ``register()``), so no protoAgent host is needed to run these tests.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "project_board"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _module
    _spec.loader.exec_module(_module)

# `graph.sdk.complete` is the host LLM seam the goal-verify gate + max-mode judge
# reach lazily (`from graph.sdk import complete`). protoAgent provides it at runtime
# but it's not a pip dep, so the standalone CI env has no `graph` package — which made
# `monkeypatch.setattr("graph.sdk.complete", …)` raise ModuleNotFoundError at patch
# time. Register a stub package so those tests can patch the seam; the default impl
# raises so a test that forgets to patch fails loudly rather than hitting a real model.
if "graph" not in sys.modules:
    _graph = types.ModuleType("graph")
    _graph.__path__ = []  # mark as a package so `graph.sdk` resolves as a submodule
    _graph_sdk = types.ModuleType("graph.sdk")

    async def _unpatched_complete(*_a, **_k):  # pragma: no cover — tests must patch this
        raise RuntimeError("graph.sdk.complete must be monkeypatched in tests")

    _graph_sdk.complete = _unpatched_complete
    _graph.sdk = _graph_sdk
    sys.modules["graph"] = _graph
    sys.modules["graph.sdk"] = _graph_sdk


@pytest.fixture
def make_board(monkeypatch):
    """Build a ``BeadsBoard`` with the ``br`` PATH check stubbed and ``_run``
    replaced by a test-supplied fake (``(*args, want_json=False) -> value``), so
    the store's projection/gate/escalation logic is exercised without the CLI."""
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")

    def _make(run_impl, *, repo="/repo", base_branch="main"):
        b = store_mod.BeadsBoard(db=None, repo=repo, base_branch=base_branch)
        monkeypatch.setattr(b, "_run", run_impl)
        return b

    return _make

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

    async def _default_knowledge_search(*_a, **_k):
        return []  # default: no lessons (tests patch this to inject hits)

    _graph_sdk.complete = _unpatched_complete
    _graph_sdk.knowledge_search = _default_knowledge_search
    _graph.sdk = _graph_sdk
    sys.modules["graph"] = _graph
    sys.modules["graph.sdk"] = _graph_sdk


@pytest.fixture(autouse=True)
def _no_real_br_version(monkeypatch, tmp_path_factory):
    """The setup preflight samples ``br --version`` (setup_check._br_version) from
    register()/the /status route/the loop gate — every tier this suite exercises
    with a fake store. Pin its runner so the UNIT tier never shells a real ``br``
    (the real-br integration tier shells ``subprocess.run`` directly and is
    untouched), and drop the per-path cache so no test sees another's sample."""
    from types import SimpleNamespace

    from project_board import br_fetch, setup_check

    monkeypatch.setattr(
        setup_check, "_subprocess_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="br 0.0.0-test\n")
    )
    setup_check._BR_VERSION_CACHE.clear()
    setup_check.publish_loop_snapshot(None)  # no running loop between tests

    # The `br` auto-fetch (v0.43.0): NEVER touch the network or ~/.protoagent from the
    # unit tier. Any test that lets register()/the loop gate see "no br on PATH" would
    # otherwise start a real 5 MB download in a daemon thread. The default downloader
    # raises (a test that wants a fetch injects its own), the data dir is a tmp dir,
    # and the process-stable fetch state is reset so no test sees another's.
    def _no_network(url, timeout=0.0):
        raise AssertionError(f"unit tier tried to download {url} — inject a fake downloader")

    _no_network.real = br_fetch._urllib_download  # for the one test that drives the real GET against a fake opener
    monkeypatch.setattr(br_fetch, "_urllib_download", _no_network)
    # pytest's own temp root (retained/pruned like tmp_path) — no mkdtemp leak per test.
    monkeypatch.setenv(br_fetch.ENV_DATA_DIR, str(tmp_path_factory.mktemp("pb-data")))
    monkeypatch.delenv(br_fetch.ENV_BR_BIN, raising=False)
    br_fetch.reset_state()
    yield
    setup_check._BR_VERSION_CACHE.clear()
    setup_check.publish_loop_snapshot(None)
    br_fetch.reset_state()


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

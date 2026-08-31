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
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import NamedTuple

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
    setup_check._REVIEW_STATUS_CACHE.clear()  # #354: the per-process capability probe, per test
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
    setup_check._REVIEW_STATUS_CACHE.clear()
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


# ── Real-GitHub integration tier (#361 slice 2) ──────────────────────────────────────
# Mirrors the real-`br` tier (tests/test_integration.py): tests/test_worktree_gh.py shells
# the ACTUAL `gh`/GitHub against a real OPEN PR to exercise the 12 read-dominant worktree
# GitHub seams a mocked `_gh` cannot validate — the exact blindness that shipped #354 (a PAT
# that cannot POST /check-runs, green through every mock, publishing to nothing for a day).
# Locally the tier SKIPS when GitHub credentials are unavailable; CI sets PB_REQUIRE_GH=1 so
# an absent/unusable credential (or an unresolvable fixture PR) FAILS the guard test rather
# than silently skipping — a silent skip is a fake with extra ceremony.
#
# No credential is embedded here: the token rides the ambient `gh` auth (GH_TOKEN /
# `gh auth login`); a PR URL is a PUBLIC identifier, never a secret. Required environment,
# documented and credential-free:
#   PB_GH_FIXTURE_PR — full URL of the OPEN fixture PR in the checkout's repo, e.g.
#                      https://github.com/protoLabsAI/projectBoard-plugin/pull/<n>. CI resolves
#                      it (see .github/workflows/ci.yml) to the maintained `PB_GH_FIXTURE_PR`
#                      repo variable when a maintainer wires a dedicated, pinned,
#                      permanently-open fixture PR, and otherwise to the PR under test — itself
#                      a real open PR while its own checks run. The reads target this PR; the two
#                      writes use DISPOSABLE contexts distinct from the production review signals,
#                      so the tier never disturbs a real review gate on it.
#   PB_REQUIRE_GH=1  — (CI only) turn an absent/unusable prerequisite into a FAILURE.
#   GH_TOKEN         — (CI only) the gh credential; a repo-scoped token that can create a
#                      commit status + a PR comment on the fixture PR (mirrors the board's PAT).

GH_FIXTURE_PR_URL = (os.environ.get("PB_GH_FIXTURE_PR") or "").strip()
_GH_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


class GhFixture(NamedTuple):
    """The resolved pinned-PR context the real-GitHub seam tests read against."""

    url: str
    slug: str
    number: str
    head_sha: str
    head_branch: str
    repo_dir: str


def gh_credentialed() -> bool:
    """`gh` on PATH AND carrying a usable credential — the real-GitHub tier's prerequisite.
    ``gh auth status`` exits 0 only when a token (GH_TOKEN or a stored login) is present and
    syntactically accepted; it is the same gate `gh` applies before every API call, so a green
    here means the reads/writes can at least authenticate. Absent → the tier skips locally and
    FAILS under PB_REQUIRE_GH."""
    if shutil.which("gh") is None:
        return False
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30).returncode == 0
    except Exception:  # noqa: BLE001 — an unusable gh is simply "not credentialed"
        return False


def gh_tier_ready() -> tuple[bool, str]:
    """(ready, reason) for the whole real-GitHub tier: a usable `gh` credential AND a configured
    pinned fixture PR. ``reason`` is the local skip message / the CI failure message."""
    if not gh_credentialed():
        return False, "no usable `gh` credential (set GH_TOKEN or run `gh auth login`) for the real-GitHub tier"
    if not GH_FIXTURE_PR_URL:
        return False, "PB_GH_FIXTURE_PR is not set to the pinned permanently-open fixture PR URL"
    return True, ""


def _gh_setup(*args: str, cwd: str, timeout: float = 60) -> str:
    """Real `gh` for FIXTURE setup ONLY (never under test) — raises on a non-zero exit, returns
    stripped stdout. The seam functions are what the tests exercise; this merely resolves the
    pinned PR's immutable facts (state, head sha/branch) the assertions pin against, exactly as
    tests/test_worktree_git.py uses ``_git_run`` for setup and ``worktree.*`` under test."""
    proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _gh_setup_json(*args: str, cwd: str, timeout: float = 60) -> dict:
    """``_gh_setup`` that parses a ``gh ... --json`` object payload — one round-trip for the
    fixture's whole identity, raising on a non-object/unparseable body."""
    data = json.loads(_gh_setup(*args, cwd=cwd, timeout=timeout) or "{}")
    if not isinstance(data, dict):
        raise RuntimeError(f"gh {' '.join(args)} returned a non-object payload: {type(data).__name__}")
    return data


@pytest.fixture
def gh_fixture() -> GhFixture:
    """Resolve the pinned, permanently-open fixture PR into a :class:`GhFixture` via REAL `gh`
    (setup calls, not the seams under test), or skip / fail per the tier's local/CI posture.

    Reads the PR's identity ONCE — head sha (40-hex), head branch, slug, number — and asserts it
    is OPEN, so every seam test pins its head-bound results against a SINGLE authoritative head
    (#328/#347: a verdict must never be attributed to a moved head). ``repo_dir`` is the checkout
    root, the cwd `gh` resolves repo + auth from."""
    ready, reason = gh_tier_ready()
    if not ready:
        if os.environ.get("PB_REQUIRE_GH"):
            pytest.fail(
                f"PB_REQUIRE_GH is set but the real-GitHub tier cannot run: {reason}. Fix the CI "
                f"credential/fixture wiring (GH_TOKEN + PB_GH_FIXTURE_PR — the maintained repo "
                f"variable, or the PR under test) — a silent skip here is exactly how #354 shipped green."
            )
        pytest.skip(reason)
    m = _GH_PR_URL_RE.search(GH_FIXTURE_PR_URL)
    assert m, f"PB_GH_FIXTURE_PR is not a GitHub PR URL: {GH_FIXTURE_PR_URL!r}"
    slug, number = m.group(1), m.group(2)
    repo_dir = str(ROOT)
    facts = _gh_setup_json("pr", "view", GH_FIXTURE_PR_URL, "--json", "state,headRefOid,headRefName", cwd=repo_dir)
    state, head_sha, head_branch = facts.get("state"), facts.get("headRefOid"), facts.get("headRefName")
    assert state == "OPEN", (
        f"the pinned fixture PR {GH_FIXTURE_PR_URL} is {state!r}, not OPEN — the tier needs a "
        f"PERMANENTLY-open PR; repoint PB_GH_FIXTURE_PR at one that is kept open"
    )
    assert isinstance(head_sha, str) and re.fullmatch(r"[0-9a-f]{40}", head_sha), (
        f"unexpected fixture head sha shape: {head_sha!r}"
    )
    assert isinstance(head_branch, str) and head_branch, f"unexpected fixture head branch: {head_branch!r}"
    return GhFixture(GH_FIXTURE_PR_URL, slug, number, head_sha, head_branch, repo_dir)

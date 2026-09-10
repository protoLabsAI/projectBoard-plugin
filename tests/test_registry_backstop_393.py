"""The loop backstop #393's fix relies on: "a registry change resets the loop's gate
preflight, which re-smokes the gate before that project's work dispatches" (review finding
R6 on PR #430).

It stranded exactly the project that most needs it. `reload()` cleared every preflight
verdict, but `_maybe_preflight` re-checks only projects with ready work or a FAILED verdict.
A project whose ready cards were all held had neither any more, so it was never re-checked
and its holds outlived a fixed gate until a restart. #430 made that reachable from the editor:
a conventions-only save of a held project now lands, where before the red smoke refused it.
Now:

- a registry change resets the verdict of every project whose routing CHANGED, and only
  those (a save to one project no longer re-runs every other project's suite);
- the preflight re-checks every project it is holding cards for, throttled once checked, so
  a checkout that yields no verdict is not re-smoked every tick. The re-check's verdict
  reaches /status like any other.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import project_board.loop as loop_mod
from project_board import health
from project_board.loop import BoardLoop


class _Store:
    def __init__(self):
        self.calls = []

    def list_features(self, state=None, include_archived=False):
        return []  # every ready card of the project is held (blocked) → nothing ready

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))
        return {"id": fid}


class _HostConfig:
    def __init__(self, section):
        self.plugin_config = {"project_board": section}


def _held_loop(tmp_path: Path, monkeypatch, *, gate: str, dirt: str = ""):
    """A loop whose project `alpha` failed its preflight and held bd-1, plus an unrelated,
    passing `beta`."""
    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    projects = {
        "alpha": {"repo": str(repo), "base_branch": "main", "local_gate_cmd": gate},
        "beta": {"repo": str(repo), "base_branch": "main", "local_gate_cmd": "exit 0"},
    }
    lp = BoardLoop({"projects": projects, "default_project": "alpha"})

    async def _dirt(*_a, **_k):
        return dirt

    monkeypatch.setattr("project_board.worktree.base_checkout_dirt", _dirt)
    monkeypatch.setattr(loop_mod, "reconfigure_cached_store", lambda **kw: True)
    store = _Store()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp._preflight_state = {"alpha": "gate exited 1: broken", "beta": True}
    lp._preflight_held = {"alpha": {"bd-1"}}
    lp._last_preflight = {"alpha": -10_000.0}
    health.publish_preflight(lp._preflight_state, lp._preflight_dirty)
    return lp, store, projects


async def test_a_registry_change_rechecks_a_held_project_and_releases_its_holds(tmp_path, monkeypatch):
    """R6: the operator fixed alpha's gate, and a Projects save (conventions only) changed its
    entry. The reload cleared alpha's verdict, and alpha has no ready work, so nothing ever
    re-checked it and bd-1 stayed held. It is re-checked now, the green verdict releases
    bd-1, and /status stops naming alpha as held."""
    marker = tmp_path / "gate-ran"
    lp, store, projects = _held_loop(tmp_path, monkeypatch, gate=f'touch "{marker}"; exit 0')
    assert "alpha" in health.preflight_snapshot()["held"]

    lp.reload(_HostConfig({"projects": {**projects, "alpha": {**projects["alpha"], "repo_conventions": "new"}}}))
    await lp._maybe_preflight()

    assert marker.exists(), "the held project's gate was never re-checked"
    assert ("clear_blocked", "bd-1") in store.calls and lp._preflight_held == {}
    assert lp._preflight_state["alpha"] is True
    assert "alpha" not in health.preflight_snapshot()["held"]


async def test_a_registry_change_keeps_the_verdicts_of_projects_it_did_not_touch(tmp_path, monkeypatch):
    """Clearing EVERY verdict on any change meant a conventions edit to alpha re-ran beta's
    whole suite in the loop, and with #430 that happens on every editor save."""
    lp, _store, projects = _held_loop(tmp_path, monkeypatch, gate="exit 0")

    lp.reload(_HostConfig({"projects": {**projects, "alpha": {**projects["alpha"], "repo_conventions": "new"}}}))

    assert "alpha" not in lp._preflight_state  # the changed project gets a fresh check
    assert lp._preflight_state["beta"] is True  # the untouched one keeps its verdict


async def test_a_held_project_that_yields_no_verdict_is_not_resmoked_every_tick(tmp_path, monkeypatch):
    """A dirty checkout gives no verdict, which leaves the reset verdict empty. Keyed on
    "never checked", that would re-run the whole gate on every tick. It runs once and is then
    throttled like a known failure."""
    marker = tmp_path / "runs"
    lp, _store, projects = _held_loop(
        tmp_path, monkeypatch, gate=f'echo run >> "{marker}"; exit 0', dirt="uncommitted changes to x.py"
    )
    lp.reload(_HostConfig({"projects": {**projects, "alpha": {**projects["alpha"], "repo_conventions": "new"}}}))

    await lp._maybe_preflight()
    await lp._maybe_preflight()

    assert marker.read_text().split() == ["run"], "the held project was re-smoked on the very next tick"
    assert lp._preflight_held == {"alpha": {"bd-1"}}  # no verdict → the hold stays (#300)

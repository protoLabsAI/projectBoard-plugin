"""Real-seam tier for publish gates (`waits_for`) and the release-freeze guard.

Everything here talks to the REAL external system — no fake `_run`, no fake HTTP, no fake
`gh` — because each seam's whole job is an external effect, and a mock of that effect is
the shape that shipped #353 (a 57-char label the fake `br` accepted and real beads
refused), #354 and #356. Registered in tests/test_external_seams.py.

Three sub-tiers, each gated the way this suite gates real tiers:

* **real `br`** (``@requires_br``, CI: PB_REQUIRE_BR on every br leg via ``br_shape``) —
  the persisted gate spec round-trips through beads. Specs routinely exceed beads' 50-char
  LABEL cap and carry ``/ @ # < space``, which is why they live in the notes field; the
  tests write one well past 50 chars through every notes writer and prove the label route
  would have been refused.
* **real npm registry** (``@requires_npm``: runs when PB_NPM_TIER or PB_REQUIRE_NPM is
  set; CI's `test (real gh)` job sets PB_REQUIRE_NPM=1, so an unreachable registry FAILS
  there instead of skipping) — ``gates.eval_npm`` against live packument reads.
* **real GitHub** (``@requires_gh``, the same ``gh_tier_ready`` gate + PB_REQUIRE_GH as
  tests/test_worktree_gh.py) — ``gates.eval_pr`` / ``gates.eval_release`` and the two
  GitHub freeze reads. ``worktree.remote_branches`` needs only git and a bare origin.

The autouse fixture in conftest stubs every one of these seams for the UNIT tier; this
file restores the genuine implementations (``conftest.REAL_SEAMS``) before calling them.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import REAL_SEAMS, gh_tier_ready
from project_board import gates, worktree
from project_board import store as store_mod
from project_board.store import NOTES_WAITS_PREFIX, BeadsBoard, BoardError

BEADS_LABEL_CAP = 50
# A realistic spec well past the label cap: scoped package + a two-comparator range.
LONG_SPEC = "npm:@protolabsai/some-very-long-package-name@>=10.20.30-rc.1 <11.0.0-0"
assert len(LONG_SPEC) > BEADS_LABEL_CAP

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)
_NPM_ON = bool(os.environ.get("PB_NPM_TIER") or os.environ.get("PB_REQUIRE_NPM"))
requires_npm = pytest.mark.skipif(
    not _NPM_ON,
    reason="real npm-registry tier is opt-in: set PB_NPM_TIER=1 (CI sets PB_REQUIRE_NPM=1 to enforce it)",
)
_GH_READY, _GH_REASON = gh_tier_ready()
requires_gh = pytest.mark.skipif(not _GH_READY, reason=_GH_REASON or "real-GitHub tier ready")
# Stable public facts the GitHub gate tests read (this plugin's own repo): a merged PR and
# a tag + GitHub release that exist forever.
PLUGIN_SLUG = "protoLabsAI/projectBoard-plugin"
MERGED_PR = 451  # "chore: release v0.58.0", merged
EXISTING_TAG = "v0.58.0"


@pytest.fixture
def real_seams(monkeypatch):
    """Put the genuine network seams back (conftest stubs them for the unit tier)."""
    monkeypatch.setattr(gates, "_http_get_json", REAL_SEAMS["gates._http_get_json"])
    monkeypatch.setattr(gates, "_gh_json", REAL_SEAMS["gates._gh_json"])
    monkeypatch.setattr(worktree, "remote_branches", REAL_SEAMS["worktree.remote_branches"])
    monkeypatch.setattr(worktree, "open_pr_heads", REAL_SEAMS["worktree.open_pr_heads"])
    monkeypatch.setattr(worktree, "active_workflow_runs", REAL_SEAMS["worktree.active_workflow_runs"])


# ── skip guards: an enforced tier never silently skips ──────────────────────────────


def test_npm_tier_cannot_silently_skip_in_ci(real_seams):
    """Deliberately NOT under ``@requires_npm``: with PB_REQUIRE_NPM set, an unreachable
    registry FAILS here rather than letting the npm tests skip green."""
    if os.environ.get("PB_REQUIRE_NPM"):
        status, doc = gates._http_get_json(f"{gates.NPM_REGISTRY}/left-pad")
        assert status == 200 and isinstance(doc, dict), f"PB_REQUIRE_NPM is set but the registry answered {status}"


def test_gh_tier_cannot_silently_skip_in_ci():
    if os.environ.get("PB_REQUIRE_GH"):
        assert _GH_READY, f"PB_REQUIRE_GH is set but the real-GitHub tier is not runnable: {_GH_REASON}"


# ── real `br`: the persisted spec fits and round-trips ─────────────────────────────


@pytest.fixture
def board(tmp_path):
    return BeadsBoard(repo=str(tmp_path), actor="test")


@requires_br
@pytest.mark.br_shape
def test_a_long_gate_spec_round_trips_through_real_br_notes(board, tmp_path):
    """The spec lands VERBATIM through create → update(files) → mark_ready (which rewrites
    notes to materialize the requirement ledger) → update(waits_for) → clear — every notes
    writer — and never leaks into files_to_modify."""
    (tmp_path / "a.py").write_text("x = 1\n")
    short = "pr:protoLabsAI/protoContent#12"
    f = board.create_feature(
        "Adopt new token",
        spec="s",
        acceptance_criteria="- WHEN x THE SYSTEM SHALL y",
        files_to_modify=["a.py"],
        waits_for=f"{LONG_SPEC}, {short}",
        source_issue="acme/widgets#8",
    )
    assert not f.get("enrichment_failed"), f.get("warning")
    got = board.get_feature(f["id"])
    assert got["waits_for"] == [LONG_SPEC, short]
    assert got["files_to_modify"] == ["a.py"] and got["source_issue"] == "acme/widgets#8"

    board.update_feature(f["id"], files_to_modify=["a.py"])  # a files-only rewrite keeps the gates
    board.mark_ready(f["id"])  # the ledger materialization rewrites notes too
    got = board.get_feature(f["id"])
    assert got["waits_for"] == [LONG_SPEC, short]
    assert got["requirements"], "mark_ready materialized the ledger"
    assert all(len(lb) <= BEADS_LABEL_CAP for lb in got["labels"])

    # The READY-queue row the claim scan reads carries the gates (br ≤0.1.23 via the
    # label-less re-fetch, newer br straight from `br ready --json`).
    row = next(r for r in board.ready_queue() if r["id"] == f["id"])
    assert row["waits_for"] == [LONG_SPEC, short]

    board.update_feature(f["id"], waits_for=[short])
    assert board.get_feature(f["id"])["waits_for"] == [short]
    board.update_feature(f["id"], waits_for=[])
    cleared = board.get_feature(f["id"])
    assert cleared["waits_for"] == [] and cleared["files_to_modify"] == ["a.py"]


@requires_br
@pytest.mark.br_shape
def test_the_label_route_would_have_been_refused_by_real_br(board):
    """Why the spec is NOT a label: real beads refuses a label this long (the #353 shape),
    failing the whole `br update`. Red-is-reachable proof that the notes placement is
    load-bearing, not a style choice."""
    f = board.create_feature("probe", spec="s")
    with pytest.raises(BoardError):
        board._run("update", f["id"], "--add-label", f"waits-for:{LONG_SPEC}")
    # …while the notes line lands.
    board._run("update", f["id"], f"--notes={NOTES_WAITS_PREFIX} {LONG_SPEC}")
    assert board.get_feature(f["id"])["waits_for"] == [LONG_SPEC]


@requires_br
def test_a_bad_spec_refuses_the_create_before_any_bead_is_minted(board):
    before = {x["id"] for x in board.list_features()}
    with pytest.raises(BoardError, match="waits_for"):
        board.create_feature("bad", spec="s", waits_for="npm:@protolabsai/ui@>=banana")
    assert {x["id"] for x in board.list_features()} == before


# ── real npm registry ────────────────────────────────────────────────────────────


@requires_npm
def test_eval_npm_met_against_the_live_registry(real_seams):
    out = gates.eval_npm(gates.parse_spec("npm:left-pad@>=1.0.0"))
    assert out["met"] is True and out["version"] == "1.3.0"


@requires_npm
def test_eval_npm_unmet_names_the_latest_version(real_seams):
    out = gates.eval_npm(gates.parse_spec("npm:left-pad@>=99.0.0"))
    assert out["met"] is False and "latest 1.3.0" in out["detail"]


@requires_npm
def test_eval_npm_encodes_a_scoped_name(real_seams):
    """`@scope/name` must reach the registry as `@scope%2fname` — a wrong encoding 404s,
    which would read as "not published yet" forever."""
    out = gates.eval_npm(gates.parse_spec("npm:@types/node@>=18.0.0"))
    assert out["met"] is True


@requires_npm
def test_eval_npm_never_published_is_unmet_not_an_error(real_seams):
    out = gates.eval_npm(gates.parse_spec("npm:@protolabsai/definitely-not-a-real-package-3f9c@>=1.0.0"))
    assert out["met"] is False and "not published yet" in out["detail"]


# ── real GitHub ──────────────────────────────────────────────────────────────────


@requires_gh
def test_eval_pr_reads_merged_and_open_from_github(real_seams, gh_fixture):
    assert gates.eval_pr(gates.parse_spec(f"pr:{PLUGIN_SLUG}#{MERGED_PR}"))["met"] is True
    live = gates.eval_pr(gates.parse_spec(f"pr:{gh_fixture.slug}#{gh_fixture.number}"))
    assert live["met"] is False and "(open)" in live["detail"]


@requires_gh
def test_eval_release_exact_tag_and_range(real_seams):
    assert gates.eval_release(gates.parse_spec(f"release:{PLUGIN_SLUG}@{EXISTING_TAG}"))["met"] is True
    missing = gates.eval_release(gates.parse_spec(f"release:{PLUGIN_SLUG}@v999.0.0"))
    assert missing["met"] is False and "no such tag" in missing["detail"]
    assert gates.eval_release(gates.parse_spec(f"release:{PLUGIN_SLUG}@>=0.58.0"))["met"] is True
    assert gates.eval_release(gates.parse_spec(f"release:{PLUGIN_SLUG}@>=999.0.0"))["met"] is False


@requires_gh
async def test_open_pr_heads_finds_the_fixture_pr_by_its_head(real_seams, gh_fixture):
    hits = await worktree.open_pr_heads(gh_fixture.slug, [gh_fixture.head_branch], cwd=gh_fixture.repo_dir)
    assert (int(gh_fixture.number), gh_fixture.head_branch) in hits
    assert await worktree.open_pr_heads(gh_fixture.slug, ["prepare-release-never-*"], cwd=gh_fixture.repo_dir) == []


@requires_gh
async def test_active_workflow_runs_reads_real_runs_and_treats_404_as_none(real_seams, gh_fixture):
    """A repo with no such workflow must answer [] (most repos have no release workflow;
    a 404 must never hold their merges), and a real workflow must parse."""
    assert await worktree.active_workflow_runs(gh_fixture.slug, "no-such-workflow.yml", cwd=gh_fixture.repo_dir) == []
    runs = await worktree.active_workflow_runs(gh_fixture.slug, "ci.yml", cwd=gh_fixture.repo_dir)
    assert isinstance(runs, list) and all(r["status"] != "completed" for r in runs)


# ── real git: the branch signal ──────────────────────────────────────────────────


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_remote_branches_matches_release_branches_on_a_real_origin(real_seams, tmp_path):
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git("config", k, v, cwd=clone)
    (clone / "f").write_text("x")
    _git("add", "f", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("push", "origin", "HEAD:main", cwd=clone)
    assert await worktree.remote_branches(str(clone), ["prepare-release*"]) == []
    _git("push", "origin", "HEAD:refs/heads/prepare-release/v1.2.3", cwd=clone)
    assert await worktree.remote_branches(str(clone), ["prepare-release*"]) == ["prepare-release/v1.2.3"]
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(worktree.WorktreeError):  # an unanswerable read RAISES (the freeze fails closed on it)
        await worktree.remote_branches(str(tmp_path / "not-a-repo"), ["x"])

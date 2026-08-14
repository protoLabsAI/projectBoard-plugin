"""Real-`br` integration tier (#136): the ACTUAL beads CLI against a throwaway
`.beads` workspace in ``tmp_path`` — no ``shutil.which`` stubs, no ``_run`` fakes.

Everything here covers what **beads itself decides** — the failure class the fake
tier (623 tests, every ``_run`` a mock) is structurally blind to:
  - #135: `merged-verified:` + a 40-char sha = 56 chars > beads' 50-char label cap —
    shipped non-functional through every fake test, CI, and two adjudications.
  - #116: DB contention reported as JSON on a ZERO exit code — a shape no fake produced.
  - #106: the `close -r` punctuation theory (disproved: punctuation is fine on real
    `br`; the actual cause was contention) — only a real `br` can settle that class.
  - #138: `br` 0.1.23 → 0.2.16 changed the `--json` payload shape ([] → {"issues":[…]});
    the pre-flight checks tested exit codes while the plugin consumes output SHAPE.

The pattern is #137's ``test_verify_merged_state_stamp_lands_through_real_br``: a
real ``BeadsBoard`` over ``tmp_path`` (``_ensure_workspace`` runs the real ``br
init``), ``skipif`` when the binary is absent. CI installs a PINNED release binary
and sets ``PB_REQUIRE_BR=1``, which turns an absent `br` into a FAILURE via the
guard test below — a silent skip is a fake with extra ceremony.

#138 MATRIX (see .github/workflows/ci.yml): the tests that assert the version-agnostic
``--json`` SHAPE contract carry ``@pytest.mark.br_shape`` and run against BOTH br 0.1.23
(bare list) and 0.2.16 (envelope). The 0.2.16 leg runs ONLY ``-m br_shape`` — the rest of
this tier pins 0.1.x-SPECIFIC quirks (``br ready --json`` omitting ``labels``, the 50-char
label cap, contention reported as JSON on a zero exit) whose whole value is that they're
version-specific; running them on 0.2.16 would test beads, not the plugin's shape handling.
"""

from __future__ import annotations

import os
import shutil

import pytest

from project_board import store as store_mod
from project_board.loop import _MERGED_VERIFIED_SHA_LEN
from project_board.store import (
    LABEL_GENS_PREFIX,
    LABEL_MERGED_VERIFIED_PREFIX,
    LABEL_READY,
    LABEL_VERIFIED_PREFIX,
    BeadsBoard,
    BoardError,
)

# A real 40-char sha — the realistic value every sha-bearing label is fed in
# production (coder_seam records `git rev-parse HEAD` verbatim; the loop truncates
# only for the merged-verified stamp, and #135 was exactly that truncation missing).
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
BEADS_LABEL_CAP = 50  # beads' label validator: "exceeds 50 characters" (br 0.1.x)

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — the integration tier needs it "
    "(CI installs the pinned release binary and sets PB_REQUIRE_BR=1)",
)


@pytest.mark.br_shape  # runs on BOTH matrix legs — the 0.2.16 leg is `-m br_shape`, so
# without this the whole leg could silently skip a broken `br` install (#136's failure mode)
def test_integration_tier_cannot_silently_skip_in_ci():
    """The skip guard (#136) — deliberately NOT under ``requires_br``: when CI says
    `br` must be present (PB_REQUIRE_BR=1, set right after the pinned install step),
    an absent binary FAILS the suite here instead of silently skipping the whole
    tier (the way #137's test skipped green through every CI run). Without the env
    var (a local checkout without beads) this is an always-green no-op — the
    ``requires_br`` skips still apply to the real tests."""
    if os.environ.get("PB_REQUIRE_BR"):
        assert shutil.which(store_mod.BR) is not None, (
            "PB_REQUIRE_BR is set but the `br` binary is not on PATH — the real-br "
            "integration tier would have silently skipped; fix the CI install step "
            "(pinned v0.1.23 release binary) before trusting this run"
        )


@pytest.fixture
def board(tmp_path):
    """A REAL ``BeadsBoard`` over a throwaway workspace: first ``_run`` triggers
    ``_ensure_workspace`` → a real ``br init`` in ``tmp_path`` (no parent `.beads`
    adoption, ADR 0055), so every test gets its own isolated db + JSONL."""
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _ready_feature(board, tmp_path, title="Ready feature"):
    """A feature that passes the REAL Ready gate: spec + AC + an existing file
    (the #110 phantom-path check runs against the bound checkout = tmp_path)."""
    (tmp_path / "target.py").write_text("x = 1\n")
    f = board.create_feature(
        title, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["target.py"]
    )
    return board.mark_ready(f["id"])


# ── label constraints (#135): every label the plugin emits, at realistic values ──


@requires_br
def test_merged_verified_stamp_fits_beads_label_cap(board):
    """`merged-verified:` + the loop's truncated sha must LAND through real `br`.
    The length rides `_MERGED_VERIFIED_SHA_LEN` — the exact knob whose absence was
    #135 (prefix + full 40-char sha = 56 > 50, VALIDATION_FAILED, stamp never
    written). Red-is-reachable: setting it back to 40 makes real `br` reject this
    write and the test FAIL (verified during development, then reverted)."""
    f = board.create_feature("Stamp target", spec="s")
    stamped = FULL_SHA[:_MERGED_VERIFIED_SHA_LEN]
    got = board.record_merged_verified(f["id"], stamped)
    labels = got["labels"] or []
    assert f"{LABEL_MERGED_VERIFIED_PREFIX}{stamped}" in labels  # it LANDED
    assert all(len(label) <= BEADS_LABEL_CAP for label in labels)


@requires_br
def test_beads_label_cap_rejects_the_135_regression_shape(board):
    """The cap is REAL, not an assumption these tests encode: the exact pre-#135
    write (`merged-verified:` + full 40-char sha = 56 chars) must be REJECTED by
    real `br` with the validator's named reason — proving the fitting-label tests
    above aren't vacuously green, and pinning the failure shape (#116's cousin:
    a structured error, stderr, non-zero exit) `_run` turns into a BoardError."""
    f = board.create_feature("Cap probe", spec="s")
    with pytest.raises(BoardError, match="exceeds 50 characters"):
        board._run("update", f["id"], "--add-label", f"{LABEL_MERGED_VERIFIED_PREFIX}{FULL_SHA}")
    assert not [label for label in board.get_feature(f["id"])["labels"] or [] if label.startswith("merged-")]


@requires_br
def test_verified_full_sha_label_fits_the_cap_with_one_char_spare(board):
    """The crash-salvage record (#91) stamps `verified:` + the FULL `git rev-parse
    HEAD` sha — 49 characters, ONE under beads' cap. The tightest label the plugin
    emits: a single character added to the prefix is the next #135, so pin the
    realistic value through the real validator."""
    f = board.create_feature("Salvage target", spec="s")
    got = board.record_verified_candidate(f["id"], branch=f"feat/{f['id']}", sha=FULL_SHA, worktree="/wt")
    label = f"{LABEL_VERIFIED_PREFIX}{FULL_SHA}"
    assert len(label) == BEADS_LABEL_CAP - 1
    assert label in (got["labels"] or [])
    assert got["verified_sha"] == FULL_SHA  # and it projects back
    # the record is REPLACED, never accumulated — a second verify swaps the sha
    got = board.record_verified_candidate(f["id"], branch=f"feat/{f['id']}", sha="f" * 40, worktree="/wt")
    stamps = [label for label in got["labels"] or [] if label.startswith(LABEL_VERIFIED_PREFIX)]
    assert stamps == [f"{LABEL_VERIFIED_PREFIX}{'f' * 40}"]


@requires_br
def test_escalation_cost_and_difficulty_labels_land_at_realistic_values(board):
    """The remaining label families — `diff:` (longest difficulty), `tier:` +
    `attempt:` (the escalation ladder's writes), `gens:` (the replaced running
    total) — all through the real store methods that emit them, all through the
    real validator (charset incl. `:`, length under the cap)."""
    f = board.create_feature("Ladder target", spec="s", acceptance_criteria="a", difficulty="architectural")
    fid = f["id"]
    assert "diff:architectural" in board.get_feature(fid)["labels"]  # the longest diff: value
    # architectural starts at the TOP rung: escalate records the attempt at `opus`
    # and returns None (ladder exhausted) — the realistic worst-case label set.
    assert board.escalate(fid, "gate failed: 2 tests red — see CI") is None
    labels = board.get_feature(fid)["labels"]
    assert "tier:opus" in labels and "attempt:1" in labels
    # gens: accumulates by REPLACING the single label (ADR 0064) — 40 then +2 = one gens:42
    board.record_gens_spent(fid, 40)
    got = board.record_gens_spent(fid, 2)
    assert [label for label in got["labels"] if label.startswith(LABEL_GENS_PREFIX)] == ["gens:42"]
    assert got["gens_spent"] == 42
    assert all(len(label) <= BEADS_LABEL_CAP for label in got["labels"])


# ── `--json` output shape: one test per want_json=True call site in store.py (#138) ──


@requires_br
@pytest.mark.br_shape
def test_json_shape_show_for_get_feature(board):
    """`br show --json` (get_feature) → the bead dict the `rows[0] if isinstance(rows,
    list) else rows` sites consume: a single-element LIST on 0.1.23, and — through the
    #138 seam — either an unwrapped ``{"issues":[…]}`` list or a bare object on 0.2.16.
    The consume-pattern collapses all three to the bead, which get_feature projects. The
    assertion is on that PROJECTABLE bead (version-agnostic), not the raw list-ness of a
    single-object `show`, which 0.2.16 may legitimately return unwrapped."""
    f = board.create_feature("Show me", spec="the spec")
    rows = board._run("show", f["id"], want_json=True)
    bead = rows[0] if isinstance(rows, list) else rows  # exactly what the three call sites do
    assert isinstance(bead, dict) and bead["id"] == f["id"], f"br show --json shape changed: {type(rows).__name__}"
    got = board.get_feature(f["id"])  # and the projection consumes it end-to-end
    assert got["id"] == f["id"] and got["spec"] == "the spec" and got["board_state"] == "backlog"


@requires_br
@pytest.mark.br_shape
def test_json_shape_list_for_list_features(board):
    """`br list --type feature --status … --limit 0 --json` (list_features) must be
    a bare LIST — the call site does `or []` then iterates rows outright."""
    f = board.create_feature("List me", spec="s")
    rows = board._run(
        "list", "--type", "feature", "--status", "open", "--status", "closed", "--limit", "0", want_json=True
    )
    assert isinstance(rows, list), f"br list --json shape changed: {type(rows).__name__}"
    assert all(isinstance(r, dict) for r in rows)
    assert f["id"] in {feat["id"] for feat in board.list_features()}


@requires_br
@pytest.mark.br_shape
def test_json_shape_ready_for_claim_next_ready(board, tmp_path):
    """`br ready --label ready --limit 0 --json` (claim_next_ready) must be a bare
    LIST of dicts carrying `id` + `issue_type` — the site iterates and filters on
    those outright. Then the claim itself lands through real `br --claim`."""
    f = _ready_feature(board, tmp_path)
    rows = board._run("ready", "--label", LABEL_READY, "--limit", "0", want_json=True)
    assert isinstance(rows, list) and rows, f"br ready --json shape changed: {type(rows).__name__}"
    assert {r["id"] for r in rows} == {f["id"]} and rows[0]["issue_type"] == "feature"
    claimed = board.claim_next_ready()
    assert claimed["id"] == f["id"] and claimed["board_state"] == "in_progress"
    assert claimed["assignee"] == "test"  # --claim assigns the actor
    # the ready-gate seam also materialized the requirement ledger through real notes
    assert [i["id"] for i in claimed["requirements"]] == ["r1"]


@requires_br  # NOT @br_shape: asserts the 0.1.x `comments`-field STRUCTURE, so 0.1.23-pinned
def test_json_shape_show_for_raw_features_with_comments(board):
    """`br show --json` re-fetch in raw_features_with_comments: the RAW bead must
    carry the `comments` list (`br list` omits it — the whole reason the re-fetch
    exists), each with the `text` the retro mines. This pins a beads-INTERNAL field
    layout (not the top-level envelope #138 fixed), so it stays on the 0.1.23 leg
    rather than asserting 0.2.16's unverified comments schema."""
    f = board.create_feature("Blocked one", spec="s")
    board.flag_blocked(f["id"], "waiting on upstream: see #99 — contention")
    raw = board.raw_features_with_comments(states=("blocked",))
    assert len(raw) == 1 and raw[0]["id"] == f["id"]
    comments = raw[0].get("comments")
    assert isinstance(comments, list) and comments, "br show --json no longer carries comments"
    assert any("waiting on upstream: see #99 — contention" in c.get("text", "") for c in comments)


@requires_br  # deliberately NOT @br_shape: a 0.1.x quirk, so it runs only on the 0.1.23 leg
def test_json_shape_ready_for_ready_queue_omits_labels(board, tmp_path):
    """`br ready --json` (ready_queue) on 0.1.23 OMITS the `labels` field — the
    documented quirk the per-row `get_feature` re-fetch works around (without it
    every candidate projects as `backlog` and the puller silently never claims).
    If an upgrade starts carrying labels here, this failing is the signal the
    workaround is droppable — exactly the class #138 needed surfaced. This asserts a
    version-SPECIFIC absence, so it is intentionally excluded from the br_shape
    matrix (the 0.2.16 leg would rightly disagree); it stays on the 0.1.23 pin."""
    f = _ready_feature(board, tmp_path)
    rows = board._run("ready", "--label", LABEL_READY, "--limit", "0", want_json=True)
    assert isinstance(rows, list) and rows
    assert "labels" not in rows[0], "br ready --json now carries labels — revisit ready_queue's re-fetch"
    queue = board.ready_queue()
    assert [q["id"] for q in queue] == [f["id"]]
    assert queue[0]["board_state"] == "ready"  # the re-fetch restored the labels


@requires_br  # NOT @br_shape: asserts the 0.1.x `dependencies`-field STRUCTURE, so 0.1.23-pinned
def test_json_shape_show_for_open_blockers(board):
    """`br show --json` (_open_blockers) — the `dependencies` entries must carry
    `id`/`status`/`dependency_type`, the three keys the filter reads. A closed
    blocker stops gating (the merge-gate semantics ride exactly this shape). Like the
    comments test above, this pins a beads-INTERNAL field layout rather than the #138
    envelope, so it stays on the 0.1.23 leg (0.2.16's dep schema is unverified here)."""
    blocker = board.create_feature("Foundation", spec="s")
    dependent = board.create_feature("Dependent", spec="s")
    board.add_dependency(dependent["id"], blocker["id"])
    rows = board._run("show", dependent["id"], want_json=True)
    bead = rows[0] if isinstance(rows, list) else rows
    deps = bead.get("dependencies")
    assert isinstance(deps, list) and deps, "br show --json no longer carries dependencies"
    assert deps[0]["id"] == blocker["id"] and deps[0]["dependency_type"] == "blocks" and "status" in deps[0]
    assert board._open_blockers(dependent["id"]) == [blocker["id"]]
    assert board.get_feature(dependent["id"])["open_depends_on"] == [blocker["id"]]
    board.cancel_feature(blocker["id"], "closing the blocker")
    assert board._open_blockers(dependent["id"]) == []  # closed → no longer gates
    assert board.get_feature(dependent["id"])["depends_on"] == [blocker["id"]]  # ledger keeps history


@requires_br
@pytest.mark.br_shape
def test_json_shape_list_for_find_by_external_ref_and_record_merge(board, tmp_path):
    """`br list --limit 0 --json` (_find_by_external_ref) must be a bare LIST whose
    rows carry `external_ref` — the field the merge webhook scans for the PR url.
    Then the ONE Done edge closes through real `br close -r "merged: <url>"` — a
    reason with `:` and `/` (the #106 punctuation class, on the record_merge path)."""
    f = _ready_feature(board, tmp_path)
    board.claim(f["id"])
    pr_url = "https://github.com/protoLabsAI/projectBoard-plugin/pull/136"
    board.open_review(f["id"], pr_url=pr_url)
    rows = board._run("list", "--limit", "0", want_json=True)
    assert isinstance(rows, list), f"br list --json shape changed: {type(rows).__name__}"
    assert pr_url in {r.get("external_ref") for r in rows}
    merged = board.record_merge(pr_url=pr_url)
    assert merged is not None and merged["id"] == f["id"] and merged["board_state"] == "done"
    assert board.record_merge(pr_url="https://github.com/o/r/pull/999") is None  # another PR's webhook


# ── `close -r` punctuation (#106) + cancel_feature's half-apply guard end-to-end ──


@requires_br
def test_close_reason_with_colon_and_em_dash_succeeds(board):
    """The #106 hypothesis, settled on real `br`: a `close -r` reason carrying `:`
    and `—` closes fine (the punctuation theory was wrong — the real cause was
    contention). The cancel projects as `cancelled`, never `done` (#47)."""
    f = board.create_feature("Cancel me", spec="s")
    got = board.cancel_feature(f["id"], "duplicate of bd-2: superseded — see #106")
    assert got["board_state"] == "cancelled" and got["cancelled"] is True
    assert got["bead_status"] == "closed"


@requires_br
def test_cancel_rollback_restores_pre_cancel_state_through_real_br(board, tmp_path, monkeypatch):
    """cancel_feature's ATOMIC-OR-CLEAN guard (#106) end-to-end: the pre-close tag
    write AND the undo ride the REAL `br`; only the `br close` itself is forced to
    fail (the contention-outlasting-retries shape that caused #106 — real two-writer
    contention is inherently racy, so the fault is injected at exactly that seam).
    The bead must land back in its exact pre-cancel state: still claimable, no
    `cancelled` tag, assignee restored — never the half-cancelled zombie."""
    f = _ready_feature(board, tmp_path)
    fid = f["id"]
    assert board.claim(fid) is not None  # assignee = actor ("test")
    real_run = board._run

    def failing_close(*args, want_json=False):
        if args and args[0] == "close":
            raise BoardError("`br close` failed: DATABASE_ERROR: database is locked")
        return real_run(*args, want_json=want_json)

    monkeypatch.setattr(board, "_run", failing_close)
    with pytest.raises(BoardError, match="database is locked"):
        board.cancel_feature(fid, "scope cut: superseded — see #106")
    monkeypatch.setattr(board, "_run", real_run)  # read the REAL final state back
    got = board.get_feature(fid)
    assert got["board_state"] == "in_progress"  # not closed, not a zombie
    assert got["cancelled"] is False and "cancelled" not in (got["labels"] or [])
    assert got["assignee"] == "test"  # restored, so it stays claimable/requeue-able


@requires_br
@pytest.mark.br_shape  # the whole point is proving remove_dependency's --type fallback
# (store.py) works against WHICHEVER real br is on this leg — 0.1.23 or 0.2.16.
def test_cancel_with_an_open_blocker_drops_the_edge_through_real_br(board):
    """The actual gap that let a live cancel silently keep failing (found live,
    2026-08-14): every existing real-`br` test either cancels a feature with NO
    open blockers, or cancels the BLOCKER itself (test_json_shape_show_for_open_
    blockers, above) — never a feature that HAS an open blocker at cancel time,
    which is the one scenario that calls `remove_dependency` for real. On a real
    `br 0.2.16` install, `remove_dependency`'s `br dep remove … --type blocks`
    hard-errored ("unexpected argument '--type' found"); `cancel_feature`'s
    per-edge `except BoardError` swallowed it (logged a warning), so the blocker
    was never dropped and `br close` kept refusing — every mocked unit test for
    this path stayed green throughout, because none of them shell out to a real
    `br`. This is the regression test for that gap, not just the store.py fix."""
    blocker = board.create_feature("Foundation", spec="s")
    dependent = board.create_feature("Dependent", spec="s")
    board.add_dependency(dependent["id"], blocker["id"])
    assert board._open_blockers(dependent["id"]) == [blocker["id"]]

    got = board.cancel_feature(dependent["id"], "scope cut — no longer needed")

    assert got["board_state"] == "cancelled" and got["cancelled"] is True
    assert got["dropped_deps"] == [blocker["id"]]
    assert board._open_blockers(dependent["id"]) == []


# ── `--limit 0` = unbounded; a real cap really truncates (the exhaustiveness invariant) ──


@requires_br
@pytest.mark.br_shape
def test_limit_zero_is_unbounded_and_a_cap_truncates(board):
    """`--limit 0` is beads' documented unlimited sentinel and a positive limit
    REALLY truncates — the pair of facts the exhaustiveness invariant rests on
    (list_features / ready_queue / _find_by_external_ref all pass `--limit 0`;
    the state filter runs in Python AFTER, so a silent cap would corrupt every
    consumer). Both hold on 0.1.23 AND 0.2.16 (the seam normalizes the envelope to
    a list either way), so this is a br_shape test. Truncation *detection* rides
    `has_more` — see the dedicated test below; here `--limit 0` must never trip it."""
    fids = {board.create_feature(f"feat {i}", spec="s")["id"] for i in range(3)}
    capped = board._run("list", "--limit", "1", want_json=True)
    assert isinstance(capped, list) and len(capped) == 1  # a cap really truncates
    unbounded = board._run("list", "--limit", "0", want_json=True)
    assert {r["id"] for r in unbounded} >= fids  # limit 0 returns everything
    assert {feat["id"] for feat in board.list_features()} == fids


@requires_br
@pytest.mark.br_shape
def test_has_more_rides_the_0_2_envelope_and_unbounded_never_truncates(board):
    """`has_more` (#138): the 0.2.x envelope carries it, 0.1.x has no envelope at all,
    so `_run` stashes it as a bool on 0.2.16 and as None on 0.1.23 — the SHAPE-presence
    signal `list_features` guards truncation on (never a version sniff). A real cap with
    rows to spare reports more on 0.2.16 (True) / nothing on 0.1.23 (None); the unbounded
    `--limit 0` the projection actually issues must report has_more=false on 0.2.16 (never
    None-vs-True ambiguity → no BoardError) and None on 0.1.23. Either way list_features
    returns the whole board without raising — the assertion the exhaustiveness invariant
    now IS, rather than merely assumes."""
    fids = {board.create_feature(f"feat {i}", spec="s")["id"] for i in range(3)}
    board._run("list", "--limit", "1", want_json=True)  # a real cap, 2 rows to spare
    assert board._last_json_has_more in (True, None), (
        "a capped query with rows to spare must report has_more=true on the 0.2.x "
        f"envelope (None on 0.1.x, no envelope) — got {board._last_json_has_more!r}"
    )
    board._run("list", "--limit", "0", want_json=True)  # the unbounded query the projection uses
    assert board._last_json_has_more in (False, None)  # never True → the truncation guard stays quiet
    feats = board.list_features()  # so the projection completes without a BoardError…
    assert {f["id"] for f in feats} == fids  # …and really is the whole board


# ── create-plus-enrich as a unit (#85/#116): the two-write seam, incl. partial write ──


@requires_br
def test_create_plus_enrich_round_trips_through_real_br(board):
    """`br create` + the enrichment `br update` as ONE unit: every field `create`
    can't take (acceptance-criteria/design/notes/labels) lands via the follow-up
    update — including an AC that STARTS with `-` (the #85 `--flag=value` hardening,
    which only a real arg parser can validate) — and projects back intact."""
    f = board.create_feature(
        "Enriched",
        spec="the spec",
        acceptance_criteria="- WHEN x THE SYSTEM SHALL y\n- second: with punctuation — dashes",
        design="per ADR 0064",
        files_to_modify=["src/a.py", "src/b.py (new)"],
        difficulty="medium",
        source_issue="protoLabsAI/projectBoard-plugin#136",
    )
    assert not f.get("enrichment_failed")
    got = board.get_feature(f["id"])
    assert got["spec"] == "the spec"
    assert got["acceptance_criteria"] == "- WHEN x THE SYSTEM SHALL y\n- second: with punctuation — dashes"
    assert got["design"] == "per ADR 0064"
    assert got["files_to_modify"] == ["src/a.py", "src/b.py (new)"]  # notes round-trip
    assert got["difficulty"] == "medium"
    assert got["source_issue"] == "protoLabsAI/projectBoard-plugin#136"  # metadata line survives


@requires_br
def test_create_enrichment_failure_leaves_repairable_bead(board, monkeypatch):
    """The partial-write path (#116/#85 repair contract) with a REAL created bead:
    the enrichment `br update` fails (injected at that seam — the #116 shape), the
    create must surface success-with-warning naming EXACTLY the missing fields, and
    the bead must really exist on the board, repairable IN PLACE via update_feature
    — never an orphan hidden behind an error."""
    real_run = board._run

    def failing_update(*args, want_json=False):
        if args and args[0] == "update":
            raise BoardError("`br update` failed: DATABASE_ERROR: database is locked")
        return real_run(*args, want_json=want_json)

    monkeypatch.setattr(board, "_run", failing_update)
    f = board.create_feature("Half written", spec="s", acceptance_criteria="- must do x", difficulty="small")
    assert f["enrichment_failed"] is True
    assert set(f["missing_fields"]) == {"acceptance_criteria", "difficulty"}
    monkeypatch.setattr(board, "_run", real_run)
    got = board.get_feature(f["id"])  # the bead EXISTS on the real board…
    assert got["spec"] == "s"  # …the create half landed…
    assert got["acceptance_criteria"] == "" and got["difficulty"] == ""  # …the enrich half didn't
    repaired = board.update_feature(f["id"], acceptance_criteria="- must do x", difficulty="small")
    assert repaired["acceptance_criteria"] == "- must do x" and repaired["difficulty"] == "small"

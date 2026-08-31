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
this tier pins 0.1.x-SPECIFIC quirks (the 50-char label cap, contention reported as JSON
on a zero exit) whose whole value is that they're version-specific; running them on 0.2.16
would test beads, not the plugin's shape handling. The ``br ready --json`` labels contract
is NOT one of them anymore (#324): ready_queue now re-fetches labels only when the row
lacks them, so its test is version-AWARE and rides the ``@br_shape`` matrix — the 0.1.23
leg exercises the label-less path, the newer legs the label-carrying one.
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


def _ready_task(board, *, assignee, title="Ready task"):
    """A task-type bead (#217) that passes the REAL Ready gate — spec + AC, NO files (a
    task ships a deliverable, not repo edits) — pre-assigned to a dispatch target so the
    claim path sees a bead already assigned to someone other than the actor."""
    f = board.create_feature(
        title,
        spec="do the task",
        acceptance_criteria="- WHEN done THE SYSTEM SHALL deliver",
        issue_type="task",
        assignee=assignee,
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


@requires_br
def test_budget_labels_land_replace_and_clear_through_real_br(board):
    """The fix-budget family (#259) — `budget:<kind>:<n>` is the only plugin label
    with TWO colons, so it must be proven through the real validator, not the fake
    `_run` (the exact class #135 hid). Land, replace (never accumulate), project
    back, named-kind clear, full clear — all via the real store methods."""
    f = board.create_feature("Budget target", spec="s")
    fid = f["id"]
    got = board.record_budget(fid, "merged-verify", 1)  # the longest kind name
    assert "budget:merged-verify:1" in (got["labels"] or [])
    assert got["budgets"] == {"merged-verify": 1}  # and it projects back
    got = board.record_budget(fid, "merged-verify", 2)  # replaced — never two of one kind
    assert [label for label in got["labels"] if label.startswith("budget:")] == ["budget:merged-verify:2"]
    board.record_budget(fid, "ci-fix", 1)
    got = board.clear_budgets(fid, ["ci-fix"])  # a climb's named-kind clear leaves the rest
    assert got["budgets"] == {"merged-verify": 2}
    got = board.clear_budgets(fid)  # the merge edge's full clear
    assert got["budgets"] == {}
    assert all(len(label) <= BEADS_LABEL_CAP for label in board.get_feature(fid)["labels"] or [])


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


@requires_br  # NOT @br_shape: pins the repeatable `--type` projection behavior on the
# 0.1.23 full-suite gate — the leg that runs the whole tier — rather than asserting
# 0.2.16's repeated-`--type` semantics, which the 0.2.16 (`-m br_shape`) leg never runs.
def test_mixed_type_projection_includes_tasks_excludes_structural_beads(board):
    """#303 (r1/r2/r5): the board projection spans coding features AND task-type beads
    (both ride the same rails: ready → claim → in_progress → in_review) but never the
    STRUCTURAL epic/milestone beads — proven against the REAL ``br list --type feature
    --type task`` repeatable-arg query, the class the fake ``_run`` tier is blind to. A
    `feature`-only query dropped every task from ``list_features`` / board_list / GET
    /features; red-is-reachable — restoring a single `--type feature` query makes the
    task assertion below FAIL against real `br`."""
    feat = board.create_feature("Coding feature", spec="s")
    task = board.create_feature("Task bead", spec="s", issue_type="task")
    epic = board.create_epic("An epic")
    milestone = board.create_milestone("A milestone", epic["id"])

    projected = {f["id"]: f for f in board.list_features()}
    assert feat["id"] in projected and task["id"] in projected  # feature + task are IN…
    assert epic["id"] not in projected  # …structural beads are OUT
    assert milestone["id"] not in projected
    assert projected[task["id"]]["issue_type"] == "task"  # and the task projects as a task


@requires_br
def test_task_claim_preserves_assignee_where_br_claim_refuses(board):
    """#356 (r8): a ready TASK pre-assigned to a non-actor dispatch target is REFUSED by the
    atomic ``br update --claim`` — it reassigns to the actor and rejects an already-assigned
    bead, the forever-``claim-race`` livelock this fix ends — while the new ``claim_task``
    edge reaches ``in_progress`` WITHOUT changing the assignee. Proven on real ``br``: the
    fake ``_run`` tier has no ``--claim`` CAS, so this class is exactly what it is blind to.
    Red-reachable: routing the task through the actor-assigning ``claim`` makes the
    assignee-preservation assertion FAIL (it lands ``test``, not ``agent-bot``)."""
    # 1) reproduce the refusal — the actor-reassigning --claim cannot claim a target-assigned task
    probe = _ready_task(board, assignee="agent-bot", title="Probe")
    with pytest.raises(BoardError):
        board._run("update", probe["id"], "--claim")  # already assigned to agent-bot → real br refuses
    assert board.get_feature(probe["id"])["assignee"] == "agent-bot"  # untouched by the refusal
    assert board.get_feature(probe["id"])["board_state"] == "ready"  # still ready (the livelock: retried forever)

    # 2) the new edge reaches in_progress and PRESERVES the dispatch target
    task = _ready_task(board, assignee="agent-bot", title="Real")
    claimed = board.claim_task(task["id"], assignee="agent-bot")
    assert claimed is not None
    assert claimed["board_state"] == "in_progress"  # transitioned off ready…
    assert claimed["assignee"] == "agent-bot"  # …WITHOUT reassigning to the actor
    assert LABEL_READY not in (claimed["labels"] or [])  # the ready label was dropped
    # (a coding feature's atomic --claim actor-assignment is exercised on real `br` by
    #  test_json_shape_ready_for_claim_next_ready / _find_by_external_ref, unchanged by r2.)


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


@requires_br
@pytest.mark.br_shape  # version-AWARE now (#324): it adapts to whichever `br ready --json`
# shape the installed binary returns, so it belongs on the normal cross-version gate — the
# 0.1.23 leg exercises the label-LESS path, the 0.2.16/0.3.2 legs the label-CARRYING one.
def test_json_shape_ready_for_ready_queue_omits_labels(board, tmp_path, monkeypatch):
    """`br ready --json` (ready_queue) SHAPE contract, across br versions (#324).
    beads-rust ≤0.1.23 OMITS the `labels` field from `br ready --json` rows, so
    ready_queue re-fetches via a batched `br show` (which carries labels) — else every
    candidate projects as `backlog` and the puller silently never claims. A newer `br`
    CARRIES labels on the ready row itself, so that re-fetch is REDUNDANT and must be
    skipped. This characterizes BOTH shapes without asserting an obsolete universal
    absence: whichever the real binary returns, the queue must project the candidate as
    `ready`, AND it must re-fetch iff (and only iff) the row lacked labels.

    Red-reachable both ways: on the label-less leg, dropping the fallback show makes the
    `board_state == "ready"` assertion FAIL; on a label-carrying leg, dropping the
    capability guard re-issues the show and the `shows == []` assertion FAILS. With ONE
    ready bead the label-less path also rides 0.1.x's OTHER quirk — a single-id `br show
    --json` is a bare dict, which ready_queue must fold back."""
    f = _ready_feature(board, tmp_path)
    rows = board._run("ready", "--label", LABEL_READY, "--limit", "0", want_json=True)
    assert isinstance(rows, list) and rows
    row_carries_labels = "labels" in rows[0]  # the version-dependent fact — not asserted either way

    # Count the batched `br show` re-fetches ready_queue issues, without disturbing behavior.
    real_run = board._run
    shows: list[tuple] = []

    def counting_run(*args, want_json=False, with_has_more=False):
        if args and args[0] == "show":
            shows.append(args)
        return real_run(*args, want_json=want_json, with_has_more=with_has_more)

    monkeypatch.setattr(board, "_run", counting_run)
    queue = board.ready_queue()
    monkeypatch.setattr(board, "_run", real_run)

    assert [q["id"] for q in queue] == [f["id"]]
    assert queue[0]["board_state"] == "ready"  # projected as ready on BOTH shapes
    if row_carries_labels:
        assert shows == [], "br ready carried labels — the redundant batched show must be skipped (#324)"
    else:
        assert shows == [("show", f["id"])], "label-less br ready rows still need exactly one batched show"


@requires_br
@pytest.mark.br_shape
def test_json_shape_batched_show_for_ready_queue(board, tmp_path):
    """The MULTI-id `br show --json` contract ready_queue's label-less re-fetch rides
    (#257): it must return a LIST of dicts carrying `id` + `labels` on BOTH matrix legs
    — the same batched-show shape list_features uses for dependencies, and the shape the
    label-less (≤0.1.23) `br ready` fallback folds back into `ready`. Whether the
    installed `br` re-fetches (label-less rows) or projects the ready rows directly
    (label-carrying rows, #324), the queue must project every candidate as `ready`."""
    f1 = _ready_feature(board, tmp_path, title="Ready one")
    # A second ready feature naming a DIFFERENT file — the same target would trip
    # the #143 overlapping-worktree gate at mark_ready.
    (tmp_path / "other.py").write_text("y = 2\n")
    f2 = board.create_feature(
        "Ready two", spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["other.py"]
    )
    f2 = board.mark_ready(f2["id"])
    rows = board._run("show", f1["id"], f2["id"], want_json=True)
    assert isinstance(rows, list) and len(rows) == 2, f"multi-id br show --json shape changed: {type(rows).__name__}"
    assert all(isinstance(r, dict) and r.get("id") and "labels" in r for r in rows)
    queue = board.ready_queue()
    assert {q["id"] for q in queue} == {f1["id"], f2["id"]}
    assert all(q["board_state"] == "ready" for q in queue)


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
    so `_run --json … with_has_more` returns it as a bool on 0.2.16 and as None on 0.1.23
    — the SHAPE-presence signal `list_features` guards truncation on (never a version
    sniff), riding each call's return value rather than shared state (#258). A real cap
    with rows to spare reports more on 0.2.16 (True) / nothing on 0.1.23 (None); the
    unbounded `--limit 0` the projection actually issues must report has_more=false on
    0.2.16 (never None-vs-True ambiguity → no BoardError) and None on 0.1.23. Either way
    list_features returns the whole board without raising — the assertion the
    exhaustiveness invariant now IS, rather than merely assumes."""
    fids = {board.create_feature(f"feat {i}", spec="s")["id"] for i in range(3)}
    # a real cap, 2 rows to spare
    _rows, has_more = board._run("list", "--limit", "1", want_json=True, with_has_more=True)
    assert has_more in (True, None), (
        "a capped query with rows to spare must report has_more=true on the 0.2.x "
        f"envelope (None on 0.1.x, no envelope) — got {has_more!r}"
    )
    # the unbounded query the projection uses
    _rows, has_more = board._run("list", "--limit", "0", want_json=True, with_has_more=True)
    assert has_more in (False, None)  # never True → the truncation guard stays quiet
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


@requires_br
def test_review_clean_sha_label_fits_the_beads_label_cap(board):
    """The #135 shape, one more time. `review-clean-sha:` is a 17-character prefix, so a
    FULL 40-char sha makes 57 — seven over beads' 50-char cap — and `br` refuses the whole
    update. That is not a degraded pin: the write FAILS and the card blocks. It shipped
    that way and blocked a live card within the hour, because the unit tests all use a
    fake `br` that has no validator.

    So pin the realistic value through the REAL one, exactly as `verified:` and
    `merged-verified:` already do."""
    f = board.create_feature("Pinned verdict", spec="s")
    got = board.set_review_substate(f["id"], store_mod.LABEL_REVIEW_CLEAN, head_sha=FULL_SHA)
    pin = next(l for l in (got["labels"] or []) if l.startswith(store_mod.LABEL_REVIEW_CLEAN_SHA_PREFIX))
    assert len(pin) <= BEADS_LABEL_CAP
    assert pin == f"{store_mod.LABEL_REVIEW_CLEAN_SHA_PREFIX}{FULL_SHA[: store_mod.SHORT_SHA_LEN]}"
    # …and the merge gate matches that short pin against a FULL live head
    row = {"id": f["id"], "board_state": "in_review", "labels": ["in-review", "review-clean", pin]}
    assert store_mod.merge_posture(row, auto_merge=True, review_gate=True, head_sha=FULL_SHA)["blockers"] == []
    moved = store_mod.merge_posture(row, auto_merge=True, review_gate=True, head_sha="b" * 40)["blockers"]
    assert any("review-clean verdict is for" in b for b in moved)

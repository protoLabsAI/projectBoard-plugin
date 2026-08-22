"""Board store — a thin wrapper over **beads** (`br`), the DAG/status authority (D8).

The board is no longer a separate SQLite store; it's a **projection over beads**
(`.beads/*.db` + git-committed JSONL) — so there is nothing to drift out of sync
with the work graph (the 82-phantom class is structurally impossible). Each feature
is a `br` issue; the 6-state board is a projection of `br` status + labels:

    backlog       status=open      (no `ready` label)
    ready         status=open      + label `ready`     (gate: spec + acceptance_criteria)
    in_progress   status=in_progress
    in_review     status=in_progress + label `in-review` (+ external_ref = pr_url)
    done          status=closed
    blocked       (flag) label `blocked` (+ a comment with the reason)

Hierarchy is `br` issue types (epic → milestone → feature) linked by parent-child
deps; the DAG is `blocks` edges. `br ready` is the puller's unblocked queue — and
because a dependent leaves `ready` until its blocker is **closed**, the foundation
**merge-gate** falls out for free (only the merge webhook closes a bead → done).
Escalation rides as labels (`diff:`, `tier:`, `attempt:`).

Two invariants live here, as before:
  1. **Ready gate** — `mark_ready` adds the `ready` label only if the bead carries a
     description (spec) + acceptance_criteria.
  2. **One Done edge** — only `record_merge` (the webhook) runs `br close`.

Notes on `br` quirks pinned down empirically (br 0.1.x):
  - `br ready --type X --label Y` returns nothing (filter AND bug); use `--label`
    alone and filter `issue_type` in Python.
  - parent-child deps do NOT block `br ready` (epics can stay open); `blocks` do.
  - create takes `--description` but NOT `--acceptance-criteria`/`--design`; set
    those with a follow-up `br update`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

from . import _TERMINAL_STATES

log = logging.getLogger("protoagent.plugins.project_board")

BR = os.environ.get("BR_BIN", "br")

# `br` surfaces a DATABASE_ERROR (SQLite `database is locked`/`busy`) when two br
# processes write the same `.beads/*.db` concurrently (the loop + a tool call, say) —
# transient contention that clears on a short retry. Retry ONLY that class (a bad-arg
# failure is not going to fix itself) with a small exponential backoff so a create/
# update isn't lost to a lock it merely lost the race for.
#
# The window is sized for REAL WAL contention, not a token retry (#116): the loop polls
# the same DB on a 30s tick while boarding writes, so a create-plus-enrich races the
# poller by construction and a budget shorter than a WAL checkpoint keeps losing. 6
# attempts backing off 0.1 → 0.2 → 0.4 → 0.8 → 1.6 → 3.2s give a ~6.3s total window —
# enough to outlast typical write contention without holding a turn for minutes. The old
# 4-attempt, 0.7s budget lost the `bd-ud1` enrichment write, dropping its `source_issue`.
_DB_RETRY_ATTEMPTS = 6
_DB_RETRY_DELAY = 0.1  # seconds; doubles each retry (0.1 → 0.2 → 0.4 → 0.8 → 1.6 → 3.2, ~6.3s total)
_DB_CONTENTION_RE = re.compile(r"DATABASE_ERROR|database is (?:locked|busy)", re.IGNORECASE)


def _contention_in_json(out) -> str:
    """Detect DB contention that `br` wrote as structured JSON on stdout with a ZERO
    exit code (#116) — a failure shape a bare `returncode == 0` check sails right past,
    so the retry loop never fires and the write is silently lost. Parse stdout and, if
    it's an error-shaped object (an ``error`` / ``message`` / ``code`` / ``status`` /
    ``detail`` field whose text names contention), return that text so ``_run`` retries
    it exactly like a stderr DATABASE_ERROR. Returns '' for a normal payload (a list of
    beads, a bead object, empty) — only error-shaped keys are sniffed, so a legit
    `status: "open"` can never be mistaken for a lock."""
    s = str(out or "").strip()
    if not s.startswith("{"):  # normal br JSON payloads are lists ('[') or empty
        return ""
    try:
        obj = json.loads(s)
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("error", "message", "code", "status", "detail"):
        val = obj.get(key)
        if isinstance(val, str) and _DB_CONTENTION_RE.search(val):
            return val
    return ""


def _issues_envelope(parsed):
    """Normalize `br --json` across the two envelope shapes beads has shipped (#138).

    beads 0.1.x returns a BARE list for `br list`/`br ready` (``[]`` / ``[{…}, …]``) and a
    single-element list for `br show`. beads 0.2.x (≥0.2.16) wraps list payloads in an
    envelope — ``{"issues": [...], "total": N, "has_more": bool, …}``. Every call site here
    consumes the *list* (or a bare `br show` bead dict), so unwrap the envelope to its
    ``issues`` list and pass everything else through untouched::

        [ … ]                → [ … ]        (0.1.x list, unchanged)
        {"issues": [ … ], …} → [ … ]        (0.2.x envelope, unwrapped)
        {"id": "bd-1", …}    → {"id": …}    (bare `br show` bead, unchanged — no "issues" key)
        None                 → None

    Non-breaking in BOTH directions: neither a br upgrade nor a downgrade needs a code
    change, so the move stays reversible (the whole point — #138 was rolled back live in
    ~30s when the raw envelope reached iterators that expected a list)."""
    if isinstance(parsed, dict) and "issues" in parsed:
        return parsed["issues"]
    return parsed


# Labels that encode board state / escalation (everything else is free-form).
LABEL_READY = "ready"
LABEL_IN_REVIEW = "in-review"
LABEL_BLOCKED = "blocked"
# A SECOND terminal edge (#47): a feature closed because it was created in error
# (bad decomposition, duplicate, scope cut) — closed like `done`, but tagged so the
# projection shows a distinct `cancelled` state and reconcilers/retro never mistake it
# for shipped work. Preserves the one-Done-edge invariant (only record_merge → `done`).
LABEL_CANCELLED = "cancelled"
# Archival (#115): a terminal feature (done/cancelled — `_TERMINAL_STATES`, shared
# with the tool-boundary dedup) whose `closed_at` has aged past the archive window is
# labeled `archived` by the loop's health sweep. It leaves the DEFAULT list_features
# projection (and so board_list + the board view) but is NEVER deleted: the bead, its
# history, and the git-backed JSONL are untouched, and `include_archived=True`
# restores the exhaustive view. Consumers whose reads must span history (board_retro)
# opt in explicitly — inheriting the default exclusion would silently turn "all time"
# into "the last archive_after_days days" (the #114 class, one layer up).
LABEL_ARCHIVED = "archived"
ARCHIVE_AFTER_DAYS_DEFAULT = 7.0
# A feature others build *on*: dependents gate on its MERGE, never its review (vs a
# non-foundation blocker, which can release dependents at in_review under dep_gate:
# review). Inert under the default dep_gate: merge (then every blocker gates on merge).
LABEL_FOUNDATION = "foundation"
# Review-gate sub-states of `in_review` (plan M5, blocking review). `review-pending`
# marks a PR whose adversarial review is running (or was interrupted — the PR
# reconcile finishes it); `changes-requested` marks a feature bounced back to the
# coder with findings (it rides through the requeue so the board shows WHY the
# feature went back). Both are inert when the review gate is off.
LABEL_REVIEW_PENDING = "review-pending"
LABEL_CHANGES_REQUESTED = "changes-requested"
# `review-clean` is the POSITIVE record that the gate ran and found nothing blocking
# — the auto-merge edge requires it when the gate is on. Its absence is NOT proof of
# a review (an inert/unrunnable gate also clears review-pending, lapsing to advisory,
# projectBoard#181), which is exactly why the merge edge can't key off "no
# review-pending". Swapped with the other two by set_review_substate; any requeue
# re-enters via review-pending, which drops it.
LABEL_REVIEW_CLEAN = "review-clean"
# `merge-hold`: the operator's per-card veto on the auto-merge edge — a green,
# verified, reviewed PR the operator still wants to QA by hand (a console layout
# change, a risky migration). The loop never sets or clears it.
LABEL_MERGE_HOLD = "merge-hold"
# Pre-ready DESIGN state (plan M6, optional): a large/architectural feature parked
# while its design/due-diligence is worked out (`mark_designing`). Informational for
# the projection/console — the HARD gate is in `mark_ready` (a design referencing an
# ADR is required at that size before the feature can go ready).
LABEL_DESIGNING = "designing"
# Which PROJECT a feature belongs to (#90): a `project:<name>` label naming the entry
# in the board's `projects:` map (projects.py) that owns this feature — so one board
# instance can serve multiple repos, the Ready gate validating each feature's paths
# against ITS project's repo, not the instance default. Stamped at create time from
# the `project` param (default = the board's `default_project`). A label, not a notes
# line: project names are alphanumeric/hyphen, safe for beads' label validator (vs the
# `/`/`#` in a source-issue, which had to move off-label, see NOTES_SOURCE_PREFIX).
LABEL_PROJECT_PREFIX = "project:"
# A project name must round-trip cleanly through a beads label — alphanumeric plus
# hyphen/underscore (colon is the label separator, so it's excluded). A name with any
# other character is rejected at create time with a named error rather than stamping a
# label beads' validator would reject (VALIDATION_FAILED) after the bead already exists.
_PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Difficulties whose blast radius demands a written design + an ADR reference before
# the feature may go ready (the M6 DESIGN gate in `mark_ready`).
DESIGN_GATED_DIFFICULTIES = ("large", "architectural")
# What counts as "references an ADR": `ADR 0076` / `ADR-76` / `adr/0076` /
# a `docs/adr/0076-…` path — case-insensitive, number required.
ADR_REF_RE = re.compile(r"(?i)\badr[\s/_-]{0,2}\d{1,4}\b|docs/adr/\d{4}-")
# Breadth cap (#143): the most files_to_modify a card of a given difficulty may name
# before the Ready gate refuses it. A card wider than this times out before it lands —
# and a timeout teaches nothing (the single worst failure mode: it burns the tiers with
# zero diff). So the gate forces the author to SPLIT the work or re-declare the card
# `large` (which then owes the design + ADR the DESIGN gate enforces). `large`/
# `architectural` carry NO cap (absent from this dict) — they answer to the design gate,
# not the breadth cap. Configurable via the `max_files_by_difficulty` config key (threaded
# to BeadsBoard through store_kw, beside repo/base_branch); a None override keeps this default.
MAX_FILES_BY_DIFFICULTY = {"small": 4, "medium": 4}
# Cumulative generations `coder.solve()` has spent on this feature (ADR 0064 P2 board
# seam) — `gens:<total>`, replaced (not accumulated as separate labels) each time so a
# single label always carries the running total for `portfolio_rollup` to read.
LABEL_GENS_PREFIX = "gens:"
# Crash-salvage record (#91) — `verified:<sha>`, replaced (never accumulated) each time
# coder.solve()'s verify boundary promotes a test-PASSING candidate. Written on the bead
# (not loop memory) so it survives a crash between verify and open_pr; recovery's no-PR
# path checks it and resumes at promote→fixups→gate→open_pr instead of rebuilding fresh.
# The branch/worktree are the CANONICAL `feat/<id>` / `feat-<id>` names (the record is
# written post-promote), so the sha is the only piece that must ride the label; the full
# {branch, sha, worktree} triple lands in a comment for the audit trail.
LABEL_VERIFIED_PREFIX = "verified:"
# In-review VERDICT currency (#131) — `merged-verified:<sha>`, replaced (never
# accumulated) each time the merge-poll reconciler re-runs the gate against the
# MERGED state (branch tip + current origin/<base>) after base moved under an
# `in_review` PR. The sha is the origin/<base> commit the verdict was verified
# against — the ONE field an adjudicator checks: label sha == current
# origin/<base> ⇒ the gate ran on the state that will actually land; anything
# else ⇒ the verdict is stale (unverified, not broken — staleness alone never
# blocks; only a gate FAILURE on the merged state does).
LABEL_MERGED_VERIFIED_PREFIX = "merged-verified:"
# The ORIGINATING GitHub issue (#97) — a structured `source-issue: owner/repo#N`
# metadata line in the bead `notes` field, beside the files_to_modify path lines.
# NOT a label: beads' label validator only allows alphanumeric/hyphen/underscore/
# colon, so the original `source:owner/repo#N` label died with VALIDATION_FAILED
# on every real write (#101) — `/` and `#` can never ride a label. Set through
# create/update's `source_issue`, projected back as the `source_issue` field the
# loop's PR opener reads to stamp `Fixes #N` (same-repo) / `Refs <url>`
# (cross-repo) on the PR body; absent, the opener falls back to scanning the
# feature text for an issue URL.
NOTES_SOURCE_PREFIX = "source-issue:"
# What `source_issue` accepts: a full GitHub issue URL, or the `owner/repo#N`
# shorthand it normalizes to. Anything else (a bare number, a PR url, free text) is
# rejected with a named error — the field is explicit provenance, so a value that
# can't name ONE exact issue must fail loudly, not store junk the PR opener would
# silently drop.
_SOURCE_ISSUE_URL_RE = re.compile(r"https://github\.com/([^/\s#]+)/([^/\s#]+)/issues/(\d+)/?")
_SOURCE_ISSUE_SLUG_RE = re.compile(r"[^/\s#]+/[^/\s#]+#\d+")
# The requirement LEDGER (#113): acceptance criteria decomposed into tracked items —
# `{id, text, status, decline_reason?}`, status ∈ open|done|declined — one JSON line
# per item in the bead `notes` (`req: {...}`), beside the files_to_modify paths and
# the `source-issue:` metadata line. Prose AC stays the authoring interface; the
# board decomposes it at `mark_ready` (the same seam as the DESIGN gate), and the
# loop's completion gate reads the ledger back — so partial completion is
# distinguishable from completion (a coder satisfying two of five requirements no
# longer produces the same board state as one satisfying five).
NOTES_REQ_PREFIX = "req:"
REQ_CLOSED_STATUSES = ("done", "declined")
# A markdown bullet (-/*/+ or `1.`/`1)`) opens a new requirement item; anything else
# is a continuation of the current one (or, with no bullets at all, plain prose = ONE item).
_AC_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def _decompose_ac(text) -> list[dict]:
    """Split acceptance-criteria prose into requirement items (#113): each markdown
    bullet becomes one `{id, text, status: "open"}` item (a non-bullet line continues
    the item above it); prose with no bullets at all is a SINGLE item. Ids are stable
    positional `r1..rN` — the coder reports dispositions against them and the
    completion gate reads them back. Empty/blank prose → no items (no ledger)."""
    s = str(text or "").strip()
    if not s:
        return []
    texts: list[str] = []
    for line in s.splitlines():
        m = _AC_BULLET_RE.match(line)
        if m:
            if m.group(1).strip():
                texts.append(m.group(1).strip())
        elif texts and line.strip():
            texts[-1] += " " + line.strip()
    if not texts:
        texts = [" ".join(s.split())]
    return [{"id": f"r{i + 1}", "text": t, "status": "open"} for i, t in enumerate(texts)]


def _all_items_disposed(items) -> bool:
    """True only when EVERY requirement item is closed (`done` or `declined`) — the
    completion-gate predicate (#113): silence/`open` is not disposition, so one open
    item means the feature may not reach in_review. Vacuously True with no items
    (no ledger → nothing gates)."""
    return all(str(i.get("status", "")).strip().lower() in REQ_CLOSED_STATUSES for i in items or ())


def apply_requirement_dispositions(items, dispositions) -> list[dict]:
    """Merge one coder round's per-item dispositions into the requirement ledger.

    ``dispositions`` is what the loop parsed from the coder's ``## Requirements``
    section: dicts of `{id, status, decline_reason?}`. Only the CLOSED statuses are
    applied (`done`/`declined` — a coder can't re-open an item, and silence leaves an
    item untouched: silence is NOT disposition, #113). Unknown ids are ignored — the
    ledger's item set is fixed at decomposition, a reply can't invent rows. A decline
    keeps its reason (the first-class "won't do, and here's why" record); a `done`
    clears any stale one. Returns a NEW list; the inputs are never mutated."""
    out = [dict(i) for i in items or ()]
    by_id = {str(i.get("id", "")): i for i in out}
    for d in dispositions or ():
        item = by_id.get(str(d.get("id", "")).strip())
        if item is None:
            continue
        status = str(d.get("status", "")).strip().lower()
        if status not in REQ_CLOSED_STATUSES:
            continue
        item["status"] = status
        reason = str(d.get("decline_reason") or "").strip()
        if status == "declined":
            if reason:
                item["decline_reason"] = reason
        else:
            item.pop("decline_reason", None)
    return out


# difficulty → initial model tier (the escalation ladder's first rung, D10).
DIFFICULTY_TIER = {"small": "smart", "medium": "reasoning", "large": "reasoning", "architectural": "opus"}
TIER_LADDER = ["smart", "reasoning", "opus"]

# A plan-item `depends_on` entry that is a plain integer is a 0-based INDEX into the
# plan. STRICT — a single optional leading '-' only. The old `lstrip('-').isdigit()`
# guard also accepted multi-dash junk like '--5' (lstrip strips BOTH dashes → '5')
# and then crashed `int('--5')` with an uncaught ValueError, taking the whole batch
# down (#92). Gating int() on this pattern keeps a malformed ref from ever reaching
# int(); a still-numeric-looking miss is named as malformed for that item alone.
_PLAN_INDEX_RE = re.compile(r"-?\d+")


def _norm_plan_title(t) -> str:
    """Normalize a title for plan-internal dep matching (trim, lowercase, collapse
    internal whitespace) — the same normalization the tool-boundary dedup uses."""
    return " ".join(str(t or "").strip().lower().split())


def _plan_item_title(item) -> str:
    """The raw title of a plan item, or '' — safe on a non-dict item (used only to
    label a malformed item in the failure report)."""
    return str(item.get("title") or "") if isinstance(item, dict) else ""


def _plan_files(val) -> list[str]:
    """Normalize a plan item's `files` (a list of paths, or a comma/newline string)
    to a clean list — a bare string must NOT reach create_feature, which iterates it
    (char-by-char for a str)."""
    if isinstance(val, str):
        return [x.strip() for x in val.replace("\n", ",").split(",") if x.strip()]
    return [str(p).strip() for p in (val or ()) if str(p).strip()]


def _plan_deps(val) -> list:
    """Normalize a plan item's `depends_on` (a list, or a comma/newline string) to a
    clean list — integer entries (plan indices) are preserved as ints; strings are
    trimmed. (bool is dropped: it's an int subclass but never a valid index/id.)"""
    if isinstance(val, str):
        return [x.strip() for x in val.replace("\n", ",").split(",") if x.strip()]
    out: list = []
    for v in val or ():
        if isinstance(v, bool):
            continue
        out.append(v if isinstance(v, int) else str(v).strip())
    return [d for d in out if d != ""]


class BoardError(Exception):
    """A rejected op (bad gate, unknown feature, `br` failure). Caller → 4xx / tool error."""


def normalize_source_issue(raw) -> str:
    """Normalize a source-issue reference to the canonical ``owner/repo#N``.

    Accepts a full GitHub issue URL (``https://github.com/owner/repo/issues/123``)
    or the ``owner/repo#N`` shorthand (returned unchanged). Anything else raises a
    named BoardError so the caller rejects just this field/item — never storing a
    value the PR opener can't resolve to one exact issue."""
    s = str(raw or "").strip()
    m = _SOURCE_ISSUE_URL_RE.fullmatch(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
    if _SOURCE_ISSUE_SLUG_RE.fullmatch(s):
        return s
    raise BoardError(
        f"invalid source_issue {raw!r} — expected a GitHub issue URL "
        "(https://github.com/owner/repo/issues/N) or owner/repo#N"
    )


def normalize_project(raw) -> str:
    """Normalize + validate a project name for the `project:<name>` label (#90).

    Trims and requires alphanumeric/hyphen/underscore (see ``_PROJECT_NAME_RE``) so the
    name round-trips cleanly through beads' label validator. Empty input returns ""
    (no project stamped — the caller falls back to the board's default); a non-empty
    value with an illegal character raises a named BoardError BEFORE the bead is minted,
    so an invalid project can never leave an orphan bead behind a VALIDATION_FAILED."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if not _PROJECT_NAME_RE.fullmatch(s):
        raise BoardError(
            f"invalid project {raw!r} — a project name must be alphanumeric with hyphens/"
            "underscores (safe for a beads label); rename the project in project_board.projects."
        )
    return s


def _render_notes(files, source_issue: str = "", requirements=()) -> str:
    """Serialize the bead `notes` field: one files_to_modify path per line, one
    `req: {…json…}` requirement-item line per ledger entry (#113), plus a trailing
    `source-issue: owner/repo#N` metadata line when set — the single shared home
    for all three (labels can't carry `/`/`#`, see NOTES_SOURCE_PREFIX; the ledger
    rides the same structured-lines path to avoid a schema migration)."""
    lines = [str(p).strip() for p in files or () if str(p).strip()]
    for item in requirements or ():
        lines.append(f"{NOTES_REQ_PREFIX} {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    if source_issue:
        lines.append(f"{NOTES_SOURCE_PREFIX} {source_issue}")
    return "\n".join(lines)


def _split_notes(notes) -> tuple[list[str], str, list[dict]]:
    """Parse the bead `notes` field back into ``(files_to_modify, source_issue,
    requirements)`` — the inverse of ``_render_notes``. Any non-blank line that isn't
    a `req:` item line or the `source-issue:` metadata line is a file path; the FIRST
    metadata line wins (the field is single-valued — the replaced-label convention,
    kept). A malformed `req:` line is dropped, never mistaken for a file path (it
    would otherwise poison files_to_modify and the ready gate's path check, #110)."""
    files: list[str] = []
    src = ""
    reqs: list[dict] = []
    for line in str(notes or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(NOTES_SOURCE_PREFIX):
            src = src or s[len(NOTES_SOURCE_PREFIX) :].strip()
            continue
        if s.startswith(NOTES_REQ_PREFIX):
            try:
                item = json.loads(s[len(NOTES_REQ_PREFIX) :].strip())
            except ValueError:
                continue
            if isinstance(item, dict) and str(item.get("id", "")).strip():
                reqs.append(item)
            continue
        files.append(s)
    return files, src, reqs


def _parse_closed_at(raw) -> float | None:
    """A bead ``closed_at`` → epoch seconds, or None when absent/unparseable. The
    archive pass treats None as NOT archivable — a terminal feature with a missing or
    mangled timestamp stays visible rather than vanishing on a guess (fail visible:
    archival must never be trigger-happy, #115). Accepts the ISO-8601 forms `br`
    emits (``Z`` or an explicit offset; a naive stamp is taken as UTC) plus a bare
    epoch number."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _complete(coro):
    """Run an async worktree coroutine to completion from this synchronous module.

    The store is sync (it shells ``br``); the CI probe (``worktree.pr_ci_status``)
    is async. The tools and API handlers reach the store from plain threads, where
    ``asyncio.run`` is correct — but if a caller ever invokes us from INSIDE a
    running event loop's thread (where ``asyncio.run`` raises RuntimeError), hop to
    a private thread with its own loop instead of failing or deadlocking."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class BeadsBoard:
    """Wraps the `br` CLI. One process-wide instance (the loop, API, and tools share
    it). `br` auto-discovers `.beads/*.db`; pass ``db`` to pin a workspace."""

    def __init__(
        self,
        db: str | None = None,
        actor: str = "agent",
        repo: str = ".",
        base_branch: str = "main",
        max_files_by_difficulty: dict | None = None,
        projects: dict | None = None,
        default_project: str = "",
    ):
        if not shutil.which(BR):
            raise BoardError(
                f"beads CLI {BR!r} not on PATH — install beads-rust (`cargo install beads_rust`), "
                "not the homebrew `bd`, or set BR_BIN"
            )
        self.db = db or None
        self.actor = actor
        self.repo = repo
        self.base_branch = base_branch
        # Per-project resolution (#90): the board's `projects:` map (name → settings,
        # resolved by projects.resolve_projects) so a single instance can serve several
        # repos. Copied so a caller's dict can't mutate it under us. `default_project` is
        # the project a feature is stamped with when create_feature names none; absent a
        # wired map (the host-free default, and every pre-#90 caller) it's "", so the
        # store keeps its single-repo behavior — every feature resolves to `self.repo`.
        self.projects = dict(projects or {})
        # A wired map with a single entry has an obvious default even if none was named;
        # a multi-project map with no default leaves it "" (create_feature then requires
        # an explicit project, or stamps nothing).
        self.default_project = str(default_project or "").strip() or (
            next(iter(self.projects)) if len(self.projects) == 1 else ""
        )
        # Breadth-cap policy (#143): difficulty → max files_to_modify before the Ready gate
        # refuses. Threaded from the `max_files_by_difficulty` config key (store_kw); a None/
        # empty override falls back to the built-in default. Copied so a caller's dict can't
        # mutate the policy under us.
        self.max_files_by_difficulty = dict(max_files_by_difficulty or MAX_FILES_BY_DIFFICULTY)
        self._workspace_ready = False  # lazily pinned on first _run (see _ensure_workspace)
        # has_more off the last want_json `_run` (#138): the 0.2.x envelope carries it,
        # 0.1.x doesn't → None means "shape absent, trust --limit 0". Read right after the
        # `_run` that set it, before any other want_json call overwrites it.
        self._last_json_has_more = None

    # ── workspace pin (ADR 0055 P0, #48) ──────────────────────────────────────
    def _ensure_workspace(self) -> None:
        """Pin the board to THIS repo's beads workspace so `br` can't walk UP the tree
        and silently adopt a parent/ancestor `.beads/` (the cross-repo bleed of #48).

        `br` discovers `.beads/` by walking UP from cwd. With `cwd=self.repo` it stops at
        the repo's own `.beads/` when one exists — but a repo with NONE escapes to whatever
        ancestor happens to have one, polluting a shared db with the wrong id prefix. So:
        an explicit `db` is the hard pin (nothing to do); otherwise, if the repo has no
        `.beads/`, run `br init` there to give it its own — after which cwd-discovery
        resolves locally and never walks up (matches the operator's manual workaround).
        Lazy + idempotent (runs once, guarded by `_workspace_ready`)."""
        if self._workspace_ready or self.db:
            self._workspace_ready = True
            return
        repo = self.repo or "."
        if not os.path.isdir(os.path.join(repo, ".beads")):
            log.warning(
                "[project_board] repo %r has no .beads/ workspace — running `br init` to pin the "
                "board here (else `br` walks up and adopts a parent db, polluting it with the wrong "
                "id prefix; ADR 0055 isolation)",
                repo,
            )
            # NB: a direct subprocess, NOT self._run — that would recurse here, and we want
            # a precise error rather than the generic `br … failed` wrapper.
            proc = subprocess.run(
                [BR, "init", "--actor", self.actor], cwd=repo, capture_output=True, text=True, timeout=30
            )
            if proc.returncode != 0 and not os.path.isdir(os.path.join(repo, ".beads")):
                raise BoardError(
                    f"repo {repo!r} has no beads workspace and `br init` failed "
                    f"({proc.stderr.strip()[:200]}) — run `br init` there, or set project_board.db_path"
                )
        self._workspace_ready = True

    # ── br invocation ─────────────────────────────────────────────────────────
    def _run(self, *args: str, want_json: bool = False):
        self._ensure_workspace()  # pin to the repo's own .beads/ before any br op (#48)
        cmd = [BR, *args, "--actor", self.actor]
        if self.db:
            cmd += ["--db", self.db]
        if want_json:
            cmd += ["--json"]
        # Run `br` IN the configured repo so its `.beads/*.db` auto-discovery resolves
        # to THIS board's workspace, not the server's process cwd (ADR 0055 P0). With a
        # per-team-agent `repo` (or an explicit `db`), the board is deterministically
        # pinned to its repo instead of polluting whatever dir the host launched from.
        # A transient DATABASE_ERROR (SQLite contention) is retried with a short backoff;
        # any other non-zero exit raises immediately. Contention surfaces TWO ways: a
        # non-zero exit with DATABASE_ERROR on stderr, OR (br 0.1.x) a ZERO exit that
        # wrote the error as structured JSON on stdout — the latter slips past a bare
        # returncode check, so it's sniffed post-parse too (#116). The retry count is
        # logged on final success so the operator can see contention was resolved.
        delay = _DB_RETRY_DELAY
        retries = 0
        for attempt in range(_DB_RETRY_ATTEMPTS):
            proc = subprocess.run(cmd, cwd=self.repo or ".", capture_output=True, text=True, timeout=30)
            err = proc.stderr.strip()
            contention = (
                (err if _DB_CONTENTION_RE.search(err) else "")
                if proc.returncode != 0
                else _contention_in_json(proc.stdout)
            )
            if proc.returncode == 0 and not contention:
                if retries:
                    log.info(
                        "[project_board] `br %s` cleared DB contention after %d retr%s",
                        args[0] if args else "",
                        retries,
                        "y" if retries == 1 else "ies",
                    )
                break
            if contention and attempt < _DB_RETRY_ATTEMPTS - 1:
                retries += 1
                log.warning(
                    "[project_board] `br %s` hit DB contention (attempt %d/%d) — backing off %.2fs: %s",
                    args[0] if args else "",
                    attempt + 1,
                    _DB_RETRY_ATTEMPTS,
                    delay,
                    contention[:120],
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise BoardError(f"`br {' '.join(args)}` failed: {(err or contention)[:300]}")
        if not want_json:
            return proc.stdout.strip()
        # `br` prefixes some JSON with INFO log lines on stderr; stdout is clean JSON.
        out = proc.stdout.strip()
        try:
            parsed = json.loads(out) if out else None
        except json.JSONDecodeError as exc:
            raise BoardError(f"`br {args[0]}` returned non-JSON: {exc} :: {out[:200]}")
        # #138: br 0.2.x wraps list payloads in an envelope ({"issues":[…],"has_more":…})
        # where 0.1.x returned a bare list. Stash has_more off the envelope (None when the
        # shape is absent, so a truncation check guards on SHAPE, never a version sniff),
        # then normalize the payload to the list/dict every call site already consumes.
        self._last_json_has_more = parsed["has_more"] if isinstance(parsed, dict) and "has_more" in parsed else None
        return _issues_envelope(parsed)

    def _create(
        self,
        title: str,
        *,
        itype: str,
        parent: str = "",
        priority: int = 2,
        description: str = "",
        external_ref: str = "",
    ) -> str:
        args = ["create", title, "--type", itype, "-p", str(priority), "--silent"]
        if parent:
            args += ["--parent", parent]
        if description:
            args += ["--description", description]
        if external_ref:
            args += ["--external-ref", external_ref]
        fid = self._run(*args).strip()
        if not fid:
            raise BoardError(f"`br create` returned no id for {title!r}")
        return fid

    # ── hierarchy (D7: epic → milestone → feature) ────────────────────────────
    def create_epic(self, title: str, description: str = "") -> dict:
        return self.get_feature(self._create(title, itype="epic", description=description))

    def create_milestone(self, title: str, epic_id: str, description: str = "") -> dict:
        return self.get_feature(self._create(title, itype="milestone", parent=epic_id, description=description))

    def create_feature(
        self,
        title: str,
        *,
        spec: str = "",
        acceptance_criteria: str = "",
        design: str = "",
        files_to_modify=(),
        parent: str = "",
        priority: int = 2,
        difficulty: str = "",
        depends_on=(),
        foundation: bool = False,
        source_issue: str = "",
        project: str = "",
    ) -> dict:
        """Create a feature bead (starts in `backlog`). Provide a self-sufficient
        spec + acceptance_criteria + the explicit files to create/modify so it can
        pass the Ready gate (ProtoMaker's spec-quality discipline — vague tasks make
        a coder produce nothing). Mark `foundation=True` for a feature others build on
        (dependents gate on its merge, never its review). `source_issue` names the
        ORIGINATING GitHub issue (a full issue URL or `owner/repo#N`, stored
        normalized) so the PR opener can stamp `Fixes #N` on the feature's PR (#97).
        `project` names the entry in the board's `projects:` map this feature builds in
        (default = the board's `default_project`); it's stamped as a `project:<name>`
        label so the Ready gate validates the feature's paths against ITS repo (#90)."""
        # Normalize BEFORE minting the bead: an invalid source_issue/project must reject
        # the whole create with a named error, never leave an orphan bead behind it.
        src = normalize_source_issue(source_issue) if str(source_issue or "").strip() else ""
        proj = normalize_project(project or self.default_project)
        fid = self._create(title, itype="feature", parent=parent, priority=priority, description=spec)
        # Enrichment `br create` can't take (acceptance-criteria/design/notes/labels) — set
        # with a follow-up `br update`. Free-text VALUES ride in `--flag=value` form so a
        # value that STARTS WITH '-' (a markdown bullet in acceptance_criteria, e.g.
        # "- do X") can never be mis-parsed as a CLI option and blow up the update (#85);
        # labels never start with '-', so they stay in the plain `--add-label <v>` form the
        # rest of the code (and its tests) pin. `enriched` names the fields this update
        # carries so a failure can report exactly what still needs writing.
        upd = []
        enriched = []
        if acceptance_criteria:
            upd += [f"--acceptance-criteria={acceptance_criteria}"]
            enriched.append("acceptance_criteria")
        if design:
            upd += [f"--design={design}"]
            enriched.append("design")
        files = [str(p).strip() for p in files_to_modify or () if str(p).strip()]
        if files or src:
            # files_to_modify + the source-issue record SHARE the bead `notes` field
            # (one path per line, the metadata line last — see _render_notes): the
            # source can't be a label (its `/`/`#` fail beads' label validator, #101).
            upd += [f"--notes={_render_notes(files, src)}"]
            if files:
                enriched.append("files_to_modify")
            if src:
                enriched.append("source_issue")
        diff = difficulty.strip().lower()
        if diff:
            # normalize first, then guard: a whitespace-only difficulty must NOT stamp a
            # malformed `diff:` label (an empty tier corrupts the escalation ladder).
            upd += ["--add-label", f"diff:{diff}"]
            enriched.append("difficulty")
        if foundation:
            upd += ["--add-label", LABEL_FOUNDATION]
            enriched.append("foundation")
        if proj:
            # `project:<name>` (#90) — stamped only when a name resolves (the param, or
            # the board's default). Validated above, so it can't fail the label validator.
            upd += ["--add-label", f"{LABEL_PROJECT_PREFIX}{proj}"]
            enriched.append("project")
        # Dependency edges are independent of the enrichment `br update` — wire them
        # FIRST so an enrichment failure can never silently drop them (QA panel on
        # #88: the early success-with-warning return below used to skip the dep loop,
        # losing edges with no repair path). A failed edge is tracked like a failed
        # field: named in the warning, repairable via board_update_feature(depends_on=…).
        failed_deps: list[str] = []
        for dep in depends_on or ():
            try:
                self.add_dependency(fid, dep)
            except BoardError:
                failed_deps.append(dep)
        if failed_deps:
            enriched.append(f"depends_on({','.join(failed_deps)})")
        if upd:
            try:
                self._run("update", fid, *upd)
            except BoardError as exc:
                # The create SUCCEEDED but enrichment didn't — NEVER re-raise here. Raising
                # would bury the id of a bead that already exists on the board, leaving an
                # orphan behind an error that hides it (the #85 trap). Return the feature (so
                # the caller HAS the id) flagged with the fields that still need writing, so
                # the model can finish the job in place with board_update_feature instead of
                # leaking an unreachable bead.
                log.warning(
                    "[project_board] feature %s created but enrichment failed (%s) — returning "
                    "success-with-warning (repair via board_update_feature); missing: %s",
                    fid,
                    exc,
                    ", ".join(enriched),
                )
                # get_feature should always resolve a just-created bead; the fallback keeps
                # the id + the tool's echo keys present even in the impossible None case.
                f = self.get_feature(fid) or {"id": fid, "board_state": "backlog", "title": title}
                f["enrichment_failed"] = True
                f["missing_fields"] = enriched
                f["warning"] = (
                    f"feature {fid} was created but enrichment failed ({exc}); its "
                    f"{', '.join(enriched)} still need writing — repair in place with "
                    f"board_update_feature(feature_id={fid!r}, …)."
                )
                return f
        if failed_deps:
            f = self.get_feature(fid) or {"id": fid, "board_state": "backlog", "title": title}
            f["enrichment_failed"] = True
            f["missing_fields"] = [f"depends_on({','.join(failed_deps)})"]
            f["warning"] = (
                f"feature {fid} was created but these dependency edges failed: "
                f"{', '.join(failed_deps)} — repair with board_update_feature(feature_id={fid!r}, "
                f"depends_on=...)."
            )
            return f
        return self.get_feature(fid)

    def add_dependency(self, fid: str, depends_on: str) -> None:
        """`fid` is blocked until `depends_on` is **closed** (`blocks` edge). This is
        also how a *foundation* gate is expressed: dependents carry a blocks-edge on
        the foundation feature, so they only become `ready` once it merges → done."""
        self._run("dep", "add", fid, depends_on, "--type", "blocks")

    def remove_dependency(self, fid: str, depends_on: str) -> None:
        """Remove a `blocks` edge — the inverse of ``add_dependency``. After this call
        `fid` is no longer gated on `depends_on`.

        `add_dependency` needs `--type` to pick what kind of edge to CREATE
        (`blocks`/`parent-child`/`related`); `br dep remove` never needed one to
        identify which edge to tear down, and some `br` builds now refuse the flag
        outright (`error: unexpected argument '--type' found` — confirmed against a
        real `br 0.2.16` install; `dep remove`'s usage is bare `<ISSUE> <DEPENDS_ON>`).
        Try with `--type` first (older builds may still expect it) and retry once
        without it on that specific CLI-parse failure — this is a version-skew
        adaptation, not error-swallowing, so any other failure (e.g. the edge
        doesn't exist) still raises."""
        try:
            self._run("dep", "remove", fid, depends_on, "--type", "blocks")
        except BoardError as exc:
            if "--type" in str(exc) and "unexpected argument" in str(exc):
                self._run("dep", "remove", fid, depends_on)
            else:
                raise

    # ── batch create from a structured decomposition (#92) ─────────────────────
    @staticmethod
    def _validate_plan_item(item, index: int) -> str:
        """A plan item must be an object carrying a non-empty title. Anything else is
        malformed and fails ITSELF (all-or-report) — raise a named reason the caller
        records against this item while the rest of the batch proceeds."""
        if not isinstance(item, dict):
            raise BoardError(f"plan item {index} is not an object (got {type(item).__name__})")
        title = str(item.get("title") or "").strip()
        if not title:
            raise BoardError(f"plan item {index} has no title")
        return title

    @staticmethod
    def _resolve_plan_dep(dep, index_to_id: dict, title_to_id: dict) -> str:
        """Resolve one plan-item `depends_on` entry to a real feature id. A dep may be
        a 0-based plan-item INDEX (an int, or a plain numeric string), the TITLE of
        another plan item, or an existing board feature id (passed through untouched —
        add_dependency validates it). Raises BoardError with a named reason on anything
        unresolvable, so the CALLER fails just that item's edge in place (#92) instead
        of letting an uncaught error kill the whole batch."""
        # bool is an int subclass — reject before the int branch swallows True/False.
        if isinstance(dep, bool):
            raise BoardError(f"dependency {dep!r} is not a valid feature reference")
        if isinstance(dep, int):
            if dep in index_to_id:
                return index_to_id[dep]
            raise BoardError(f"plan-item index {dep} is out of range (or its item failed to create)")
        s = str(dep).strip()
        if not s:
            raise BoardError("empty dependency reference")
        # A plain integer STRING is a plan-item index. Gate int() on the STRICT
        # _PLAN_INDEX_RE (single optional leading '-') so multi-dash junk like '--5'
        # never reaches int() and blows up (#92 AC8).
        if _PLAN_INDEX_RE.fullmatch(s):
            idx = int(s)
            if idx in index_to_id:
                return index_to_id[idx]
            raise BoardError(f"plan-item index {idx} is out of range (or its item failed to create)")
        # '--5' passes the OLD loose `lstrip('-').isdigit()` guard but not the strict
        # one — name it as a malformed index for THIS item rather than passing it
        # downstream (where it would be mis-read as a `br` flag).
        if s.lstrip("-").isdigit():
            raise BoardError(
                f"dependency {dep!r} looks like a plan-item index but is malformed "
                "(only a single optional leading '-' is allowed)"
            )
        # otherwise: the title of another plan item, else an existing board feature id.
        key = _norm_plan_title(s)
        if key in title_to_id:
            return title_to_id[key]
        return s  # assume an existing board feature id; add_dependency validates it

    def create_from_plan(self, plan, mark_ready: bool = False) -> dict:
        """Batch-create a whole decomposition in ONE call — ``plan`` is a list of
        feature sections (each: title / spec / acceptance_criteria / files /
        difficulty / depends_on / foundation / source_issue). Reuses ``create_feature``'s validation,
        enrichment, and success-with-warning contract PER ITEM (#85): a malformed item
        fails ITSELF with a named reason and the rest proceed (all-or-report, never
        all-or-nothing). The single-create tool is unchanged.

        Dependency edges BETWEEN plan items are resolved AFTER every create — the ids
        aren't known up front, so a ``depends_on`` entry may reference another plan
        item by 0-based index (int or numeric string) or by title, or name an existing
        board feature id; an unresolvable/malformed ref fails that item's edge with a
        named reason (success-with-warning), never the batch. With ``mark_ready=True``
        only items that created CLEANLY (no enrichment/dep warning) are promoted."""
        if not isinstance(plan, (list, tuple)):
            raise BoardError("plan must be a list of feature sections")

        created: list[tuple[int, dict, dict]] = []  # (plan index, source item, feature)
        index_to_id: dict[int, str] = {}
        title_to_id: dict[str, str] = {}
        results: list[dict] = []

        # ── phase 1: validate + create each item (deps deferred to phase 2) ──────
        for i, item in enumerate(plan):
            try:
                title = self._validate_plan_item(item, i)
            except BoardError as exc:
                results.append({"index": i, "created": False, "title": _plan_item_title(item), "error": str(exc)})
                continue
            try:
                f = self.create_feature(
                    title,
                    spec=str(item.get("spec") or ""),
                    acceptance_criteria=str(item.get("acceptance_criteria") or ""),
                    design=str(item.get("design") or ""),
                    files_to_modify=_plan_files(item.get("files", item.get("files_to_modify"))),
                    parent=str(item.get("parent") or ""),
                    priority=int(item.get("priority", 2) or 2),
                    difficulty=str(item.get("difficulty") or ""),
                    depends_on=(),  # wired in phase 2, once every plan-item id is known
                    foundation=bool(item.get("foundation", False)),
                    source_issue=str(item.get("source_issue") or ""),
                )
            except BoardError as exc:
                results.append({"index": i, "created": False, "title": title, "error": str(exc)})
                continue
            index_to_id[i] = f["id"]
            title_to_id[_norm_plan_title(title)] = f["id"]
            created.append((i, item, f))
            r = dict(f)
            r["index"] = i
            r["created"] = True
            results.append(r)

        # ── phase 2: wire inter-item dep edges now every id is resolvable ─────────
        result_by_id = {r["id"]: r for r in results if r.get("created")}
        for _i, item, f in created:
            failed: list[str] = []
            for dep in _plan_deps(item.get("depends_on")):
                try:
                    self.add_dependency(f["id"], self._resolve_plan_dep(dep, index_to_id, title_to_id))
                except BoardError as exc:
                    failed.append(f"{dep} ({exc})")
            if failed:
                r = result_by_id[f["id"]]
                r["enrichment_failed"] = True
                r["missing_fields"] = list(r.get("missing_fields") or []) + [f"depends_on({d})" for d in failed]
                prior = f"{r['warning']} " if r.get("warning") else ""
                r["warning"] = (
                    f"{prior}feature {f['id']} was created but these dependency edges failed: "
                    f"{'; '.join(failed)} — repair with "
                    f"board_update_feature(feature_id={f['id']!r}, depends_on=...)."
                )

        # ── phase 3: promote the cleanly-created items ATOMICALLY (#111) ──────────
        # A per-item `mark_ready` loop flips the `ready` label one bead at a time, so an
        # idle loop could claim the first promoted item before the rest land — priority
        # only ranks what is ALREADY ready. Instead: validate + prep every clean item
        # first (none of that adds the `ready` label), then flip the whole batch's label
        # in a SINGLE `br update`. The puller (a separate `br ready` process) sees either
        # the pre-state or the fully-promoted batch — never a partial one, so no item can
        # be claimed before every item in the batch is ready.
        if mark_ready:
            promote: list[dict] = []
            for _i, _item, f in created:
                r = result_by_id[f["id"]]
                if r.get("enrichment_failed"):
                    continue  # a warned item isn't clean → don't auto-promote it
                try:
                    self._prepare_ready(f["id"])  # gate + ledger, but NOT the `ready` label
                except BoardError as exc:
                    r["ready"] = False
                    r["ready_error"] = str(exc)
                    continue
                promote.append(r)
            if promote:
                # One `br update <id1> <id2> …` — every clean item crosses into `ready`
                # in a single write the puller can't interleave a claim into.
                self._run(
                    "update",
                    *[r["id"] for r in promote],
                    "--add-label",
                    LABEL_READY,
                    "--remove-label",
                    LABEL_DESIGNING,
                )
                for r in promote:
                    r["board_state"] = "ready"
                    r["ready"] = True

        n_created = len(created)
        return {
            "items": results,
            "created_ids": [f["id"] for _i, _item, f in created],
            "summary": {
                "requested": len(plan),
                "created": n_created,
                "failed": len(plan) - n_created,
                "ready": sum(1 for r in results if r.get("ready")),
                "warnings": sum(1 for r in results if r.get("enrichment_failed")),
            },
        }

    # ── partial update (the repair path) ──────────────────────────────────────
    def update_feature(
        self,
        fid: str,
        *,
        title: str | None = None,
        spec: str | None = None,
        acceptance_criteria: str | None = None,
        design: str | None = None,
        files_to_modify=None,
        difficulty: str | None = None,
        depends_on: list[str] | None = None,
        foundation: bool | None = None,
        source_issue: str | None = None,
    ) -> dict:
        """Partially update an existing feature's fields (a board-level `br update`).
        Only the arguments you pass (non-``None``) are written; every other field is
        left untouched. This is the escape from the 'unrepairable bead' trap: a feature
        the Ready gate rejects for a missing `spec` / `acceptance_criteria` /
        `files_to_modify` can be fixed IN PLACE and re-marked ready, instead of being
        cancelled and recreated from scratch. ``title`` renames the feature (`br
        update --title`); a whitespace-only value collapses to empty → untouched (the
        difficulty convention — a bead can never carry a blank title). ``depends_on``
        ADDS blocking edges and ``foundation=True`` adds the foundation label
        (None/False = untouched) — the repair half of create's success-with-warning
        contract. ``source_issue`` (a full GitHub issue URL or ``owner/repo#N``)
        sets/replaces the originating-issue record the PR opener stamps as
        ``Fixes #N`` (#97)."""
        f = self._require(fid)
        args = ["update", fid]
        # Free-text VALUES ride in `--flag=value` form so a value STARTING WITH '-' (a
        # markdown bullet, a leading-dash path) can't be mis-parsed as a CLI option and
        # fail the update (#85) — the same hardening as create_feature's enrichment. Labels
        # never start with '-', so `--add/remove-label` stay in the plain form below.
        if title is not None and title.strip():
            args += [f"--title={title.strip()}"]
        if spec is not None:
            args += [f"--description={spec}"]
        if acceptance_criteria is not None:
            args += [f"--acceptance-criteria={acceptance_criteria}"]
        if design is not None:
            args += [f"--design={design}"]
        set_source = source_issue is not None and str(source_issue).strip()
        if files_to_modify is not None or set_source:
            # files_to_modify + source_issue + the requirement ledger SHARE the bead
            # `notes` field (labels can't carry the source's `/`/`#`, #101; the
            # ledger rides the same structured lines, #113), and `br update --notes`
            # replaces the whole field — so rewrite it with every untouched part
            # carried forward from the current projection: a files-only update
            # must never drop the source-issue line or the ledger, nor any other
            # combination. An invalid source_issue raises the named error BEFORE
            # `br update` runs, so it never half-applies a mixed update;
            # whitespace-only = no-op (the difficulty convention).
            files = (
                [str(p).strip() for p in files_to_modify if str(p).strip()]
                if files_to_modify is not None
                else f.get("files_to_modify") or []
            )
            src = normalize_source_issue(source_issue) if set_source else str(f.get("source_issue") or "")
            args += [f"--notes={_render_notes(files, src, f.get('requirements') or [])}"]
        if difficulty is not None:
            # difficulty rides as a single `diff:` label — replace any stale one (the
            # same single-label-replaced pattern record_gens_spent uses for `gens:`).
            # Normalize first; a whitespace-only value collapses to empty → leave the
            # existing label untouched (clear nothing, add nothing) rather than stamping a
            # malformed `diff:` that would corrupt the escalation ladder's tier selection.
            diff = difficulty.strip().lower()
            if diff:
                for stale in [l for l in f.get("labels") or [] if l.startswith("diff:")]:
                    args += ["--remove-label", stale]
                args += ["--add-label", f"diff:{diff}"]
        if foundation:
            # Complete the create-repair contract: a foundation flag dropped by a failed
            # create can be restored here (QA panel on #88, round 4 — same undeliverable-
            # promise class as depends_on). None/False = leave the label untouched.
            args += ["--add-label", LABEL_FOUNDATION]
        if len(args) > 2:  # something to write beyond the bare `update <fid>`
            self._run(*args)
        # Same partial-failure contract as create_feature (panel round 7): one bad id
        # must not abort the batch after earlier edges landed — apply what applies,
        # name what failed, and let the tool boundary surface it for another repair.
        failed_deps: list[str] = []
        for dep in depends_on or ():
            try:
                self.add_dependency(fid, dep)
            except BoardError:
                failed_deps.append(dep)
        f = self.get_feature(fid)
        if failed_deps and f is not None:
            f["enrichment_failed"] = True
            f["missing_fields"] = [f"depends_on({','.join(failed_deps)})"]
            f["warning"] = (
                f"feature {fid} was updated but these dependency edges failed: "
                f"{', '.join(failed_deps)} — repair with board_update_feature(feature_id={fid!r}, "
                f"depends_on=...)."
            )
        return f

    # ── the Ready gate (invariant #1) ─────────────────────────────────────────
    def _repo_for(self, f: dict) -> str:
        """The repo root a feature's files_to_modify are validated against (#90). When
        the feature carries a `project` known to this board's `projects:` map, that
        project's repo; otherwise the instance repo — so a board with no map (or a
        feature with no project) keeps its single-repo behavior. Guards the two ways an
        entry can lack a usable repo (absent, or set to a blank) by falling back."""
        name = str(f.get("project") or "").strip()
        entry = self.projects.get(name) if name else None
        return str((entry or {}).get("repo") or "").strip() or self.repo

    def _prepare_ready(self, fid: str) -> None:
        """Enforce the Ready gate and materialize the requirement ledger for ``fid``
        WITHOUT flipping the ``ready`` label — the prep half of ``mark_ready``, split
        out so a batch promotion can validate + prep every item first and then flip the
        whole batch's ``ready`` label in ONE ``br update`` (#111): the puller must never
        observe a partially-promoted batch (some items ready, some not). Raises
        BoardError if the gate rejects the feature."""
        f = self._require(fid)
        if f["board_state"] not in ("backlog", "ready"):
            raise BoardError(f"can't mark ready from {f['board_state']!r}")
        missing = [k for k in ("spec", "acceptance_criteria") if not str(f.get(k, "")).strip()]
        if not f.get("files_to_modify"):
            missing.append("files_to_modify")
        if missing:
            raise BoardError(
                f"Ready gate: feature {fid!r} is missing {', '.join(missing)} — a feature is "
                "Ready only with a spec, testable acceptance criteria, and the explicit files "
                "to create/modify (a junior — or a coding agent — could pick it up and finish). "
                f"Fill the missing field(s) in place with board_update_feature(feature_id={fid!r}, "
                "…) and mark it ready again — no need to cancel and recreate the bead."
            )
        # #110: every files_to_modify path must resolve in the bound checkout — a phantom
        # path (a plausible-but-wrong file the card author guessed) is invisible until a
        # coder burns a run chasing it. A `(new)` marker (case-insensitive, anywhere in the
        # entry) declares the file doesn't exist yet and bypasses the existence check.
        # #90: the bound checkout is THIS FEATURE's project repo (via its `project` label),
        # not the instance default — so a multi-repo board checks each card against its own
        # repo; a card with no project falls back to the instance repo (single-repo path).
        repo = self._repo_for(f)
        phantom = [
            p for p in f["files_to_modify"] if "(new)" not in p.lower() and not os.path.exists(os.path.join(repo, p))
        ]
        if phantom:
            raise BoardError(
                f"Ready gate: feature {fid!r} is missing files_to_modify paths that do not exist "
                f"in the repo (bound root: {os.path.abspath(repo)!r}, set via project_board.repo): "
                f"{', '.join(phantom)} — correct the path, add a `(new)` marker, or fix the repo binding."
            )
        # BREADTH cap (#143): a small/medium card that names more files than its difficulty
        # allows times out before it lands — and a timeout teaches nothing (it burns the top
        # tiers with zero diff, the single worst failure mode). Refuse it here so the author
        # must SPLIT the work or re-declare the card `large` (which then owes the design + ADR
        # the DESIGN gate below enforces). `large`/`architectural` carry no cap (absent from
        # the dict) → exempt from breadth, still subject to that design gate. Configurable via
        # the `max_files_by_difficulty` config key.
        diff = str(f.get("difficulty", "")).strip().lower()
        cap = self.max_files_by_difficulty.get(diff)
        if cap is not None and len(f["files_to_modify"]) > cap:
            n = len(f["files_to_modify"])
            raise BoardError(
                f"Breadth gate: feature {fid!r} is difficulty={diff!r} and names {n} "
                f"files_to_modify, over the cap of {cap} for a {diff} card — a card this wide "
                "times out before it lands, and a timeout teaches nothing. Either SPLIT it into "
                "cards each at or under the cap, or re-declare it difficulty=`large` (which then "
                "owes a design + ADR reference before it can go ready). Tune the limit with the "
                "max_files_by_difficulty config key if this cap is wrong."
            )
        # SHARED-FILE overlap (#143): two non-terminal cards naming the same file with no
        # depends_on edge between them build off a stale base and collide — the loop can claim
        # one while the other sits unmerged, so the second builds on a base that's about to
        # move under it. Refuse unless a dependency edge (in EITHER direction) already orders
        # them. (The in-flight hot-file guard only stops PARALLEL edits, and never fires at
        # max_concurrent=1 — this is the serialization that guard can't provide, enforced at
        # the gate.) `list_features` is the exhaustive non-archived projection; terminal
        # (done/cancelled) cards no longer contend for the file, so they're skipped.
        my_files = {p for p in f["files_to_modify"] if str(p).strip()}
        if my_files:
            my_deps = set(f.get("depends_on") or [])
            my_project = str(f.get("project") or "")
            conflicts: list[tuple[str, list[str]]] = []
            for other in self.list_features():
                if other["id"] == fid or other["board_state"] in _TERMINAL_STATES:
                    continue
                # #197: paths only collide INSIDE a project — every plugin repo carries
                # PROTO.md/CLAUDE.md/AGENTS.md, so bare-path comparison deadlocks any two
                # doc-touching cards in different repos on a multi-project board (#90).
                # Unstamped cards ("" project) keep the old single-repo behavior.
                if str(other.get("project") or "") != my_project:
                    continue
                shared = sorted(my_files & {p for p in (other.get("files_to_modify") or []) if str(p).strip()})
                if not shared:
                    continue
                linked = other["id"] in my_deps or fid in set(other.get("depends_on") or [])
                if not linked:
                    conflicts.append((other["id"], shared))
            if conflicts:
                detail = "; ".join(f"{oid} (shares {', '.join(files)})" for oid, files in conflicts)
                raise BoardError(
                    f"Shared-file gate: feature {fid!r} names files_to_modify already claimed by "
                    f"non-terminal card(s) with no depends_on edge between them: {detail}. Two cards "
                    "editing the same file without a dependency edge build off a stale base and "
                    f"collide — add a depends_on edge (board_update_feature(feature_id={fid!r}, "
                    "depends_on=[…])) so one waits for the other to merge, or split the file work "
                    "so they don't overlap."
                )
        # DESIGN gate (plan M6): a large/architectural feature is a decision, not just
        # a task — it may not go ready until its `design` field exists AND references
        # the ADR that records the decision (run /due-diligence, write the ADR, cite
        # it). Small/medium features are untouched.
        if str(f.get("difficulty", "")).strip().lower() in DESIGN_GATED_DIFFICULTIES:
            design = str(f.get("design", "")).strip()
            if not design:
                raise BoardError(
                    f"Design gate: feature {fid!r} is difficulty={f.get('difficulty')!r} but has no "
                    "`design` — at this blast radius the decision must be designed first (run the "
                    "due-diligence workflow, record the decision as an ADR, and put the design + "
                    "ADR reference in the feature's design field)."
                )
            if not ADR_REF_RE.search(design):
                raise BoardError(
                    f"Design gate: feature {fid!r} is difficulty={f.get('difficulty')!r} and has a "
                    "design, but the design references no ADR — record the decision as an ADR and "
                    "cite it (e.g. 'ADR 0077') so the rationale outlives this feature."
                )
        # Requirement ledger (#113): decompose the acceptance-criteria prose into
        # tracked items HERE — the same seam as the gates above (the PM authors prose;
        # the coder sees items). Only when the bead carries no ledger yet: a re-mark
        # (requeue → ready → mark_ready) must never wipe recorded dispositions back
        # to `open`. Stored in `notes` beside files_to_modify/source-issue.
        if not f.get("requirements"):
            items = _decompose_ac(f.get("acceptance_criteria", ""))
            if items:
                self._run(
                    "update",
                    fid,
                    f"--notes={_render_notes(f.get('files_to_modify'), str(f.get('source_issue') or ''), items)}",
                )

    def mark_ready(self, fid: str) -> dict:
        """Promote a single feature to `ready` (backlog → ready): enforce the gate,
        materialize the requirement ledger, then flip the `ready` label."""
        self._prepare_ready(fid)
        self._run("update", fid, "--add-label", LABEL_READY, "--remove-label", LABEL_DESIGNING)
        return self.get_feature(fid)

    def mark_designing(self, fid: str, note: str = "") -> dict:
        """Park a pre-ready feature in the DESIGNING state (label) while its design/
        due-diligence is worked out — the optional waiting room in front of the M6
        design gate. Purely informational; `mark_ready` still enforces the gate."""
        f = self._require(fid)
        if f["board_state"] not in ("backlog", "ready"):
            raise BoardError(f"can't mark designing from {f['board_state']!r}")
        self._run("update", fid, "--add-label", LABEL_DESIGNING, "--remove-label", LABEL_READY)
        if note:
            self._comment(fid, f"designing: {note}")
        return self.get_feature(fid)

    # ── the puller (Ready → In Progress) ──────────────────────────────────────
    def claim_next_ready(self, assignee: str = "") -> dict | None:
        """Atomically pull the top-priority unblocked, board-`ready` **feature** →
        `in_progress`. Returns None if nothing is ready. (`br ready` is priority-
        ordered; we filter `feature` in Python to dodge the --type+--label quirk.)"""
        # `--limit 0` = unlimited: `br ready` defaults `--limit 20`, and we filter to
        # `feature` (+ non-blocked) in Python AFTER, so a capped queue could hide the only
        # claimable feature behind 20 epics/blocked rows (the exhaustiveness invariant).
        ready = self._run("ready", "--label", LABEL_READY, "--limit", "0", want_json=True) or []
        feats = [b for b in ready if b.get("issue_type") == "feature" and LABEL_BLOCKED not in (b.get("labels") or [])]
        if not feats:
            return None
        fid = feats[0]["id"]
        # --claim is atomic: assignee=actor + status=in_progress. Drop the `ready`
        # label so it projects as in_progress, not ready.
        self._run("update", fid, "--claim", "--remove-label", LABEL_READY)
        if assignee:
            self._run("update", fid, "--assignee", assignee)
        return self.get_feature(fid)

    def claim(self, fid: str, assignee: str = "") -> dict | None:
        """Atomically claim a SPECIFIC ready feature → `in_progress` (vs
        ``claim_next_ready``, which takes the top of the queue). The loop uses this to
        skip a candidate whose files overlap an in-flight build. Returns the feature,
        or None if it's no longer claimable (changed state, or lost the claim race)."""
        f = self.get_feature(fid)
        if f is None or f["board_state"] != "ready":
            return None
        try:
            self._run("update", fid, "--claim", "--remove-label", LABEL_READY)
        except BoardError as exc:
            # `br --claim` rejects an already-assigned bead. This was a SILENT skip (the
            # loop never claims + logs nothing — a nasty trap); log it so it's visible.
            log.info(
                "[project_board] %s not claimable (likely already assigned — "
                'clear with `br update %s --assignee ""`): %s',
                fid,
                fid,
                exc,
            )
            return None
        if assignee:
            self._run("update", fid, "--assignee", assignee)
        return self.get_feature(fid)

    # ── In Progress → In Review ───────────────────────────────────────────────
    def open_review(self, fid: str, *, pr_url: str) -> dict:
        f = self._require(fid)
        if f["board_state"] != "in_progress":
            raise BoardError(f"open_review expects in_progress, got {f['board_state']!r}")
        self._run("update", fid, "--add-label", LABEL_IN_REVIEW, "--external-ref", pr_url)
        return self.get_feature(fid)

    def set_review_substate(self, fid: str, label: str | None, note: str = "") -> dict:
        """Swap the review-gate sub-state labels (``review-pending`` /
        ``changes-requested`` / ``review-clean``) — exactly one (or none) at a time. ``note`` (the
        findings block, a clean-review line) is recorded as a comment so the
        review history lives on the bead."""
        self._require(fid)
        args = ["update", fid]
        for known in (LABEL_REVIEW_PENDING, LABEL_CHANGES_REQUESTED, LABEL_REVIEW_CLEAN):
            if known != label:
                args += ["--remove-label", known]
        if label:
            args += ["--add-label", label]
        self._run(*args)
        if note:
            self._comment(fid, note)
        return self.get_feature(fid)

    def bounce_ci_fail(self, fid: str, reason: str = "") -> dict:
        """In Review → In Progress on CI failure (drop the in-review label). The
        feature parks in_progress for the operator to requeue (single-coder path)."""
        f = self._require(fid)
        if f["board_state"] != "in_review":
            raise BoardError(f"bounce expects in_review, got {f['board_state']!r}")
        self._run("update", fid, "--remove-label", LABEL_IN_REVIEW)
        if reason:
            self._comment(fid, f"CI failed: {reason}")
        return self.get_feature(fid)

    def record_review_bounce(self, fid: str, findings: str = "") -> dict:
        """Record an adverse code-review bounce as a DISTINCT comment on the bead —
        the review sibling of ``bounce_ci_fail``'s ``CI failed:`` note, kept separate
        from the requeue so the review history survives on the bead even though the
        same open PR is reused. Expects ``in_review`` — the state an adverse review
        lands from; the caller then ``requeue``s onto the same PR (pr_url preserved)."""
        f = self._require(fid)
        if f["board_state"] != "in_review":
            raise BoardError(f"review bounce expects in_review, got {f['board_state']!r}")
        self._comment(fid, f"review requested changes: {findings}" if findings else "review requested changes")
        return f

    def requeue(self, fid: str) -> dict:
        """Put a feature back to `ready` for re-dispatch (keeps its open PR via
        external_ref). The puller re-claims it and the loop re-dispatches — at the
        higher tier if it was just escalated; open_pr pushes to the existing PR."""
        self._require(fid)
        # Clear the assignee too — without it `br update --claim` on the re-pull
        # fails ("already assigned to <actor>") and the feature can't be re-dispatched.
        self._run(
            "update",
            fid,
            "--status",
            "open",
            "--assignee",
            "",
            "--add-label",
            LABEL_READY,
            "--remove-label",
            LABEL_IN_REVIEW,
        )
        return self.get_feature(fid)

    def block_from_review(self, fid: str, reason: str) -> dict:
        """Drop the in-review label and flag Blocked — used when the escalation
        ladder is exhausted on a CI failure."""
        self._require(fid)
        self._run("update", fid, "--remove-label", LABEL_IN_REVIEW, "--add-label", LABEL_BLOCKED)
        if reason:
            self._comment(fid, f"escalation exhausted: {reason}")
        return self.get_feature(fid)

    # ── the ONE Done edge (invariant #2) ──────────────────────────────────────
    def record_merge(self, *, pr_url: str) -> dict | None:
        """Close the feature whose PR merged — the ONLY path to `done`. Idempotent;
        returns None if no feature carries that PR url (a webhook for another PR)."""
        f = self._find_by_external_ref(pr_url)
        if f is None:
            return None
        if f["board_state"] != "done":
            fid = f["id"]
            # A merged PR is ground truth (#196): whatever blocked the card is moot once
            # its change shipped — and `br close` refuses while open blocker edges remain
            # (the #145 class cancel already handles) — so clear the blocked label and
            # drop open incoming edges FIRST, or a blocked card whose PR merged sticks in
            # `blocked` forever and needs a hand-unblock.
            if LABEL_BLOCKED in (f.get("labels") or []):
                self._run("update", fid, "--remove-label", LABEL_BLOCKED)
            for blocker_id in self._open_blockers(fid):
                try:
                    self.remove_dependency(fid, blocker_id)
                    log.info("[project_board] record_merge %s: dropped blocks edge from %s", fid, blocker_id)
                except BoardError:
                    log.warning(
                        "[project_board] record_merge %s: could not drop edge from %s (close may still fail)",
                        fid,
                        blocker_id,
                    )
            self._run("close", fid, "-r", f"merged: {pr_url}")
        return self.get_feature(f["id"])

    # ── the second terminal edge: cancel (not merge) ──────────────────────────
    def cancel_feature(self, fid: str, reason: str = "") -> dict:
        """Cancel a feature created in error (bad decomposition, duplicate, scope cut).

        Modeled DELIBERATELY as a second terminal edge so it doesn't break the
        one-Done-edge invariant: it tags the bead `cancelled` and closes it with an
        auditable reason (`br close -r`). The `cancelled` label makes the projection show
        a distinct `cancelled` state — NOT `done` — so the merge/CI reconcilers (which
        only touch `in_review`) and the loop-retro (which mines done/blocked) never
        mistake a cancel for shipped or regressed work. Audit-preserving (the bead + its
        history survive), vs a hard `br delete` tombstone. Clears the assignee so a
        revived id could be re-claimed. Idempotent-ish: re-cancelling a cancelled feature
        just re-closes it.

        ATOMIC-OR-CLEAN (#106): the tag/unassign write lands BEFORE `br close`, so a failing
        close (a reason `br` can't parse, contention outlasting the retries, …) would strand
        the feature as a claimable zombie — still OPEN, still `ready`-labelled, now also
        `cancelled` and unassigned. On any close failure we undo the tag + restore the prior
        assignee, then re-raise: the feature lands back in its exact pre-cancel state and the
        caller sees the error, never a silent half-cancel."""
        f = self._require(fid)
        prior_assignee = f.get("assignee", "")
        self._run("update", fid, "--add-label", LABEL_CANCELLED, "--assignee", "")
        # Drop open incoming `blocks` edges before closing (#145): `br close` refuses
        # when blockers are unresolved, but a cancel is a scope-cut — prerequisites
        # being unfinished is irrelevant. Log each dropped edge for the audit trail.
        dropped: list[str] = []
        for blocker_id in self._open_blockers(fid):
            try:
                self.remove_dependency(fid, blocker_id)
                dropped.append(blocker_id)
                log.info("[project_board] cancel %s: dropped blocks edge from %s", fid, blocker_id)
            except BoardError:
                log.warning(
                    "[project_board] cancel %s: could not drop edge from %s (close may still fail)",
                    fid,
                    blocker_id,
                )
        try:
            self._run("close", fid, "-r", f"cancelled: {reason}" if reason else "cancelled")
        except BoardError:
            undo = ["update", fid, "--remove-label", LABEL_CANCELLED]
            # Only rewrite the assignee if there was one — a bead that was already
            # unassigned needs no restore (and `--assignee ""` would be a redundant write).
            if prior_assignee:
                undo += ["--assignee", prior_assignee]
            self._run(*undo)
            raise
        result = self.get_feature(fid) or {}
        if dropped:
            result["dropped_deps"] = dropped
        return result

    def delete_feature(self, fid: str, reason: str = "") -> dict:
        """Hard-delete a feature (a `br` tombstone) — the harder sibling of
        ``cancel_feature``. For a feature that should leave NO trace on the board (a pure
        mistake / duplicate), vs a cancel which keeps a visible, reopenable `cancelled`
        lane. Still goes THROUGH the board (not a raw `br` reach-around) so board ↔ JSONL
        stay in step; `br delete` tombstones in the JSONL (recoverable) rather than
        nuking history. Refuses (BoardError, via `br`'s non-zero exit) when the feature
        has dependents — deleting it would orphan them; cancel or re-point them first.
        Returns the deleted feature's last projection (the API echo)."""
        f = self._require(fid)
        self._run("delete", fid, "--reason", f"deleted: {reason}" if reason else "deleted")
        return f

    # ── archival (#115): age terminal features out of the live view — never delete ─
    def archive_stale(
        self, archive_after_days: float = ARCHIVE_AFTER_DAYS_DEFAULT, now: float | None = None
    ) -> list[str]:
        """Label every terminal feature (``_TERMINAL_STATES``: done/cancelled) whose
        ``closed_at`` is older than ``archive_after_days`` with ``archived`` — the
        board's unbounded-growth valve (#115), run from the loop's periodic health
        sweep (no scheduler of its own). ARCHIVAL, NOT DELETION: only the label is
        written — the bead, its history, and the git-backed JSONL are untouched, and
        ``list_features(include_archived=True)`` still returns everything. A feature
        with no parseable ``closed_at`` is left alone (fail visible — never archive
        on a guess). Best-effort per feature; returns the ids archived this pass."""
        cutoff = (time.time() if now is None else now) - float(archive_after_days) * 86400.0
        archived: list[str] = []
        # The default listing already excludes `archived`, so an archived bead never
        # takes a second (redundant) label write on every sweep.
        for f in self.list_features():
            if f["board_state"] not in _TERMINAL_STATES:
                continue
            ts = _parse_closed_at(f.get("closed_at"))
            if ts is None or ts > cutoff:
                continue
            try:
                self._run("update", f["id"], "--add-label", LABEL_ARCHIVED)
                archived.append(f["id"])
            except BoardError:
                log.warning("[project_board] archive pass: labeling %s failed (skipped this sweep)", f["id"])
        return archived

    # ── Blocked flag (not a lane) ─────────────────────────────────────────────
    def flag_blocked(self, fid: str, reason: str) -> dict:
        self._require(fid)
        # Clear the assignee with the block: `br update --claim` rejects an already-
        # assigned bead, so a later reset-to-ready would be SILENTLY un-claimable (the
        # loop ticks forever, never claims, logs nothing). A blocked feature is terminal
        # until requeued, so dropping the assignee here is safe — and lets a requeue
        # (`--status open --add-label ready`) be re-claimed without a manual unassign.
        self._run("update", fid, "--add-label", LABEL_BLOCKED, "--assignee", "")
        if reason:
            self._comment(fid, f"blocked: {reason}")
        return self.get_feature(fid)

    def clear_blocked(self, fid: str) -> dict:
        self._require(fid)
        self._run("update", fid, "--remove-label", LABEL_BLOCKED)
        return self.get_feature(fid)

    # ── escalation ladder (D10) — mechanical; the *policy* (whether to climb at
    #    all) lives in the loop, which only escalates when distinct per-tier coders
    #    are configured. With a single coder these are simply never called, so a
    #    one-ACP-agent setup writes no tier/attempt labels (difficulty stays purely
    #    optional metadata). ───────────────────────────────────────────────────────
    def initial_tier(self, fid: str) -> str:
        f = self._require(fid)
        return DIFFICULTY_TIER.get(f.get("difficulty", ""), "smart")

    def current_tier(self, fid: str) -> str:
        """The highest tier this feature has been tried at (from `tier:` labels),
        else its difficulty-derived initial tier."""
        f = self._require(fid)
        present = [l.split(":", 1)[1] for l in f.get("labels") or [] if l.startswith("tier:")]
        idxs = [TIER_LADDER.index(t) for t in present if t in TIER_LADDER]
        return TIER_LADDER[max(idxs)] if idxs else DIFFICULTY_TIER.get(f.get("difficulty", ""), "smart")

    def escalate(self, fid: str, reason: str) -> str | None:
        """Record the failed attempt at the current tier and advance to the next
        rung. Returns the new tier, or None if already at the top (caller blocks)."""
        cur = self.current_tier(fid)
        self.record_attempt(fid, tier=cur, outcome=reason)
        nxt = self.next_tier(cur)
        if nxt:
            self._run("update", fid, "--add-label", f"tier:{nxt}")
        return nxt

    def record_attempt(self, fid: str, *, tier: str, outcome: str) -> dict:
        """Log an attempt (tier + outcome) as labels — `attempt:N` counts the tries;
        the loop reads these to walk fast→smart→reasoning and stop at the top."""
        f = self._require(fid)
        n = len([a for a in f.get("attempts", [])]) + 1
        self._run("update", fid, "--add-label", f"attempt:{n}", "--add-label", f"tier:{tier}")
        self._comment(fid, f"attempt {n} (tier={tier}): {outcome}")
        return self.get_feature(fid)

    def next_tier(self, current: str) -> str | None:
        """The next rung up the ladder, or None at the top (→ caller blocks)."""
        try:
            i = TIER_LADDER.index(current)
        except ValueError:
            return TIER_LADDER[0]
        return TIER_LADDER[i + 1] if i + 1 < len(TIER_LADDER) else None

    # ── coder.solve() cost accounting (ADR 0064 P2 board seam) ────────────────
    def record_gens_spent(self, fid: str, n: int) -> dict:
        """Accumulate `n` more generations `coder.solve()` spent on this feature onto
        its `gens:<total>` label — a single, replaced label so `portfolio_rollup` (the
        PM tier) can read the running cost without raw reads, per the ADR's cost-v1
        ethos. Called once per `solve()` run, win or lose (a failed search still spent
        gens). Best-effort in the sense that a `br` hiccup here must never fail the
        build the way a missing PR would — callers should treat it as fire-and-forget."""
        f = self._require(fid)
        total = int(f.get("gens_spent", 0)) + max(0, int(n))
        stale = [l for l in f.get("labels") or [] if l.startswith(LABEL_GENS_PREFIX)]
        args = ["update", fid]
        for label in stale:
            args += ["--remove-label", label]
        args += ["--add-label", f"{LABEL_GENS_PREFIX}{total}"]
        self._run(*args)
        return self.get_feature(fid)

    # ── verified-candidate salvage record (#91) ───────────────────────────────
    def record_verified_candidate(self, fid: str, *, branch: str, sha: str, worktree: str) -> dict:
        """Persist the verified candidate's identity — a single, replaced
        `verified:<sha>` label (the `gens:` pattern) plus a comment carrying the full
        {branch, sha, worktree} — written at coder_seam's verify boundary so a crash
        between verify and open_pr can salvage the already-test-passing build instead
        of rebuilding fresh. Fire-and-forget like record_gens_spent: a `br` hiccup
        here must never fail a build whose tests already passed."""
        f = self._require(fid)
        args = ["update", fid]
        for stale in [l for l in f.get("labels") or [] if l.startswith(LABEL_VERIFIED_PREFIX)]:
            args += ["--remove-label", stale]
        args += ["--add-label", f"{LABEL_VERIFIED_PREFIX}{sha}"]
        self._run(*args)
        self._comment(fid, f"verified candidate: branch={branch} sha={sha} worktree={worktree}")
        return self.get_feature(fid)

    def clear_verified_candidate(self, fid: str) -> dict:
        """Drop the `verified:` salvage record — the crash window it covers has closed
        (the PR opened) or the record failed its recovery checks (worktree/sha drift),
        so it must not linger to confuse a later recovery. No-op without the label."""
        f = self._require(fid)
        stale = [l for l in f.get("labels") or [] if l.startswith(LABEL_VERIFIED_PREFIX)]
        if not stale:
            return f
        args = ["update", fid]
        for label in stale:
            args += ["--remove-label", label]
        self._run(*args)
        return self.get_feature(fid)

    # ── merged-state verify stamp (#131) ──────────────────────────────────────
    def record_merged_verified(self, fid: str, sha: str) -> dict:
        """Stamp the origin/<base> sha the in_review verdict was verified against
        (#131) — a single, replaced `merged-verified:<sha>` label (the `gens:`
        pattern), written by the merge-poll reconciler after the gate passed on
        the MERGED state (branch tip + that base commit). Fire-and-forget like
        record_gens_spent: a `br` hiccup here must never fail the reconcile (the
        next poll just re-verifies)."""
        f = self._require(fid)
        args = ["update", fid]
        for stale in [l for l in f.get("labels") or [] if l.startswith(LABEL_MERGED_VERIFIED_PREFIX)]:
            args += ["--remove-label", stale]
        args += ["--add-label", f"{LABEL_MERGED_VERIFIED_PREFIX}{sha}"]
        self._run(*args)
        return self.get_feature(fid)

    # ── requirement ledger write-back (#113) ──────────────────────────────────
    def set_requirements(self, fid: str, items) -> dict:
        """Write the requirement ledger back to the bead — the loop calls this after
        each coder round with the dispositions merged in (see
        ``apply_requirement_dispositions``), so the LEDGER on the bead — not the
        coder's reply text — is what the completion gate reads. `br update --notes`
        replaces the whole field, so the files/source halves are carried forward
        from the current projection (the update_feature contract)."""
        f = self._require(fid)
        self._run(
            "update",
            fid,
            f"--notes={_render_notes(f.get('files_to_modify'), str(f.get('source_issue') or ''), items)}",
        )
        return self.get_feature(fid)

    # ── reads (the projection) ────────────────────────────────────────────────
    def get_feature(self, fid: str) -> dict | None:
        rows = self._run("show", fid, want_json=True)
        if not rows:
            return None
        return self._project(rows[0] if isinstance(rows, list) else rows)

    def list_features(self, state: str | None = None, include_archived: bool = False) -> list[dict]:
        """All feature rows for the board projection (every state, incl. the Done
        column); pass ``state`` to narrow the projection to one board state.

        INVARIANT: **a board query that reads as exhaustive must be exhaustive, or must
        document its cap.** `br list` defaults `--limit 50` (and `br ready` defaults 20),
        so this passes `--limit 0` (the documented unlimited sentinel) to make the query
        genuinely unbounded. Without it every consumer (PR reconcile, sweep/recover, the
        pending-review count, dedup, the ready scan, /features, board_list) would silently
        see only the first 50 rows — and worse, the cap applies IN `br` while the state
        filter runs afterward in Python, so `state="in_review"` would mean 'in_review among
        the first 50 rows br returned', not all of them.

        DOCUMENTED cap (#115): the default projection is the LIVE board — features
        labeled ``archived`` (terminal + past the archive window, see ``archive_stale``)
        are excluded unless ``include_archived=True``. The `br` query itself stays
        unbounded; the narrowing is this one visible, opt-out-able label filter. A
        consumer whose read must span history (``raw_features_with_comments`` →
        board_retro) passes the flag explicitly.
        """
        # All statuses — `br list` defaults to open/in_progress, but the board view
        # needs `closed` features too (that's the Done column). `--limit 0` = unlimited
        # (see the exhaustiveness invariant above): the 50-row default would truncate the
        # projection before the Python state filter ever ran.
        rows = (
            self._run(
                "list",
                "--type",
                "feature",
                "--status",
                "open",
                "--status",
                "in_progress",
                "--status",
                "closed",
                "--status",
                "deferred",
                "--limit",
                "0",
                want_json=True,
            )
            or []
        )
        # #138/#114: on br 0.2.x the envelope reports has_more — turn `--limit 0` = unbounded
        # from an ASSUMPTION into an assertion. A `--limit 0` page that still reports more rows
        # means the exhaustiveness invariant is broken and this projection would be silently
        # truncated (every consumer reads it as the whole board); fail loud instead of guessing.
        # Guarded on has_more SHAPE presence — None on 0.1.x, where `--limit 0` stays trusted —
        # not a version sniff. Checked BEFORE ready_queue() below, whose own _run overwrites it.
        if self._last_json_has_more:
            raise BoardError(
                "`br list --limit 0` reported has_more=true — the unbounded feature query was "
                "truncated, so the board projection would be incomplete (#114/#138). Check the "
                "installed beads version's `--limit 0` semantics before trusting the board."
            )
        # `br list` omits the `dependencies` array; `br show` carries it. Batch all IDs
        # into ONE call so `_project` sees real edges — avoids N+1 subprocess spawns on
        # this continuously-polled endpoint (#144). Guard the empty-rows case: `br show`
        # with no arguments is an error.
        if rows:
            ids = [r["id"] for r in rows if r.get("id")]
            if ids:
                batch = self._run("show", *ids, want_json=True) or []
                if isinstance(batch, dict):  # 0.1.x bare-dict single-bead path
                    batch = [batch]
                show_by_id = {r["id"]: r for r in batch if isinstance(r, dict) and r.get("id")}
                for r in rows:
                    rid = r.get("id")
                    if rid and rid in show_by_id and "dependencies" not in r:
                        r["dependencies"] = show_by_id[rid].get("dependencies")
        out = [self._project(r) for r in rows]
        if not include_archived:
            out = [f for f in out if not f["archived"]]
        # Cross-reference the puller's ready queue: a `ready` feature the puller won't
        # claim is dep-blocked even if the show batch missed its edges.
        claimable = {f["id"] for f in self.ready_queue()}
        for f in out:
            if f["board_state"] == "ready" and f["id"] not in claimable:
                f["dag_blocked"] = True
        if state:
            out = [f for f in out if f["board_state"] == state]
        out.sort(key=lambda f: (f["priority"], f["id"]))
        return out

    def annotate_ci_status(self, feats: list[dict]) -> list[dict]:
        """Join projected features with their PR's LIVE CI rollup, in place (#107).

        Every row gains ``ci_status`` — ``worktree.pr_ci_status``'s token
        (``passing|failing|pending|none``; only BLOCKING checks decide it, a red
        advisory bot never reads as failing) or ``""`` for a row that wasn't probed
        — and ``ci_summary``: the failing check NAMES. The log excerpt
        ``pr_ci_status`` appends after a blank line is dropped — a listing row is a
        triage signal, not a 3000-char build log (board_get_feature / the CI bounce
        carry the full detail).

        COST — the design decision: this is one ``gh`` network round-trip per
        probed feature, so the join is **opt-in** (board_list's ``with_ci`` /
        ``failing_only`` flags), not always-on and not TTL-cached.
          - Always-on would tax EVERY board_list — the PM's hottest read, mostly
            serving states where CI is irrelevant — with N network calls and
            GitHub rate-limit burn.
          - A TTL cache answers "is it red NOW" with stale data — the exact
            question the flag exists to answer (a fresh push flips red → pending →
            green well inside any useful TTL) — and adds mutable cross-call state
            to a store that is otherwise a pure projection over beads.
        Within an opted-in call the cost is still bounded: only NON-terminal
        features carrying a ``pr_url`` are probed (a done/cancelled PR is already
        merged or dead — its rollup is noise, and done rows are the unbounded
        class), and the probes run CONCURRENTLY in one event-loop hop, so
        wall-clock is the slowest single ``gh`` call, not the sum. Best-effort
        like ``pr_ci_status`` itself: a ``gh`` failure reads ``none``, never
        raises into the listing."""
        from . import worktree  # lazy, matching the other cross-module reaches

        for f in feats:
            f.setdefault("ci_status", "")
            f.setdefault("ci_summary", "")
        live = [f for f in feats if f.get("pr_url") and f.get("board_state") not in _TERMINAL_STATES]
        if not live:
            return feats

        async def _probe_all():
            return await asyncio.gather(*(worktree.pr_ci_status(f["pr_url"], cwd=self.repo or ".") for f in live))

        for f, (status, summary) in zip(live, _complete(_probe_all())):
            f["ci_status"] = status
            f["ci_summary"] = summary.split("\n\n", 1)[0]
        return feats

    def raw_features_with_comments(self, states: tuple[str, ...] = ("done", "blocked")) -> list[dict]:
        """Raw ``br`` dicts (WITH ``comments``) for features in the given board states
        — the loop-retro's data source. ``list_features`` projects comments away and
        ``br list`` omits them, so re-fetch each terminal feature via ``br show`` (which
        carries the full comment history — the attempt/outcome record the retro mines).
        Defaults to the terminal states (done + blocked = completed + failed work).

        THE TRAP (#115): this read EXPLICITLY opts into archived features
        (``include_archived=True``). The retro mines ALL completed work — inheriting
        list_features' default archive exclusion would silently turn every
        retrospective into 'the last archive_after_days days'."""
        ids = [f["id"] for f in self.list_features(include_archived=True) if f.get("board_state") in states]
        raw: list[dict] = []
        for fid in ids:
            rows = self._run("show", fid, want_json=True)
            if rows:
                raw.append(rows[0] if isinstance(rows, list) else rows)
        return raw

    def ready_queue(self, relaxed: bool = False) -> list[dict]:
        """Board-`ready`, dep-unblocked **features** (priority order) — the puller's
        queue. `br ready` already excludes a feature with any OPEN `blocks` dep, so by
        default a dependent waits for its blockers to **close** (merge). With
        ``relaxed`` (``dep_gate: review``) also release a dep-blocked feature whose
        every still-open blocker is a NON-foundation feature already at ``in_review``
        — build on code that's in review, not merged. Foundation blockers always gate
        on merge."""
        # `--limit 0` = unlimited: `br ready` defaults `--limit 20`, but the puller's queue
        # must see EVERY ready feature (the exhaustiveness invariant) — a cap here would
        # drop ready work past row 20 from the scan and the relaxed-gate cross-reference.
        ready = self._run("ready", "--label", LABEL_READY, "--limit", "0", want_json=True) or []
        # `br ready --json` omits the labels field (beads-rust ≤0.1.23), so projecting
        # its rows directly makes board_state() see no `ready` label → "backlog", and
        # the puller's `board_state != "ready"` guard self-rejects every candidate (the
        # loop ticks forever but silently never claims). Re-fetch each via get_feature
        # — `br show` carries labels — so board_state/blocked/diff/dag_blocked project
        # correctly. `br ready` is priority-ordered; iterating it preserves that.
        out = [
            f for f in (self.get_feature(b["id"]) for b in ready if b.get("issue_type") == "feature") if f is not None
        ]
        if not relaxed:
            return out
        have = {f["id"] for f in out}
        by_id = {f["id"]: f for f in self.list_features()}
        for fid, f in by_id.items():
            if fid in have or f["board_state"] != "ready" or f["blocked"]:
                continue
            blockers = [by_id.get(d) for d in self._open_blockers(fid)]
            if blockers and all(
                b is not None and not b["foundation"] and b["board_state"] == "in_review" for b in blockers
            ):
                out.append(f)
        return out

    def _open_blockers(self, fid: str) -> list[str]:
        """The ids of `fid`'s still-open `blocks` dependencies (`br list` omits deps,
        so this needs `br show`). A closed blocker has merged → it no longer gates."""
        rows = self._run("show", fid, want_json=True)
        if not rows:
            return []
        bead = rows[0] if isinstance(rows, list) else rows
        return [
            d["id"]
            for d in (bead.get("dependencies") or [])
            if d.get("dependency_type") == "blocks" and d.get("status") != "closed"
        ]

    # ── helpers ───────────────────────────────────────────────────────────────
    def _comment(self, fid: str, text: str) -> None:
        try:
            self._run("comments", "add", fid, text)
        except BoardError:
            log.warning("[project_board] could not add comment to %s", fid)

    def _require(self, fid: str) -> dict:
        f = self.get_feature(fid)
        if f is None:
            raise BoardError(f"unknown feature {fid!r}")
        return f

    def _find_by_external_ref(self, ref: str) -> dict | None:
        # `--limit 0` = unlimited: this scans EVERY feature for the merged PR's url, so a
        # 50-row cap would let record_merge silently miss (never close) a feature past
        # row 50 — the same exhaustiveness invariant as list_features.
        rows = self._run("list", "--limit", "0", want_json=True) or []
        match = next((r for r in rows if r.get("external_ref") == ref), None)
        return self._project(match) if match else None

    @staticmethod
    def board_state(bead: dict) -> str:
        """Project a `br` bead (status + labels) onto a board state."""
        labels = set(bead.get("labels") or [])
        status = bead.get("status")
        if status == "closed":
            # A closed bead is `done` UNLESS it was cancelled (the second terminal edge):
            # a cancel keeps it closed + auditable but distinct from shipped work (#47).
            return "cancelled" if LABEL_CANCELLED in labels else "done"
        if LABEL_BLOCKED in labels:
            return "blocked"
        if status == "in_progress":
            return "in_review" if LABEL_IN_REVIEW in labels else "in_progress"
        if status == "deferred":
            return "backlog"
        return "ready" if LABEL_READY in labels else "backlog"

    def _project(self, bead: dict) -> dict:
        """A `br` bead → the board's feature view (stable shape for the loop/API)."""
        labels = bead.get("labels") or []
        diff = next((l.split(":", 1)[1] for l in labels if l.startswith("diff:")), "")
        # Which project (#90) this feature builds in, from the single `project:<name>`
        # label — "" when unstamped (a pre-#90 feature, or a board with no default),
        # in which case the Ready gate falls back to the instance repo.
        project = next((l[len(LABEL_PROJECT_PREFIX) :] for l in labels if l.startswith(LABEL_PROJECT_PREFIX)), "")
        attempts = sorted(
            int(l.split(":", 1)[1]) for l in labels if l.startswith("attempt:") and l.split(":", 1)[1].isdigit()
        )
        # coder.solve()'s cumulative generation cost (ADR 0064 P2), read from the
        # single replaced `gens:<total>` label — 0 for a feature the seam never touched.
        gens_spent = next(
            (
                int(l[len(LABEL_GENS_PREFIX) :])
                for l in labels
                if l.startswith(LABEL_GENS_PREFIX) and l[len(LABEL_GENS_PREFIX) :].isdigit()
            ),
            0,
        )
        # The crash-salvage record (#91): the sha of the last test-verified candidate,
        # from the single replaced `verified:<sha>` label — "" when none was recorded.
        verified_sha = next(
            (l[len(LABEL_VERIFIED_PREFIX) :] for l in labels if l.startswith(LABEL_VERIFIED_PREFIX)),
            "",
        )
        # The bead `notes` field carries files_to_modify (one path per line), the
        # requirement ledger (#113, one `req: {…}` line per item), AND the
        # originating-issue record (#97) as a `source-issue: owner/repo#N` metadata
        # line — split them apart so neither structured line ever leaks into the
        # file list. source_issue is "" when unset (the loop's PR opener then falls
        # back to scanning the feature text for an issue URL).
        files_to_modify, source_issue, requirements = _split_notes(bead.get("notes"))
        # `dag_blocked`: marked `ready` but a `blocks` dependency is still open, so
        # the puller won't claim it. Only `br show` carries dependencies (`br list`
        # doesn't); list_features patches this by cross-referencing the puller.
        state = self.board_state(bead)
        blocks_edges = [d for d in (bead.get("dependencies") or []) if d.get("dependency_type") == "blocks"]
        dag_blocked = state == "ready" and any(d.get("status") != "closed" for d in blocks_edges)
        # The `blocks` dependency ledger vs. its live subset. `br show` carries
        # dependencies (`br list` omits them, so BOTH are [] in a list projection —
        # read a single feature via get_feature for the real edges). `depends_on` is
        # EVERY blocking edge — the historical ledger, including already-merged
        # (closed) blockers; `open_depends_on` keeps only the edges whose blocker is
        # still open — the live "what is actually blocking me right now" signal.
        depends_on = [d["id"] for d in blocks_edges if d.get("id")]
        open_depends_on = [d["id"] for d in blocks_edges if d.get("id") and d.get("status") != "closed"]
        return {
            "id": bead.get("id"),
            "title": bead.get("title", ""),
            "board_state": state,
            "dag_blocked": dag_blocked,
            "bead_status": bead.get("status"),
            # when the bead closed (`br` exposes it) — the archive pass selects on it,
            # and the board view sorts the Done column most-recent-first by it (#115).
            "closed_at": bead.get("closed_at", ""),
            "spec": bead.get("description", ""),
            "acceptance_criteria": bead.get("acceptance_criteria", ""),
            "design": bead.get("design", ""),
            "files_to_modify": files_to_modify,
            "priority": bead.get("priority", 2),
            "issue_type": bead.get("issue_type", ""),
            "parent": bead.get("parent", ""),
            "pr_url": bead.get("external_ref", ""),
            "assignee": bead.get("assignee", ""),
            "blocked": LABEL_BLOCKED in labels,
            "cancelled": LABEL_CANCELLED in labels,
            # archived is VISIBILITY, not a state: the feature stays done/cancelled;
            # the label only drops it from the default list_features projection (#115).
            "archived": LABEL_ARCHIVED in labels,
            "foundation": LABEL_FOUNDATION in labels,
            "difficulty": diff,
            "depends_on": depends_on,
            "open_depends_on": open_depends_on,
            "attempts": attempts,
            "gens_spent": gens_spent,
            "verified_sha": verified_sha,
            "source_issue": source_issue,
            "requirements": requirements,
            "project": project,
            "labels": labels,
            "repo": self.repo,
            "base_branch": self.base_branch,
        }


def escalation_enabled(cfg: dict) -> bool:
    """Escalation is opt-in: a `coders` map (tier → delegate) with >1 distinct
    delegate. A single ACP coder ⇒ no ladder (one dispatch then Blocked; CI fail
    parks for the operator), so difficulty/tier stay irrelevant — shared by the
    loop (initial dispatch) and the API (`/ci`) so they apply the same policy."""
    coders = (cfg or {}).get("coders") or {}
    return len({str(v) for v in coders.values()}) > 1


# Board cache keyed by workspace (db, repo, base_branch). The loop, API, and tools
# that share a workspace still share one BeadsBoard, but a DIFFERENT db/repo gets
# its own — so a configured `db_path` actually pins the workspace and a config
# reload with a new db gets a fresh board. The old single global ignored its kwargs
# after the first call, collapsing every board onto whichever db the first caller
# happened to use — defeating db_path and any per-instance isolation (ADR 0055 P0).
_BOARDS: dict[tuple[str | None, str, str], BeadsBoard] = {}


def get_store(db: str | None = None, **kw) -> BeadsBoard:
    key = (db or None, kw.get("repo", "."), kw.get("base_branch", "main"))
    board = _BOARDS.get(key)
    if board is None:
        board = BeadsBoard(db, **kw)
        _BOARDS[key] = board
    return board

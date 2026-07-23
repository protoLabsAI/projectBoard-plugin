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

import json
import logging
import os
import re
import shutil
import subprocess
import time

log = logging.getLogger("protoagent.plugins.project_board")

BR = os.environ.get("BR_BIN", "br")

# `br` surfaces a DATABASE_ERROR (SQLite `database is locked`/`busy`) when two br
# processes write the same `.beads/*.db` concurrently (the loop + a tool call, say) —
# transient contention that clears on a short retry. Retry ONLY that class (a bad-arg
# failure is not going to fix itself) with a small exponential backoff so a create/
# update isn't lost to a lock it merely lost the race for.
_DB_RETRY_ATTEMPTS = 4
_DB_RETRY_DELAY = 0.1  # seconds; doubles each retry (0.1 → 0.2 → 0.4)
_DB_CONTENTION_RE = re.compile(r"DATABASE_ERROR|database is (?:locked|busy)", re.IGNORECASE)

# Labels that encode board state / escalation (everything else is free-form).
LABEL_READY = "ready"
LABEL_IN_REVIEW = "in-review"
LABEL_BLOCKED = "blocked"
# A SECOND terminal edge (#47): a feature closed because it was created in error
# (bad decomposition, duplicate, scope cut) — closed like `done`, but tagged so the
# projection shows a distinct `cancelled` state and reconcilers/retro never mistake it
# for shipped work. Preserves the one-Done-edge invariant (only record_merge → `done`).
LABEL_CANCELLED = "cancelled"
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
# Pre-ready DESIGN state (plan M6, optional): a large/architectural feature parked
# while its design/due-diligence is worked out (`mark_designing`). Informational for
# the projection/console — the HARD gate is in `mark_ready` (a design referencing an
# ADR is required at that size before the feature can go ready).
LABEL_DESIGNING = "designing"

# Difficulties whose blast radius demands a written design + an ADR reference before
# the feature may go ready (the M6 DESIGN gate in `mark_ready`).
DESIGN_GATED_DIFFICULTIES = ("large", "architectural")
# What counts as "references an ADR": `ADR 0076` / `ADR-76` / `adr/0076` /
# a `docs/adr/0076-…` path — case-insensitive, number required.
ADR_REF_RE = re.compile(r"(?i)\badr[\s/_-]{0,2}\d{1,4}\b|docs/adr/\d{4}-")
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
# The ORIGINATING GitHub issue (#97) — `source:owner/repo#N`, a single replaced label
# (the `gens:`/`verified:` pattern). Set through create/update's `source_issue`,
# projected back as the `source_issue` field the loop's PR opener reads to stamp
# `Fixes #N` (same-repo) / `Refs <url>` (cross-repo) on the PR body; absent, the
# opener falls back to scanning the feature text for an issue URL.
LABEL_SOURCE_PREFIX = "source:"
# What `source_issue` accepts: a full GitHub issue URL, or the `owner/repo#N`
# shorthand it normalizes to. Anything else (a bare number, a PR url, free text) is
# rejected with a named error — the field is explicit provenance, so a value that
# can't name ONE exact issue must fail loudly, not store junk the PR opener would
# silently drop.
_SOURCE_ISSUE_URL_RE = re.compile(r"https://github\.com/([^/\s#]+)/([^/\s#]+)/issues/(\d+)/?")
_SOURCE_ISSUE_SLUG_RE = re.compile(r"[^/\s#]+/[^/\s#]+#\d+")

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


class BeadsBoard:
    """Wraps the `br` CLI. One process-wide instance (the loop, API, and tools share
    it). `br` auto-discovers `.beads/*.db`; pass ``db`` to pin a workspace."""

    def __init__(self, db: str | None = None, actor: str = "agent", repo: str = ".", base_branch: str = "main"):
        if not shutil.which(BR):
            raise BoardError(
                f"beads CLI {BR!r} not on PATH — install beads-rust (`cargo install beads_rust`), "
                "not the homebrew `bd`, or set BR_BIN"
            )
        self.db = db or None
        self.actor = actor
        self.repo = repo
        self.base_branch = base_branch
        self._workspace_ready = False  # lazily pinned on first _run (see _ensure_workspace)

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
        # any other non-zero exit raises immediately.
        delay = _DB_RETRY_DELAY
        for attempt in range(_DB_RETRY_ATTEMPTS):
            proc = subprocess.run(cmd, cwd=self.repo or ".", capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                break
            err = proc.stderr.strip()
            if attempt < _DB_RETRY_ATTEMPTS - 1 and _DB_CONTENTION_RE.search(err):
                log.warning(
                    "[project_board] `br %s` hit DB contention (attempt %d/%d) — backing off %.2fs: %s",
                    args[0] if args else "",
                    attempt + 1,
                    _DB_RETRY_ATTEMPTS,
                    delay,
                    err[:120],
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise BoardError(f"`br {' '.join(args)}` failed: {err[:300]}")
        if not want_json:
            return proc.stdout.strip()
        # `br` prefixes some JSON with INFO log lines on stderr; stdout is clean JSON.
        out = proc.stdout.strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError as exc:
            raise BoardError(f"`br {args[0]}` returned non-JSON: {exc} :: {out[:200]}")

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
    ) -> dict:
        """Create a feature bead (starts in `backlog`). Provide a self-sufficient
        spec + acceptance_criteria + the explicit files to create/modify so it can
        pass the Ready gate (ProtoMaker's spec-quality discipline — vague tasks make
        a coder produce nothing). Mark `foundation=True` for a feature others build on
        (dependents gate on its merge, never its review). `source_issue` names the
        ORIGINATING GitHub issue (a full issue URL or `owner/repo#N`, stored
        normalized) so the PR opener can stamp `Fixes #N` on the feature's PR (#97)."""
        # Normalize BEFORE minting the bead: an invalid source_issue must reject the
        # whole create with a named error, never leave an orphan bead behind it.
        src = normalize_source_issue(source_issue) if str(source_issue or "").strip() else ""
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
        if files_to_modify:
            # files_to_modify lives in the bead `notes` field, one path per line.
            notes = "\n".join(str(p).strip() for p in files_to_modify if str(p).strip())
            upd += [f"--notes={notes}"]
            enriched.append("files_to_modify")
        diff = difficulty.strip().lower()
        if diff:
            # normalize first, then guard: a whitespace-only difficulty must NOT stamp a
            # malformed `diff:` label (an empty tier corrupts the escalation ladder).
            upd += ["--add-label", f"diff:{diff}"]
            enriched.append("difficulty")
        if foundation:
            upd += ["--add-label", LABEL_FOUNDATION]
            enriched.append("foundation")
        if src:
            upd += ["--add-label", f"{LABEL_SOURCE_PREFIX}{src}"]
            enriched.append("source_issue")
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

        # ── phase 3: promote ONLY the cleanly-created items ──────────────────────
        if mark_ready:
            for _i, _item, f in created:
                r = result_by_id[f["id"]]
                if r.get("enrichment_failed"):
                    continue  # a warned item isn't clean → don't auto-promote it
                try:
                    self.mark_ready(f["id"])
                    r["board_state"] = "ready"
                    r["ready"] = True
                except BoardError as exc:
                    r["ready"] = False
                    r["ready_error"] = str(exc)

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
        cancelled and recreated from scratch. ``depends_on`` ADDS blocking edges and
        ``foundation=True`` adds the foundation label (None/False = untouched) — the
        repair half of create's success-with-warning contract. ``source_issue`` (a
        full GitHub issue URL or ``owner/repo#N``) sets/replaces the originating-issue
        record the PR opener stamps as ``Fixes #N`` (#97)."""
        f = self._require(fid)
        args = ["update", fid]
        # Free-text VALUES ride in `--flag=value` form so a value STARTING WITH '-' (a
        # markdown bullet, a leading-dash path) can't be mis-parsed as a CLI option and
        # fail the update (#85) — the same hardening as create_feature's enrichment. Labels
        # never start with '-', so `--add/remove-label` stay in the plain form below.
        if spec is not None:
            args += [f"--description={spec}"]
        if acceptance_criteria is not None:
            args += [f"--acceptance-criteria={acceptance_criteria}"]
        if design is not None:
            args += [f"--design={design}"]
        if files_to_modify is not None:
            # files_to_modify lives in the bead `notes` field, one path per line.
            notes = "\n".join(str(p).strip() for p in files_to_modify if str(p).strip())
            args += [f"--notes={notes}"]
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
        if source_issue is not None and str(source_issue).strip():
            # An invalid value raises the named error BEFORE `br update` runs, so a bad
            # source_issue never half-applies a mixed update. Whitespace-only = no-op
            # (the difficulty convention). Single replaced label — the `diff:` pattern.
            src = normalize_source_issue(source_issue)
            for stale in [l for l in f.get("labels") or [] if l.startswith(LABEL_SOURCE_PREFIX)]:
                args += ["--remove-label", stale]
            args += ["--add-label", f"{LABEL_SOURCE_PREFIX}{src}"]
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
    def mark_ready(self, fid: str) -> dict:
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
        ready = self._run("ready", "--label", LABEL_READY, want_json=True) or []
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
        ``changes-requested``) — exactly one (or none) at a time. ``note`` (the
        findings block, a clean-review line) is recorded as a comment so the
        review history lives on the bead."""
        self._require(fid)
        args = ["update", fid]
        for known in (LABEL_REVIEW_PENDING, LABEL_CHANGES_REQUESTED):
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
            self._run("close", f["id"], "-r", f"merged: {pr_url}")
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
        just re-closes it."""
        self._require(fid)
        self._run("update", fid, "--add-label", LABEL_CANCELLED, "--assignee", "")
        self._run("close", fid, "-r", f"cancelled: {reason}" if reason else "cancelled")
        return self.get_feature(fid)

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

    # ── reads (the projection) ────────────────────────────────────────────────
    def get_feature(self, fid: str) -> dict | None:
        rows = self._run("show", fid, want_json=True)
        if not rows:
            return None
        return self._project(rows[0] if isinstance(rows, list) else rows)

    def list_features(self, state: str | None = None) -> list[dict]:
        # All statuses — `br list` defaults to open/in_progress, but the board view
        # needs `closed` features too (that's the Done column).
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
                want_json=True,
            )
            or []
        )
        out = [self._project(r) for r in rows]
        # `br list` omits dependencies, so mark dag_blocked by cross-referencing the
        # puller: a `ready` feature the puller WON'T claim is blocked by an open dep.
        claimable = {f["id"] for f in self.ready_queue()}
        for f in out:
            if f["board_state"] == "ready" and f["id"] not in claimable:
                f["dag_blocked"] = True
        if state:
            out = [f for f in out if f["board_state"] == state]
        out.sort(key=lambda f: (f["priority"], f["id"]))
        return out

    def raw_features_with_comments(self, states: tuple[str, ...] = ("done", "blocked")) -> list[dict]:
        """Raw ``br`` dicts (WITH ``comments``) for features in the given board states
        — the loop-retro's data source. ``list_features`` projects comments away and
        ``br list`` omits them, so re-fetch each terminal feature via ``br show`` (which
        carries the full comment history — the attempt/outcome record the retro mines).
        Defaults to the terminal states (done + blocked = completed + failed work)."""
        ids = [f["id"] for f in self.list_features() if f.get("board_state") in states]
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
        ready = self._run("ready", "--label", LABEL_READY, want_json=True) or []
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
        rows = self._run("list", want_json=True) or []
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
        # `dag_blocked`: marked `ready` but a `blocks` dependency is still open, so
        # the puller won't claim it. Only `br show` carries dependencies (`br list`
        # doesn't); list_features patches this by cross-referencing the puller.
        state = self.board_state(bead)
        dag_blocked = state == "ready" and any(
            d.get("dependency_type") == "blocks" and d.get("status") != "closed"
            for d in (bead.get("dependencies") or [])
        )
        return {
            "id": bead.get("id"),
            "title": bead.get("title", ""),
            "board_state": state,
            "dag_blocked": dag_blocked,
            "bead_status": bead.get("status"),
            "spec": bead.get("description", ""),
            "acceptance_criteria": bead.get("acceptance_criteria", ""),
            "design": bead.get("design", ""),
            "files_to_modify": [l.strip() for l in (bead.get("notes") or "").splitlines() if l.strip()],
            "priority": bead.get("priority", 2),
            "issue_type": bead.get("issue_type", ""),
            "parent": bead.get("parent", ""),
            "pr_url": bead.get("external_ref", ""),
            "assignee": bead.get("assignee", ""),
            "blocked": LABEL_BLOCKED in labels,
            "cancelled": LABEL_CANCELLED in labels,
            "foundation": LABEL_FOUNDATION in labels,
            "difficulty": diff,
            "attempts": attempts,
            "gens_spent": gens_spent,
            "verified_sha": verified_sha,
            # The originating issue (#97): normalized `owner/repo#N` from the single
            # replaced `source:` label — "" when unset (the loop's PR opener then falls
            # back to scanning the feature text for an issue URL).
            "source_issue": next(
                (l[len(LABEL_SOURCE_PREFIX) :] for l in labels if l.startswith(LABEL_SOURCE_PREFIX)),
                "",
            ),
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

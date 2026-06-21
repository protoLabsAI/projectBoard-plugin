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
import shutil
import subprocess

log = logging.getLogger("protoagent.plugins.project_board")

BR = os.environ.get("BR_BIN", "br")

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

# difficulty → initial model tier (the escalation ladder's first rung, D10).
DIFFICULTY_TIER = {"small": "smart", "medium": "reasoning", "large": "reasoning", "architectural": "opus"}
TIER_LADDER = ["smart", "reasoning", "opus"]


class BoardError(Exception):
    """A rejected op (bad gate, unknown feature, `br` failure). Caller → 4xx / tool error."""


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
        proc = subprocess.run(cmd, cwd=self.repo or ".", capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise BoardError(f"`br {' '.join(args)}` failed: {proc.stderr.strip()[:300]}")
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
    ) -> dict:
        """Create a feature bead (starts in `backlog`). Provide a self-sufficient
        spec + acceptance_criteria + the explicit files to create/modify so it can
        pass the Ready gate (ProtoMaker's spec-quality discipline — vague tasks make
        a coder produce nothing). Mark `foundation=True` for a feature others build on
        (dependents gate on its merge, never its review)."""
        fid = self._create(title, itype="feature", parent=parent, priority=priority, description=spec)
        upd = []
        if acceptance_criteria:
            upd += ["--acceptance-criteria", acceptance_criteria]
        if design:
            upd += ["--design", design]
        if files_to_modify:
            # files_to_modify lives in the bead `notes` field, one path per line.
            upd += ["--notes", "\n".join(str(p).strip() for p in files_to_modify if str(p).strip())]
        if difficulty:
            upd += ["--add-label", f"diff:{difficulty.strip().lower()}"]
        if foundation:
            upd += ["--add-label", LABEL_FOUNDATION]
        if upd:
            self._run("update", fid, *upd)
        for dep in depends_on or ():
            self.add_dependency(fid, dep)
        return self.get_feature(fid)

    def add_dependency(self, fid: str, depends_on: str) -> None:
        """`fid` is blocked until `depends_on` is **closed** (`blocks` edge). This is
        also how a *foundation* gate is expressed: dependents carry a blocks-edge on
        the foundation feature, so they only become `ready` once it merges → done."""
        self._run("dep", "add", fid, depends_on, "--type", "blocks")

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
                "to create/modify (a junior — or a coding agent — could pick it up and finish)."
            )
        self._run("update", fid, "--add-label", LABEL_READY)
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

"""The orchestration loop — the deterministic state machine around the spine.

A background surface (ADR 0018 ``register_surface``) that pulls ``ready`` features
and drives each: worktree → coder → PR → review. It is the ONLY thing that moves a
feature forward through the build states; ``done`` it never sets — that is the
merge webhook (``api.record_merge``), the single external edge (invariant #2).

    ready ──claim──▶ in_progress
       worktree add  →  delegate_to(coder) in worktree  →  push + gh pr create
       │                       [finally: reap coder subprocess]
       └──▶ in_review  ──delegate_to(reviewer)──▶  (CI + review on the PR)
                 │
   merge webhook ▼                 CI fail ▼                 any failure ▼
   /merge poll                in_progress (bounce)     blocked (flag + reason)
              done

CI status arrives out-of-band via the board API (``api.py``). ``done`` is set by
the merge webhook (``api.record_merge``) — or, when no public webhook URL is
reachable, by the loop's **PR reconcile** (``merge_poll``), which asks ``gh`` for
each ``in_review`` PR's state and drives the terminal edges: merged → done (the same
idempotent edge), closed-unmerged → blocked. Up to ``max_concurrent`` features build
concurrently, each in its own worktree.

**coder.solve() board seam (ADR 0064 P2, opt-in, see ``coder_seam.py``).** On a
fresh build (not a keep-worktree/CI-bounce re-dispatch), when the `coder` plugin is
importable AND the feature has acceptance criteria AND a runnable acceptance-test
command is configured, ``delegate_to(coder)`` is replaced by
``coder_seam.dispatch()`` — an execution-grounded ladder (greedy → best-of-k →
tree-search) that runs the feature's acceptance tests in real candidate worktrees
and gates on them actually PASSING, never an LLM judge. It composes WITH the
`coders`-map tier ladder below (search happens WITHIN a tier; a search that never
passes is a capability failure that escalates/blocks exactly like a no-diff
dispatch). Missing any of the three gates ⇒ honest degrade to the single shot above.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import types

from .. import br_fetch, coder_seam, config, health, setup_check, work_snapshot, worktree
from ..failures import PRE_MODEL_DISPATCH_CLASS, classify, is_pre_model_dispatch_failure
from ..projects import default_project as resolve_default_project
from ..projects import resolve_projects
from .. import store as store_mod
from ..store import (
    BoardError,
    LABEL_CHANGES_REQUESTED,
    LABEL_MERGED_VERIFIED_PREFIX,
    LABEL_REVIEW_CLEAN,
    LABEL_REVIEW_PENDING,
    LABEL_REVIEWED_HEAD_PREFIX,
    LABEL_TASK,
    _all_items_disposed,
    apply_requirement_dispositions,
    budgets_from_labels,
    escalation_enabled,
    get_store,
    knob_bool,
    merge_posture,
    reconfigure_cached_store,
)

log = logging.getLogger("protoagent.plugins.project_board")

# The merged-state verdict stamp (#131) rides a ``merged-verified:<sha>`` LABEL, and
# beads caps labels at 50 chars. The 16-char prefix + a full 40-char sha = 56 chars, so
# EVERY real write died VALIDATION_FAILED (#135) — the stamp #132 promised was silently
# never written (the fake-``_run`` unit tests accepted any label and hid it). A 12-char
# short sha (28 chars total) fits with room to spare and still uniquely identifies an
# origin/<base> commit (git's own default abbreviation). The read-back comparison in
# ``_verify_merged_state`` truncates the live origin/<base> to the SAME width so the
# ``stamped == current`` currency check stays exact.
_MERGED_VERIFIED_SHA_LEN = 12

# The review-verdict head stamp (#328) rides a ``reviewed-head:<sha>`` LABEL under the
# SAME 50-char beads label cap that forced ``merged-verified:`` short (#135) — so the PR
# head sha is abbreviated to the same 12-char width, and the reconcile's stale-verdict
# check truncates the live head to this width before comparing (``stamped == current``
# stays exact only when both sides use the same abbreviation).
_REVIEWED_HEAD_SHA_LEN = 12

# The title the review gate renders its blocking-findings block under (``_render_findings``)
# and records on the bead alongside ``changes-requested`` (set_review_substate's ``note``).
# The #340 recovery scans the comment history for this anchor to re-derive the fix feedback
# a shutdown dropped from the in-memory ``_ci_feedback`` — so the two MUST stay in sync.
_REVIEW_FINDINGS_TITLE = "Review findings (blocking)"

# After this many consecutive failed reap attempts for the same worktree path, stop
# logging at WARNING and downgrade to DEBUG to avoid log spam (e.g. 464 lines for
# one stuck path — the bug this constant defends against).
_REAP_WARN_CAP = 5

# The block-reason prefix a preflight hold stamps on a card (#90) — the marker boot
# recovery scans for (#186). The holds live in the loop's in-memory `_preflight_held`,
# which dies with the process: without this marker a restart could never tell its own
# holds from an operator's blocks, and the cards would stay held forever (blocked cards
# are invisible to both `_ready_projects` and a fresh `_preflight_state`).
PREFLIGHT_BLOCK_PREFIX = "gate preflight failed"

# `flag_blocked` records its reason as a `blocked: <reason>` bead comment (the same
# format retro.py mines); the LAST such comment is the card's CURRENT block reason.
_BLOCKED_COMMENT_RE = re.compile(r"^\s*blocked:\s*(.*)", re.I | re.S)


def _last_block_reason(feat: dict) -> str:
    """The most recent ``blocked: <reason>`` comment on a raw ``br`` dict — a card
    requeued and re-blocked carries older reasons too, and only the last one says why
    it is blocked NOW. Returns "" when no reason was ever recorded."""
    reason = ""
    for c in feat.get("comments") or []:
        text = (c.get("text") or c.get("body") or c.get("content") or "") if isinstance(c, dict) else str(c or "")
        m = _BLOCKED_COMMENT_RE.match(text.strip())
        if m:
            reason = m.group(1).strip()
    return reason


# ── external re-dispatch feedback bridge (the /review route → the loop) ──────────
# An adverse-review bounce POSTed to /features/{fid}/review is handled in the API
# router — a DIFFERENT object from the running loop (register() mounts both, so they
# share a process but not an instance). This dict is the seam between them: the
# router stashes the findings here and the loop drains them into its per-run
# ``_ci_feedback`` the next time it builds a dispatch prompt — the same lever the
# in-loop review gate writes directly. Keyed by feature id; last write wins.
#
# A plain module global here was reload-unstable (#256): a plugin reload re-imports
# this module as a FRESH object while the running loop keeps the old one, so the
# newly-mounted router wrote a NEW dict and the loop drained the old — the findings
# were silently stranded. The dict now lives on a process-stable ``sys.modules``
# data slot (the coder_seam #178 pattern, same as ``_drive_slot`` below), which a
# reload never replaces — every module instance binds the SAME dict. Shared, not
# copied: a copy would orphan the other instance's future writes all over again.
_FEEDBACK_SLOT_PREFIX = "project_board.review_feedback::"


def _feedback_slot():
    # The plugin-root package (`project_board` / the host's `protoagent_plugin_<id>`),
    # NOT the module's parent. loop.py used to live at `<root>.loop`, so its
    # `rsplit(".", 1)[0]` was the root; after the #268 split this module is
    # `<root>.loop._common`, so we take the first component to keep the SAME
    # process-stable slot key the monolith used (a reload must find the same slot).
    pkg = __name__.split(".")[0] if "." in __name__ else __name__
    name = _FEEDBACK_SLOT_PREFIX + pkg
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = (
            "Process-stable holder for project_board's queued review-bounce findings (#256) — data, not code."
        )
        holder.pending = {}
        holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder


_PENDING_FEEDBACK: dict[str, str] = _feedback_slot().pending


def queue_review_feedback(fid: str, findings: str) -> None:
    """Stash an adverse-review bounce's ``findings`` so the loop leads ``fid``'s next
    dispatch prompt with them — the cross-instance sibling of the in-loop review
    gate's ``_ci_feedback`` write (``POST /features/{fid}/review`` calls this). Blank
    findings are a no-op (nothing to carry back)."""
    text = str(findings or "").strip()
    if not text:
        return
    _PENDING_FEEDBACK[fid] = (
        "An adverse code review REQUESTED CHANGES on your PR. Fix every finding "
        "below in the existing branch (the PR updates on push) — do not rewrite "
        "unrelated code.\n\n" + text
    )


def _parse_gate_files(raw: object) -> list[str]:
    """Read ``project_board.gate_files`` — the repo standing gate files (#108) that
    ride every coder prompt regardless of a card's ``files_to_modify``.

    Accepts a list/tuple of paths, or a single comma-/whitespace-separated string
    (so both ``["CHANGELOG.md", "docs/api.json"]`` and ``"CHANGELOG.md docs/api.json"``
    work — mirrors ``config.parse_env_passthrough``). Returns a de-duplicated list,
    order preserved. Missing/blank ⇒ empty (the documented default)."""
    if not raw:
        return []
    parts = raw.replace(",", " ").split() if isinstance(raw, str) else raw
    seen: dict[str, None] = {}
    for name in parts:
        name = str(name).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


# ── auto gate resolution ────────────────────────────────────────────────────────
# The pre-PR gate is repo-specific, and hard-coding one repo's check steps into the
# orchestrator (or the operator's dispatch) rots two ways: the repo's CI changes and
# the transcription silently goes stale (green-locally / red-in-CI), or the same team
# is pointed at a DIFFERENT repo and the gate is simply wrong. So ``local_gate_cmd:
# "auto"`` asks the loop to DISCOVER the gate from the bound checkout.
#
# WHAT the gate is (and isn't): the coder's iterate-to-green loop, so it must be the
# FAST, HERMETIC, deterministic slice of CI — lint + typecheck + unit tests, runnable
# in a worktree in minutes with no services/secrets/matrix/image-builds. It is NOT a
# full-CI replica. A complex CI's heavy jobs (integration, cross-platform matrix,
# docker publish, release, deploy) stay CI-only; they run once on the PR as the human's
# merge gate, and anything the local slice missed comes back via the CI-bounce re-
# dispatch. So a repo with a big CI declares a dedicated ``gate`` target = that fast
# slice, distinct from a heavy ``ci`` — which is why ``gate`` is the top precedence.
#
# ECOSYSTEM-NEUTRAL: node is just one case. The contract is "declare ONE gate target
# your own CI also calls"; the runner is inferred from how the repo builds:
#   1. package.json script  gate / ci / check / verify   → ``pnpm run <name>``   (node)
#   2. Makefile / justfile   gate / ci / check target     → ``make <name>`` / ``just <name>``
#      (this is the path for Python / Rust / Go / anything — e.g. `make gate` =
#       `ruff check . && pytest -q`)
#   3. package.json present, none declared               → ``pnpm -r --if-present typecheck build test``
#   4. nothing recognized                                → "" (no gate; fail-open, warns)
# An explicit command always passes through unchanged; blank still means "no gate".
# Resolved once at construction (the coder only ever touches worktrees, so the bound
# checkout is a stable base); the deployment clones the repo before the loop starts.
_PNPM_INSTALL = "pnpm install --frozen-lockfile --prefer-offline"
# Precedence of DECLARED target names. ``gate`` first: it is the unambiguous "this is
# the pre-PR coder gate (the fast slice)", so a repo whose ``ci`` is the whole heavy
# suite can point coders at ``gate`` without the loop grabbing the heavy target.
_GATE_TARGET_NAMES = ("gate", "ci", "check", "verify")


def _resolve_gate_cmd(raw: str, repo_path: str) -> str:
    """Resolve ``local_gate_cmd``. Only the sentinel ``"auto"`` triggers discovery;
    an explicit command (or blank = no gate) is returned unchanged."""
    raw = (raw or "").strip()
    if raw != "auto":
        return raw
    pkg = os.path.join(repo_path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                scripts = (json.load(fh) or {}).get("scripts", {}) or {}
        except (OSError, ValueError):
            scripts = {}
        for name in _GATE_TARGET_NAMES:
            if name in scripts:
                return f"{_PNPM_INSTALL} && pnpm run {name}"
        # No declared entrypoint — run the standard checks any workspace exposes.
        # ``-r --if-present`` self-skips workspaces missing the script, so this is a
        # safe superset: a repo with only tests runs only tests.
        return (
            f"{_PNPM_INSTALL} && pnpm -r --if-present typecheck "
            "&& pnpm -r --if-present build && pnpm -r --if-present test"
        )
    for fname, runner in (("Makefile", "make"), ("makefile", "make"), ("justfile", "just"), ("Justfile", "just")):
        fpath = os.path.join(repo_path, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath, encoding="utf-8") as fh:
                    body = fh.read()
            except OSError:
                body = ""
            for target in _GATE_TARGET_NAMES:
                if re.search(rf"(?m)^{target}:", body):
                    return f"{runner} {target}"
    log.warning(
        "[project_board] local_gate_cmd=auto but no gate could be discovered in %s "
        "(no package.json gate/ci/check script, no Makefile/justfile gate/ci target) — "
        "running gateless. Declare a `gate` target (e.g. `make gate` = lint + unit tests) "
        "to make this repo team-ready.",
        repo_path,
    )
    return ""


# Deterministic test-coverage gate (path-based — no LLM, no diff). A code change must
# ship a test; checking the changed-file LIST is instant and immune to the truncation
# that made the old LLM-eyeballs-the-diff verifier false-reject tests it couldn't see.
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)(test_[^/]+|conftest)\.py$|(^|/)[^/]+_test\.py$|\.(test|spec)\.[jt]sx?$")
_CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs")


def _is_test_path(p: str) -> bool:
    return bool(_TEST_PATH_RE.search(p))


def _is_code_path(p: str) -> bool:
    return p.endswith(_CODE_EXTS)


# Error/summary lines worth keeping from a failing CI log — the ones that name the
# ACTUAL failure (pytest's "FAILED … - AssertionError: golden field map …", ruff's
# "F841"/"would reformat", a conflict, version drift) so the attempt comment the retro
# mines is CLASSIFIABLE, not a generic "checks red".
_CI_SIGNAL_RE = re.compile(
    r"FAILED|Error|assert|\bF\d{3}\b|reformat|no column|out of sync|conflict|drift|lint-imports", re.I
)


def _ci_failure_reason(summary: str, max_chars: int = 500) -> str:
    """Distill a CI summary into a compact but classifiable failure reason for the
    attempt comment (the loop-retro mines these to bucket recurring failures).

    The useful signal is NOT the ``Failing checks:`` header — it's the failing check
    NAMES plus the tail of the failing log, where pytest/ruff print the real error.
    Falls back to the header / ``checks red`` when there's nothing better."""
    if not summary:
        return "checks red"
    checks = [ln[2:].strip() for ln in summary.splitlines() if ln.startswith("- ")]
    head = "; ".join(checks) if checks else summary.splitlines()[0].strip()
    detail = ""
    if "Failing log" in summary:
        log = summary.split("Failing log", 1)[1]
        errs = [ln.strip() for ln in log.splitlines() if ln.strip() and _CI_SIGNAL_RE.search(ln)]
        if errs:
            detail = " · ".join(errs[-4:])
        else:
            tail = [ln.strip() for ln in log.splitlines() if ln.strip()]
            detail = tail[-1] if tail else ""
    reason = f"{head} — {detail}" if detail else head
    return reason[:max_chars]


_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _parse_pr_url(pr_url: str) -> tuple[str, str]:
    """``https://github.com/owner/name/pull/123`` → ``("123", "owner/name")``;
    ``("", "")`` when it doesn't look like a GitHub PR url."""
    m = _PR_URL_RE.search(pr_url or "")
    return (m.group(2), m.group(1)) if m else ("", "")


# The coder is asked to END its reply with a `## Summary` section — but the reply is
# the whole ACP message, so the summary sits after pages of step-by-step narration.
# Match the heading at line start; keep from the LAST occurrence so an early mention
# of the phrase mid-narration doesn't truncate the real section (#56).
_SUMMARY_HEADING_RE = re.compile(r"^##\s*Summary\b", re.MULTILINE)

# The requirement-ledger disposition section (#113) — the `## Summary` pattern's
# sibling: the coder reports one line per item id (`- r2: done`, `- r3: declined —
# <reason>`). Same last-occurrence discipline as the summary; the section ends at
# the next `## ` heading. Only the CLOSED statuses parse — silence (or an explicit
# `open`) is not a disposition, so an unreported item stays open on the ledger.
_REQ_HEADING_RE = re.compile(r"^##\s*Requirements\b", re.MULTILINE)
_REQ_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?`?(?P<id>[A-Za-z0-9][\w.-]*)`?\s*[:\-—–]\s*"
    r"(?P<status>done|declined)\b\s*(?:[:\-—–]\s*)?(?P<reason>.*)$",
    re.IGNORECASE,
)


def _parse_requirements_reply(text: str) -> list[dict]:
    """Parse the coder reply's ``## Requirements`` section into disposition dicts
    (`{id, status, decline_reason?}`) for ``apply_requirement_dispositions``. Keeps
    the LAST such heading (a mid-narration mention must not shadow the real section,
    the #56 lesson), reads until the next heading, and skips any line that isn't a
    well-formed `<id>: done|declined [— reason]` row — a malformed row is silence,
    and silence is not disposition. No section → no dispositions."""
    headings = list(_REQ_HEADING_RE.finditer(text or ""))
    if not headings:
        return []
    out: list[dict] = []
    for line in text[headings[-1].end() :].splitlines():
        if line.strip().startswith("##"):
            break  # the next section — the ledger block ended
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        d = {"id": m.group("id"), "status": m.group("status").lower()}
        reason = m.group("reason").strip()
        if d["status"] == "declined" and reason:
            d["decline_reason"] = reason
        out.append(d)
    return out


def _requirement_gate_diagnostics(result: str, open_items: list[dict]) -> dict:
    """The requirement-gate diagnostic payload (#284). When the completion gate bounces
    a feature for unresolved dispositions, these are the fields that tell a *parse* miss
    (the coder wrote a ``## Requirements`` section the loop failed to read) apart from
    genuine *silence* (no section at all): the dispositions the loop DID parse, the ids
    still open, the reply length, whether a ``## Requirements`` heading is present, and
    the first 200 chars after the LAST such heading (the same last-occurrence discipline
    as ``_parse_requirements_reply``). Shared by the gate's INFO log and the persisted
    bounce comment so the card and the log always report the identical picture."""
    text = result or ""
    headings = list(_REQ_HEADING_RE.finditer(text))
    return {
        "dispositions": _parse_requirements_reply(text),
        "open_ids": [i.get("id") for i in open_items],
        "result_len": len(text),
        "has_requirements_heading": bool(headings),
        "after_heading": text[headings[-1].end() :][:200] if headings else "",
    }


def _requirement_gate_diag_line(diag: dict) -> str:
    """One-line rendering of ``_requirement_gate_diagnostics`` for BOTH the gate's INFO
    log and the persisted bounce comment — one source so the two can never drift."""
    return (
        f"dispositions={diag['dispositions']!r} "
        f"open_ids={diag['open_ids']!r} "
        f"len(result)={diag['result_len']} "
        f"has_requirements_heading={diag['has_requirements_heading']} "
        f"after_heading={diag['after_heading']!r}"
    )


def _pr_body(result: str, feature: dict) -> str:
    """The feature PR's description: the coder's ``## Summary`` section, never the
    raw output stream. Control-marker lines (``NO_TEST_NEEDED: …``) are dropped from
    the kept text; with no summary heading at all, a short template stands in — the
    raw reply is never the fallback."""
    headings = list(_SUMMARY_HEADING_RE.finditer(result or ""))
    if headings:
        kept = result[headings[-1].start() :]
        lines = [ln for ln in kept.splitlines() if not ln.strip().startswith("NO_TEST_NEEDED")]
        body = "\n".join(lines).strip()
    else:
        body = f"## Summary\n\n{feature.get('title') or ''} (`{feature.get('id') or ''}`)\n\nSee the diff for details."
    return body[:4000]


# The NO_TEST_NEEDED escape hatch (#264) — structural evidence, not a substring
# scan: the marker only counts when it stands at line start inside the FINAL
# ``## Summary`` section (the ``_pr_body`` last-occurrence discipline), which ends
# at the next heading of ANY level — ``#`` through ``######``, not just ``##``,
# else a marker parked under a later ``# Appendix`` would slip past the gate. A
# mention mid-narration, or one outside the summary, is prose — not a declaration —
# and the reason is mandatory: a bare marker carries no evidence.
_NO_TEST_MARKER_RE = re.compile(r"^NO_TEST_NEEDED\s*:\s*(?P<reason>\S.*)$")


def _no_test_marker(reply: str) -> str | None:
    """The coder's ``NO_TEST_NEEDED: <reason>`` declaration, or ``None``. Keeps the
    LAST ``## Summary`` heading (a mid-narration mention must not shadow the real
    section), reads until the next heading of ANY level — a marker in a LATER
    section is outside the summary and must not count — and accepts only a
    line-start ``NO_TEST_NEEDED: <reason>`` row inside it — anything else is
    narration, and narration is not a declaration."""
    headings = list(_SUMMARY_HEADING_RE.finditer(reply or ""))
    if not headings:
        return None
    for line in reply[headings[-1].end() :].splitlines():
        if line.strip().startswith("#"):
            break  # the next section, at any heading level — the summary block ended
        m = _NO_TEST_MARKER_RE.match(line.strip())
        if m:
            return m.group("reason").strip()
    return None


# ── source-issue → PR "Fixes #N" line (pure metadata; the coder never touches it) ─
# At PR-open the loop stamps the ORIGINATING issue onto the generated body itself.
# The source issue is either an explicit ``source_issue`` field or the FIRST GitHub
# issue URL in the feature's text. When that issue lives in the PR's OWN target repo,
# ``Fixes #N`` (GitHub's repo-scoped closing keyword) auto-closes it on merge; a
# cross-repo issue can't be closed by a bare ``#N`` there, so it gets a plain
# ``Refs <full-url>`` link instead. One line of pure metadata — no coder round-trip.
_ISSUE_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)")
# GitHub's issue-closing keywords (close/fix/resolve + their conjugations).
_CLOSING_KW = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


def _source_issue(feature: dict) -> tuple[str, int] | None:
    """The issue this feature's PR should reference: ``(slug, n)`` — ``slug`` is the
    issue's ``owner/repo`` (``""`` when the source is a bare number ⇒ same repo) and
    ``n`` the issue number — or ``None`` when the feature names no source issue.

    Precedence: an explicit ``source_issue`` field (a full URL, ``owner/repo#n``, or a
    bare ``#n``/``n``) wins; otherwise the FIRST GitHub issue URL in the feature text.
    The explicit field is the store's projection of the bead-notes ``source-issue:``
    metadata line (#101 — beads' label charset can't carry ``/``/``#``, so the record
    lives off-label in `notes`); this reader only ever sees the projected value."""
    raw = str(feature.get("source_issue") or "").strip()
    if raw:
        m = _ISSUE_URL_RE.search(raw)
        if m:
            return (m.group(1), int(m.group(2)))
        m = re.fullmatch(r"([^/\s]+/[^/\s]+)#(\d+)", raw)
        if m:
            return (m.group(1), int(m.group(2)))
        m = re.fullmatch(r"#?(\d+)", raw)
        if m:
            return ("", int(m.group(1)))
        return None
    text = "\n".join(
        str(feature.get(k) or "") for k in ("description", "spec", "design", "acceptance_criteria", "title")
    )
    m = _ISSUE_URL_RE.search(text)
    return (m.group(1), int(m.group(2))) if m else None


def _inject_source_issue_line(body: str, issue_slug: str, n: int, target_repo: str) -> str:
    """Append the source-issue reference to ``body`` (idempotently).

    Same-repo (the issue's ``owner/repo`` equals the PR's ``target_repo``, or the
    source was a bare number) ⇒ ``Fixes #n`` (auto-closes on merge); cross-repo ⇒
    ``Refs <full-url>`` — a bare ``#n`` can't refer to another repo's issue, and an
    unresolvable ``target_repo`` (``repo_slug`` failed open) degrades to the safe
    ``Refs`` link rather than a possibly-wrong ``Fixes``.

    Suppression is scope-aware: a full-URL reference to THIS issue (``\\b``-bounded, so
    ``issues/12`` never matches ``issues/123``) suppresses in either case; the
    ``Fixes/Closes #n`` shorthand only suppresses SAME-repo — cross-repo it cannot
    name this issue, so it must NOT block the ``Refs`` line."""
    url = f"https://github.com/{issue_slug}/issues/{n}" if issue_slug else ""
    same_repo = (not issue_slug) or (bool(target_repo) and issue_slug.lower() == target_repo.lower())
    url_present = bool(url) and re.search(rf"https://github\.com/{re.escape(issue_slug)}/issues/{n}\b", body)
    if same_repo:
        shorthand = re.search(rf"{_CLOSING_KW}\s+#{n}\b", body, re.I)
        if shorthand or url_present:
            return body
        return f"{body.rstrip()}\n\nFixes #{n}"
    if url_present:
        return body
    return f"{body.rstrip()}\n\nRefs {url}"


async def _source_issue_still_open(source_issue_raw: str, cwd: str) -> bool:
    """Re-check the source issue's state before opening a PR (#166).

    Returns True (proceed) when the issue is open, when the gh call fails for
    any reason (fail-open — a failed read must not block a legitimate PR), or
    when the slug can't be resolved. Returns False only when gh exits 0 and
    reports a state other than 'open'."""
    if not source_issue_raw:
        return True
    parsed = _source_issue({"source_issue": source_issue_raw})
    if parsed is None:
        return True  # unparseable reference → fail-open
    slug, n = parsed
    if not slug:
        # Bare issue number — resolve owner/repo from the worktree's remote.
        try:
            slug = await worktree.repo_slug(cwd=cwd)
        except Exception:  # noqa: BLE001
            return True  # fail-open
    if not slug:
        return True  # couldn't resolve → fail-open
    try:
        rc, out, _err = await worktree._gh("api", f"repos/{slug}/issues/{n}", "--jq", ".state", cwd=cwd, timeout=15)
    except Exception:  # noqa: BLE001 — timeout / infra error → fail-open
        return True
    if rc != 0:
        return True  # gh error → fail-open
    state = out.strip().strip('"').lower()
    return state == "open"


def _issue_closed_by_board_sibling(store, feature: dict) -> bool:
    """Did the current board's OWN work close this feature's source issue (#253)?

    A multi-slice board can put several features under one ``source_issue`` (one
    issue split across cards). When one slice merges first, its ``Fixes #N``
    closes the shared issue — at which point the #166 closed-issue guard would
    wrongly cancel every REMAINING sibling as "superseded", even though each
    still has its own slice of work to ship. This distinguishes that case from a
    genuine external supersede: a SIBLING feature (a different card carrying the
    same source issue) sitting in ``done`` WITH a ``pr_url`` is the board's own
    merged PR that closed the issue, so the current feature should still open its
    own PR rather than be cancelled.

    Returns True when such a sibling exists (⇒ skip the #166 cancel, keep
    working) OR when the store read fails — the check fails OPEN in the same
    direction as ``_source_issue_still_open``: a read error must never turn into
    a cancel that throws away completed coder work. Returns False only when the
    store was read cleanly and named no done sibling for this issue (⇒ the
    closure came from outside the board, so the #166 cancel proceeds unchanged).
    Source issues are compared in parsed ``(slug, n)`` form so a URL, an
    ``owner/repo#N`` and a bare ``#N`` for the same issue all match."""
    parsed = _source_issue(feature)
    if parsed is None:
        return False  # no resolvable source issue → nothing a sibling could share
    try:
        siblings = store.list_features(state="done")
    except Exception:  # noqa: BLE001 — store read failed → fail open (skip the cancel)
        return True
    fid = feature.get("id")
    for s in siblings:
        # A sibling is another card whose merged PR could have closed the issue:
        # skip self, and skip a done card that never opened a PR (nothing of its
        # could have carried the closing `Fixes #N`).
        if s.get("id") == fid or not s.get("pr_url"):
            continue
        if _source_issue(s) == parsed:
            return True
    return False


# ── live concurrency knobs ─────────────────────────────────────────────────────
# The scalar cfg keys a config reload re-applies to the RUNNING loop (BoardLoop.reload,
# wired through ADR 0018 ``register_surface(reload=)``). Project routing is also live,
# but is handled as one validated unit below rather than as independent scalar knobs.
# Everything else in cfg is read once and needs a restart. Keep this list in
# step with the manifest's ``settings:`` block (those are the console fields) and
# the "Concurrency" section of docs/configuration.md. The manifest half is enforced
# by tests/test_docs_reference.py; the doc half is enforced by its config-key coverage.
LIVE_KNOBS = ("max_concurrent", "max_pending_reviews", "max_concurrent_sessions", "auto_merge", "coder", "br_autofetch")
LIVE_KNOB_FLOORS = {"max_concurrent": 1, "max_pending_reviews": 0, "max_concurrent_sessions": 0}
# Knobs that are booleans (coerced by _knob_bool) or strings (stripped); everything
# else in LIVE_KNOBS is an int with a floor in LIVE_KNOB_FLOORS.
# `br_autofetch` (v0.43.0) is live so an operator who left it off can flip it on and
# have the paused loop fetch `br` on its next setup check — no restart.
LIVE_BOOL_KNOBS = ("auto_merge", "br_autofetch")
# `coder` is live since v0.42.0 (review on #212): the drive resolves `self.coder_name`
# per attempt, so a reload can hand the running loop a new delegate name — and the
# setup preflight's coder gap (the fresh-archetype case: boot with no coder, pick one
# in Settings) then clears and the paused loop resumes WITHOUT a restart. The `coders`
# ladder map and `repo` stay restart knobs; `setup_status` reports them as
# `loop_cfg_stale` when a reload changes them under a running loop.
_RUNG_CURSOR = itertools.count()


def _next_rung_cursor() -> int:
    """A per-process counter so consecutive cards do not all open on the same provider.

    Deliberately NOT persisted: spread is a statistical property across many dispatches,
    not a guarantee about any one card, and a restart re-seeding at zero costs nothing."""
    return next(_RUNG_CURSOR)


def should_rotate_provider(category: str, siblings: list[str], tried: int) -> bool:
    """Should this failure move to the NEXT provider at the same rung, rather than
    sleeping to retry the same one (#362)?

    Only for a QUOTA failure, and only while an untried sibling remains. A rate limit
    says nothing about the model's ability — only that this provider is spent — so it
    must not consume the transient-retry budget (60s × 5, all on the exhausted provider)
    nor the capability ladder. Every other class keeps the existing behaviour exactly:
    transient infra backs off, capability climbs a rung, terminal blocks.

    Kept pure and separate from the drive loop so the POLICY is testable on its own —
    the decision is the whole feature, and it was previously buried in a 565-line
    function where the only way to exercise it was to drive an entire card."""
    return category == "rate_limit" and len(siblings) > 1 and tried < len(siblings) - 1


def rung_delegates(value) -> list[str]:
    """One ladder rung → its interchangeable delegate names, in declaration order.

    A rung is a CAPABILITY tier. The delegates WITHIN a rung are interchangeable
    PROVIDERS of that capability — that distinction is the whole point (#362):

    * climbing a rung means "a stronger model may succeed where a weaker one failed";
    * rotating within a rung means "this model is fine, its QUOTA is not".

    A rate limit is not a capability signal, and before this it consumed the capability
    ladder's budget as though it were — the same category error #339 fixed for infra
    dispatch failures. 644 rate-limit lines in one agent's log, each one sleeping 60s and
    re-dispatching the SAME exhausted provider up to five times before blocking the card,
    while five other declared, working ACP coders sat idle.

    ``"sonnet"`` → ``["sonnet"]``, so a string rung behaves EXACTLY as before and every
    existing config is untouched. Blanks and duplicates are dropped, order preserved."""
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return out


LIVE_STR_KNOBS = ("coder",)
_CONFIG_SECTION = "project_board"


def _knob_int(cfg: dict, key: str, default: int, *, floor: int) -> int:
    """``int(cfg[key])`` floored — the one coercion every live knob shares, so the
    constructor and ``reload()`` can't drift on what "1" or "-1" means."""
    return max(floor, int(cfg.get(key, default)))


# The bool-knob coercion lives in store.knob_bool (ONE helper — store.annotate_next_action
# reads the same knobs and must not drift on what "false" means); the loop's name stays.
_knob_bool = knob_bool


def _plugin_section(new_config) -> dict:
    """The ``project_board`` section out of whatever the host handed ``reload()``:
    the ``LangGraphConfig`` (``.plugin_config[section]``, the documented ADR 0018
    payload), or a plain dict — either the section itself or a ``{section: {...}}``
    wrapper. Anything else ⇒ ``{}`` (reload becomes a no-op, never a crash)."""
    if new_config is None:
        return {}
    pconf = getattr(new_config, "plugin_config", None)
    if isinstance(pconf, dict):
        sec = pconf.get(_CONFIG_SECTION)
        return sec if isinstance(sec, dict) else {}
    if isinstance(new_config, dict):
        inner = new_config.get(_CONFIG_SECTION)
        if isinstance(inner, dict):
            return inner
        return new_config
    return {}


# ── operator cancel of an in-flight drive (#211) ──────────────────────────────
# The cancel verbs (the board_cancel_feature tool, POST …/cancel) live in the same
# process as the loop but hold no handle on it. This process-stable sys.modules slot
# (the coder_seam #178 pattern — survives a plugin reload, which re-imports this
# module while the loop surface keeps running) maps fid → the running drive task, so
# a cancel can stop the coder instead of letting the drive run on to open a PR.
_DRIVE_SLOT_PREFIX = "project_board.live_drives::"


def _drive_slot():
    # The plugin-root package (`project_board` / the host's `protoagent_plugin_<id>`),
    # NOT the module's parent. loop.py used to live at `<root>.loop`, so its
    # `rsplit(".", 1)[0]` was the root; after the #268 split this module is
    # `<root>.loop._common`, so we take the first component to keep the SAME
    # process-stable slot key the monolith used (a reload must find the same slot).
    pkg = __name__.split(".")[0] if "." in __name__ else __name__
    name = _DRIVE_SLOT_PREFIX + pkg
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's running drive tasks (#211) — data, not code."
        holder.drives = {}
        holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder


def _register_drive(fid: str, task) -> None:
    _drive_slot().drives[fid] = task


def _unregister_drive(fid: str, task) -> None:
    drives = _drive_slot().drives
    if drives.get(fid) is task:
        drives.pop(fid, None)


def live_drive(fid: str):
    """The running drive task for ``fid``, or None."""
    task = _drive_slot().drives.get(fid)
    return None if task is None or task.done() else task


def request_drive_cancel(fid: str) -> bool:
    """Cancel the running drive for ``fid`` (its coder subprocess is reaped by
    dispatch_coder's ``finally``; the drive's own CancelledError handler closes an
    already-opened PR, reaps the worktree and comments the card). Thread-safe: the
    cancel verbs run in a worker thread. Returns True if a live drive was signalled."""
    task = _loop.live_drive(fid)
    if task is None:
        return False
    try:
        task.get_loop().call_soon_threadsafe(task.cancel)
    except Exception:  # noqa: BLE001 — loop closed / task gone
        return False
    return True


# ── live-loop registry (ADR 0326, #326) ──────────────────────────────────────
# The merged-verify budget reset verb (the board_reset_merged_verify_budget tool)
# must clear the RUNNING loop's in-process budget cache, not just the persisted label:
# ``_budget_get`` lets that cache win over the bead's labels (#259), so a cached
# exhausted count would keep holding the auto-merge edge even after the label is gone.
# The verb holds no handle on the loop, so — like ``_drive_slot`` — the live loop
# publishes itself into a process-stable ``sys.modules`` data slot (survives a plugin
# reload, which re-imports this module while the loop surface keeps running). One loop
# per process; last writer wins. Distinct from setup_check's `project_board.live_loop::`
# snapshot slot — that one holds the running loop's CONFIG snapshot, this one the live
# loop OBJECT; a shared name would cross the two holders' attributes.
_LOOP_SLOT_PREFIX = "project_board.loop_instance::"


def _loop_slot():
    # The plugin-root package (`project_board` / the host's `protoagent_plugin_<id>`),
    # NOT the module's parent. loop.py used to live at `<root>.loop`, so its
    # `rsplit(".", 1)[0]` was the root; after the #268 split this module is
    # `<root>.loop._common`, so we take the first component to keep the SAME
    # process-stable slot key the monolith used (a reload must find the same slot).
    pkg = __name__.split(".")[0] if "." in __name__ else __name__
    name = _LOOP_SLOT_PREFIX + pkg
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's running loop (#326) — data, not code."
        holder.loop = None
        holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder


def _register_loop(loop) -> None:
    _loop_slot().loop = loop


def _unregister_loop(loop) -> None:
    slot = _loop_slot()
    if slot.loop is loop:
        slot.loop = None


def live_loop():
    """The running BoardLoop for this process, or None (loop never started)."""
    return _loop_slot().loop


def reset_merged_verify_budget(fid: str, store) -> bool:
    """Invalidate the live loop's in-process merged-verify budget for ``fid`` so an
    operator's budget reset takes effect on the NEXT reconcile without a host restart
    (ADR 0326, #326). The store already dropped the persisted `budget:merged-verify:<n>`
    label, but ``_budget_get`` lets the loop's cache win over the labels (#259), so a
    cached exhausted count would keep holding the auto-merge edge. Delegates to the live
    loop's ``_invalidate_merged_verify_budget`` (PINS the count to 0 under the reset lock
    and re-clears the label there too) so a reconcile that has ALREADY read the at-cap
    count can't slip its ``max+1`` exhaustion sentinel in after the reset — the sentinel
    write is a compare-and-set under the same lock and now reads the pinned 0. Thread-safe
    across the worker thread the reset verb runs on and the loop's async reconcile (the
    same cross-thread pattern as ``live_drive``). Returns True when a live loop was found
    to invalidate (False when the loop never started — nothing in-process to invalidate,
    the store's label clear alone suffices for the next process)."""
    loop = live_loop()
    if loop is None:
        return False
    loop._invalidate_merged_verify_budget(fid, store)
    return True


def cancel_pr_comment(fid: str) -> str:
    return f"cancelled by operator — see card {fid}"


def cancel_side_effects(fid: str, pr_url: str = "", *, cwd: str = ".") -> dict:
    """What a cancel must do BEYOND the store edge (#211): close the card's open PR
    (best-effort — a gh failure logs a warning and never blocks the cancel) and stop
    its in-flight drive, if any. Shared by the tool and the route; sync, so it runs
    in the tool's worker thread as-is and via ``asyncio.to_thread`` from the route."""
    out = {"pr_closed": False, "pr_detail": "", "drive_cancelled": False}
    if pr_url:
        ok, detail = worktree.close_pr_sync(pr_url, comment=cancel_pr_comment(fid), cwd=cwd)
        if ok and detail in (worktree.PR_ALREADY_MERGED, worktree.PR_ALREADY_CLOSED):
            # Nothing to close — say which, never "closed" (and never ask for a by-hand
            # close of merged work).
            out["pr_detail"] = detail
            log.info("[project_board] %s cancelled — %s %s, nothing to close", fid, pr_url, detail)
        elif ok:
            out["pr_closed"] = True
            log.info("[project_board] %s cancelled — closed %s", fid, pr_url)
        else:
            out["pr_detail"] = detail[:300]
            log.warning("[project_board] %s cancelled — could not close %s: %s", fid, pr_url, detail[:300])
    out["drive_cancelled"] = _loop.request_drive_cancel(fid)
    if out["drive_cancelled"]:
        log.info("[project_board] %s cancelled — stopping its in-flight drive", fid)
    return out


_MAX_MODE_JUDGE_SYS = (
    "You are a strict code reviewer choosing the best of several diffs for the same "
    "task. Pick the one that most completely and correctly satisfies the acceptance "
    "criteria. Answer with ONLY the candidate number."
)

# The loop's bounded re-dispatch counters ↔ their persisted `budget:<kind>` label
# kinds (#259): budget kind → the fid-keyed cache dict on BoardLoop. Bead state is
# the durable source of truth — every consult goes through `_budget_get` (cache
# miss → derive from the bead's labels), every spend through `_budget_set` and
# every reset through `_budget_reset` (cache + label together), so a freshly
# constructed loop resumes each budget where the old process left it and an
# exhausted budget blocks instead of silently re-arming.
_BUDGET_KINDS: dict[str, str] = {
    "ci-fix": "_ci_fix_attempts",
    "goal-fix": "_goal_fix_attempts",
    "gate-fix": "_gate_fix_attempts",
    "req-fix": "_req_fix_attempts",
    "empty-result": "_empty_results",
    "rebase": "_rebase_attempts",
    "merged-verify": "_merged_verify_attempts",
    "auto-merge": "_auto_merge_failures",
    "review-fix": "_review_fix_attempts",
    "review-run": "_review_run_failures",
    "unblock-retry": "_unblock_retries",
}

# Which classifier categories the blocked sweep will clear on its own. A block self-heals
# only when the thing that caused it is the kind that passes: a rate limit, a network or
# timeout blip, a base that moved under the build. `auth` and `terminal` are deliberately
# absent — no amount of waiting fixes a bad credential or an unrecognised failure, and
# silently re-running those burns budget while looking like progress.
_SELF_HEALING_BLOCKS = frozenset({"rate-limit", "transient", "merge-conflict"})
# How many times one card may be auto-unblocked before the operator is told instead.
# Deliberately small: a card that has failed transiently three times is not unlucky, it
# has a problem a human needs to see.
_UNBLOCK_RETRY_MAX = 2
# How long one blocked-card alert suppresses an identical repeat. Long, because a blocked
# card can sit for hours and the inbox's 300s default would re-alert on every restart —
# the exact noise #341 opened with. The KEY carries the incident, so a genuinely new
# failure is never suppressed by this window.
_ALERT_DEDUP_S = 7 * 24 * 3600


def _inbox_db_path():
    """This agent's inbox SQLite file, or None when it can't be resolved.

    Mirrors the host's own resolution (``server.agent_init._agent_store_db("inbox")``),
    which the plugin must not import — ``server`` is off-limits to plugins, and reaching
    into it for a notification would be a layering break. ``infra.paths.instance_paths``
    IS the sanctioned seam (already used by br_fetch and the board store), and the store
    dir is per-instance, so a fleet member resolves inside its own workspace.

    Two filenames, in the host's order: the constant ``agent.db``, else — for an install
    predating that constant — the single name-keyed ``*.db`` left in the dir. Two or more
    is genuinely ambiguous (an agent renamed before the host's fix); picking wrong would
    file the alert into a database nobody reads, so that returns None and the caller
    falls back to its loud log line."""
    try:
        from infra.paths import instance_paths

        base = Path(instance_paths().store("inbox"))
    except Exception:  # noqa: BLE001 — host without instance paths / no inbox store
        return None
    if not base.is_dir():
        return None
    target = base / "agent.db"
    if target.exists():
        return target
    legacy = sorted(p for p in base.glob("*.db") if p.name != "agent.db")
    return legacy[0] if len(legacy) == 1 else None


# The loop package object, resolved once. Rebindable seams (get_store, merge_posture,
# ...) are read through it so a test that monkeypatches ``project_board.loop.<name>``
# is still observed by helpers/methods that moved into sibling edge modules (#268).
_loop = sys.modules[__package__]

# Re-export the full loop kernel (constants, regexes, helpers, imported modules and
# process-stable state) so every edge module shares the exact globals BoardLoop had
# before the split. Underscore names are listed explicitly so ``import *`` picks
# them up.
__all__ = [
    "rung_delegates",
    "should_rotate_provider",
    "_next_rung_cursor",
    "asyncio",
    "Path",
    "hashlib",
    "json",
    "logging",
    "os",
    "re",
    "shutil",
    "sys",
    "threading",
    "time",
    "types",
    "br_fetch",
    "coder_seam",
    "config",
    "health",
    "setup_check",
    "work_snapshot",
    "worktree",
    "PRE_MODEL_DISPATCH_CLASS",
    "classify",
    "is_pre_model_dispatch_failure",
    "resolve_default_project",
    "resolve_projects",
    "store_mod",
    "BoardError",
    "LABEL_CHANGES_REQUESTED",
    "LABEL_MERGED_VERIFIED_PREFIX",
    "LABEL_REVIEW_CLEAN",
    "LABEL_REVIEW_PENDING",
    "LABEL_REVIEWED_HEAD_PREFIX",
    "LABEL_TASK",
    "_all_items_disposed",
    "apply_requirement_dispositions",
    "budgets_from_labels",
    "escalation_enabled",
    "get_store",
    "knob_bool",
    "merge_posture",
    "reconfigure_cached_store",
    "log",
    "_MERGED_VERIFIED_SHA_LEN",
    "_REVIEWED_HEAD_SHA_LEN",
    "_REVIEW_FINDINGS_TITLE",
    "_REAP_WARN_CAP",
    "PREFLIGHT_BLOCK_PREFIX",
    "_BLOCKED_COMMENT_RE",
    "_last_block_reason",
    "_FEEDBACK_SLOT_PREFIX",
    "_feedback_slot",
    "_PENDING_FEEDBACK",
    "queue_review_feedback",
    "_parse_gate_files",
    "_PNPM_INSTALL",
    "_GATE_TARGET_NAMES",
    "_resolve_gate_cmd",
    "_TEST_PATH_RE",
    "_CODE_EXTS",
    "_is_test_path",
    "_is_code_path",
    "_CI_SIGNAL_RE",
    "_ci_failure_reason",
    "_PR_URL_RE",
    "_parse_pr_url",
    "_SUMMARY_HEADING_RE",
    "_REQ_HEADING_RE",
    "_REQ_LINE_RE",
    "_parse_requirements_reply",
    "_requirement_gate_diagnostics",
    "_requirement_gate_diag_line",
    "_pr_body",
    "_NO_TEST_MARKER_RE",
    "_no_test_marker",
    "_ISSUE_URL_RE",
    "_CLOSING_KW",
    "_source_issue",
    "_inject_source_issue_line",
    "_source_issue_still_open",
    "_issue_closed_by_board_sibling",
    "LIVE_KNOBS",
    "LIVE_KNOB_FLOORS",
    "LIVE_BOOL_KNOBS",
    "LIVE_STR_KNOBS",
    "_CONFIG_SECTION",
    "_knob_int",
    "_knob_bool",
    "_plugin_section",
    "_DRIVE_SLOT_PREFIX",
    "_drive_slot",
    "_register_drive",
    "_unregister_drive",
    "live_drive",
    "request_drive_cancel",
    "_LOOP_SLOT_PREFIX",
    "_loop_slot",
    "_register_loop",
    "_unregister_loop",
    "live_loop",
    "reset_merged_verify_budget",
    "cancel_pr_comment",
    "cancel_side_effects",
    "_MAX_MODE_JUDGE_SYS",
    "_BUDGET_KINDS",
    "_SELF_HEALING_BLOCKS",
    "_UNBLOCK_RETRY_MAX",
    "_ALERT_DEDUP_S",
    "_inbox_db_path",
]

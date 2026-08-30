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
import json
import logging
import os
import re
import shutil
import sys
import time
import types

from . import br_fetch, coder_seam, config, health, setup_check, worktree
from .failures import classify
from .projects import default_project as resolve_default_project
from .projects import resolve_projects
from .store import (
    BoardError,
    LABEL_CHANGES_REQUESTED,
    LABEL_MERGED_VERIFIED_PREFIX,
    LABEL_REVIEW_CLEAN,
    LABEL_REVIEW_PENDING,
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
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
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
# the README's "Concurrency" section.
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
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
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
    task = live_drive(fid)
    if task is None:
        return False
    try:
        task.get_loop().call_soon_threadsafe(task.cancel)
    except Exception:  # noqa: BLE001 — loop closed / task gone
        return False
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
    out["drive_cancelled"] = request_drive_cancel(fid)
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


class BoardLoop:
    def __init__(self, cfg: dict, *, gap_reporter: setup_check.GapReporter | None = None):
        self.cfg = cfg or {}
        # Setup preflight (v0.42.0): the host-gap reporter register() built around the
        # plugin registry (a no-op reporter when the host lacks the seam or — in the
        # suite — none is handed in), and the PATH probe the preflight uses. Both are
        # attributes so a test can swap them without touching the environment.
        self._gap_reporter = gap_reporter or setup_check.GapReporter(None)
        self._which = shutil.which
        # Per-feature project resolution (#90 slice 2): the board's `projects:` map
        # (name → execution settings, resolved by projects.resolve_projects) so a single
        # loop can serve several repos. Absent a map, resolve_projects synthesizes a
        # SINGLE implicit project from the flat top-level keys, so a pre-#90 config
        # behaves exactly as before — every feature resolves to `self._default_project`.
        # `_project_cfg(feature)` reads the `project:<name>` label stamped in slice 1 and
        # returns THAT project's settings; every repo/gate/coder/preflight decision below
        # resolves through it, falling back to the instance-level knobs the flat config
        # parsed. `_default_project` is the fallback for a feature carrying no label.
        self._projects = resolve_projects(self.cfg)
        self._default_project = resolve_default_project(self.cfg)
        # NO default coder name (v0.42.0). An unset `coder` is "" — a preflight failure
        # the operator sees (setup_check), never a phantom delegate name that exists on
        # one machine and blocks every feature with "delegate not configured" elsewhere.
        self.coder_name = str(self.cfg.get("coder") or "").strip()
        self.reviewer_name = self.cfg.get("reviewer", "quinn")
        # Review dispatch is OPT-IN (default off). The fleet's PR-review pipeline
        # already reviews PRs the moment they're opened, so the loop doesn't need to
        # `delegate_to(reviewer)` — it just opens the PR and lets the pipeline + CI +
        # the merge webhook gate it. Turn this on only for repos NOT covered by a
        # PR-review pipeline (then a reachable `reviewer` a2a delegate is required).
        self.review_dispatch = bool(self.cfg.get("review_dispatch", False))
        # BLOCKING review gate (plan M5, OPT-IN, default off — review_dispatch stays
        # the advisory alternative). After open_review the loop runs the host's
        # adversarial `code-review` workflow (ADR 0077) on the PR, parses the findings
        # convention, and: clean → the feature stays in_review for the merge edge;
        # blocking findings → bounce back to the coder with the findings injected into
        # the retry prompt, EXACTLY like the CI bounce, bounded by `review_fix_max`
        # (mirror of ci_fix_max); exhaustion → flag_blocked — never a silent merge.
        self.review_gate = bool(self.cfg.get("review_gate", False))
        self.review_workflow = str(self.cfg.get("review_workflow", "code-review")).strip() or "code-review"
        self.review_fix_max = max(0, int(self.cfg.get("review_fix_max", 2)))
        # Cap on consecutive UNRUNNABLE gate attempts (runner missing / workflow dying /
        # panel steps failing) before escalating to the operator via flag_blocked —
        # fail closed without re-burning the workflow every poll forever (ADR 0078 D3).
        self.review_run_max = max(1, int(self.cfg.get("review_run_max", 3)))
        # Goal-verification gate (OPT-IN, default off). When on, a DETERMINISTIC pre-PR
        # check (no LLM, no diff dump): a code change must ship a test — CI runs tests but
        # can't require their presence, so the gate does. A miss → re-dispatch/escalate
        # instead of opening a testless PR; correctness itself is CI's job. (Was an
        # LLM-eyeballs-the-diff check — it false-rejected tests it couldn't see past the
        # diff truncation, burning whole tier ladders on phantom gaps; see _verify_goal.)
        self.goal_verify = bool(self.cfg.get("goal_verify", False))
        # Max-Mode (MiMo Tier-2, OPT-IN, default 1 = off). When >1, a hard feature is
        # attempted with N parallel candidates and `_judge_candidates` picks the best
        # diff. Costs N× tokens, so gate it to hard work. The parallel-dispatch wiring
        # is tracked in #21; this ships the reusable best-of-N judge it composes.
        self.max_mode_n = max(1, int(self.cfg.get("max_mode_n", 1) or 1))
        self.interval = float(self.cfg.get("loop_interval_s", 30))
        self.root = self.cfg.get("worktrees_root", ".worktrees")
        self.enabled = bool(self.cfg.get("loop_enabled", False))
        # Escalation is OPT-IN: a `coders` map (tier → delegate name) with ≥2
        # distinct delegates. With a single ACP coder there's no ladder — one
        # dispatch, then Blocked on failure — so difficulty/tier stay irrelevant
        # and we never write redundant tier/attempt labels.
        self.coders = {str(k): str(v) for k, v in (self.cfg.get("coders") or {}).items()}
        self.escalation_on = escalation_enabled(self.cfg)
        # Concurrency: drive up to `max_concurrent` features at once, each in its own
        # worktree. 1 (the default) = serial — the safe default for token + merge-
        # integration cost; raise it on a repo that parallelizes cleanly. LIVE: a
        # config reload re-applies it to the running loop via `reload()` (ADR 0018
        # register_surface reload=), so the console Settings field takes effect on
        # the next tick — no restart. Lowering it never kills a drive: in-flight
        # builds finish and the loop just stops claiming until it's under the cap.
        self.max_concurrent = _knob_int(self.cfg, "max_concurrent", 1, floor=1)
        # Review-queue WIP limit: pause new claims when this many PRs already await
        # review, so the loop can't pile up PRs faster than they merge (flooding CI /
        # reviewers). 0 = unlimited. LIVE (see max_concurrent).
        self.max_pending_reviews = _knob_int(self.cfg, "max_pending_reviews", 5, floor=0)
        # Dependency gate: "merge" (default) — a dependent waits for every blocker to
        # merge (done); "review" — a NON-foundation blocker releases its dependents at
        # in_review (more parallelism, at the risk of building on un-merged code).
        # Foundation blockers always gate on merge.
        self.relaxed_gate = str(self.cfg.get("dep_gate", "merge")).lower() == "review"
        # Stuck-drive watchdog: hard cap on a single coder dispatch (the only
        # otherwise-unbounded await in a drive — git/gh calls already self-time-out).
        # 0 disables it. A timeout reaps the coder subprocess and is a capability
        # failure (escalate-or-block), not a transient retry.
        self.coder_timeout = float(self.cfg.get("coder_timeout_s", 1800))
        # Merge poll: a fallback to the /webhook/pr Done edge for deployments with no
        # public webhook URL. On by default (cheap; only probes `in_review` PRs).
        self.merge_poll = bool(self.cfg.get("merge_poll", True))
        self.merge_poll_interval = float(self.cfg.get("merge_poll_interval_s", 60))
        # Health sweep: periodic self-heal (reclaim slots from dead drives, reap
        # orphaned worktrees). 0 disables it.
        self.sweep_interval = float(self.cfg.get("health_sweep_interval_s", 300))
        # Archive window (#115): terminal features (done/cancelled) whose closed_at is
        # older than this many days get the `archived` label during the health sweep —
        # out of the default board view, NEVER deleted (query back via
        # include_archived). Rides the sweep cadence; no scheduler of its own.
        self.archive_after_days = float(self.cfg.get("archive_after_days", 7))
        # CI-feedback edge (closed-loop verify): poll in_review PRs' check-runs and,
        # on a FAILING rollup, bounce the feature back to the coder with the failure
        # injected as feedback (vs the old open-loop: a red PR sat in_review forever).
        # Rides the merge-poll cadence. `ci_fix_max` caps re-dispatches before the
        # feature is blocked for human triage (a real bug, not a self-fixable nit).
        self.ci_poll = bool(self.cfg.get("ci_poll", self.merge_poll))
        self.ci_fix_max = max(0, int(self.cfg.get("ci_fix_max", 2)))
        # Auto-rebase a stale/conflicting in_review PR onto base. Parallel PRs branch
        # off the SAME base, and the hot-file guard serializes DISPATCH not the branch
        # BASE — so each merge re-stales the others (a sibling's change lands in the
        # same files). On BEHIND (stale, no conflict) a clean rebase + force-push fixes
        # it with NO coder; on DIRTY (a real conflict) the rebase aborts and the coder
        # is re-dispatched to re-resolve, bounded by rebase_fix_max. Rides the
        # merge-poll cadence; defaults to merge_poll's value. The same flag also
        # gates the merged-state VERIFY (#131): a CLEAN PR whose base moved gets the
        # gate re-run against the merged state (no push) and the sha stamped as
        # `merged-verified:<sha>` — bounded by the same rebase_fix_max budget.
        self.auto_rebase = bool(self.cfg.get("auto_rebase", self.merge_poll))
        self.rebase_fix_max = max(0, int(self.cfg.get("rebase_fix_max", 1)))
        # Re-verify budget for the merged-state gate (#131): how many times a single
        # in_review card's verdict is re-run after a sibling merge moves base under it.
        # Used to ride rebase_fix_max (=1) — fine when a human adjudicates a stale
        # stamp, fatal when the loop is the adjudicator: with auto_merge on, a card
        # held for review across ONE sibling merge went stale and parked forever
        # (2026-08-20, bd-c3k/bd-ohx). Each re-verify costs one gate run and only
        # happens when base actually moved, so the bound is "how many sibling merges
        # can a held card survive". 0 = unlimited.
        self.merged_verify_max = max(0, int(self.cfg.get("merged_verify_max", 5)))
        # The MERGE edge (opt-in, LIVE): when an in_review PR is green by every gate the
        # loop itself runs — GitHub says CLEAN (required checks + branch protection),
        # the merged-state verdict is stamped against the CURRENT base, the review gate
        # recorded review-clean — merge it. Before this the loop built, verified, and
        # reviewed PRs and then parked them "for the adjudicator": a chat-driven sweep
        # that is only as durable as the session scheduling it (2026-08-20: 7 green PRs
        # sat overnight after a restart dropped that session's one-shot). A card
        # labelled `merge-hold` is never auto-merged — the operator's per-card veto.
        self.auto_merge = _knob_bool(self.cfg, "auto_merge", False)
        # `br` fetched on first run (v0.43.0, br_fetch.py): default on. Read here so the
        # live-knob machinery (reload) has an attribute to diff against; the fetch itself
        # is armed by register() and re-armed by the setup gate below.
        self.br_autofetch = _knob_bool(self.cfg, "br_autofetch", True)
        self.merge_method = str(self.cfg.get("merge_method", "squash")).strip().lower() or "squash"
        # Failed merge attempts (a refusal, a race) before the loop stops trying and
        # leaves the PR for a human — logged once on the bead, never a block.
        self.auto_merge_max = max(1, int(self.cfg.get("auto_merge_max", 3)))
        # Pre-PR goal-verify gap: a rejected diff (e.g. missing tests) is fixable by
        # the SAME coder told what's missing — NOT a model-capability failure. So
        # carry the gap as feedback + re-dispatch the same tier, bounded by
        # `goal_fix_max`, BEFORE escalating/blocking (else a top-tier diff:large
        # feature blocks on attempt 1 with no chance to add the tests).
        self.goal_fix_max = max(0, int(self.cfg.get("goal_fix_max", 2)))
        # Empty-result cap (#198, retry policy #2991): a dispatch that COMPLETES with
        # no worktree diff AND no tool-call activity is `empty_result` — the coder
        # connected but never executed (a wedged adapter/session, a refusal). Often a
        # transient ACP hiccup, so occurrences below this cap retry on the SAME tier
        # (same prompt) before any failure is counted — pre-escalation, no ladder
        # attempt spent. Once the cap is hit the failure IS recorded and the normal
        # escalation ladder proceeds (single coder / ladder top → blocked for triage
        # with the failure class + evidence in the reason).
        self.empty_result_max = max(1, int(self.cfg.get("empty_result_max", 2)))
        # Auto-fix command run in the worktree BEFORE opening the PR (e.g.
        # "ruff check --fix . && ruff format ."). The coder is edit-only — it can't run
        # the repo's linter/formatter, so trivial lint/format nits would otherwise fail
        # CI and burn a whole bounce/escalation (bd-2fd: a full opus fix blocked on one
        # unused import). Best-effort; CI is still the real gate. Empty = off.
        self.format_cmd = str(self.cfg.get("format_cmd", "")).strip()
        # Pre-PR LOCAL GATE: the repo's real check command(s) run in the worktree
        # AFTER fixups and BEFORE open_pr (e.g. "ruff check . && uv run --no-sync pytest
        # tests/ -q"). The coder is edit-only — it can't run the suite — so a failure on
        # a knowable fact (a lint nit, a golden-map test, a wrong schema/column, version
        # drift) only surfaces in CI, then thrashes the bounce/escalation ladder. Running
        # it here hands the SAME coder the actual output to fix in-worktree, so the PR
        # opens already-green. Best-effort early filter: if it can't pass within
        # local_gate_max same-tier tries, the PR opens anyway (CI + the ci-fix budget
        # stay the backstop) — a flaky/misconfigured gate never blocks good work. Empty = off.
        # ``auto`` ⇒ discover the gate from the bound repo (see _resolve_gate_cmd);
        # an explicit command or blank (= no gate) passes through. Resolved here once,
        # so every downstream reader (coder_solve_test_cmd, _run_local_gate, _preflight,
        # candidate preference) sees the concrete command with no further plumbing.
        self.local_gate_cmd = _resolve_gate_cmd(str(self.cfg.get("local_gate_cmd", "")), str(self.cfg.get("repo", ".")))
        self.local_gate_max = max(0, int(self.cfg.get("local_gate_max", 2)))
        self.local_gate_timeout = float(self.cfg.get("local_gate_timeout_s", 600))
        self.local_gate_output_chars = max(500, int(self.cfg.get("local_gate_output_chars", 4000)))
        # Gate PREFLIGHT (fail-CLOSED; default on when a gate is configured). Before
        # dispatching ANY work, smoke-run ``local_gate_cmd`` on the CLEAN base checkout.
        # If the gate can't launch (missing tool) or fails on the untouched base, the
        # coder environment is broken — HOLD all ready work (flag it blocked, with the
        # reason, so the stall is visible on the board) rather than burn generations on a
        # gate no coder could pass, and re-check each cycle so work resumes the moment
        # it's fixed. This is the fail-CLOSED complement to ``_run_local_gate``'s per-PR
        # fail-OPEN: a flaky gate must never block good work, but an UNRUNNABLE gate must
        # never start bad work. A healthy repo passes instantly — nothing changes. A
        # preflight timeout is treated as indeterminate → allow (never wedge on a slow
        # gate). Opt out with ``preflight: false``.
        self.preflight = bool(self.cfg.get("preflight", True))
        self.preflight_timeout = float(self.cfg.get("preflight_timeout_s", self.local_gate_timeout))
        # Per-PROJECT preflight isolation (#90 slice 2): keyed by project name, not a
        # single scalar — a broken gate in project A holds only A's ready work while B
        # keeps dispatching. Each value is None=unchecked, True=runnable, str=failure
        # reason; `_preflight_held[name]` is the set of fids THIS loop blocked for that
        # project's failed preflight; `_last_preflight[name]` throttles its re-checks.
        self._preflight_state: dict[str, bool | str | None] = {}
        # Projects whose last preflight ran against a non-base checkout (#255): the
        # verdict was downgraded to indeterminate, and /status says so rather than
        # leaving the operator with a silently-permissive gate.
        self._preflight_dirty: dict[str, str] = {}
        self._last_preflight: dict[str, float] = {}
        self._preflight_held: dict[str, set[str]] = {}
        # When each project's CURRENT failure reason was first logged in full (#263).
        # A held project re-checks every ~60s; the multi-KB gate tail is ERROR-worthy
        # once per DISTINCT reason, and identical repeats collapse to a one-line
        # "still held (Ns)" WARNING (see _record_preflight_failure).
        self._preflight_failed_at: dict[str, float] = {}
        # ── coder.solve() board seam (ADR 0064 P2, opt-in) ─────────────────────────
        # Route a FRESH build (not a keep-worktree/CI-bounce re-dispatch) through the
        # `coder` plugin's execution-grounded solve() ladder (greedy → best-of-k →
        # tree-search) instead of a single delegate_to(acp) shot — gated on the
        # feature's acceptance tests actually PASSING in a real worktree, never an
        # LLM judge. HONEST DEGRADE (coder_seam.should_use_solve): only fires when
        # the `coder` plugin is importable (host has it enabled) AND this feature
        # carries acceptance_criteria AND a runnable test command is configured
        # below — missing any of the three falls back to today's single shot, so an
        # existing deployment can't regress just by upgrading. Composes WITH (does
        # NOT replace) the coders-map tier ladder: solve() searches within the
        # CURRENT tier; a search that never passes raises SolveExhausted, which
        # `_drive` treats as the same capability failure as a no-diff dispatch
        # (escalate a tier, or block) — the tier ladder still climbs when search
        # itself stalls.
        #
        # Precedence vs. Max-Mode (`max_mode_n>1`, below): coder_solve ONLY preempts
        # Max-Mode when Max-Mode itself is off (`max_mode_n<=1`) — see
        # `_use_coder_solve`. Without this, a board already running the README's own
        # execution-grounded Max-Mode recipe (`max_mode_n>1` + `local_gate_cmd`) would
        # silently stop using Max-Mode the moment the separate `coder` plugin became
        # importable for any unrelated reason, with zero change to THIS board's own
        # config — and unlike Max-Mode's LLM-judge fallback (which always ships a
        # best-effort PR), an exhausted solve() ladder blocks the feature outright.
        # That's a behavior change an operator must opt into, not inherit for free.
        self.coder_solve = bool(self.cfg.get("coder_solve", True))
        # The ladder's verifier: the command that runs THIS feature's (coder-
        # authored) acceptance tests in a candidate worktree, e.g. "pytest tests/ -q".
        # Blank ⇒ falls back to local_gate_cmd (many repos already configure that as
        # the real test command); still blank ⇒ no runnable oracle ⇒ honest degrade.
        self.coder_solve_test_cmd = str(self.cfg.get("coder_solve_test_cmd", "")).strip() or self.local_gate_cmd
        self.coder_solve_test_timeout = float(self.cfg.get("coder_solve_test_timeout_s", 300))
        self.coder_solve_budget = max(1, int(self.cfg.get("coder_solve_budget", 6)))
        self.coder_solve_k = max(1, int(self.cfg.get("coder_solve_k", 3)))
        self.coder_solve_tree_depth = max(0, int(self.cfg.get("coder_solve_tree_depth", 2)))
        # max_concurrent is FEATURE-level (one drive per slot). Within each drive the
        # best-of-k rung dispatches `coder_solve_k` ACP sessions concurrently, so peak
        # ACP processes = max_concurrent × coder_solve_k. Set max_concurrent_sessions to
        # cap that (0 = unlimited within the k budget; 1 = serialise k candidates).
        # LIVE (see max_concurrent) — applies to the NEXT dispatch; a running
        # best-of-k fan-out keeps the semaphore it was built with.
        self.max_concurrent_sessions = _knob_int(self.cfg, "max_concurrent_sessions", 0, floor=0)
        # Rung 4 (ADR 0064 P3): a richer generator for the HARDEST features — reached
        # only after greedy AND best-of-k AND tree-search all fail their tests. Fusion
        # can't tool-call (it's a plain completion, not an ACP session), so it's an
        # `openai`-type delegate name, resolved per-dispatch in `_drive` (mirroring how
        # `coder`/`reviewer` are resolved) — never here, this is just config plumbing.
        # Blank ⇒ no fusion rung; the ladder stops at tree-search exactly as before.
        self.coder_solve_fusion_delegate = str(self.cfg.get("coder_solve_fusion_delegate", "")).strip()
        self.coder_solve_fusion_k = max(1, int(self.cfg.get("coder_solve_fusion_k", 2)))
        # Fusion can't tool-call and returns whole-file replacements with no diff —
        # a file over this cap risks a silent truncated "complete" rewrite (see
        # coder_seam.fusion_viable_for_files). Gated BEFORE dispatch, not after:
        # an oversized feature just skips the fusion rung (fusion_delegate=None
        # for that dispatch), it never gets to attempt-and-corrupt.
        self.coder_solve_fusion_max_file_chars = max(
            1, int(self.cfg.get("coder_solve_fusion_max_file_chars", coder_seam.FUSION_MAX_FILE_CHARS_DEFAULT))
        )
        self.coder_solve_fusion_max_total_chars = max(
            1, int(self.cfg.get("coder_solve_fusion_max_total_chars", coder_seam.FUSION_MAX_TOTAL_CHARS_DEFAULT))
        )
        # KG lessons (the flywheel READ half): before dispatching a coder, query the
        # knowledge graph (via graph.sdk) for distilled lessons relevant to THIS feature
        # and inject them into the prompt — so the coder heeds this area's known failure
        # modes on attempt 1. The loop-retro writes those lessons (domain `loop-lessons`).
        # Best-effort: any SDK/store error → no injection (never blocks a build). Off when
        # kg_lessons is false or no store is configured.
        self.kg_lessons = bool(self.cfg.get("kg_lessons", True))
        self.kg_lessons_k = max(1, int(self.cfg.get("kg_lessons_k", 3)))
        self.kg_lessons_domain = str(self.cfg.get("kg_lessons_domain", "loop-lessons")).strip()
        # Repo standing gate files (#108): files EVERY change in this repo must keep
        # green (a CHANGELOG, a coverage manifest, an API golden…). A tight card-level
        # `files_to_modify` would otherwise suppress them, since a card author can't
        # enumerate per-repo obligations. These ride the coder prompt as a separate
        # block — a prompt addition, NOT ledger items (#113) and NOT `files_to_modify`
        # entries. Per-repo, default empty. Accepts a list, or a comma/space-separated
        # string (mirrors env_passthrough), de-duplicated with order preserved.
        self.gate_files = _parse_gate_files(self.cfg.get("gate_files"))
        # Repo conventions (#108): free-text markdown the host repo populates with the
        # standing rules a coder must follow that no card author will enumerate per-card
        # — "CI runs ruff", "every PR needs a changelog fragment", "this file is
        # GENERATED, don't hand-edit it", "if a convention here doesn't exist, STOP".
        # Where `gate_files` names the PATHS that must stay green, this carries the
        # RULES + formats around them. Injected verbatim as a `## Repo conventions`
        # block in the coder prompt (no per-card opt-out). Empty by default → no block.
        self.repo_conventions = str(self.cfg.get("repo_conventions", "") or "")
        # Env hygiene (#78, tightened by F8a): the host identifies/authenticates THIS
        # agent via env vars (AGENT_NAME, PROTOAGENT_*, A2A_* — see config.py). None of
        # them belong to the gate preflight, the pre-PR local_gate_cmd, the format_cmd,
        # or the coder we spawn. The loop's own gate/format/preflight children get the
        # narrow allowlist baseline only (config.sanitized_env(mode="allowlist") via
        # _child_env); the coder's ACP session env stays blacklist-stripped.
        # ``env_passthrough`` is the escape hatch on both tiers: a deployment that
        # legitimately needs a specific var to reach children whitelists it here (a
        # list, or a comma/space-separated string).
        self.env_passthrough = config.parse_env_passthrough(self.cfg)
        self._store_kw = dict(
            db=self.cfg.get("db_path") or None,
            repo=self.cfg.get("repo", "."),
            base_branch=self.cfg.get("base_branch", "main"),
            max_files_by_difficulty=self.cfg.get("max_files_by_difficulty"),
            # #90: hand the store the same resolved map, so its own per-feature reads
            # (the Ready gate's `_repo_for`) resolve to the same project the loop does.
            projects=self._projects,
            default_project=self._default_project,
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._shutting_down: bool = False
        # The running drive tasks, and the worktrees they hold (fid → (repo, wt,
        # branch)) so shutdown can reap any a cancel mid-drive would orphan; the coder
        # subprocess itself is reaped by dispatch_coder's finally.
        self._drives: set[asyncio.Task] = set()
        self._inflight: dict[str, tuple[str, str, str]] = {}
        # files_to_modify of each in-flight feature, for the hot-file overlap guard
        # (don't run two parallel coders that edit the same file → sure conflict).
        self._inflight_files: dict[str, set[tuple[str, str]]] = {}  # fid -> {(project, path)} (#197)
        # #311: at most ONE self-dispatch (a task the board runs through HOST.invoke as
        # its own agent) in flight per board — a second self-assigned task parks rather
        # than invoking the host recursively. Set before the self-drive is spawned and
        # cleared by its done-callback (which fires for EVERY terminal state), so the
        # one-in-flight guard can never leak even on a cancel-before-start.
        self._self_inflight: bool = False
        self._last_poll = 0.0  # monotonic ts of the last merge poll
        self._last_sweep = 0.0  # monotonic ts of the last health sweep
        # CI-feedback state (in-memory, per run): fid → last failing-CI summary (fed
        # into the re-dispatch prompt) and fid → count of CI-fix re-dispatches so far.
        # The counter dicts below (every `_BUDGET_KINDS` entry) are CACHES over the
        # bead's persisted `budget:<kind>:<n>` labels (#259) — consult/spend/reset
        # them ONLY through _budget_get/_budget_set/_budget_reset, so a restart
        # resumes each budget from the bead and an exhausted budget still blocks.
        # The prompt-feedback dicts (_ci_feedback/_ci_prior_diff/_review_prior)
        # stay memory-only: they enrich the next prompt, they never gate anything.
        self._ci_feedback: dict[str, str] = {}
        self._ci_prior_diff: dict[str, str] = {}
        self._ci_fix_attempts: dict[str, int] = {}
        # Pre-PR goal-verify gap re-dispatches so far (fid → count), same-tier.
        self._goal_fix_attempts: dict[str, int] = {}
        # empty_result occurrences so far (fid → count, #198) — a completed dispatch
        # with no diff AND no tool-call activity. Retried once on the SAME tier
        # before any failure is counted (#2991); after empty_result_max occurrences
        # the failure is recorded and the normal escalation ladder proceeds. Reset
        # on a climb, so each tier gets its own retry window.
        self._empty_results: dict[str, int] = {}
        # Pre-PR local-gate failure re-dispatches so far (fid → count), same-tier.
        self._gate_fix_attempts: dict[str, int] = {}
        # Pre-PR requirement-ledger bounces so far (fid → count, #113) — open items
        # at the open_pr seam re-dispatch same-tier/keep-worktree with the open list,
        # sharing the goal-fix budget knob (goal_fix_max: the same "the coder didn't
        # deliver everything it was told; tell it what's missing" class).
        self._req_fix_attempts: dict[str, int] = {}
        # Rebase-conflict re-dispatches so far (fid → count) when a sibling merge
        # leaves a PR with a real (non-clean) conflict against base.
        self._rebase_attempts: dict[str, int] = {}
        # Merged-state verifications run so far (fid → count, #131) — the verdict
        # half of the rebase edge, bounded by the same rebase_fix_max budget so a
        # base that moves repeatedly doesn't burn a gate run every poll forever.
        self._merged_verify_attempts: dict[str, int] = {}
        self._auto_merge_failures: dict[str, int] = {}
        # #211: drives whose operator-cancel cleanup already ran (or is running) — a
        # second cancel verb landing mid-cleanup re-enters _drive's CancelledError
        # handler; this keeps the close + trail comment from happening twice.
        self._cancel_done: set[str] = set()
        # #207: cards whose auto-merge is holding on a DRAFT PR and have been told so
        # on the bead (ONE comment per hold, mirroring the give-up comment — a failed
        # `gh pr ready` (fork PR, no write on base) is otherwise a silent permanent
        # hold at DEBUG). Cleared when the PR is seen non-draft again.
        self._draft_noted: set[str] = set()
        # Review-gate bounce re-dispatches so far (fid → count), same-tier — the
        # review sibling of _ci_fix_attempts (plan M5).
        self._review_fix_attempts: dict[str, int] = {}
        # Consecutive review runs that could not complete (panel step failed / no
        # runner) — after review_run_max the feature is Blocked for the operator
        # instead of re-burning the workflow every poll (ADR 0078 D3: fail closed,
        # escalate; never judge from a partial panel).
        self._review_run_failures: dict[str, int] = {}
        # How many times the blocked sweep has auto-cleared each card (see
        # _recover_blocked); past _UNBLOCK_RETRY_MAX the operator is told instead.
        self._unblock_retries: dict[str, int] = {}
        # Feature ids already reported to the operator as stuck, so a card that stays
        # blocked is announced once rather than every sweep (see _notify_operator).
        self._notified_blocks: set[str] = set()
        # Last parsed findings JSON per fid — fed back as the recipe's
        # prior_findings input so a bounce re-review is a DELTA review
        # (GitHub-native review memory, ADR 0078 D5).
        self._review_prior: dict[str, str] = {}
        # Features whose review gate is RUNNING in this process right now (#205).
        # The gate has two call sites — the drive (PR just opened) and the
        # reconcile's resume edge (an in_review card still review-pending) — and
        # the resume edge cannot tell an *interrupted* gate from one that is simply
        # still running: the pending label is set the moment the gate starts and a
        # panel takes minutes, while the reconcile polls every merge_poll_interval.
        # Without this guard every PR was reviewed twice on the same head (2× panel
        # tokens), a duplicate verdict burned the bounce budget (1/2 → 2/2 with no
        # fix between), and two non-deterministic verdicts raced for the label.
        self._review_inflight: set[str] = set()
        # Consecutive failed reap attempts per worktree id (not path, so it survives
        # repo-path changes). After _REAP_WARN_CAP failures the noise is downgraded
        # from WARNING to DEBUG; a successful reap resets the counter.
        self._reap_failures: dict[str, int] = {}
        # In-flight background store write-backs per fid (#258): coder_seam's
        # record_gens/record_verified callbacks are SYNC and invoked ON the event loop
        # mid-dispatch, so they can't await asyncio.to_thread — _record_bg schedules
        # the br call on a worker thread instead and parks the future here; the drive
        # awaits the batch right after dispatch returns (_await_bg_records), keeping
        # the pre-#258 ordering (records land before the PR opens) minus the stall.
        self._bg_records: dict[str, list] = {}

    def _store(self):
        return get_store(**self._store_kw)

    # ── persisted fix budgets (#259): bead labels are truth, the dicts are caches ─
    def _budget_cache(self, kind: str) -> dict[str, int]:
        return getattr(self, _BUDGET_KINDS[kind])

    async def _budget_get(self, store, fid: str, kind: str, feature: dict | None = None) -> int:
        """The counter's current value: the cache when this process already consulted
        it, else DERIVED from the bead's `budget:<kind>:<n>` label — a freshly
        constructed loop resumes the budget where the last process left it. Pass the
        in-hand ``feature`` projection to derive without a store read; a read hiccup
        derives 0 (fail open: the budget re-counts, it never blocks spuriously). The
        cache always wins over the projection: a mid-flow ``_budget_reset`` pins 0
        there precisely so a caller's pre-reset ``feature`` (its labels still carry
        the old count) can never rehydrate a budget the reset just granted back."""
        cache = self._budget_cache(kind)
        if fid in cache:
            return cache[fid]
        if feature is None:
            try:
                feature = await asyncio.to_thread(store.get_feature, fid)
            except Exception:  # noqa: BLE001 — a derive hiccup must never break the edge
                feature = None
        n = budgets_from_labels((feature or {}).get("labels")).get(kind, 0)
        if n:  # keep the dicts' "no key ⇒ nothing ever spent" semantics; 0 re-derives
            cache[fid] = n
        return n

    async def _budget_set(self, store, fid: str, kind: str, n: int) -> None:
        """Spend: write the counter to the cache AND the bead (`budget:<kind>:<n>`,
        replaced — the `gens:` pattern). The label write is best-effort: a `br`
        hiccup must never fail the edge that spent the budget (the cache still
        carries it for this process; the next process derives one count lower)."""
        self._budget_cache(kind)[fid] = n
        try:
            await asyncio.to_thread(store.record_budget, fid, kind, n)
        except Exception:  # noqa: BLE001 — bookkeeping must never break the edge
            log.warning("[project_board] %s budget %s=%d not persisted", fid, kind, n, exc_info=True)

    async def _budget_reset(self, store, fid: str, *kinds: str) -> None:
        """Reset counters — cache and bead labels together. No ``kinds`` = ALL of
        them: the terminal edges (merge / PR-closed), where the fid leaves the
        flow, so the cache keys drop outright. Named ``kinds`` are the MID-FLOW
        edges (a tier climb, a gate pass, a clean review) — there the caller keeps
        driving with the ``feature`` projection it already holds, whose labels
        still carry the pre-reset counts, so the cache must PIN 0 (authoritative
        "freshly reset"), never just forget the fid: a popped key would let the
        very next ``_budget_get(..., feature)`` rehydrate the exhausted count from
        that stale snapshot and block the fresh window the reset granted.
        Best-effort on the bead, like ``_budget_set``."""
        names = kinds or tuple(_BUDGET_KINDS)
        for kind in names:
            if kinds:
                self._budget_cache(kind)[fid] = 0
            else:
                self._budget_cache(kind).pop(fid, None)
        try:
            await asyncio.to_thread(store.clear_budgets, fid, list(kinds) if kinds else None)
        except Exception:  # noqa: BLE001 — bookkeeping must never break the edge
            log.warning("[project_board] %s budget reset (%s) not persisted", fid, ", ".join(names), exc_info=True)

    def _child_env(self) -> dict[str, str]:
        """The sanitized environment for a subprocess the loop spawns directly (gate
        preflight, ``local_gate_cmd``, ``format_cmd``). These children run repo-defined
        commands over coder-written code, so they get the narrow ALLOWLIST baseline
        (PATH/HOME/locale/TMPDIR/TERM/SHELL/USER/CI and its Windows system mirror —
        see config.py) plus ``env_passthrough`` — not merely ``os.environ`` minus the
        host block (F8a, tightening #78). The coder's ACP session environment stays
        on the blacklist tier (see config.py)."""
        return config.sanitized_env(self.env_passthrough, mode="allowlist")

    # ── per-feature project resolution (#90 slice 2) ──────────────────────────────
    def _project_name(self, feature: dict) -> str:
        """The project a feature builds in: its `project:<name>` label (stamped in
        slice 1), or the board's default project when the feature carries none — a
        pre-#90 feature, or a board with no `projects:` map."""
        return str(feature.get("project") or "").strip() or self._default_project

    def _project_cfg(self, feature: dict) -> dict:
        """The resolved execution settings for THIS feature's project — repo,
        base_branch, local_gate_cmd, coders, gate_files, repo_conventions, every
        `coder_solve_*` knob, … (see projects.py). Falls back to the default project's
        settings when the feature's label names no known project (or names none), so a
        single-repo board and every pre-#90 feature resolve exactly as before."""
        entry = self._projects.get(self._project_name(feature))
        if entry is None:
            entry = self._projects.get(self._default_project)
        return entry or {}

    def _repo_for(self, feature: dict) -> str:
        """The repo root this feature builds in (#90). A feature carrying an explicit
        `project:<name>` label resolves STRICTLY to that project's `repo` — overriding
        the instance default the store stamped on it (this is the whole point of the
        slice: a labeled feature builds in ITS repo, not the board's). An unlabeled
        feature — pre-#90, or a board with no `projects:` map — keeps the store-stamped
        repo, then the default project's, then the instance default (back-compat)."""
        name = str(feature.get("project") or "").strip()
        if name:
            repo = str((self._projects.get(name) or {}).get("repo") or "").strip()
            if repo:
                return repo
        return (
            str(feature.get("repo") or "").strip()
            or str(self._project_cfg(feature).get("repo") or "").strip()
            or self._store_kw["repo"]
        )

    def _base_branch_for(self, feature: dict) -> str:
        """The base branch this feature's PR targets (#90). A labeled feature whose
        project declares a `base_branch` uses it; otherwise the store-stamped value, the
        default project's, then the instance default."""
        name = str(feature.get("project") or "").strip()
        if name:
            base = str((self._projects.get(name) or {}).get("base_branch") or "").strip()
            if base:
                return base
        return (
            str(feature.get("base_branch") or "").strip()
            or str(self._project_cfg(feature).get("base_branch") or "").strip()
            or self._store_kw.get("base_branch")
            or "main"
        )

    def _local_gate_cmd_for(self, feature: dict) -> str:
        """The pre-PR / preflight gate command for this feature's project (#90). When
        the project entry declares `local_gate_cmd` it is resolved against THAT project's
        repo (so ``auto`` discovers the right checkout); otherwise the instance-level
        gate the flat config already resolved at init."""
        pc = self._project_cfg(feature)
        if "local_gate_cmd" in pc:
            return _resolve_gate_cmd(str(pc.get("local_gate_cmd") or ""), self._repo_for(feature))
        return self.local_gate_cmd

    def _format_cmd_for(self, feature: dict) -> str:
        """The pre-PR auto-fix command (``format_cmd``) for this feature's project
        (#90), else the instance default."""
        pc = self._project_cfg(feature)
        if "format_cmd" in pc:
            return str(pc.get("format_cmd") or "").strip()
        return self.format_cmd

    def _gate_files_for(self, feature: dict) -> list[str]:
        """The repo standing gate files (#108) for this feature's project (#90), else
        the instance default."""
        pc = self._project_cfg(feature)
        if "gate_files" in pc:
            return _parse_gate_files(pc.get("gate_files"))
        return self.gate_files

    def _repo_conventions_for(self, feature: dict) -> str:
        """The repo conventions prose (#108) for this feature's project (#90), else the
        instance default."""
        pc = self._project_cfg(feature)
        if "repo_conventions" in pc:
            return str(pc.get("repo_conventions") or "")
        return self.repo_conventions

    def _coders_for(self, feature: dict) -> dict[str, str]:
        """The tier→delegate `coders` map for this feature's project (#90), else the
        instance default — so escalation on a multi-repo board climbs the ladder the
        FEATURE's project declares, not the board's."""
        raw = self._project_cfg(feature).get("coders")
        if isinstance(raw, dict) and raw:
            return {str(k): str(v) for k, v in raw.items()}
        return self.coders

    def _coder_solve_test_cmd_for(self, feature: dict) -> str:
        """The coder.solve() verifier command for this feature's project (#90): the
        project's `coder_solve_test_cmd`, else its resolved gate command (many repos
        configure that as the real test command), else the instance default."""
        pc = self._project_cfg(feature)
        v = str(pc.get("coder_solve_test_cmd", "")).strip()
        if v:
            return v
        gate = self._local_gate_cmd_for(feature)
        return gate or self.coder_solve_test_cmd

    def _coder_solve_settings(self, feature: dict) -> dict:
        """Resolve the coder.solve() search knobs for this feature's project (#90). Each
        `coder_solve_*` knob prefers the project entry, falling back to the instance-level
        value the flat config parsed — so a board with no `projects:` map is unchanged."""
        pc = self._project_cfg(feature)

        def _int(key: str, default: int, floor: int) -> int:
            if key in pc:
                try:
                    return max(floor, int(pc[key]))
                except (TypeError, ValueError):
                    return default
            return default

        def _float(key: str, default: float) -> float:
            if key in pc:
                try:
                    return float(pc[key])
                except (TypeError, ValueError):
                    return default
            return default

        fusion = self.coder_solve_fusion_delegate
        if "coder_solve_fusion_delegate" in pc:
            fusion = str(pc.get("coder_solve_fusion_delegate") or "").strip()
        return {
            "test_cmd": self._coder_solve_test_cmd_for(feature),
            "test_timeout": _float("coder_solve_test_timeout_s", self.coder_solve_test_timeout),
            "budget": _int("coder_solve_budget", self.coder_solve_budget, 1),
            "k": _int("coder_solve_k", self.coder_solve_k, 1),
            "tree_depth": _int("coder_solve_tree_depth", self.coder_solve_tree_depth, 0),
            "fusion_delegate": fusion,
            "fusion_k": _int("coder_solve_fusion_k", self.coder_solve_fusion_k, 1),
            "fusion_max_file_chars": _int(
                "coder_solve_fusion_max_file_chars", self.coder_solve_fusion_max_file_chars, 1
            ),
            "fusion_max_total_chars": _int(
                "coder_solve_fusion_max_total_chars", self.coder_solve_fusion_max_total_chars, 1
            ),
            "max_concurrent_sessions": _int("max_concurrent_sessions", self.max_concurrent_sessions, 0),
        }

    def _all_repos(self) -> list[str]:
        """Every distinct repo root the board builds in — one per project (#90). A
        filesystem sweep that isn't scoped to a single feature (the orphaned-worktree
        reap) must cover every project's checkout, not just the instance default."""
        seen: dict[str, None] = {}
        for entry in self._projects.values():
            repo = str((entry or {}).get("repo") or "").strip()
            if repo:
                seen.setdefault(repo, None)
        if not seen:
            seen.setdefault(self._store_kw["repo"], None)
        return list(seen)

    # ── lifecycle (register_surface start/stop) ───────────────────────────────
    def start(self):
        if not self.enabled:
            log.info("[project_board] loop disabled (project_board.loop_enabled=false) — board API still serves")
            return None
        self._task = asyncio.create_task(self._run(), name="project-board-loop")
        # The RUNNING loop's config, in a process-stable slot: after a reload the
        # routers see the new config while this loop keeps its construction-time
        # `coders`/`repo`/…; /status compares the two and says "restart to apply".
        setup_check.publish_loop_snapshot(self.cfg)
        log.info(
            "[project_board] loop started (coder=%s reviewer=%s every %ss, max_concurrent=%d, "
            "merge_poll=%s, coder_timeout=%ss; live on config reload: %s, projects — everything else "
            "needs a restart)",
            self.coder_name or "<unset>",
            self.reviewer_name,
            self.interval,
            self.max_concurrent,
            self.merge_poll,
            self.coder_timeout,
            ", ".join(LIVE_KNOBS),
        )
        self._warn_if_review_gate_unrunnable()
        if self.coder_solve and self.coder_solve_k > 1:
            peak = self.max_concurrent * self.coder_solve_k
            cap_note = f", capped at {self.max_concurrent_sessions}" if self.max_concurrent_sessions > 0 else ""
            log.info(
                "[project_board] coder_solve_k=%d: peak concurrent ACP sessions = "
                "max_concurrent × coder_solve_k = %d × %d = %d%s "
                "(set max_concurrent_sessions to cap this)",
                self.coder_solve_k,
                self.max_concurrent,
                self.coder_solve_k,
                peak,
                cap_note,
            )
        return self._task

    def _warn_if_review_gate_unrunnable(self) -> None:
        """Boot-time preflight for the review gate (#180): review_gate=True with
        neither a workflow runner (``STATE.workflow_run`` — absent when the
        workflows plugin is disabled) nor a resolvable reviewer means EVERY
        review will fail closed. Say so once, loudly, at loop start — instead of
        letting the operator correlate per-feature gate warnings with a plugin
        toggle by reading the server log. Advisory only: the per-run gate still
        fails closed on its own; a runner appearing later just works."""
        if not self.review_gate:
            return
        runner = None
        try:
            from runtime.state import STATE

            runner = getattr(STATE, "workflow_run", None)
        except Exception:  # noqa: BLE001 — non-protoAgent host (tests)
            runner = None
        if runner is not None or self._resolve_delegate(self.reviewer_name, "a2a") is not None:
            return
        log.warning(
            "[project_board] review_gate is on but no review runner available "
            "(workflows plugin disabled? reviewer_name not set?) — every review will fail closed"
        )

    def reload(self, new_config) -> dict:
        """Apply live knobs and project routing to the RUNNING loop.

        The host fires this on every config reload with the new ``LangGraphConfig``
        (ADR 0018 ``register_surface(reload=)``) — the running surface survives a
        settings save, so without this hook a ``max_concurrent`` edit in the console
        only took effect after a restart (the loop reads ``cfg`` once, at
        construction). ``new_config`` may be the host config object (``.plugin_config
        [section]``) or the plain section dict; a section with no such key leaves that
        knob alone (a host without the manifest's defaults merged in). Returns the
        ``{knob: (old, new)}`` diff and logs it, so the splunk line for "did my
        settings change land" is one grep. Never raises: a malformed value logs a
        warning and keeps the current knob — a typo in Settings must not stall the
        loop mid-drive."""
        section = _plugin_section(new_config)
        changed: dict[str, tuple] = {}

        # A project's fields form one routing policy: resolving a repo with the old
        # base/gate/coder map (or vice versa) would cross streams. Validate a complete
        # candidate first, then atomically replace both the loop's map and the shared
        # board's map. In-flight drives have already resolved their checkout; new
        # claims and every subsequent dispatch use the replacement.
        if "projects" in section or "default_project" in section:
            candidate = dict(self.cfg)
            if "projects" in section:
                candidate["projects"] = section.get("projects")
            if "default_project" in section:
                candidate["default_project"] = section.get("default_project")
            try:
                projects = resolve_projects(candidate)
                default = resolve_default_project(candidate)
            except (TypeError, ValueError) as exc:
                log.warning("[project_board] reload: project routing is malformed — keeping current map: %s", exc)
            else:
                old_projects = self._projects
                old_default = self._default_project
                if projects != old_projects or default != old_default:
                    self._projects = projects
                    self._default_project = default
                    if "projects" in section:
                        self.cfg["projects"] = section.get("projects")
                    if "default_project" in section:
                        self.cfg["default_project"] = section.get("default_project")
                    self._store_kw["projects"] = projects
                    self._store_kw["default_project"] = default
                    reconfigure_cached_store(
                        db=self._store_kw["db"],
                        repo=self._store_kw["repo"],
                        base_branch=self._store_kw["base_branch"],
                        projects=projects,
                        default_project=default,
                    )
                    # A repo that previously failed preflight gets a clean evaluation
                    # under its new routing. Retain `_preflight_held`: it records cards
                    # the loop itself blocked and is needed to release them on recovery.
                    self._preflight_state.clear()
                    self._last_preflight.clear()
                    self._preflight_dirty.clear()
                    self._preflight_failed_at.clear()
                    changed["projects"] = (tuple(old_projects), tuple(projects))
                    if old_default != default:
                        changed["default_project"] = (old_default, default)
        for key in LIVE_KNOBS:
            if key not in section:
                continue
            attr = "coder_name" if key == "coder" else key
            cur = getattr(self, attr)
            try:
                if key in LIVE_BOOL_KNOBS:
                    new = _knob_bool(section, key, cur)
                elif key in LIVE_STR_KNOBS:
                    new = str(section.get(key) or "").strip()
                else:
                    new = _knob_int(section, key, cur, floor=LIVE_KNOB_FLOORS[key])
            except (TypeError, ValueError):
                log.warning("[project_board] reload: %s=%r is malformed — keeping %r", key, section.get(key), cur)
                continue
            if new != cur:
                setattr(self, attr, new)
                # Write the knob back into the SHARED cfg dict — `self.cfg` is the very
                # dict register() handed the routers and the tools (one `dict(cfg)`,
                # never copied), so this is how a live edit reaches every read path that
                # takes its posture from cfg rather than from the loop: the preflight's
                # `_setup_status` (a `coder` edit clears the coder gap here, not only on
                # the router's /status) and store.annotate_next_action (#208: a board_list
                # / /features after an `auto_merge` save says "auto-merge pending", not
                # "awaiting-merge", without a restart). Tested in test_next_action.py.
                self.cfg[key] = new
                changed[key] = (cur, new)
        if changed:
            setup_check.publish_loop_snapshot(self.cfg)
            log.info(
                "[project_board] reload applied live: %s (in-flight drives: %d)",
                ", ".join(f"{k} {o}→{n}" for k, (o, n) in changed.items()),
                len(self._drives),
            )
        return changed

    async def stop(self):
        self._shutting_down = True
        self._stop.set()
        if self._task:
            setup_check.publish_loop_snapshot(None)  # no running loop → nothing to be stale against
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Cancel any in-flight drives and await them out. A drive cancelled mid-flight
        # can't run its own cleanup, so its worktree stays in self._inflight — reaped
        # below. (A completed/blocked drive already popped itself.)
        drives, self._drives = list(self._drives), set()
        for t in drives:
            t.cancel()
        if drives:
            await asyncio.gather(*drives, return_exceptions=True)
        inflight, self._inflight = dict(self._inflight), {}
        for fid, (repo, wt, branch) in inflight.items():
            try:
                ok = await worktree.remove_worktree(repo, wt, branch or "")
                if ok:
                    log.info("[project_board] reaped in-flight worktree on shutdown: %s", wt)
                else:
                    log.warning("[project_board] worktree reap on shutdown failed (directory remains): %s", wt)
            except Exception:  # noqa: BLE001 — teardown must not raise out of shutdown
                log.warning("[project_board] worktree reap on shutdown failed: %s", wt, exc_info=True)

    # ── crash recovery (runs once, before the puller claims new work) ──────────
    async def _reconcile_orphan(self, fid: str):
        """A claimed feature with no live drive: if its PR actually got opened (a crash
        between ``open_pr`` and ``open_review``) adopt it → ``in_review``; else, if a
        VERIFIED candidate was recorded at coder_seam's verify boundary and still checks
        out on disk (a crash between verify and ``open_pr``), salvage it — resume at
        promote → fixups → gate → open_pr instead of re-solving (#91); otherwise reset
        it to ``ready`` for a clean rebuild (a stale worktree is cleaned when the
        puller re-claims it). Shared by boot recovery and the health sweep."""
        store = self._store()
        feature = await asyncio.to_thread(store.get_feature, fid) or {}
        # #217/#304: a task bead has no PR/worktree, so the PR-adopt / verified-candidate
        # salvage below never apply. A task parked on a human/unassigned assignee is
        # NOT orphaned — it is intentionally in_progress awaiting async delivery
        # (API/chat), the same "leave it, an out-of-band edge resolves it" posture an
        # in_review PR gets — so leave it be. A task on a SISTER-AGENT assignee (ACP
        # coder OR A2A agent) whose drive died mid-flight IS orphaned: requeue it for a
        # clean re-dispatch.
        if feature.get("issue_type") == LABEL_TASK:
            assignee = str(feature.get("assignee") or "").strip()
            if self._resolve_task_delegate(assignee) is not None:
                await asyncio.to_thread(store.requeue, fid)
                log.info("[project_board] %s task reset to ready (sister-agent drive died — re-dispatch)", fid)
            return
        pr_url = await worktree.pr_url_for_branch(
            worktree.branch_name(fid, feature.get("title") or ""), cwd=self._repo_for(feature)
        )
        if pr_url:
            await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
            log.info("[project_board] %s already had a PR → in_review (%s)", fid, pr_url)
        elif await self._salvage_verified_candidate(store, fid):
            pass  # resumed + PR opened → in_review (logged inside)
        else:
            await asyncio.to_thread(store.requeue, fid)
            log.info("[project_board] %s reset to ready (no PR — rebuild fresh)", fid)

    @staticmethod
    def _clear_verified(store, fid: str) -> None:
        """Best-effort drop of the salvage record — bookkeeping only, never raises."""
        try:
            store.clear_verified_candidate(fid)
        except Exception:  # noqa: BLE001 — a failed clear must not fail recovery
            log.warning("[project_board] %s clear_verified_candidate failed (ignored)", fid, exc_info=True)

    async def _salvage_verified_candidate(self, store, fid: str) -> bool:
        """Crash salvage (#91): resume a build whose candidate already PASSED its
        acceptance tests but crashed before ``open_pr``.

        ``coder_seam.dispatch`` records the verified candidate at its verify boundary
        (a ``verified:<sha>`` label + a bead comment with {branch, sha, worktree}). If
        that record still checks out EXACTLY — the canonical worktree dir exists, it
        has the recorded branch checked out at the recorded sha, and the pre-PR gate
        passes on it NOW — resume the tail of the drive (promote → fixups → gate →
        open_pr → in_review) instead of throwing a verified build away to re-solve.
        ANY doubt (no record, worktree gone, branch/sha drift, gate red now, any error
        anywhere) → False, and the caller falls through to today's rebuild-fresh
        unchanged — a wrong salvage ships unverified code; a skipped one only costs a
        rebuild."""
        try:
            f = await asyncio.to_thread(store.get_feature, fid) or {}
            sha = str(f.get("verified_sha") or "").strip()
            if not sha:
                return False
            repo = self._repo_for(f)
            base = self._base_branch_for(f)
            title_raw = f.get("title") or ""
            branch = worktree.branch_name(fid, title_raw)
            wt = os.path.join(repo, self.root, worktree.worktree_dir(fid, title_raw))
            if not os.path.isdir(wt):
                log.info("[project_board] %s salvage: verified worktree gone — rebuild fresh", fid)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            rc, head, _err = await worktree._git(wt, "rev-parse", "HEAD")
            if rc != 0 or head.strip() != sha:
                log.info(
                    "[project_board] %s salvage: sha drift (%s ≠ recorded %s) — rebuild fresh",
                    fid,
                    head.strip()[:12],
                    sha[:12],
                )
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            rc, cur, _err = await worktree._git(wt, "rev-parse", "--abbrev-ref", "HEAD")
            if rc != 0 or cur.strip() != branch:
                log.info("[project_board] %s salvage: branch drift (%s ≠ %s) — rebuild fresh", fid, cur.strip(), branch)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            # Resume the drive's tail in its normal order: promote (a no-op — the
            # record is written post-promote, so the candidate already holds the
            # canonical name) → fixups → gate → open_pr. The gate re-runs NOW: a
            # candidate that verified before the crash but fails today (base moved,
            # env changed) is a doubt, not a ship.
            wt, branch = await worktree.promote_worktree(repo, wt, branch, fid, self.root, title=title_raw)
            await self._run_fixups(wt, f)
            if await self._run_local_gate(wt, f) is not None:
                log.info("[project_board] %s salvage: gate fails on the candidate now — rebuild fresh", fid)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            title = f"feat: {f.get('title') or fid}"
            body = await self._with_source_issue_ref(f, wt, _pr_body("", f))
            pr_url = await worktree.open_pr(
                wt, branch, base=base, title=title, body=body, promote_draft=not f.get("pr_url")
            )
            await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
            await asyncio.to_thread(self._clear_verified, store, fid)
            log.info("[project_board] %s salvaged the verified candidate → %s (no re-solve)", fid, pr_url)
            return True
        except Exception:  # noqa: BLE001 — ANY doubt/error → today's rebuild-fresh path
            log.warning("[project_board] %s salvage attempt failed — rebuild fresh", fid, exc_info=True)
            return False

    async def _recover(self):
        """On boot, reconcile every ``in_progress`` feature the previous run left
        mid-drive (a drive doesn't survive a restart). ``in_review`` features are NOT
        touched — they have a PR and the webhook/poll resolves them. Also releases the
        previous run's orphaned preflight holds (#186) — see
        ``_recover_preflight_holds``."""
        store = self._store()
        for f in await asyncio.to_thread(store.list_features, state="in_progress"):
            try:
                await self._reconcile_orphan(f["id"])
            except Exception:  # noqa: BLE001 — recovery is best-effort, per feature
                log.warning("[project_board] recovery for %s failed", f["id"], exc_info=True)
        # Store-only helper — run the whole scan+release off the event loop (#258).
        await asyncio.to_thread(self._recover_preflight_holds, store)

    def _recover_preflight_holds(self, store) -> None:
        """Release the PREVIOUS run's preflight holds on boot (#186). `_preflight_held`
        is in-memory and dies with the process, so a restart orphans every card the old
        loop flag_blocked'd for a failed preflight: the cards are blocked (not ready),
        which makes them invisible to `_ready_projects`, and the fresh `_preflight_state`
        is empty — nothing would ever re-smoke their project or clear them. A restart is
        also the moment the environment most plausibly changed, so simply unblock them:
        a still-broken gate re-holds them one tick later (`_maybe_preflight` +
        `_hold_ready_for_preflight` — fail-closed is preserved), a fixed one lets them
        build. Cards blocked for any OTHER reason are never touched."""
        try:
            blocked = store.raw_features_with_comments(states=("blocked",))
        except Exception:  # noqa: BLE001 — a failed scan must not stop the loop from booting
            log.warning("[project_board] boot preflight release: blocked-card scan failed", exc_info=True)
            return
        for feat in blocked:
            fid = feat.get("id")
            if not fid or not _last_block_reason(feat).startswith(PREFLIGHT_BLOCK_PREFIX):
                continue
            try:
                store.clear_blocked(fid)
                log.info(
                    "[project_board] boot: released orphaned preflight hold on %s (re-checked on the first tick)",
                    fid,
                )
            except Exception:  # noqa: BLE001 — best-effort, per feature
                log.warning("[project_board] boot preflight release: clear_blocked failed for %s", fid, exc_info=True)

    # ── periodic health sweep (self-heal during the run) ───────────────────────
    async def _maybe_sweep(self):
        """Run the health sweep at most once per ``health_sweep_interval`` (0 = off)."""
        if not self.sweep_interval:
            return
        now = time.monotonic()
        if now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        await self._sweep()

    async def _sweep(self):
        """Self-heal: (a) reset ``in_progress`` features that no live drive owns (a
        drive died without finishing) — same reconcile as boot recovery; (b) reap
        ``feat-<id>`` worktrees whose feature is gone or already terminal —
        ``done``/``cancelled`` (a missed reap); (c) label terminal features past the
        archive window ``archived``
        (#115) — the board's growth valve; archival only, nothing is ever deleted.
        Best-effort; a per-item failure never stops the sweep or the loop."""
        store = self._store()
        for f in await asyncio.to_thread(store.list_features, state="in_progress"):
            fid = f["id"]
            if fid in self._inflight_files:
                continue  # a live drive owns it
            try:
                log.info("[project_board] sweep: %s in_progress with no live drive", fid)
                await self._reconcile_orphan(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reconcile for %s failed", fid, exc_info=True)
        # #90: reap orphaned worktrees across EVERY project's checkout, not just the
        # instance default — a multi-repo board holds feat-<id> worktrees under each
        # project's repo, and a worktree resolved in repo A must be reaped in repo A.
        for repo in self._all_repos():
            await self._sweep_worktrees(store, repo)
        # (b2) the blocked lane: self-heal what can be, and TELL THE OPERATOR about what
        # cannot — a blocked card used to leave the queue with only a log line, so
        # dependents sat `ready` waiting on a blocker that would never arrive.
        await self._recover_blocked(store)
        # (c) the archive pass (#115): age done/cancelled features out of the live
        # view so the Done column doesn't bury recent work — a label write only. Runs
        # ONCE per sweep (project-independent — the board db is shared), after the
        # per-repo worktree reap above.
        try:
            archived = await asyncio.to_thread(store.archive_stale, self.archive_after_days)
            if archived:
                log.info(
                    "[project_board] sweep: archived %d terminal feature(s): %s", len(archived), ", ".join(archived)
                )
        except Exception:  # noqa: BLE001
            log.warning("[project_board] sweep archive pass failed", exc_info=True)

    async def _recover_blocked(self, store) -> None:
        """The blocked lane's self-heal + escalation pass.

        Before this, every block was terminal in practice: the card left the queue, the
        only record was a WARNING in a log nobody reads, and the board said nothing. A
        transient coder timeout and a bad credential died in exactly the same silent way,
        and dependent cards sat `ready` forever waiting on a blocker that would never
        arrive. That is the failure mode this pass exists to end.

        Two outcomes, never zero:

        * the block classifies as self-healing (`rate-limit` / `transient` /
          `merge-conflict`) and the card has retries left → clear it, requeue it, spend
          one `unblock-retry`; the next tick re-dispatches it.
        * anything else — `auth`, `terminal`, an unclassified block, or a card that has
          spent its retries → the OPERATOR is told, once, naming the card and the actual
          reason. It stays blocked; a human decides.

        Best-effort per card, exactly like the rest of the sweep: one card that fails to
        recover must never stop the pass or the loop."""
        try:
            blocked = await asyncio.to_thread(store.list_features, state="blocked")
        except Exception:  # noqa: BLE001
            log.warning("[project_board] blocked sweep: could not list blocked features", exc_info=True)
            return
        for f in blocked:
            fid = f["id"]
            try:
                cls = str(f.get("blocked_class") or "").strip()
                reason = str(f.get("blocked_reason") or "").strip()
                spent = await self._budget_get(store, fid, "unblock-retry", f)
                if cls in _SELF_HEALING_BLOCKS and spent < _UNBLOCK_RETRY_MAX:
                    await self._budget_set(store, fid, "unblock-retry", spent + 1)
                    await asyncio.to_thread(store.clear_blocked, fid)
                    await asyncio.to_thread(store.requeue, fid)
                    self._notified_blocks.discard(fid)  # a LATER block is news again
                    log.info(
                        "[project_board] blocked sweep: %s auto-unblocked (%s, retry %d/%d): %s",
                        fid,
                        cls,
                        spent + 1,
                        _UNBLOCK_RETRY_MAX,
                        reason[:120],
                    )
                    continue
                why = (
                    f"{cls or 'unclassified'} block"
                    if spent < _UNBLOCK_RETRY_MAX
                    else f"{cls} block, {spent} auto-retr{'y' if spent == 1 else 'ies'} spent"
                )
                title = str(f.get("title") or "").strip()
                self._notify_operator(
                    fid,
                    f"Board card {fid} is blocked and will not clear itself ({why}): "
                    f"{reason or 'no reason recorded'}" + (f" — {title}" if title else ""),
                )
            except Exception:  # noqa: BLE001
                log.warning("[project_board] blocked sweep for %s failed", fid, exc_info=True)

    def _notify_operator(self, fid: str, text: str) -> None:
        """Put ONE item in the operator's inbox for a card that has stopped moving.

        Deduped on the feature id so a card that stays blocked across many sweeps is
        reported once, not every five minutes — the point is to be noticed, and an alert
        that repeats forever is an alert that gets filtered.

        Feature-detected: the inbox is a host module this plugin must not hard-depend on.
        A host without it still gets the WARNING below, which is strictly louder than the
        silence a block used to leave."""
        if fid in self._notified_blocks:
            return
        self._notified_blocks.add(fid)
        try:
            from inbox.store import InboxStore  # host module — absent on older hosts

            InboxStore().add(text, priority="now", source="project_board", dedup_key=f"blocked:{fid}")
            log.warning("[project_board] %s blocked — operator notified: %s", fid, text[:160])
        except Exception:  # noqa: BLE001 — no inbox seam, or it refused; say so loudly anyway
            log.warning(
                "[project_board] %s blocked and NOT self-healing (no operator inbox reachable): %s",
                fid,
                text[:200],
            )

    async def _sweep_worktrees(self, store, repo: str) -> None:
        """Reap orphaned ``feat-<id>`` worktrees under one project's checkout (#90) —
        the per-repo half of the health sweep, factored out so it runs once per project
        repo. Best-effort; a per-item failure never stops the sweep."""
        for wtid in worktree.list_feature_worktrees(repo, self.root):
            # A `.gN`/`.cN` candidate worktree is not a feature id (bd-1cp.g1) — its
            # board state lives on the PARENT feature, so resolve through that (#91):
            # skip while the parent's drive is live, reap when the parent is gone or
            # terminal (done/cancelled — the terminal-edge reap's crash backstop, #109).
            # The old raw-id `get_feature` lookup failed every sweep and just warned
            # forever without ever reaping the candidate.
            fid = worktree.parent_feature_id(wtid)
            if fid in self._inflight_files:
                continue  # a live drive owns this worktree (or its candidates)
            try:
                f = await asyncio.to_thread(store.get_feature, fid)
                if f is None or f["board_state"] in ("done", "cancelled"):
                    reaped = await worktree.reap_feature_worktree(repo, self.root, wtid)
                    if reaped:
                        self._reap_failures.pop(wtid, None)
                        log.info("[project_board] sweep: reaped orphaned worktree feat-%s", wtid)
                    else:
                        n = self._reap_failures.get(wtid, 0) + 1
                        self._reap_failures[wtid] = n
                        if n <= _REAP_WARN_CAP:
                            log.warning(
                                "[project_board] sweep: could not reap orphaned worktree feat-%s (attempt %d)",
                                wtid,
                                n,
                            )
                        else:
                            log.debug(
                                "[project_board] sweep: could not reap orphaned worktree feat-%s (attempt %d)",
                                wtid,
                                n,
                            )
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reap for %s failed", wtid, exc_info=True)

    # ── setup preflight (v0.42.0) ─────────────────────────────────────────────
    def _ensure_br(self) -> dict:
        """Arm the `br` auto-fetch for THIS loop's config (br_fetch.ensure_br — once per
        process, returns at once). Tests swap this."""
        return br_fetch.ensure_br(self.cfg, which=self._which)

    def _delegate_resolver(self):
        """ONE roster read per preflight (``coder_seam.delegate_resolver``) — the
        dispatch path's ``_resolve_delegate`` re-parses the YAML per name, which is
        fine for one lookup and not for every name every tick. Tests swap this."""
        return coder_seam.delegate_resolver("acp")

    def _setup_status(self) -> dict:
        """The board's setup preflight for THIS loop's config — `br`/`gh` via the
        injectable PATH probe, the coder names against one roster read. Compared to
        its OWN config it is never stale; the `/status` route owns the stale view."""
        return setup_check.setup_status(
            self.cfg,
            which=self._which,
            delegates=self._delegate_resolver(),
            loop_snapshot=setup_check.snapshot_of(self.cfg),
        )

    async def _setup_gate(self) -> bool:
        """Hold the puller until the setup preflight has no loop blockers (no `br`,
        an unresolvable coder, an unbound repo — each of which turned EVERY tick into
        a traceback before v0.42.0). Re-checks every `loop_interval_s`, so installing
        `br` or declaring the delegate recovers WITHOUT a restart; logs ONE warning
        when it pauses and one info line when it resumes. Every check is also
        forwarded to the host's setup-gap seam (edge-triggered, so a steady state is
        silent). Returns False only when the loop was stopped while paused."""
        paused = False
        while not self._stop.is_set() and not self._shutting_down:
            try:
                # Off the event loop: the preflight may shell `br --version` (once per
                # path) and reads the delegates YAML — neither belongs on the loop.
                # ensure_br first: a no-op once br resolves / the fetch is in flight or
                # spent; it arms the fetch when `br_autofetch` was flipped on live.
                await asyncio.to_thread(self._ensure_br)
                status = await asyncio.to_thread(self._setup_status)
                self._gap_reporter.report(status)
                blockers = setup_check.loop_blockers(status)
            except Exception:  # noqa: BLE001 — a broken probe must fail OPEN, never wedge the loop
                log.warning("[project_board] setup preflight errored — proceeding", exc_info=True)
                blockers = []
            if not blockers:
                if paused:
                    log.info("[project_board] loop resumed — setup gaps cleared")
                return True
            if not paused:
                log.warning("[project_board] loop paused: %s", setup_check.blocker_summary(status))
                paused = True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
        return False

    # ── the puller ────────────────────────────────────────────────────────────
    async def _run(self):
        # Setup gate FIRST: with no `br` the recovery below can't even build a store,
        # and with no coder the first dispatch blocks every card. Pause (one warning,
        # cheap re-check each interval) instead of entering the tick loop.
        if not await self._setup_gate():
            return
        try:
            await self._recover()
        except Exception:  # noqa: BLE001 — recovery must never stop the loop from starting
            log.exception("[project_board] crash recovery failed")
        log.info("[project_board] recovery done — entering tick loop")
        while not self._stop.is_set() and not self._shutting_down:
            # Re-check at every tick boundary: a gap that opens mid-run (br removed,
            # the delegate deleted) pauses the ticks again; a passing preflight is a
            # `which` + a roster read and changes nothing about the tick itself.
            if not await self._setup_gate():
                return
            spawned = False
            try:
                await self._maybe_reconcile()
                await self._maybe_sweep()
                await self._maybe_preflight()  # fail-closed: hold work if the gate can't run
                spawned = await self._spawn_ready()
            except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
                log.exception("[project_board] loop tick failed")
            # Idle (nothing started, nothing running) → sleep the full interval. Busy
            # → re-check soon so a freed concurrency slot refills and merges land
            # promptly (the poll itself stays rate-limited by merge_poll_interval).
            idle = not spawned and not self._drives
            timeout = self.interval if idle else min(self.interval, 3.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _spawn_ready(self) -> bool:
        """Claim Ready features up to the concurrency cap and spawn a drive for each,
        with two back-pressure gates: pause when too many PRs already await review
        (``max_pending_reviews``), and skip a candidate whose ``files_to_modify``
        overlap an in-flight build (the hot-file guard — two parallel coders editing
        the same file are a guaranteed merge conflict). Returns True if it started at
        least one drive (so the runner stays hot). Async so every store read/claim
        rides ``asyncio.to_thread`` (#258) — the scan must not stall the event loop;
        the drives themselves are still spawned on the loop (``create_task``).

        Every tick that reaches the claim scan emits ONE parseable ``claim_decision``
        ``log.info`` (#124): the fid(s) selected this tick and, for each higher-priority
        ``ready_queue`` candidate passed over, the structured reason it was skipped
        (hot-file overlap with which in-flight fid, ``claim()`` returned None, not
        ready/blocked). That is the evidence to tell a lost claim race from the hot-file
        guard from a ``ready_queue`` mis-ordering when a lower-priority card claims ahead
        of a higher one — the payload is JSON, so a future observer parses it without
        grepping log levels."""
        if len(self._drives) >= self.max_concurrent:
            return False
        # Fail-closed gate preflight, per-project (#90): a project whose gate can't run on
        # clean base HOLDS its own ready work (surfaced on the board) rather than dispatch
        # coders that can never pass it — but a broken gate in project A must NOT hold
        # project B, so this holds only the failed projects and the claim loop below skips
        # their candidates while continuing to dispatch every runnable project.
        if any(isinstance(v, str) for v in self._preflight_state.values()):
            await asyncio.to_thread(self._hold_ready_for_preflight)
        store = self._store()
        # Review-queue WIP limit — don't claim new work while the review queue is full.
        if (
            self.max_pending_reviews
            and len(await asyncio.to_thread(store.list_features, state="in_review")) >= self.max_pending_reviews
        ):
            return False
        spawned = False
        # file → the in-flight (or claimed-this-tick) fid that owns it, so a hot-file
        # skip can NAME the build it collides with, not just report "some overlap".
        file_owner: dict[str, str] = {}
        for owner_fid, owner_files in self._inflight_files.items():
            for key in owner_files:
                file_owner.setdefault(key, owner_fid)
        busy = set(file_owner)
        selected: list[str] = []
        skipped: list[dict] = []  # {fid, reason, …} per passed-over candidate, priority order
        parked: list[str] = []  # #217: task beads claimed to in_progress without a slot (human-wait)
        ready = await asyncio.to_thread(store.ready_queue, relaxed=self.relaxed_gate)
        for candidate in ready:  # priority order, dep-unblocked
            if len(self._drives) >= self.max_concurrent:
                break  # remaining candidates are lower priority than what we already selected
            cid = candidate["id"]
            if candidate.get("board_state") != "ready" or candidate.get("blocked"):
                # a blocked-flagged feature can carry the `ready` label too
                reason = "blocked" if candidate.get("blocked") else f"state={candidate.get('board_state')}"
                skipped.append({"fid": cid, "reason": reason})
                continue
            # Per-project preflight hold (#90): this candidate's project can't run its
            # gate on clean base — skip it (it was flag_blocked above), but keep scanning
            # so a sibling in a HEALTHY project still gets claimed this tick.
            pname = self._project_name(candidate)
            if isinstance(self._preflight_state.get(pname), str):
                skipped.append({"fid": cid, "reason": "preflight-hold", "project": pname})
                continue
            # #217: task-type dispatch — a `task` bead ships a DELIVERABLE (a doc, a
            # decision, an artifact ref), not a diff, so it takes NO git worktree and
            # skips the hot-file guard below (with no worktree there is no file to
            # collide on). A sister-agent assignee — an ACP coder OR an A2A agent
            # (#304) — is driven via its native adapter (a real drive → counts toward
            # max_concurrent); a task assigned to the board's OWN agent (its coder name,
            # or the self/agent aliases) is driven first-party through HOST.invoke (#311,
            # also a real drive); a human/unassigned task is parked in_progress to await
            # async delivery (API/chat) and does NOT hold a slot.
            if candidate.get("issue_type") == LABEL_TASK:
                outcome = await self._dispatch_task(store, candidate)
                if outcome in ("acp", "a2a", "self"):
                    selected.append(cid)
                    spawned = True
                elif outcome == "parked":
                    parked.append(cid)
                else:  # "race" — lost the atomic claim, still ready for the next tick
                    skipped.append({"fid": cid, "reason": "claim-race"})
                continue
            # #197: key by (project, path) — bare paths false-collide across projects
            # (every repo has PROTO.md); an unstamped card ("" project) behaves as before.
            files = {(pname, p) for p in (candidate.get("files_to_modify") or [])}
            overlap = files & busy
            if overlap:
                # would edit a file an in-flight build owns → defer a tick
                owners = sorted({file_owner[k] for k in overlap})
                skipped.append(
                    {
                        "fid": cid,
                        "reason": "hot-file",
                        "overlaps": owners,
                        "files": sorted(path for _proj, path in overlap),
                    }
                )
                continue
            claimed = await asyncio.to_thread(store.claim, cid, assignee=self.coder_name)
            if claimed is None:
                skipped.append({"fid": cid, "reason": "claim-race"})  # raced / no longer ready
                continue
            self._inflight_files[claimed["id"]] = files
            for key in files:
                file_owner.setdefault(key, claimed["id"])
            task = asyncio.create_task(self._drive(claimed), name=f"pb-drive-{claimed['id']}")
            self._drives.add(task)
            _register_drive(claimed["id"], task)  # reachable by the cancel verbs (#211)
            task.add_done_callback(self._make_drive_done_cb(claimed["id"]))
            busy |= files
            selected.append(claimed["id"])
            spawned = True
        if selected or skipped or parked:
            log.info(
                "[project_board] claim_decision %s",
                json.dumps(
                    {"parked": parked, "selected": selected, "skipped": skipped},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        return spawned

    def _make_drive_done_cb(self, fid: str):
        """A drive task's done-callback: drop it from the running set and release the
        files it held (so a deferred file-conflicting candidate can be claimed next)."""

        def _cb(task: asyncio.Task):
            self._drives.discard(task)
            self._inflight_files.pop(fid, None)
            _unregister_drive(fid, task)
            self._cancel_done.discard(fid)  # a later drive of the same card gets its own cancel edge
            # #258: drop any write-back futures a failed dispatch left un-awaited —
            # the executor still runs them (fire-and-forget); only the parking is freed.
            self._bg_records.pop(fid, None)

        return _cb

    # ── task-type dispatch (#217) ─────────────────────────────────────────────
    async def _dispatch_task(self, store, candidate: dict) -> str:
        """Claim + dispatch a ``task`` bead, branching on its assignee. Returns:

        - ``"acp"`` / ``"a2a"`` — the assignee resolves to a sister-agent delegate of
          that type (ACP coder or A2A agent, #304) → claimed and a ``_drive_task`` was
          spawned (into ``self._drives``, so it counts toward ``max_concurrent`` exactly
          like a coding drive). The caller treats both alike: a drive was started.
        - ``"self"`` — the assignee is the board's OWN agent (#311) → a first-party
          ``_drive_self_task`` was spawned through ``HOST.invoke`` (also into
          ``self._drives`` → counts toward ``max_concurrent``). Caller: a drive started.
        - ``"parked"`` — a human/unassigned task, or a name that resolves to NEITHER
          type (or a self task with no host seam / one already in flight) → claimed to
          ``in_progress`` and left there to await async delivery (API/chat). No drive,
          no slot held.
        - ``"race"`` — the atomic claim was lost (someone else took it); still ready.

        A task assigned to THIS board's OWN agent (its coder name, or the reserved
        ``self``/``agent`` aliases) is NOT a sister-agent dispatch — it is first-party
        work the host does itself through ``HOST.invoke`` (#311). That is detected BEFORE
        delegate resolution (the board's own name always means "I do it") and delegated to
        ``_dispatch_self``, which can also return ``"self"`` (a first-party drive spawned).

        The assignee is read from the CANDIDATE (the pre-claim projection) and passed
        back to ``claim`` so the claim preserves it — a task's assignee is its dispatch
        target, not the coder identity a coding claim stamps."""
        cid = candidate["id"]
        assignee = str(candidate.get("assignee") or "").strip()
        if self._is_self_assignee(assignee):
            return await self._dispatch_self(store, candidate, assignee)
        delegate = self._resolve_task_delegate(assignee)
        claimed = await asyncio.to_thread(store.claim, cid, assignee=assignee)
        if claimed is None:
            return "race"
        if delegate is None:
            # Human assignee, none, or a name that resolves to NEITHER an ACP coder nor
            # an A2A sister agent: nothing to dispatch. The task is now in_progress; its
            # deliverable arrives out-of-band (record_delivery via the API/chat). The
            # loop moves on and this parked card holds no slot.
            log.info(
                "[project_board] %s task parked in_progress — awaiting %s delivery (assignee=%s)",
                cid,
                "human" if assignee else "unassigned",
                assignee or "<none>",
            )
            return "parked"
        # A sister-agent assignee (ACP coder OR A2A agent, #304) → drive it via its
        # native adapter. No worktree, so it owns no files (empty set: present so the
        # done-callback's pop and the sweep's live-drive check both see it, but colliding
        # with nothing in the hot-file guard).
        self._inflight_files[cid] = set()
        task = asyncio.create_task(self._drive_task(claimed, delegate), name=f"pb-task-{cid}")
        self._drives.add(task)
        _register_drive(cid, task)  # reachable by the cancel verbs (#211)
        task.add_done_callback(self._make_drive_done_cb(cid))
        return str(getattr(delegate, "type", "") or "acp")

    # ── #311: first-party self-dispatch through the host agent ────────────────────
    # A task assigned to the board's OWN agent — the reserved ``self``/``agent`` aliases,
    # or the board's configured ``coder`` name.
    _SELF_ASSIGNEE_ALIASES = ("self", "agent")

    def _is_self_assignee(self, assignee: str) -> bool:
        """True when a task's assignee names THIS board's own agent — the reserved
        ``self``/``agent`` aliases, or the board's configured ``coder`` name (#311). Such
        a task is dispatched through ``HOST.invoke`` (first-party), NOT a sister-agent
        delegate. Case-insensitive; an empty assignee (human/unassigned) is never self,
        and the aliases resolve even when no ``coder`` name is configured."""
        a = assignee.strip().lower()
        if not a:
            return False
        if a in self._SELF_ASSIGNEE_ALIASES:
            return True
        return bool(self.coder_name) and a == self.coder_name.strip().lower()

    async def _dispatch_self(self, store, candidate: dict, assignee: str) -> str:
        """Dispatch a task assigned to the board's OWN agent through ``HOST.invoke`` (#311)
        — the first-party sibling of ``_dispatch_task``'s sister-agent path. The board is
        the assignee: rather than shell out to a delegate, it drives its own ``invoke``
        seam with the task prompt + a stable per-card session id and records the reply via
        the SAME ``record_delivery`` lifecycle (→ in_review for independent board_verify).
        Returns like ``_dispatch_task``:

        - ``"self"`` — a first-party drive was spawned (into ``self._drives`` → counts
          toward ``max_concurrent``, exactly like an ACP/A2A task drive).
        - ``"parked"`` — the host predates the ``invoke`` seam (r2), OR a self-dispatch is
          already in flight for this board (r3): claimed to ``in_progress`` and left, no
          drive, no slot. Preserves the existing park behaviour when self work can't run.
        - ``"race"`` — the atomic claim was lost.

        Only ONE self-dispatch runs per board at a time (``self._self_inflight``): a second
        self-assigned task parks rather than invoking the host recursively/concurrently."""
        cid = candidate["id"]
        invoke = coder_seam.resolve_self_invoke()
        # r2: a host with no HOST.invoke seam (older host / host-free env) parks with the
        # existing log line. r3: a self-dispatch already in flight → park the second
        # rather than recurse. Both claim to in_progress and hold no slot, exactly like a
        # human/unassigned park.
        # r3b: `_self_inflight` is cleared by the drive's done-callback, which fires as soon
        # as the drive is CANCELLED — while an uncancellable worker thread may still be
        # executing on the host. `host_invoke_busy()` is owned by that thread, so it stays
        # true until the call really returns and the second task parks instead of racing it.
        if invoke is None or self._self_inflight or coder_seam.host_invoke_busy():
            claimed = await asyncio.to_thread(store.claim, cid, assignee=assignee)
            if claimed is None:
                return "race"
            why = "no HOST.invoke seam" if invoke is None else "a self-dispatch is already in flight"
            log.info(
                "[project_board] %s self task parked in_progress — awaiting agent delivery (%s)",
                cid,
                why,
            )
            return "parked"
        claimed = await asyncio.to_thread(store.claim, cid, assignee=assignee)
        if claimed is None:
            return "race"
        # Raise the one-in-flight guard BEFORE the drive is spawned, and clear it from the
        # drive's done-callback (``_make_self_done_cb``) — which asyncio runs for EVERY
        # terminal state, including a cancel before the coroutine ever starts — so the
        # guard can never leak. A self-drive owns no worktree/files (empty set, like the
        # sister-agent task path) but IS a real drive counting toward max_concurrent.
        self._self_inflight = True
        self._inflight_files[cid] = set()
        session_id = f"board-self-{cid}"  # stable per card
        task = asyncio.create_task(self._drive_self_task(claimed, invoke, session_id), name=f"pb-self-{cid}")
        self._drives.add(task)
        _register_drive(cid, task)  # reachable by the cancel verbs (#211)
        task.add_done_callback(self._make_self_done_cb(cid))
        return "self"

    def _make_self_done_cb(self, fid: str):
        """Done-callback for a self-dispatch drive (#311): clear the one-in-flight guard —
        so the next self-assigned task can dispatch — on TOP of the standard drive cleanup.
        Runs for every terminal state asyncio can reach (success, failure, or a cancel
        even before the coroutine starts), so ``_self_inflight`` can never leak."""
        base = self._make_drive_done_cb(fid)

        def _cb(task: asyncio.Task):
            self._self_inflight = False
            base(task)

        return _cb

    async def _drive_self_task(self, feature: dict, invoke, session_id: str) -> None:
        """Drive a task assigned to the board's OWN agent through ``HOST.invoke`` (#311) —
        the first-party sibling of ``_drive_task``. No worktree, no git, no PR: build the
        same deliverable prompt, drive ``coder_seam.dispatch_self`` (bounded by
        ``coder_timeout_s``), then record the reply as the deliverable (``record_delivery``
        → in_review, where the board's independent ``board_verify`` closes it). A dispatch
        failure/timeout is classified and blocks the card EXACTLY like the sister-agent
        task path (r5) — a self task has no model ladder to climb (the assignee is the
        host itself), so the capability-escalation edge collapses to a terminal block."""
        store = self._store()
        fid = feature["id"]
        prompt = self._build_task_prompt(feature)
        try:
            reply = await coder_seam.dispatch_self(invoke, prompt, session_id, timeout=self.coder_timeout or None)
        except asyncio.CancelledError:
            raise  # operator cancel / shutdown owns the edge — never a block
        except (worktree.WorktreeError, worktree.CoderTimeout) as exc:
            if self._shutting_down:
                log.info("[project_board] %s self task dispatch aborted by shutdown — no block", fid)
                return
            policy = classify(str(exc))
            log.warning("[project_board] %s self task blocked (%s): %s", fid, policy.category, exc)
            await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s self task dispatch unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            return
        await asyncio.to_thread(store.record_delivery, fid, text=reply or "")
        log.info("[project_board] %s self task delivered (%d chars) → in_review", fid, len(reply or ""))

    # ── the PR reconcile (terminal-edge fallback to the webhook) ───────────────
    async def _maybe_reconcile(self):
        """Run the PR reconcile at most once per ``merge_poll_interval`` (and only when
        enabled) — cheap, but no reason to hammer ``gh`` every busy tick."""
        if not self.merge_poll:
            return
        now = time.monotonic()
        if now - self._last_poll < self.merge_poll_interval:
            return
        self._last_poll = now
        await self._reconcile_prs()

    async def _reconcile_prs(self):
        """Reconcile each ``in_review`` feature against its PR's real state — the
        fallback to the webhook and the active half of the terminal edges (for
        deployments GitHub can't post a webhook to, where a feature would otherwise
        sit in_review forever): ``MERGED`` → done (+reap); ``CLOSED`` unmerged →
        Blocked for triage (+reap; the work was rejected, don't silently re-dispatch);
        ``OPEN`` → leave it in review."""
        store = self._store()
        # #196: blocked cards can carry a PR too (review-verify blocks, closed-PR triage,
        # manual flags) — a merged PR is ground truth for them exactly as for in_review,
        # and scanning only in_review left merged-but-blocked cards stuck forever. They
        # take ONLY the MERGED edge below: CLOSED would rewrite their blocked reason, and
        # the OPEN-branch gates (rebase/CI/review) must not run against held work.
        in_review = await asyncio.to_thread(store.list_features, state="in_review")
        blocked = await asyncio.to_thread(store.list_features, state="blocked")
        for f in [*in_review, *blocked]:
            fid = f["id"]
            pr_url = f.get("pr_url")
            if not pr_url:
                continue
            # #90: reconcile each PR against ITS project's checkout, not the board default.
            repo = self._repo_for(f)
            try:
                state = await worktree.pr_state(pr_url, cwd=repo)
                if f.get("board_state") == "blocked" and state != "MERGED":
                    continue
                if state == "MERGED":
                    if await asyncio.to_thread(store.record_merge, pr_url=pr_url):
                        await worktree.reap_feature_worktree(repo, self.root, fid)
                        self._ci_feedback.pop(fid, None)
                        self._review_prior.pop(fid, None)
                        # Merge edge: EVERY fix budget resets — cache and the bead's
                        # `budget:` labels together (#259) — so a reopened/requeued
                        # card starts with full budgets, exactly as pre-persistence.
                        await self._budget_reset(store, fid)
                        # A merge with the gate still unhappy is a human override —
                        # reality wins, but it must be visible, not silent.
                        if self.review_gate and LABEL_CHANGES_REQUESTED in (f.get("labels") or []):
                            log.warning(
                                "[project_board] %s merged with review changes-requested still set "
                                "(human override): %s",
                                fid,
                                pr_url,
                            )
                        log.info("[project_board] reconcile → done: %s (%s)", fid, pr_url)
                elif state == "CLOSED":
                    await asyncio.to_thread(
                        store.flag_blocked, fid, f"PR closed without merging — needs triage: {pr_url}"
                    )
                    await worktree.reap_feature_worktree(repo, self.root, fid)
                    self._ci_feedback.pop(fid, None)
                    self._review_prior.pop(fid, None)
                    # Closed-unmerged is the other terminal edge: same full reset,
                    # so a post-triage requeue starts with fresh budgets (#259).
                    await self._budget_reset(store, fid)
                    log.info("[project_board] reconcile → blocked (PR closed): %s (%s)", fid, pr_url)
                elif state == "OPEN":
                    # Keep a stale/conflicting PR mergeable BEFORE the CI reconcile: a
                    # sibling merge re-stales the others off the shared base, and a rebase
                    # force-pushes + re-runs CI — so checking CI on the stale head first
                    # would just be thrown away.
                    if self.auto_rebase and await self._maybe_rebase(store, f, pr_url, repo):
                        continue
                    # The VERDICT half of the rebase edge (#131): a sibling merge
                    # moved base under this still-CLEAN PR (no conflict, so the
                    # rebase above left it alone) — the state that will actually
                    # LAND was never gated. Re-run the gate on the merged state
                    # (no push) and stamp the sha; only a red gate blocks.
                    if self.auto_rebase and await self._verify_merged_state(store, f, pr_url, repo):
                        continue  # blocked on a red merged-state gate → nothing further this pass
                    if self.ci_poll:
                        await self._reconcile_ci(store, fid, pr_url, repo, feature=f)
                    # The merge-edge half of the review gate (M5): an in_review PR still
                    # marked review-pending had its gate interrupted (host restart, dead
                    # workflow run) — finish it here so the gate can't silently lapse into
                    # advisory. Skip when the CI reconcile just requeued the feature. A
                    # gate that is merely still RUNNING (the drive's call, minutes long)
                    # is not interrupted — _review_gate's in-flight guard makes this a
                    # no-op for it (#205), so this edge never double-reviews a head.
                    if (
                        self.review_gate
                        and LABEL_REVIEW_PENDING in (f.get("labels") or [])
                        and (await asyncio.to_thread(store.get_feature, fid) or {}).get("board_state") == "in_review"
                    ):
                        await self._review_gate(store, fid, pr_url, repo)
                    # The MERGE edge — last, so it sees this pass's rebase / verify /
                    # CI / review outcomes, and re-reads the feature rather than
                    # trusting the snapshot those gates may have changed.
                    if self.auto_merge:
                        await self._maybe_auto_merge(store, fid, pr_url, repo)
            except Exception:  # noqa: BLE001 — a reconcile error must never kill the loop
                log.warning("[project_board] reconcile for %s failed", fid, exc_info=True)

    async def _auto_merge_blockers(self, store, feature: dict, pr_url: str, repo: str) -> list[str]:
        """Why this in_review PR must NOT be auto-merged right now — empty means every
        gate the loop owns is green AND current. Each reason is a short, greppable
        phrase; the caller logs them at debug so a parked PR is explainable, not
        mysterious. Order: the cheap board reads first, GitHub last."""
        fid = feature["id"]
        labels = set(feature.get("labels") or [])
        # The board-side half is shared with the PM-facing `next_action` (#208,
        # store.merge_posture) — one decoding of the review sub-state labels.
        why: list[str] = list(
            merge_posture(feature, auto_merge=self.auto_merge, review_gate=self.review_gate)["blockers"]
        )
        if await self._budget_get(store, fid, "auto-merge", feature) >= self.auto_merge_max:
            why.append("merge attempts exhausted")
        if why:
            return why
        # Verdict currency (#131): the merged-state gate must have run against the
        # base that will actually land. Stale = unverified, so hold (never block).
        # But ONLY when there is a local gate to have verified the merged state WITH:
        # `_verify_merged_state` returns before stamping when `local_gate_cmd` is
        # blank (the default), so demanding the stamp regardless made auto_merge
        # unreachable on every board without a local gate — `merged-verified stamp
        # (none)` forever, at debug level, while review-clean + CI-green cards sat
        # in_review (#209). Without a local gate CI + GitHub's CLEAN are the gates,
        # as the verify edge's own docstring says.
        if self.auto_rebase and self._local_gate_cmd_for(feature):
            base = self._base_branch_for(feature)
            head = await worktree.origin_head_sha(repo, base)
            if not head:
                return ["base sha unavailable"]
            stamped = next(
                (
                    lb[len(LABEL_MERGED_VERIFIED_PREFIX) :]
                    for lb in labels
                    if lb.startswith(LABEL_MERGED_VERIFIED_PREFIX)
                ),
                "",
            )
            if not stamped or not head.startswith(stamped):
                return [f"merged-verified stamp {stamped or '(none)'} ≠ {base}@{head[:_MERGED_VERIFIED_SHA_LEN]}"]
        info = await worktree.pr_merge_info(pr_url, cwd=repo)
        mss = info.get("mergeStateStatus") or ""
        if info.get("isDraft") is True:
            # #207: GitHub reports CLEAN for a draft whose checks pass, so the status
            # alone never says "draft" — and `gh pr merge` refuses a draft, which used
            # to burn an auto_merge_max attempt per poll and park the card on "merge
            # attempts exhausted" with no hint. A named blocker instead; the fix is
            # one `gh pr ready` (open_pr already does it for an adopted coder draft).
            return [
                f"draft (PR is a draft — run `gh pr ready {pr_url}`; the loop never spends a merge attempt on a draft)"
            ]
        if mss != "CLEAN":
            # BLOCKED = required checks not satisfied; UNSTABLE = a non-required check
            # failing; BEHIND/DIRTY = the rebase edge's job; UNKNOWN = GitHub still
            # computing; "" = gh failed. None of them is a merge.
            return [f"github mergeStateStatus={mss or 'unavailable'}"]
        return []

    async def _maybe_auto_merge(self, store, fid: str, pr_url: str, repo: str) -> bool:
        """Merge an in_review PR once every gate the loop owns is green and current
        (see ``_auto_merge_blockers``). Returns True if it merged. The board flips to
        done on the next reconcile pass (the existing MERGED edge — one Done path,
        idempotent, webhook-compatible). A refusal is retried next pass up to
        ``auto_merge_max`` times, then recorded on the bead and left for a human —
        never a block: the work is good, only the merge didn't land."""
        feature = await asyncio.to_thread(store.get_feature, fid)
        if feature is None:  # card deleted between the reconcile snapshot and this re-read
            log.debug("[project_board] %s vanished before auto-merge — nothing to merge", fid)
            return False
        why = await self._auto_merge_blockers(store, feature, pr_url, repo)
        if why:
            # Store-only bookkeeping (a bead comment) — off the event loop (#258).
            await asyncio.to_thread(self._note_draft_hold, store, fid, pr_url, why)
            log.debug("[project_board] %s not auto-merging: %s", fid, "; ".join(why))
            return False
        self._draft_noted.discard(fid)
        ok, detail = await worktree.merge_pr(pr_url, method=self.merge_method, cwd=repo)
        if not ok:
            # gh's exit code is not the verdict — the merge may have landed and a
            # later step failed, or a concurrent merge (webhook, human) beat us. The
            # PR's real state is.
            ok = (await worktree.pr_state(pr_url, cwd=repo)) == "MERGED"
        if ok:
            await self._budget_reset(store, fid, "auto-merge")
            log.info(
                "[project_board] %s auto-merged (%s, all gates green + current): %s", fid, self.merge_method, pr_url
            )
            # Remote-branch cleanup, best-effort; the worktree (which still holds the
            # local branch) is reaped when the reconcile reads MERGED.
            branch = worktree.branch_name(fid, (feature or {}).get("title") or "")
            if not await worktree.delete_remote_branch(repo, branch):
                log.info("[project_board] %s remote branch %s not deleted (already gone or protected)", fid, branch)
            return True
        n = await self._budget_get(store, fid, "auto-merge", feature) + 1
        await self._budget_set(store, fid, "auto-merge", n)
        if n >= self.auto_merge_max:
            try:
                await asyncio.to_thread(
                    store.comment,
                    fid,
                    f"auto-merge gave up after {n} attempt(s) — every gate is green but GitHub refused the "
                    f"merge; needs a human: {pr_url}\n{detail}",
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
                log.warning("[project_board] %s auto-merge give-up comment failed", fid, exc_info=True)
            log.warning("[project_board] %s auto-merge gave up after %d attempt(s): %s", fid, n, detail)
        else:
            log.warning(
                "[project_board] %s auto-merge refused (attempt %d/%d): %s", fid, n, self.auto_merge_max, detail
            )
        return False

    def _note_draft_hold(self, store, fid: str, pr_url: str, why: list[str]) -> None:
        """ONE bead comment the first time the auto-merge edge holds on a draft (#207):
        `open_pr`'s `gh pr ready` can fail (a fork PR, no write on base) or the operator
        may have drafted the PR — either way the hold was only a DEBUG line, invisible
        on the card. Mirrors the give-up comment; a comment failure never breaks the
        reconcile. The mark clears when the PR is seen non-draft, so a later re-draft
        is noted again (once)."""
        if not any(w.startswith("draft") for w in why):
            self._draft_noted.discard(fid)
            return
        if fid in self._draft_noted:
            return
        self._draft_noted.add(fid)
        try:
            store.comment(
                fid,
                f"auto-merge is holding: the PR is a draft — run `gh pr ready {pr_url}` (or leave it drafted "
                f"as a hold); the loop never spends a merge attempt on a draft",
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
            log.warning("[project_board] %s draft-hold comment failed", fid, exc_info=True)
        log.info("[project_board] %s auto-merge holding on a draft PR: %s", fid, pr_url)

    async def _maybe_rebase(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """If a sibling merge left this in_review PR BEHIND/DIRTY vs base, refresh it.

        Returns True if it acted (rebased / re-dispatched / blocked) so the caller skips
        the CI reconcile this pass; False when there's nothing to do (CLEAN, a checks-only
        BLOCKED, an UNKNOWN still computing, or a transient gh/infra hiccup → next poll
        retries). BEHIND (stale, no conflict) → a clean rebase + force-push, NO coder.
        DIRTY (a real conflict) → the rebase aborts, so re-dispatch the coder to re-resolve
        off the fresh base, bounded by rebase_fix_max, then Blocked for a manual rebase."""
        fid = feature["id"]
        mss = await worktree.pr_merge_state(pr_url, cwd=repo)
        if mss not in ("BEHIND", "DIRTY"):
            return False  # CLEAN / BLOCKED(checks) / UNKNOWN(computing) / DRAFT → not ours
        base = self._base_branch_for(feature)
        branch = worktree.branch_name(fid, feature.get("title") or "")
        outcome, detail = await worktree.rebase_onto_base(repo, branch, base, root=self.root)
        if outcome == "clean":
            log.info("[project_board] %s auto-rebased onto %s (was %s) — force-pushed", fid, base, mss)
            return True
        if outcome == "error":
            log.warning(
                "[project_board] %s auto-rebase hit infra trouble (%s) — next poll retries: %s", fid, mss, detail
            )
            return False  # transient — don't burn the coder budget on an infra blip
        # outcome == "conflict": a real merge conflict only the coder can resolve.
        n = await self._budget_get(store, fid, "rebase", feature)
        if n >= self.rebase_fix_max:
            await asyncio.to_thread(
                store.flag_blocked,
                fid,
                f"rebase conflict with {base} after {n} attempt(s) — needs a manual rebase: {pr_url}",
            )
            await worktree.reap_feature_worktree(repo, self.root, fid)
            log.warning("[project_board] %s blocked (rebase conflict, %d attempt(s)): %s", fid, n, detail)
            return True
        await self._budget_set(store, fid, "rebase", n + 1)
        self._ci_prior_diff.pop(fid, None)
        self._ci_feedback[fid] = (
            f"Your branch now CONFLICTS with `{base}` — a sibling change merged into the same "
            f"file(s): {detail}. Re-apply your change onto the latest `{base}` and resolve the "
            "conflict, keeping BOTH sides' intent. Then stop."
        )
        await asyncio.to_thread(store.requeue, fid)
        log.info(
            "[project_board] %s rebase conflict — re-dispatch %d/%d to resolve (%s): %s",
            fid,
            n + 1,
            self.rebase_fix_max,
            mss,
            detail,
        )
        return True

    async def _verify_merged_state(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """Re-verify an ``in_review`` PR's VERDICT after its base moved (#131).

        The rebase above only acts on BEHIND/DIRTY — but without strict base-freshness
        a PR whose base advanced still reads CLEAN, merges clean, and nobody ever ran
        the gate on the state that will actually land (five straight PRs, verified by
        hand). So when current ``origin/<base>`` ≠ the ``merged-verified:<sha>`` stamp
        on the bead (a missing stamp counts as moved — the first poll verifies and
        stamps), build the merged state (branch tip + that base commit) in a throwaway
        worktree, run ``local_gate_cmd`` there, and stamp the SHORT base sha the verdict
        was verified against — the ONE field an adjudicator checks for verdict currency
        (short because ``merged-verified:`` + a full 40-char sha = 56 chars blew beads'
        50-char label cap, so #132's stamp never actually landed until #135). The stamp
        is best-effort bookkeeping: a ``BoardError`` writing it is caught and logged so
        the required CI/merge reconciliation is never skipped, and a write that didn't
        land never spends the re-verify budget. Same principle as the completion gate
        (#113): verify the property, don't trust the report.

        NON-BLOCKING by default: a moved base is unverified, not broken. A green gate
        (or one that can't run — the ``_run_local_gate`` fail-open contract; CI is
        still the real gate) just refreshes the stamp and the card stays in review;
        only a CLEAN gate FAILURE on the merged state blocks. Bounded by
        ``merged_verify_max`` (0 = unlimited): once spent, re-verification stops and
        the stale stamp stays visible to the adjudicator rather than the loop burning
        a gate run every poll forever — with ``auto_merge`` on that hold is the merge
        edge's, so exhaustion logs a WARNING naming the remedy. A merge conflict is the
        DIRTY/rebase edge's job and an infra error retries next poll — neither burns
        budget nor stamps. Returns True only when it BLOCKED the card (the caller
        skips the rest of this pass)."""
        fid = feature["id"]
        if not self._local_gate_cmd_for(feature):
            return False  # no gate → nothing to verify the merged state WITH
        base = self._base_branch_for(feature)
        base_sha = await worktree.origin_head_sha(repo, base)
        if not base_sha:
            return False  # transient git/infra hiccup — next poll retries
        stamped = next(
            (
                l[len(LABEL_MERGED_VERIFIED_PREFIX) :]
                for l in feature.get("labels") or []
                if l.startswith(LABEL_MERGED_VERIFIED_PREFIX)
            ),
            "",
        )
        if stamped == base_sha[:_MERGED_VERIFIED_SHA_LEN]:
            return False  # the verdict is current — base hasn't moved since it was stamped
        n = await self._budget_get(store, fid, "merged-verify", feature)
        if self.merged_verify_max and n >= self.merged_verify_max:
            if n == self.merged_verify_max:  # log the exhaustion once, then stay quiet
                await self._budget_set(store, fid, "merged-verify", n + 1)
                if self.auto_merge:
                    # The loop IS the adjudicator here, and a stale stamp is a hard hold
                    # on the merge edge — say so, and say what unsticks it.
                    log.warning(
                        "[project_board] %s merged-verify budget (%d) spent with auto_merge on — the card "
                        "will NOT auto-merge until base stops moving or merged_verify_max is raised "
                        "(0 = unlimited); label it merge-hold to hand it to a human: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
                # Only claim a "stale stamp" when one actually exists. With the budget
                # exhausted and NO stamp ever written (e.g. rebase_fix_max=0, or the
                # pre-#135 world where every write failed), the adjudicator sees an
                # UNVERIFIED merged state — reporting a stale verdict that isn't there
                # is the same lie #132 was built to prevent.
                if stamped:
                    log.info(
                        "[project_board] %s base moved again but the merged-verify budget (%d) is spent — "
                        "leaving the stale merged-verified stamp for the adjudicator: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
                else:
                    log.info(
                        "[project_board] %s base moved but the merged-verify budget (%d) is spent and no "
                        "merged-verified stamp was ever written — the merged state stays unverified: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
            return False
        branch = worktree.branch_name(fid, feature.get("title") or "")
        outcome, detail = await worktree.merged_state_worktree(repo, branch, base_sha, root=self.root)
        if outcome == "error":
            log.warning("[project_board] %s merged-state verify hit infra trouble — next poll retries: %s", fid, detail)
            return False
        if outcome == "conflict":
            # A real conflict is the DIRTY/rebase edge's job (pr_merge_state reads
            # DIRTY once GitHub recomputes) — not a verdict, not a reason to block.
            log.info(
                "[project_board] %s merged-state verify: merge conflicts (%s) — leaving to the rebase edge", fid, detail
            )
            return False
        try:
            failure = await self._run_local_gate(detail, feature)
        finally:
            await worktree.remove_worktree(repo, detail)
        short = base_sha[:_MERGED_VERIFIED_SHA_LEN]
        if failure is None:
            # Green: the verdict still holds on the merged state. Stamp the SHORT sha,
            # then — and only then — spend a budget unit. The stamp is optional
            # bookkeeping: a BoardError writing it must NOT abort the reconcile pass
            # (the merge/CI edges below still have to run) nor burn the re-verify budget
            # on a write that didn't land — the next poll simply re-verifies (#135).
            try:
                await asyncio.to_thread(store.record_merged_verified, fid, short)
            except BoardError:
                log.warning(
                    "[project_board] %s merged-state gate green but stamping the verified sha failed — "
                    "reconcile continues, next poll re-verifies: %s",
                    fid,
                    pr_url,
                    exc_info=True,
                )
                return False
            await self._budget_set(store, fid, "merged-verify", n + 1)
            log.info(
                "[project_board] %s merged-state gate green — verdict re-verified against %s@%s",
                fid,
                base,
                short,
            )
            return False
        await self._budget_set(store, fid, "merged-verify", n + 1)
        await asyncio.to_thread(
            store.flag_blocked,
            fid,
            f"gate FAILED on the merged state (branch + {base}@{short}) — the PR merges clean "
            f"but the RESULT is broken; needs triage: {pr_url}\n{failure}",
        )
        await worktree.reap_feature_worktree(repo, self.root, fid)
        log.warning("[project_board] %s blocked (merged-state gate failed against %s@%s)", fid, base, short)
        return True

    async def _reconcile_ci(self, store, fid: str, pr_url: str, repo: str, feature: dict | None = None):
        """Closed-loop verify edge: an OPEN ``in_review`` PR whose checks FAILED is
        bounced back to the coder — and the re-dispatch *improves on the last try*
        rather than blindly repeating it (the missing OODA correction; before this a
        red PR sat in_review forever, then a same-model retry re-made the same mistake).

        Two improvement levers, both ProtoMaker-style:
        - **Carry the lesson forward** — inject the CI failure summary AND the prior
          attempt's diff into the next prompt (fresh-both keeps a fresh session, but
          the coder sees what it tried and why it failed).
        - **Same-tier fix, THEN escalate** — a red check is usually a fixable nit (a
          lint error, a golden-map update, a flaky assertion) the current tier can
          self-correct once it SEES the error, not a model-capability ceiling. So
          spend ``ci_fix_max`` same-tier retries first; only when those are exhausted
          does a configured `coders` ladder climb a tier (smart→reasoning→opus, the
          ladder is the bound → top tier fails → Blocked). Without a ladder the
          exhausted budget blocks directly. (Escalating on the FIRST failure burned
          the expensive tiers on one-line lint fixes — the goal-fix budget already
          learned this lesson; the CI path now mirrors it.)

        Two guards keep this from bouncing a PR it shouldn't (bd-1zp):
        - **Merged/closed guard** — ``_reconcile_prs`` read the PR state at the top of
          the poll, but the rebase/`gh` round-trips since then leave a window in which
          the PR could have merged or closed. Re-read the state right here and bail on
          anything that is no longer ``OPEN`` — a CI fix must NEVER dispatch against a
          PR that has already left review.
        - **Advisory filter** — ``pr_ci_status`` only reports ``failing`` when a
          *blocking* check (a required check or a GitHub Actions run) is red; a red
          third-party advisory status (CodeRabbit, a coverage bot) reads ``passing`` and
          never triggers a bounce.

        Passing/pending/no-checks left in review (the merge edge resolves it)."""
        if await worktree.pr_state(pr_url, cwd=repo) != "OPEN":
            return  # merged/closed since the poll started -> never dispatch a CI fix
        status, summary = await worktree.pr_ci_status(pr_url, cwd=repo)
        if status != "failing":
            return
        # Carry the lesson: the CI error + the diff that failed it (best-effort).
        self._ci_feedback[fid] = summary
        self._ci_prior_diff[fid] = await worktree.pr_diff(pr_url, cwd=repo)

        async def _block(reason: str):
            await asyncio.to_thread(store.flag_blocked, fid, reason)
            self._ci_feedback.pop(fid, None)
            self._ci_prior_diff.pop(fid, None)
            await self._budget_reset(store, fid, "ci-fix")

        # Same-tier CI-fix budget FIRST (both ladder and single-coder): a red check
        # is usually a fixable nit the current tier can correct once it sees the
        # error — don't burn a stronger model on a one-line lint fix. The CI error +
        # prior diff are already injected above, so the re-dispatch improves on the
        # last try rather than repeating it.
        attempts = await self._budget_get(store, fid, "ci-fix", feature)
        if attempts < self.ci_fix_max:
            await self._budget_set(store, fid, "ci-fix", attempts + 1)
            await asyncio.to_thread(store.requeue, fid)
            log.info(
                "[project_board] reconcile → same-tier CI-fix (attempt %d/%d): %s",
                attempts + 1,
                self.ci_fix_max,
                fid,
            )
            return

        # Same-tier budget exhausted. With a ladder, climb a model tier and reset the
        # per-tier budget so the new rung gets its own fix attempts; without one, block.
        if self.escalation_on:
            nxt = await asyncio.to_thread(store.escalate, fid, f"CI failed: {_ci_failure_reason(summary)}")
            if not nxt:
                await _block(
                    f"CI failing at the top model tier after {attempts} same-tier fix(es) — needs triage: {pr_url}"
                )
                await worktree.reap_feature_worktree(repo, self.root, fid)
                log.warning("[project_board] reconcile → blocked (CI fails at top tier): %s", fid)
                return
            await self._budget_reset(store, fid, "ci-fix")  # fresh same-tier budget at the new rung
            await asyncio.to_thread(store.requeue, fid)
            log.info("[project_board] reconcile → escalate to %s + re-dispatch (CI failed): %s", nxt, fid)
            return

        await _block(f"CI still failing after {attempts} fix attempt(s) — needs triage: {pr_url}")
        await worktree.reap_feature_worktree(repo, self.root, fid)
        log.warning("[project_board] reconcile → blocked (CI fails, %d attempt(s) exhausted): %s", attempts, fid)

    async def _drive_task(self, feature: dict, delegate) -> None:
        """Drive a ``task`` bead with a sister-agent assignee — an ACP coder OR an A2A
        agent (#217/#304) — the task sibling of ``_drive``. No worktree, no git, no PR:
        dispatch the spec + acceptance criteria to the assignee delegate over its native
        adapter (``coder_seam.dispatch_task`` picks acp/a2a by the delegate's type),
        then record the reply as the deliverable (``record_delivery`` → in_review, where
        a human or auto-verify closes it). A dispatch failure/timeout is classified like
        a coder failure and blocks the card for triage — a task has no model ladder to
        climb (the assignee is fixed), so the capability-escalation edge collapses to the
        coder path's terminal block."""
        store = self._store()
        fid = feature["id"]
        prompt = self._build_task_prompt(feature)
        try:
            reply = await coder_seam.dispatch_task(delegate, prompt, timeout=self.coder_timeout or None)
        except asyncio.CancelledError:
            raise  # operator cancel / shutdown owns the edge — never a block
        except (worktree.WorktreeError, worktree.CoderTimeout) as exc:
            if self._shutting_down:
                log.info("[project_board] %s task dispatch aborted by shutdown — no block", fid)
                return
            policy = classify(str(exc))
            log.warning("[project_board] %s task blocked (%s): %s", fid, policy.category, exc)
            await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s task dispatch unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            return
        await asyncio.to_thread(store.record_delivery, fid, text=reply or "")
        log.info("[project_board] %s task delivered (%d chars) → in_review", fid, len(reply or ""))

    def _build_task_prompt(self, feature: dict) -> str:
        """The ``delegate_to`` prompt for a ``task`` bead (#217): the spec + acceptance
        criteria, framed as a request for a DELIVERABLE (a doc, a decision, an artifact
        ref), NOT a code change — a task has no worktree or PR, and the delegate's reply
        IS the deliverable ``record_delivery`` records."""
        title = feature.get("title", "")
        spec = feature.get("spec", "")
        criteria = feature.get("acceptance_criteria", "")
        criteria_block = f"\n## Acceptance criteria (definition of done)\n{criteria}\n" if criteria.strip() else ""
        return (
            f"You have been assigned ONE task. Complete it and reply with the "
            f"deliverable — a document, a decision, or a reference to the artifact you "
            f"produced. There is no code change, worktree, or PR: your reply IS the "
            f"deliverable, so make it self-contained.\n\n"
            f"# {title}\n\n"
            f"## Task\n{spec}\n"
            f"{criteria_block}"
        )

    def _record_bg(self, fid: str, label: str, fn, *args, **kwargs) -> None:
        """Run one store write-back on a worker thread WITHOUT awaiting it (#258) —
        the offload for coder_seam's ``record_gens``/``record_verified`` callbacks,
        which are sync and invoked ON the event loop mid-dispatch (so the call site
        can't reach ``asyncio.to_thread`` itself). The future is parked per fid;
        ``_await_bg_records`` is the drive's barrier. Failures are logged and dropped
        — both call sites are documented fire-and-forget/best-effort."""

        def _call() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 — fire-and-forget: never raise into the executor
                log.warning("[project_board] %s background %s write failed (ignored)", fid, label, exc_info=True)

        self._bg_records.setdefault(fid, []).append(asyncio.get_running_loop().run_in_executor(None, _call))

    async def _await_bg_records(self, fid: str) -> None:
        """Barrier for ``_record_bg``'s in-flight write-backs. The drive awaits it as
        soon as dispatch returns, so gens/verified records have landed on the bead
        before the PR opens — the same ordering the synchronous callbacks gave, just
        off the event loop. Never raises (``_record_bg`` swallows per-write)."""
        for fut in self._bg_records.pop(fid, []):
            await fut

    async def _drive(self, feature: dict):
        """Drive one feature ready→in_review (or →blocked). `done` is set later by
        the merge webhook. With per-tier coders configured, a *capability* failure
        (coder errored / produced no diff) climbs the ladder; with a single coder
        it blocks at once — no redundant tier dance."""
        store = self._store()
        fid = feature["id"]
        # #90: resolve repo/base/coders from THIS feature's project, not the instance
        # default — so a multi-repo board builds each feature in its own checkout and
        # escalates through the ladder its project declares.
        repo = self._repo_for(feature)
        base = self._base_branch_for(feature)
        coders = self._coders_for(feature)
        title = f"feat: {feature['title']}"
        raw_title = feature.get("title") or ""  # #227: slugged onto the branch/dir tail
        tier = (await asyncio.to_thread(store.current_tier, fid)) if self.escalation_on else ""
        retries = 0  # transient-failure retries at the current tier (reset on a climb)
        wt = branch = None
        pr_url = None  # set once open_pr returns — the cancel paths below close it (#211)
        keep_wt = False  # reuse the worktree on a goal-fix retry (keep the impl; add tests)
        try:
            while True:
                # Rebuild the prompt each attempt so a re-dispatch (CI bounce,
                # goal-verify gap, or tier escalation) picks up the latest
                # _ci_feedback + _ci_prior_diff. Fetch this area's distilled lessons
                # from the KG (best-effort, async) and inject them — the flywheel READ.
                lessons = await self._fetch_kg_lessons(feature)
                prompt = self._build_prompt(feature, lessons=lessons)
                coder_name = coders.get(tier, self.coder_name) if self.escalation_on else self.coder_name
                coder = self._resolve_delegate(coder_name, "acp")
                if coder is None:
                    await asyncio.to_thread(
                        store.flag_blocked, fid, f"coder delegate {coder_name!r} not configured/enabled"
                    )
                    return
                try:
                    # How this attempt gets its worktree + coder result:
                    #  • keep_wt  → REUSE the kept worktree (impl intact), one re-dispatch.
                    #    A goal-fix/gate-fix retry must not throw the implementation away —
                    #    the coder only ADDS what the reviewer flagged (usually tests); a
                    #    fresh rebuild makes it re-implement and never reach the tests (the
                    #    bd-2fd/bd-3cj block).
                    #  • coder.solve (ADR 0064 P2, opt-in) → the execution-grounded
                    #    ladder over the feature's acceptance tests (coder_seam.py).
                    #    Same "from-scratch build only" rule as max-mode: a carried-
                    #    forward re-dispatch FIXES the existing diff with one coder.
                    #    Only preempts max-mode when max_mode_n<=1 (_use_coder_solve) —
                    #    a board already running Max-Mode keeps that behavior.
                    #  • max-mode → N parallel candidates, judge, promote the winner (#21).
                    #    ONLY for a from-scratch build: a carried-forward re-dispatch (a CI
                    #    bounce / goal-fix / gate-fix — all signalled by _ci_feedback) FIXES
                    #    the existing diff with one coder, so it must NOT re-fan-out N.
                    #  • otherwise → one fresh worktree, one dispatch.
                    if keep_wt and wt is not None:
                        keep_wt = False  # consume the reuse
                        self._inflight[fid] = (repo, wt, branch)
                        # Tap this re-dispatch into the live monitor (#84) — same gen 1,
                        # continuing the current build (no progress_new_run, so the drawer
                        # keeps the prior history rather than blanking on a keep-worktree fix).
                        result = await coder_seam.dispatch_coder_tapped(
                            coder, wt, prompt, fid=fid, gen=1, tier=tier, timeout=self.coder_timeout or None
                        )
                    elif self._use_coder_solve(feature) and not self._ci_feedback.get(fid):
                        files_to_modify = feature.get("files_to_modify") or []
                        # #90: every solve() knob resolves from THIS feature's project.
                        solve = self._coder_solve_settings(feature)
                        fusion = (
                            self._resolve_delegate(solve["fusion_delegate"], "openai")
                            if solve["fusion_delegate"]
                            else None
                        )
                        if fusion is not None:
                            # Gate BEFORE dispatch: fusion can't tool-call and returns
                            # whole-file replacements, so an oversized file risks a
                            # silent truncated rewrite (coder_seam.fusion_viable_for_files).
                            # Not viable ⇒ this dispatch just skips the fusion rung — the
                            # ladder still runs greedy/best-of-k/tree-search unchanged.
                            viable, reason = coder_seam.fusion_viable_for_files(
                                repo,
                                files_to_modify,
                                max_file_chars=solve["fusion_max_file_chars"],
                                max_total_chars=solve["fusion_max_total_chars"],
                            )
                            if not viable:
                                log.info("[project_board] %s fusion rung skipped for this dispatch: %s", fid, reason)
                                fusion = None
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        wt, branch, result = await coder_seam.dispatch(
                            task=prompt,
                            coder=coder,
                            repo=repo,
                            base=base,
                            root=self.root,
                            fid=fid,
                            dispatch_timeout=self.coder_timeout or None,
                            test_cmd=solve["test_cmd"],
                            test_timeout=solve["test_timeout"],
                            budget=solve["budget"],
                            k=solve["k"],
                            tree_depth=solve["tree_depth"],
                            record_gens=lambda n: self._record_bg(fid, "record_gens", store.record_gens_spent, fid, n),
                            fusion_delegate=fusion,
                            fusion_k=solve["fusion_k"],
                            files_to_modify=files_to_modify,
                            fusion_max_file_chars=solve["fusion_max_file_chars"],
                            # #86: same host-env strip the gate/preflight/format spawns
                            # get — keep the whitelist consistent across every subprocess.
                            env_passthrough=self.env_passthrough,
                            tier=tier,  # #84: label each solve gen with the current tier
                            # #91: persist the verified candidate on the bead at the
                            # verify boundary, so a crash before open_pr is salvageable.
                            record_verified=lambda br_name, sha, wt_path: self._record_bg(
                                fid,
                                "record_verified",
                                store.record_verified_candidate,
                                fid,
                                branch=br_name,
                                sha=sha,
                                worktree=wt_path,
                            ),
                            commit_message=title,
                            title=raw_title,  # #227: canonical branch/dir slug tail
                            max_concurrent_sessions=solve["max_concurrent_sessions"],
                        )
                        self._inflight[fid] = (repo, wt, branch)
                    elif self.max_mode_n > 1 and not self._ci_feedback.get(fid):
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        wt, branch, result = await self._dispatch_max_mode(
                            feature, coder, prompt, repo, base, fid, tier
                        )
                        self._inflight[fid] = (repo, wt, branch)
                    else:
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        wt, branch = await worktree.create_worktree(repo, base, fid, self.root, title=raw_title)
                        self._inflight[fid] = (repo, wt, branch)  # track for shutdown reaping
                        result = await coder_seam.dispatch_coder_tapped(
                            coder, wt, prompt, fid=fid, gen=1, tier=tier, timeout=self.coder_timeout or None
                        )  # taps live monitor (#84); reaps subprocess; CoderTimeout if it overruns
                    # #258 barrier: any record_gens/record_verified write-backs the
                    # dispatch scheduled on worker threads land before the drive
                    # proceeds toward open_pr (the pre-offload ordering).
                    await self._await_bg_records(fid)
                    # Requirement-ledger write-back (#113): merge the reply's
                    # `## Requirements` dispositions into the ledger and persist it on
                    # the bead — the LOOP writes dispositions, never the coder, and the
                    # completion gate below reads the ledger, never the reply text.
                    # Silence leaves an item open. The bead write is best-effort (the
                    # gate still checks the in-hand merged ledger), but a re-dispatch
                    # after requeue re-projects from the bead, so a lost write only
                    # costs a repeat disposition, never a false "disposed".
                    ledger = list(feature.get("requirements") or [])
                    if ledger:
                        ledger = apply_requirement_dispositions(ledger, _parse_requirements_reply(result or ""))
                        feature["requirements"] = ledger
                        try:
                            await asyncio.to_thread(store.set_requirements, fid, ledger)
                        except Exception:  # noqa: BLE001 — bookkeeping must not fail the build
                            log.warning(
                                "[project_board] %s requirement write-back failed (gate still checks "
                                "the merged ledger)",
                                fid,
                                exc_info=True,
                            )
                    # Goal-verification gate: confirm the diff meets the acceptance
                    # criteria before opening a PR. A gap is a capability failure (the
                    # coder didn't deliver) → escalate/block, don't open the PR.
                    if self.goal_verify:
                        gap = await self._verify_goal(feature, wt, base, result or "")
                        if gap:
                            # A goal-verify gap (e.g. the coder skipped tests) is
                            # fixable by the SAME coder told what's missing — not a
                            # model-capability failure. Carry the gap (+ the rejected
                            # diff, stashed by _verify_goal) as feedback and re-dispatch
                            # the same tier, bounded by goal_fix_max, BEFORE escalating.
                            n = await self._budget_get(store, fid, "goal-fix", feature)
                            if n < self.goal_fix_max:
                                await self._budget_set(store, fid, "goal-fix", n + 1)
                                # KEEP the worktree (the impl is in its files); the coder
                                # only ADDS what the reviewer flagged. The diff is on disk,
                                # so don't also carry it as prompt text (redundant/confusing).
                                self._ci_prior_diff.pop(fid, None)
                                self._ci_feedback[fid] = (
                                    "Your implementation from the previous attempt is ALREADY in this "
                                    "worktree's files. A reviewer rejected it before it could open a PR "
                                    f"for: {gap}. ADD what's missing to the existing files (usually the "
                                    "tests) — do NOT rewrite or delete the working implementation. Then stop."
                                )
                                log.info(
                                    "[project_board] %s goal-verify gap — re-dispatch %d/%d (tier=%s, keep worktree): %s",
                                    fid,
                                    n + 1,
                                    self.goal_fix_max,
                                    tier or "default",
                                    gap,
                                )
                                keep_wt = True  # reuse the worktree (impl intact) on the retry
                                continue
                            raise worktree.WorktreeError(f"goal verification failed: {gap}")
                    # Auto-fix lint/format before the PR — the coder can't run the repo's
                    # formatter (edit-only), so this clears trivial nits that would fail CI.
                    await self._run_fixups(wt, feature)
                    # Pre-PR local gate: run the repo's real checks in the worktree and, on
                    # failure, hand the coder the actual output to fix IN-WORKTREE before a PR
                    # (and a CI round-trip) ever opens. Same-tier, keep-worktree, bounded by
                    # local_gate_max; on exhaustion open the PR anyway (CI is the backstop).
                    gate_out = await self._run_local_gate(wt, feature)
                    if gate_out is not None:
                        n = await self._budget_get(store, fid, "gate-fix", feature)
                        if n < self.local_gate_max:
                            await self._budget_set(store, fid, "gate-fix", n + 1)
                            self._ci_prior_diff.pop(fid, None)  # impl is on disk; don't echo it back
                            self._ci_feedback[fid] = (
                                "Your changes are ALREADY in this worktree's files, but the pre-PR "
                                "gate failed. FIX what it reports in the existing files, then stop — "
                                "the loop opens the PR. Do NOT rewrite working code. Gate output:\n\n" + gate_out
                            )
                            log.info(
                                "[project_board] %s pre-PR gate failed — re-dispatch %d/%d (tier=%s, keep worktree)",
                                fid,
                                n + 1,
                                self.local_gate_max,
                                tier or "default",
                            )
                            keep_wt = True
                            continue
                        log.warning(
                            "[project_board] %s pre-PR gate still failing after %d fix(es) — opening PR anyway (CI backstop)",
                            fid,
                            n,
                        )
                    # Completion gate (#113, the planSpec mechanism): a feature may NOT
                    # reach in_review with OPEN requirement items — the ledger decides,
                    # not the coder's say-so. Same seam as the local gate (after the
                    # coder's fix rounds, before the PR opens). Open items bounce back
                    # same-tier/keep-worktree with the list, bounded by the goal-fix
                    # budget; exhaustion is a capability failure (escalate/block via
                    # the handler below), NEVER a PR with unaddressed requirements —
                    # unlike the local gate, this one does not fail open: a `declined`
                    # with a reason is always available, so "can't" has a valid exit.
                    if ledger and not _all_items_disposed(ledger):
                        open_items = [i for i in ledger if str(i.get("status", "")).lower() not in ("done", "declined")]
                        # #284: instrument the gate. An unresolved-disposition bounce is
                        # only diagnosable if we can tell a PARSE miss (the coder wrote a
                        # `## Requirements` section the loop failed to read) from genuine
                        # SILENCE. Record — at the gate, on the merged ledger the write-back
                        # above produced — the parsed dispositions, the still-open ids,
                        # len(result), whether the reply carried a `## Requirements` heading,
                        # and the first 200 chars after it.
                        diag = _requirement_gate_diagnostics(result or "", open_items)
                        log.info(
                            "[project_board] %s requirement gate diagnostics: %s",
                            fid,
                            _requirement_gate_diag_line(diag),
                        )
                        listing = "\n".join(f"- {i.get('id')}: {i.get('text')}" for i in open_items)
                        n = await self._budget_get(store, fid, "req-fix", feature)
                        if n < self.goal_fix_max:
                            await self._budget_set(store, fid, "req-fix", n + 1)
                            self._ci_prior_diff.pop(fid, None)  # impl is on disk; don't echo it back
                            self._ci_feedback[fid] = (
                                "Your implementation is ALREADY in this worktree's files, but these "
                                "requirement items are still OPEN on the ledger — no disposition was "
                                "recorded for them. Address each one, then END your reply with a "
                                "`## Requirements` section marking EVERY item `done` or `declined — "
                                "<concrete reason>` (silence is not a disposition):\n\n" + listing
                            )
                            # #284: persist the SAME diagnostic payload on the bead so the
                            # NEXT occurrence is diagnosable from the card, not only the log
                            # — the log line is process-local, the comment survives a restart.
                            try:
                                await asyncio.to_thread(
                                    store.comment,
                                    fid,
                                    f"requirement gate bounce (re-dispatch {n + 1}/{self.goal_fix_max}) — "
                                    f"diagnostics: {_requirement_gate_diag_line(diag)}",
                                )
                            except Exception:  # noqa: BLE001 — bookkeeping must not fail the build
                                log.warning(
                                    "[project_board] %s requirement gate diagnostic comment failed",
                                    fid,
                                    exc_info=True,
                                )
                            log.info(
                                "[project_board] %s requirement gate: %d open item(s) — re-dispatch %d/%d "
                                "(tier=%s, keep worktree)",
                                fid,
                                len(open_items),
                                n + 1,
                                self.goal_fix_max,
                                tier or "default",
                            )
                            keep_wt = True
                            continue
                        raise worktree.WorktreeError(
                            f"requirements unresolved: {len(open_items)} item(s) still open after "
                            f"{n} fix round(s): " + ", ".join(str(i.get("id")) for i in open_items)
                        )
                    # Source-issue closed guard (#166): re-check the issue before
                    # opening a PR. A closed source issue means another PR already
                    # resolved the ticket — opening a duplicate wastes reviewer/CI
                    # cycles. Fail-open: any gh error lets the PR proceed normally.
                    # #253: but on a MULTI-SLICE board several features can share one
                    # source_issue — when a sibling slice merges first its `Fixes #N`
                    # closes the shared issue, and this guard must NOT then cancel the
                    # remaining siblings. Only cancel when the closure came from OUTSIDE
                    # the board's own feature set (no done sibling with a PR for it).
                    si_raw = str(feature.get("source_issue") or "").strip()
                    if (
                        si_raw
                        and not await _source_issue_still_open(si_raw, wt)
                        and not await asyncio.to_thread(_issue_closed_by_board_sibling, store, feature)
                    ):
                        reason = f"source issue {si_raw} already closed — work superseded"
                        log.info("[project_board] %s skipping PR — %s", fid, reason)
                        try:
                            await asyncio.to_thread(store.cancel_feature, fid, reason)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "[project_board] %s cancel_feature failed — flagging blocked instead",
                                fid,
                                exc_info=True,
                            )
                            await asyncio.to_thread(store.flag_blocked, fid, reason)
                        await worktree.remove_worktree(repo, wt, branch or "")
                        self._inflight.pop(fid, None)
                        return
                    # Operator cancel (#211): re-read the card right before the PR edge.
                    # A card cancelled while the coder was finishing must NOT get a PR —
                    # the work is the operator's to salvage, not the loop's to publish
                    # (the old path opened it, then open_review refused the cancelled
                    # card and left an orphaned red PR nobody owned).
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        await self._end_cancelled_drive(store, fid, repo, wt, branch)
                        return
                    body = await self._with_source_issue_ref(feature, wt, _pr_body(result, feature))
                    # #207: un-draft an adopted PR only on the card's FIRST adoption (no
                    # pr_url yet → the draft is the coder's). A re-dispatch of a card that
                    # already owns its PR (CI-fail bounce) leaves a draft alone: that is
                    # the operator's hold on the loop's own PR, not the coder's.
                    pr_url = await worktree.open_pr(
                        wt, branch, base=base, title=title, body=body, promote_draft=not feature.get("pr_url")
                    )
                    # …and again after: a cancel that lands during the push/create
                    # closes the PR it just opened rather than handing it to open_review.
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
                        return
                except (worktree.NoChangesError, worktree.WorktreeError) as exc:
                    if self._shutting_down:
                        log.info("[project_board] %s dispatch aborted by shutdown — no escalation", fid)
                        self._inflight.pop(fid, None)
                        return
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        # The cancel verb reaped the worktree under the coder (#175) and the
                        # dispatch/commit failed on it — that is the cancel, not a blockable
                        # failure on a closed card.
                        log.info("[project_board] %s dispatch ended by operator cancel: %s", fid, exc)
                        await self._end_cancelled_drive(store, fid, repo, wt, branch)
                        return
                    policy = classify(str(exc))
                    # Empty result (#198, retry policy #2991): a dispatch that COMPLETED
                    # with no worktree diff AND no tool-call activity is its own failure
                    # class — the coder connected but never executed. That is often a
                    # transient ACP hiccup, so the first occurrence at a tier retries
                    # ONCE on the SAME tier with the same prompt BEFORE any failure is
                    # counted (pre-escalation: the ladder is not consulted, no escalation
                    # attempt spent). Only when the retry ALSO comes back empty is the
                    # failure recorded — with the ACP stop-reason the monitor tap stashed
                    # (the WHY the retro and drawer show) — and the normal escalation
                    # ladder proceeds (single coder / ladder top → blocked, reason naming
                    # the class + evidence).
                    if isinstance(exc, worktree.NoChangesError):
                        had_tools, stop = self._empty_result_signals(fid)
                        if not had_tools:
                            n = await self._budget_get(store, fid, "empty-result", feature) + 1
                            await self._budget_set(store, fid, "empty-result", n)
                            evidence = (
                                f"empty coder reply — no diff, no tool calls (stop_reason={stop or 'none reported'})"
                            )
                            if n < self.empty_result_max:
                                # The same_tier_retry marker on the attempt comment is how
                                # the retro tells this retry from an escalation: the tier
                                # is unchanged and no failure is counted yet.
                                try:
                                    await asyncio.to_thread(
                                        store.record_attempt,
                                        fid,
                                        tier=tier or await asyncio.to_thread(store.current_tier, fid),
                                        outcome=f"empty_result: {evidence} — same_tier_retry (pre-escalation)",
                                    )
                                except Exception:  # noqa: BLE001 — attempt bookkeeping must never mask the verdict
                                    log.warning(
                                        "[project_board] %s empty_result attempt record failed", fid, exc_info=True
                                    )
                                log.info(
                                    "[project_board] %s Retrying on same tier (empty reply) %d/%d — "
                                    "no failure counted: %s",
                                    fid,
                                    n,
                                    self.empty_result_max,
                                    evidence,
                                )
                                continue
                            # The same-tier retry also returned empty — NOW it is a coder
                            # failure: record it, then let the normal ladder climb.
                            reason = f"empty_result: {evidence} — {n} occurrence(s), same-tier retry exhausted"
                            try:
                                await asyncio.to_thread(
                                    store.record_attempt,
                                    fid,
                                    tier=tier or await asyncio.to_thread(store.current_tier, fid),
                                    outcome=reason,
                                )
                            except Exception:  # noqa: BLE001 — attempt bookkeeping must never mask the verdict
                                log.warning("[project_board] %s empty_result attempt record failed", fid, exc_info=True)
                            await self._budget_reset(store, fid, "empty-result")  # a climb gets its own retry window
                            if self.escalation_on:
                                nxt = await asyncio.to_thread(store.escalate, fid, reason[:200])
                                if nxt:
                                    log.info(
                                        "[project_board] %s escalating %s→%s (empty-reply retry exhausted): %s",
                                        fid,
                                        tier,
                                        nxt,
                                        evidence,
                                    )
                                    tier = nxt
                                    retries = 0
                                    # Fresh per-tier budgets on the climb — mirrors the
                                    # shared capability-escalation path below.
                                    await self._budget_reset(store, fid, "goal-fix", "gate-fix", "req-fix")
                                    continue
                            log.warning("[project_board] %s blocked (%s)", fid, reason)
                            await asyncio.to_thread(store.flag_blocked, fid, reason)
                            if wt:
                                await worktree.remove_worktree(repo, wt, branch or "")
                            self._inflight.pop(fid, None)
                            return
                    # A capability failure = the coder didn't deliver (no diff / dispatch
                    # error / timed out). Those are NOT transient-retried (re-running the
                    # same coder won't help) — they escalate a tier or block. Only true
                    # infra failures (push/fetch/gh network/rate-limit) get the backoff.
                    # A dispatch failure is a capability failure ONLY when the classifier
                    # does not recognize it as transient. A rate limit / session limit /
                    # network blip inside "coder dispatch failed: …" is the PROVIDER
                    # refusing the call, not the coder failing the task — escalating on
                    # it burned the whole tier ladder in ten seconds (three attempts,
                    # three tiers, a block; 2026-08-28, bd-cwpv.12/.16) and left `tier:`
                    # labels that misrouted the card when it was requeued after the reset.
                    dispatch_failed = str(exc).startswith("coder dispatch failed") and not policy.retryable
                    capability = (
                        isinstance(exc, (worktree.NoChangesError, worktree.CoderTimeout, coder_seam.SolveExhausted))
                        or dispatch_failed
                        or str(exc).startswith("goal verification failed")
                        or str(exc).startswith("requirements unresolved")
                    )
                    # 1. Transient infra → back off and retry the SAME tier (a re-dispatch
                    #    off the latest base also clears a merge conflict).
                    if policy.retryable and not capability and retries < policy.max_attempts - 1:
                        retries += 1
                        log.info(
                            "[project_board] %s %s — retry %d/%d in %ss: %s",
                            fid,
                            policy.category,
                            retries + 1,
                            policy.max_attempts,
                            policy.base_delay_s,
                            exc,
                        )
                        await asyncio.sleep(policy.base_delay_s)
                        continue
                    # 2. Capability failure + a ladder → climb a model tier (fresh budget).
                    if self.escalation_on and capability:
                        nxt = await asyncio.to_thread(store.escalate, fid, str(exc)[:200])
                        if nxt:
                            log.info("[project_board] %s escalating %s→%s: %s", fid, tier, nxt, exc)
                            # Keep the worktree ONLY across the escalation classes whose repair
                            # rounds explicitly retained it (#282): a goal-verify / requirement-
                            # ledger gate exhausts its keep-worktree fix budget with the VERIFIED
                            # impl still on disk and _ci_feedback seeded to "your work is already
                            # in this worktree". Carry that worktree into the escalated dispatch so
                            # the stronger tier sees the work and the feedback stays truthful.
                            # Anything else is a from-scratch build: the seeded feedback would lie
                            # about a worktree that no longer exists, so clear it (+ the prior diff)
                            # to keep prompt and worktree consistent.
                            keep_wt_class = str(exc).startswith(("goal verification failed", "requirements unresolved"))
                            if keep_wt_class and wt is not None:
                                keep_wt = True  # reuse the verified worktree; _ci_feedback is truthful
                            elif isinstance(exc, worktree.CoderTimeout):
                                # A timeout carries NO diff and NO CI output, so the stronger tier
                                # would otherwise get a BYTE-IDENTICAL prompt — blind to the fact a
                                # prior attempt ran out of time and what it was doing when killed
                                # (#146). Seed the CI/review-bounce feedback lever with the ring
                                # buffer's timeout context so the escalated dispatch leads with it.
                                self._ci_feedback[fid] = self._timeout_escalation_context(fid)
                                self._ci_prior_diff.pop(fid, None)  # a timeout produced no diff to echo back
                            else:
                                # Fresh worktree ahead — drop any gate-fix feedback describing the
                                # OLD worktree's files so the new build isn't told its work is on
                                # disk when it isn't.
                                self._ci_feedback.pop(fid, None)
                                self._ci_prior_diff.pop(fid, None)
                            tier = nxt
                            retries = 0
                            # Fresh goal-fix / local-gate / ledger budgets at the new tier —
                            # otherwise a tier that exhausted its retries hands the next
                            # (stronger) tier a spent budget, so it blocks on its first gap
                            # without a real shot.
                            await self._budget_reset(store, fid, "goal-fix", "gate-fix", "req-fix")
                            continue
                    # 3. Terminal, or retries/ladder exhausted → Blocked.
                    log.warning("[project_board] %s blocked (%s): %s", fid, policy.category, exc)
                    await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
                    if wt:
                        await worktree.remove_worktree(repo, wt, branch or "")
                    self._inflight.pop(fid, None)
                    return
                # Built + PR opened. The fleet PR-review pipeline reviews it on open;
                # only dispatch an explicit review when configured to (review_dispatch).
                log.info("[project_board] %s coder done (%d chars) → %s", fid, len(result or ""), pr_url)
                await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
                # Gate passed — reset the pre-PR budgets (goal-fix, local-gate, the
                # requirement ledger #113, and the empty-result count #198).
                await self._budget_reset(store, fid, "goal-fix", "gate-fix", "req-fix", "empty-result")
                if self.review_gate:
                    # Blocking adversarial review (M5). May requeue the feature with
                    # findings injected — the next drive carries them in the prompt.
                    await self._review_gate(store, fid, pr_url, repo)
                elif self.review_dispatch:
                    await self._request_review(fid, pr_url)
                # Keep the worktree (a CI-fail bounce re-dispatches); reaping happens
                # on a terminal block above, and the coder subprocess is already reaped.
                self._inflight.pop(fid, None)  # built OK — not an interrupted build to reap
                return
        except asyncio.CancelledError:
            if self._shutting_down or self._stop.is_set():
                raise  # shutdown owns the reap (stop() sweeps _inflight)
            # An operator cancel stopped this drive mid-flight (#211): the coder
            # subprocess is already reaped by dispatch_coder's finally; close any PR
            # it opened, reap the worktree, leave the trail on the card — and end
            # quietly (the task completes, never a CancelledError in the loop log).
            log.info("[project_board] %s drive stopped by operator cancel", fid)
            await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
        except BoardError as exc:
            if await asyncio.to_thread(self._cancelled, store, fid):
                # The card closed under us (open_review refused a cancelled card): a
                # cancel edge, not a block — and the PR it would have handed over is ours
                # to close.
                log.info("[project_board] %s cancelled under the drive: %s", fid, exc)
                await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
                return
            log.warning("[project_board] %s blocked (board): %s", fid, exc)
            await asyncio.to_thread(store.flag_blocked, fid, str(exc))
            self._inflight.pop(fid, None)
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            if wt:
                await worktree.remove_worktree(repo, wt, branch or "")
            self._inflight.pop(fid, None)

    # ── operator cancel during a drive (#211) ────────────────────────────────
    @staticmethod
    def _cancelled(store, fid: str) -> bool:
        """Is the card ``cancelled`` NOW? A fresh store read at the PR seam — fail OPEN
        (a read failure is not a cancel): the drive then proceeds exactly as before."""
        try:
            f = store.get_feature(fid)
        except Exception:  # noqa: BLE001 — BoardError, a fake store without the read
            return False
        return bool(f) and str(f.get("board_state") or "") == "cancelled"

    async def _end_cancelled_drive(self, store, fid: str, repo: str, wt, branch, *, pr_url: str | None = None) -> None:
        """Finish a drive whose card was cancelled by the operator: close the PR if one
        opened (with a comment pointing at the card), reap the worktree (#1 lifecycle
        rule), comment the trail on the card, release the slot. No flag_blocked, no
        escalation, nothing raised — a cancel is a terminal edge, not a failure.

        Runs ONCE per drive and to completion: a second cancel verb landing while this
        is awaiting gh/git cancels the drive task again, which would re-enter via the
        CancelledError handler — the ``_cancel_done`` mark makes that re-entry a no-op,
        and the body runs shielded so the repeat cancel can't abandon the reap or the
        trail halfway (a shutdown cancel is still honoured: ``stop()`` owns that reap)."""
        if fid in self._cancel_done:
            return
        self._cancel_done.add(fid)
        inner = asyncio.ensure_future(self._finish_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url))
        while True:
            try:
                await asyncio.shield(inner)
                return
            except asyncio.CancelledError:
                if inner.done() or self._shutting_down or self._stop.is_set():
                    inner.cancel()
                    raise
                log.info("[project_board] %s repeat cancel during cancel cleanup — finishing it", fid)

    async def _finish_cancelled_drive(self, store, fid: str, repo: str, wt, branch, *, pr_url: str | None) -> None:
        if not pr_url and branch:
            # The cancel may have landed INSIDE `git push` / `gh pr create` (the task
            # cancel kills the child, but GitHub may already have the PR) — the drive
            # never got a URL back, so look the branch up before declaring "no PR".
            try:
                pr_url = await worktree.pr_url_for_branch(branch, cwd=repo) or None
            except Exception:  # noqa: BLE001 — a gh failure here is not a cancel failure
                log.debug("[project_board] %s cancel: pr lookup for %s failed", fid, branch, exc_info=True)
        note = f"cancelled by operator — drive ended without a PR (branch {branch or '?'})"
        if pr_url:
            ok, detail = await worktree.close_pr(pr_url, comment=cancel_pr_comment(fid), cwd=repo)
            if ok and detail in (worktree.PR_ALREADY_MERGED, worktree.PR_ALREADY_CLOSED):
                note = f"cancelled by operator — {pr_url} {detail}, nothing to close"
                log.info("[project_board] %s cancelled — %s %s", fid, pr_url, detail)
            elif ok:
                note = f"cancelled by operator — closed {pr_url}"
                log.info("[project_board] %s cancelled — closed %s", fid, pr_url)
            else:
                note = f"cancelled by operator — could not close {pr_url} ({detail[:200]}); close it by hand"
                log.warning("[project_board] %s cancelled — could not close %s: %s", fid, pr_url, detail[:300])
        if wt:
            await worktree.remove_worktree(repo, wt, branch or "")
            note += "; worktree reaped"
        try:
            await asyncio.to_thread(store.comment, fid, note)
        except Exception:  # noqa: BLE001 — the trail is best-effort
            log.debug("[project_board] %s cancel comment failed", fid, exc_info=True)
        self._inflight.pop(fid, None)
        log.info("[project_board] %s %s", fid, note)

    async def _with_source_issue_ref(self, feature: dict, wt: str, body: str) -> str:
        """Stamp the feature's source issue onto the PR body — ``Fixes #n`` when the
        issue is in the PR's own target repo, ``Refs <url>`` cross-repo. No source
        issue ⇒ body unchanged (and no ``gh`` round-trip). ``worktree.repo_slug`` fails
        open, so an unresolvable target repo degrades to a ``Refs`` link — never a
        wrong ``Fixes`` and never a blocked PR."""
        parsed = _source_issue(feature)
        if not parsed:
            return body
        issue_slug, n = parsed
        target_repo = await worktree.repo_slug(cwd=wt)
        return _inject_source_issue_line(body, issue_slug, n, target_repo)

    async def _request_review(self, fid: str, pr_url: str):
        """Hand the PR to the reviewer (an a2a delegate, e.g. quinn). Best-effort:
        a review-dispatch failure doesn't block the feature — CI + the merge
        webhook are the gate; the reviewer is advisory signal."""
        reviewer = self._resolve_delegate(self.reviewer_name, "a2a")
        if reviewer is None:
            log.info("[project_board] no reviewer %r configured — skipping review dispatch", self.reviewer_name)
            return
        from plugins.delegates.adapters import ADAPTERS

        try:
            msg = f"Please review this PR for correctness and acceptance: {pr_url}"
            await ADAPTERS["a2a"].dispatch(reviewer, msg)
        except Exception as exc:  # noqa: BLE001 — fully best-effort: a review-dispatch
            # failure (DelegateError, httpx/connection, anything) must NEVER block a
            # feature whose PR already opened. CI + the merge webhook are the gate.
            log.warning("[project_board] review dispatch for %s failed: %s", fid, exc)

    # ── blocking review gate (plan M5) ────────────────────────────────────────
    async def _review_gate(self, store, fid: str, pr_url: str, repo: str) -> None:
        """Run the adversarial review workflow on the just-opened PR and act on the
        findings — the review sibling of the CI bounce:

        - **clean** (no blocker/major surviving the verify pass) → clear the review
          sub-state; the feature stays in_review for the merge edge.
        - **blocking findings** → store them on the bead (comment), inject them into
          the retry prompt via ``_ci_feedback`` (+ the PR diff via ``_ci_prior_diff``
          — the same carry-the-lesson levers), label ``changes-requested``, and
          requeue — bounded by ``review_fix_max``.
        - **budget exhausted** → ``flag_blocked`` for human review. NEVER a silent
          merge, and never a silent pass: a gate that can't run (no workflow runner,
          no parser, no reviewer) leaves the feature in_review with a warning — the
          same posture as CI being unreachable.

        Sequencing (ADR 0064): this is deliberately a single call-site-agnostic
        method — when the board face of execution-grounded selection lands, moving
        the gate after test-passing candidate selection is a one-line move.

        Re-entrancy (#205): at most ONE gate per feature runs at a time in this
        process. A second call while the first is still running (the reconcile's
        resume edge seeing the pending label the running gate just set) is a no-op
        — the running gate will land its own verdict. The resume edge keeps its
        job for gates that actually died (host restart: the set is empty on boot).
        """
        if fid in self._review_inflight:
            log.debug("[project_board] %s review gate already running — not re-armed", fid)
            return
        self._review_inflight.add(fid)
        try:
            await self._review_gate_run(store, fid, pr_url, repo)
        finally:
            self._review_inflight.discard(fid)

    async def _review_gate_run(self, store, fid: str, pr_url: str, repo: str) -> None:
        """The gate body — see ``_review_gate`` (the re-entrancy guard) for the contract."""
        await asyncio.to_thread(store.set_review_substate, fid, LABEL_REVIEW_PENDING)
        output, why = await self._run_review_workflow(fid, pr_url)
        if output is None:
            # Could not review — ``why`` names the actual cause (#180: no runner +
            # no reviewer, failed panel steps, a dead call — a failed finder step is
            # not a review; judging from it is how an unreviewed PR gets promoted,
            # ADR 0078 D3). Leave review-pending so the PR reconcile retries next
            # poll — but bounded: a persistently unrunnable gate escalates to the
            # operator instead of re-burning the workflow every poll forever.
            reason = why or "review produced no output"
            n = await self._budget_get(store, fid, "review-run") + 1
            await self._budget_set(store, fid, "review-run", n)
            if n >= self.review_run_max:
                # Deliberately KEEP review-pending through the block (#181): blocked
                # features aren't reconciled, so the label is inert while blocked —
                # but the moment the operator unblocks, the feature is back in_review
                # with review-pending set and the next poll re-arms the gate. Clearing
                # it here left an unblocked feature indistinguishable from a clean
                # review, so its PR could merge un-reviewed.
                await asyncio.to_thread(
                    store.flag_blocked,
                    fid,
                    f"review gate could not complete after {n} attempt(s) — {reason} — "
                    f"needs operator attention: {pr_url}",
                )
                await self._budget_reset(store, fid, "review-run")
                log.warning("[project_board] %s blocked (review gate unrunnable %d times: %s)", fid, n, reason)
                return
            log.warning(
                "[project_board] %s review gate could not run (%d/%d): %s — will retry on the next poll",
                fid,
                n,
                self.review_run_max,
                reason,
            )
            return
        await self._budget_reset(store, fid, "review-run")
        findings = self._parse_findings(output)
        if findings is None:
            # Host predates the findings convention (ADR 0077) — the gate can't
            # judge, so it must not pretend to. Record and leave in review.
            await asyncio.to_thread(
                store.set_review_substate, fid, None, note="review gate: host lacks graph.review.findings — gate inert"
            )
            log.warning("[project_board] %s review gate inert (no findings parser on this host)", fid)
            return
        # Remember this round's findings — the next run (a bounce re-review) passes
        # them back as the recipe's prior_findings input, making it a DELTA review
        # (drop fixed, carry still-open) instead of a from-scratch re-litigation.
        try:
            self._review_prior[fid] = json.dumps([f.to_dict() for f in findings]) if findings else ""
        except Exception:  # noqa: BLE001 — memory is an optimization, never a gate failure
            self._review_prior.pop(fid, None)
        blocking = [f for f in findings if f.verdict != "refuted" and f.severity in ("blocker", "major")]
        if not blocking:
            await asyncio.to_thread(
                store.set_review_substate,
                fid,
                LABEL_REVIEW_CLEAN,
                note=f"review gate: clean — {len(findings)} finding(s), none blocking (blocker/major)",
            )
            await self._budget_reset(store, fid, "review-fix")
            log.info("[project_board] %s review gate clean (%d non-blocking finding(s))", fid, len(findings))
            return

        rendered = self._render_findings(blocking)
        n = await self._budget_get(store, fid, "review-fix")
        if n >= self.review_fix_max:
            await asyncio.to_thread(store.set_review_substate, fid, None, note=rendered)
            await asyncio.to_thread(
                store.flag_blocked,
                fid,
                f"review findings persist after {n} fix attempt(s) — needs human review: {pr_url}",
            )
            self._ci_feedback.pop(fid, None)
            self._ci_prior_diff.pop(fid, None)
            await self._budget_reset(store, fid, "review-fix")
            log.warning("[project_board] %s blocked (review findings, %d bounce(s) exhausted)", fid, n)
            return
        await self._budget_set(store, fid, "review-fix", n + 1)
        # Carry the lesson exactly like the CI bounce: findings as the rejection
        # feedback + the reviewed diff so the coder fixes THIS attempt, not a fresh one.
        self._ci_prior_diff[fid] = await worktree.pr_diff(pr_url, cwd=repo)
        self._ci_feedback[fid] = (
            "An adversarial code review of your PR REQUESTED CHANGES. Fix every finding "
            "below in the existing branch (the PR updates on push) — do not rewrite "
            "unrelated code.\n\n" + rendered
        )
        await asyncio.to_thread(store.set_review_substate, fid, LABEL_CHANGES_REQUESTED, note=rendered)
        await asyncio.to_thread(store.requeue, fid)
        log.info(
            "[project_board] %s review gate bounce %d/%d (%d blocking finding(s))",
            fid,
            n + 1,
            self.review_fix_max,
            len(blocking),
        )

    async def _run_review_workflow(self, fid: str, pr_url: str) -> tuple[str | None, str | None]:
        """Produce the raw review output for a PR: the host's workflow runner
        (``runtime.state.STATE.workflow_run`` — published by the workflows plugin,
        no plugin import needed) running ``review_workflow``, else the configured
        a2a reviewer told to emit the findings convention.

        Returns ``(output, None)`` on success, ``(None, reason)`` when the review
        could not happen. The reason names the ACTUAL cause — runner missing,
        failed panel steps, a dead call — so the gate's retry warning and eventual
        block reason tell the operator what to fix instead of making them
        correlate a generic three-hypothesis message with the server log (#180:
        the live incident was simply the workflows plugin being disabled)."""
        number, repo_slug = _parse_pr_url(pr_url)
        runner = None
        try:
            from runtime.state import STATE

            runner = getattr(STATE, "workflow_run", None)
        except Exception:  # noqa: BLE001 — non-protoAgent host (tests) → try the reviewer
            runner = None
        # Why the workflow path yielded nothing (None = there was no runner at all) —
        # composed into the reason when the reviewer fallback can't save the run.
        no_run_reason: str | None = None
        if runner is not None and number:
            try:
                inputs: dict = {"pr": number, "repo": repo_slug}
                prior = self._review_prior.get(fid)
                if prior:
                    inputs["prior_findings"] = prior
                result = await runner(self.review_workflow, inputs)
                failed = list((result or {}).get("failed") or [])
                if failed:
                    # A partial panel is NOT a review (ADR 0078 D3): a starved/errored
                    # finder means unreviewed angles, and a verdict synthesized from
                    # the survivors reads as clean coverage it never had.
                    log.warning(
                        "[project_board] %s review workflow %r had failed step(s) %s — fail closed, not a review",
                        fid,
                        self.review_workflow,
                        failed,
                    )
                    return None, (
                        f"workflow {self.review_workflow!r} ran but had failed step(s): "
                        f"{', '.join(str(s) for s in failed)}"
                    )
                output = str((result or {}).get("output") or "")
                if output:
                    return output, None
                return None, f"workflow {self.review_workflow!r} ran but produced no output"
            except Exception as exc:  # noqa: BLE001 — a dead workflow ≠ a dead loop
                log.warning("[project_board] %s review workflow %r failed: %s", fid, self.review_workflow, exc)
                no_run_reason = f"workflow {self.review_workflow!r} call failed: {exc}"
                # fall through to the reviewer alternative
        elif runner is not None:
            no_run_reason = f"workflow runner present but no PR number parses from {pr_url!r}"
        reviewer = self._resolve_delegate(self.reviewer_name, "a2a")
        if reviewer is None:
            if no_run_reason is not None:
                return None, f"{no_run_reason}; no reviewer fallback configured"
            return None, "no workflow runner available and no reviewer configured"
        from plugins.delegates.adapters import ADAPTERS

        try:
            msg = (
                f"Adversarially review this pull request: {pr_url}\n\n"
                "Read the diff, verify each suspicion against the code, and report ONLY "
                "evidence-backed findings as a fenced ```json array of objects "
                '{"file", "line", "severity" (blocker|major|minor|nit), "category", '
                '"claim", "evidence", "verdict" (confirmed|refuted|uncertain)}. '
                "No findings → an empty array []."
            )
            output = await ADAPTERS["a2a"].dispatch(reviewer, msg)
            if output is None:
                return None, f"reviewer {self.reviewer_name!r} returned no output"
            return output, None
        except Exception as exc:  # noqa: BLE001
            log.warning("[project_board] %s reviewer fallback failed: %s", fid, exc)
            return None, f"reviewer {self.reviewer_name!r} call failed: {exc}"

    @staticmethod
    def _parse_findings(output: str):
        """The findings convention parser (ADR 0077), imported from the HOST lazily —
        the contract both this gate and the craft skill consume. None = the host
        doesn't ship it (gate goes inert rather than guessing at prose)."""
        try:
            from graph.review.findings import parse_findings
        except ImportError:
            return None
        return parse_findings(output or "")

    @staticmethod
    def _render_findings(findings) -> str:
        try:
            from graph.review.findings import render_findings_markdown

            return render_findings_markdown(findings, title="Review findings (blocking)")
        except ImportError:  # unreachable when _parse_findings succeeded; belt+braces
            return "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.claim}" for f in findings)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _use_coder_solve(self, feature: dict) -> bool:
        """The P2 board-seam dispatch decision (ADR 0064) — see coder_seam.py.
        `coder_solve` is this repo's own opt-out valve (default on); the actual
        grounding gate (coder plugin importable + acceptance criteria + a runnable
        test command) lives in ``coder_seam.should_use_solve``.

        Max-Mode takes precedence when both are configured (`max_mode_n>1`): a
        board already relying on Max-Mode's judge-fallback (always ships a
        best-effort PR) must not have that silently swapped for solve()'s harder
        "block if nothing passes" behavior just because the separate `coder`
        plugin became importable. Enabling coder.solve on such a board is a
        deliberate config change (set `max_mode_n<=1`), never a side effect of
        installing `coder` for something else."""
        if not self.coder_solve:
            return False
        if self.max_mode_n > 1:
            return False
        return coder_seam.should_use_solve(feature, test_cmd=self._coder_solve_test_cmd_for(feature))

    def _resolve_delegate(self, name: str, expect_type: str):
        """Look up a live delegate by name from the delegates registry. Returns the
        Delegate or None (not configured / wrong type / plugin disabled). Thin
        wrapper — the real lookup is shared with api.py's test-rung route via
        ``coder_seam.resolve_delegate``."""
        return coder_seam.resolve_delegate(name, expect_type)

    def _resolve_task_delegate(self, assignee: str):
        """Resolve a task assignee to its dispatchable SISTER-AGENT delegate — ACP
        first, then A2A (#304). Review dispatch already reaches an ``a2a`` agent, but a
        task assignee was resolved as ``acp`` only, so a named A2A sister agent fell
        through to the human/unassigned park. Returns the Delegate (of either type) or
        None (empty/human assignee, or a name that resolves to NEITHER type)."""
        if not assignee:
            return None
        return self._resolve_delegate(assignee, "acp") or self._resolve_delegate(assignee, "a2a")

    async def _run_fixups(self, wt: str, feature: dict | None = None) -> None:
        """Run the repo's auto-fix command (``format_cmd``, e.g.
        ``ruff check --fix . && ruff format .``) in the worktree before opening the PR.
        The coder is edit-only — it can't run the linter/formatter, so trivial lint/format
        nits would otherwise fail CI and burn a bounce/escalation. Best-effort: no command
        configured, or any error/timeout, just proceeds (CI is still the real lint gate).
        Resolves ``format_cmd`` from the feature's project when given (#90)."""
        cmd = self._format_cmd_for(feature) if feature is not None else self.format_cmd
        if not cmd:
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt,
                env=self._child_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=180)
        except Exception as exc:  # noqa: BLE001 — best-effort; CI still gates lint
            log.info("[project_board] fixups command failed (proceeding — CI still gates): %s", exc)

    async def _run_local_gate(self, wt: str, feature: dict | None = None) -> str | None:
        """Run the pre-PR local gate (``local_gate_cmd``) in the worktree.

        Returns ``None`` when the gate passes (exit 0), when no gate is configured,
        or when the gate itself couldn't run (timeout / unlaunchable command) — a
        broken or flaky gate must never block otherwise-good work, so those degrade
        to "pass" (CI is still the real gate). Returns the captured output (tail,
        truncated to ``local_gate_output_chars``) on a CLEAN non-zero exit, so the
        caller can hand it to the coder to fix. Resolves the gate command from the
        feature's project when given (#90)."""
        cmd = self._local_gate_cmd_for(feature) if feature is not None else self.local_gate_cmd
        if not cmd:
            return None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.local_gate_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning("[project_board] pre-PR gate timed out (%ss) — treating as pass", self.local_gate_timeout)
                return None
            if proc.returncode == 0:
                return None
            if proc.returncode is not None and proc.returncode < 0:
                # Killed by a signal — the member shutting down (SIGTERM reaches the
                # child), an operator `kill`, the OOM killer — NOT the repo failing its
                # own gate. Same posture as the timeout above: the gate couldn't run to
                # a verdict, so it must not produce one. Seen 2026-08-20: a restart
                # landed mid merged-state gate, pytest died at 13% with rc=-15, and the
                # feature was flag_blocked "gate FAILED on the merged state" against a
                # PR whose CI was fully green.
                log.warning(
                    "[project_board] pre-PR gate killed by signal %d (shutdown / external kill) — "
                    "no verdict, treating as pass (CI still gates)",
                    -proc.returncode,
                )
                return None
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            return text or f"gate command exited {proc.returncode} with no output"
        except Exception as exc:  # noqa: BLE001 — a gate that can't run must not block
            log.info("[project_board] pre-PR gate failed to run (treating as pass — CI still gates): %s", exc)
            return None

    # ── gate preflight (fail-closed, PER PROJECT: never start work a broken gate can't accept) ──
    def _ready_projects(self, store) -> list[str]:
        """The distinct projects with ready work right now — the set _maybe_preflight
        smokes each tick (order-preserving, deduped)."""
        names: list[str] = []
        seen: set[str] = set()
        for f in store.list_features(state="ready"):
            name = self._project_name(f)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    async def _maybe_preflight(self) -> None:
        """Re-run each project's gate preflight while it hasn't passed, throttled per
        project (#90). Once a project passes it stays passed for the run (a healthy env
        doesn't spontaneously lose its toolchain; a per-PR gate failure is handled in the
        drive, not here). Runs for every project with ready work AND every project still
        marked failed — the latter so a project whose ready work got HELD (and so dropped
        out of `ready`) still re-checks and can recover."""
        if not self.preflight:
            return
        store = self._store()
        # Store-only scan — off the event loop (#258).
        names = list(await asyncio.to_thread(self._ready_projects, store))
        seen = set(names)
        # A failed project may have no ready work left (its cards got held) — keep
        # re-checking it so it can recover and release those holds.
        for name, st in self._preflight_state.items():
            if isinstance(st, str) and name not in seen:
                seen.add(name)
                names.append(name)
        now = time.monotonic()
        ran = False
        for name in names:
            cmd = self._local_gate_cmd_for({"project": name})
            if not cmd:
                self._preflight_state[name] = True  # nothing to smoke → runnable
                continue
            state = self._preflight_state.get(name)
            if state is True:
                continue  # already passed this run
            # First check runs immediately (state is None); re-checks of a KNOWN-failed
            # preflight are throttled so a slow gate isn't hammered every tick.
            if state is not None and (now - self._last_preflight.get(name, 0.0)) < max(self.interval, 60.0):
                continue
            self._last_preflight[name] = now
            ran = True
            await self._preflight(
                name,
                cmd,
                self._repo_for({"project": name}),
                self._base_branch_for({"project": name}),
            )
        if ran:
            # Surface the verdicts on /status (#255) — a board that stops picking work
            # up must be able to say why without the operator reading the log.
            health.publish_preflight(self._preflight_state, self._preflight_dirty)

    async def _preflight(self, name: str, cmd: str, repo: str, base: str = "") -> None:
        """Smoke-run project ``name``'s gate on its base checkout. Sets
        ``self._preflight_state[name]``: ``True`` when the gate exits 0 (runnable), a
        reason string on a CLEAN non-zero exit or a launch failure (broken environment →
        hold THIS project's work). A TIMEOUT is indeterminate → allow (a slow gate must
        not wedge the board). Releases this project's holds on recovery.

        A DIRTY checkout yields NO VERDICT AT ALL (#255, corrected in #300). Coders only
        touch worktrees, so the main checkout normally still sits at base — but the
        OPERATOR edits it by hand, and then whatever the gate just did was about their
        uncommitted work, not about the base every worktree branches from. That cuts BOTH
        ways, which the first cut of this got wrong by only distrusting a red result:

        * a red gate on a dirty tree must not CONVICT the base (freezing real work over a
          local edit, whose only symptom on the board is an empty ``selected: []``), and
        * a green gate on a dirty tree must not ACQUIT it either — an operator's local fix
          can make a genuinely broken base pass, and releasing the holds on that evidence
          dispatches coders onto a base no gate has actually cleared.

        So on dirt this records the dirt for ``/status``, logs, and returns WITHOUT
        touching ``_preflight_state`` or this project's holds: a project already held for
        a clean red stays held (only a clean green may release it), and one that was never
        held is not newly held (state stays ``None``, so the claim scan keeps dispatching
        — the posture a timeout already had). Fail-closed and fail-open both keep their
        meaning, and each is decided only on evidence that supports it."""
        log.info("[project_board] preflight[%s]: smoking the gate on clean base — %s", name, cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=repo,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.preflight_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning(
                    "[project_board] preflight[%s] timed out (%ss) — indeterminate, allowing dispatch",
                    name,
                    self.preflight_timeout,
                )
                self._preflight_state[name] = True
                return
            # The dirt probe runs BEFORE the exit code is read, because it decides
            # whether the exit code means anything at all — for a pass exactly as much
            # as for a failure (see the docstring).
            dirt = await worktree.base_checkout_dirt(repo, base)
            if dirt:
                self._preflight_dirty[name] = dirt
                log.warning(
                    "[project_board] preflight[%s]: the checkout at %s is NOT at base (%s), so the gate "
                    "just ran against those local edits — no verdict either way. State and holds are "
                    "left exactly as they were (a project held for a clean red stays held; an unheld one "
                    "keeps dispatching). Commit or stash to get a real verdict. Gate exited %s.",
                    name,
                    repo,
                    dirt,
                    proc.returncode,
                )
                return
            self._preflight_dirty.pop(name, None)
            if proc.returncode == 0:
                if isinstance(self._preflight_state.get(name), str):
                    log.info("[project_board] preflight[%s] RECOVERED — gate runnable again, releasing held work", name)
                self._preflight_failed_at.pop(name, None)
                self._preflight_state[name] = True
                await asyncio.to_thread(self._release_preflight_holds, name)
                return
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            text = text or f"gate exited {proc.returncode} with no output"
            if self._record_preflight_failure(name, text):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — the gate does not pass on clean base; "
                    "HOLDING that project's work until the environment is fixed:\n%s",
                    name,
                    text,
                )
        except asyncio.CancelledError:
            if self._shutting_down:
                log.info("[project_board] preflight[%s] cancelled by shutdown — no verdict", name)
                return
            raise
        except Exception as exc:  # noqa: BLE001 — a gate that CANNOT LAUNCH is the broken-env case we must catch
            if self._record_preflight_failure(name, f"gate command could not run: {exc}"):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — %s; HOLDING that project's work until fixed.",
                    name,
                    self._preflight_state[name],
                )

    def _record_preflight_failure(self, name: str, reason: str) -> bool:
        """Set project ``name``'s failure ``reason`` and say whether to log it in full
        (#263). The gate tail is multi-KB diagnostic signal exactly once per DISTINCT
        failure — but a held project re-checks every ~60s, and an unchanged failure
        re-logged at ERROR each time buries the log without adding anything. First or
        DIFFERENT reason → True (caller emits the full ERROR); identical repeat →
        emits a one-line "still held" WARNING here and returns False."""
        prev = self._preflight_state.get(name)
        now = time.monotonic()
        self._preflight_state[name] = reason
        if prev != reason:
            self._preflight_failed_at[name] = now
            return True
        held = int(now - self._preflight_failed_at.get(name, now))
        log.warning(
            "[project_board] preflight[%s] still held (%ds) — same failure as last check, tail already logged",
            name,
            held,
        )
        return False

    def _hold_ready_for_preflight(self) -> None:
        """Flag every ready feature whose PROJECT's preflight failed blocked with that
        project's reason (#90), so the hold shows on the board instead of a silent stall.
        Features in projects whose gate CAN run are left alone — a broken gate in project
        A never holds project B."""
        store = self._store()
        for f in store.list_features(state="ready"):
            fid = f["id"]
            name = self._project_name(f)
            reason = self._preflight_state.get(name)
            if not isinstance(reason, str):
                continue  # this feature's project can run its gate (or hasn't been checked)
            held = self._preflight_held.setdefault(name, set())
            if fid in held or f.get("blocked"):
                continue
            tail = reason.splitlines()[-1][:200]
            short = f"{PREFLIGHT_BLOCK_PREFIX} — the coder environment can't run the gate: {tail}"
            try:
                store.flag_blocked(fid, short)
                held.add(fid)
                log.info("[project_board] preflight hold: flagged %s blocked (project %s gate not runnable)", fid, name)
            except Exception:  # noqa: BLE001 — a hold that can't be recorded must not kill the tick
                log.warning("[project_board] preflight hold: flag_blocked failed for %s", fid, exc_info=True)

    def _release_preflight_holds(self, name: str) -> None:
        """Clear the blocks this loop placed for project ``name``'s failed preflight (only
        those — never clobber a feature blocked for another reason)."""
        held = self._preflight_held.get(name)
        if not held:
            return  # nothing to release — don't build the store (it may need a CLI/DB
            # that isn't present) just to iterate an empty set. A clean preflight (the
            # common path) must never touch the store: the resulting error would be
            # caught by _preflight's outer except and masquerade as a gate failure.
        store = self._store()
        for fid in list(held):
            try:
                store.clear_blocked(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] preflight release: clear_blocked failed for %s", fid, exc_info=True)
        self._preflight_held.pop(name, None)

    async def _verify_goal(self, feature: dict, wt: str, base: str, coder_reply: str = "") -> str | None:
        """Pre-PR gate — DETERMINISTIC: no LLM, no diff dump. The one thing it adds over
        CI is requiring a test to EXIST for a code change (CI runs tests but can't require
        their presence). So it just checks the changed-file LIST for a test file — cheap,
        instant, and immune to the truncation that made the old "LLM eyeballs the diff"
        version false-reject tests it couldn't see (smart/reasoning/opus each "failed" on
        tests they'd actually written — tests sort LAST by path and fell off the cap, ~40
        min of cycles wasted). CORRECTNESS is CI's job — it runs the tests the coder wrote;
        a wrong diff fails CI and the CI-feedback edge bounces it back.

        ESCAPE HATCH: not every code change needs a test (a pure refactor, config/docs-as-
        code, a constant tweak). The coder — which saw the actual change — can declare
        ``NO_TEST_NEEDED: <reason>`` at line start inside its final ``## Summary`` section
        (#264 — structural via ``_no_test_marker``, not a substring scan: a mid-narration
        mention is not a declaration); we log the reason and pass, rather than burning
        retries on a test that doesn't apply. Returns a gap string (→ re-dispatch/
        escalate) or None. Fails OPEN on any error (never blocks a good PR on infra)."""
        ac = (feature.get("acceptance_criteria") or "").strip()
        if not ac:
            return None
        try:
            await worktree.stage_all(wt)
            _rc, names, _err = await worktree._git(wt, "diff", "--cached", "--name-only", f"origin/{base}")
        except Exception:  # noqa: BLE001 — best-effort
            return None
        changed = [n for n in (names or "").split() if n]
        if not changed:
            return None  # an empty diff is open_pr's NoChangesError job, not ours
        code = [n for n in changed if _is_code_path(n) and not _is_test_path(n)]
        if code and not any(_is_test_path(n) for n in changed):
            reason = _no_test_marker(coder_reply or "")
            if reason is not None:
                log.info(
                    "[project_board] %s no-test accepted (coder declared): %s",
                    feature.get("id"),
                    reason[:200],
                )
                return None
            head = ", ".join(code[:6]) + ("…" if len(code) > 6 else "")
            return (
                "no test was added/updated for the code change — add a test covering the new "
                f"behavior, or declare `NO_TEST_NEEDED: <reason>` on its own line in your final "
                f"`## Summary` section if a test genuinely doesn't apply (refactor/config/docs) "
                f"(code: {head})"
            )
        return None

    async def _judge_candidates(self, feature: dict, base: str, worktrees: list[str]) -> int | None:
        """Max-Mode best-of-N judge: given N candidate worktrees for the same feature,
        pick the index whose diff best satisfies the ``acceptance_criteria``. Returns
        the winning index, or ``None`` when there's no non-empty candidate.

        Reuses the goal-verify diff+``complete()`` seam. Best-effort: candidates with no
        diff are skipped; if the judge errors or is unparseable, falls back to the first
        non-empty candidate (never returns a worse-than-arbitrary answer). The N-parallel
        dispatch that produces ``worktrees`` is tracked in #21; this is the judge it calls."""
        ac = (feature.get("acceptance_criteria") or "").strip()
        diffs: list[str] = []
        for wt in worktrees:
            try:
                await worktree.stage_all(wt)
                _rc, d, _err = await worktree._git(wt, "diff", "--cached", f"origin/{base}")
            except Exception:  # noqa: BLE001 — judging is best-effort
                d = ""
            diffs.append((d or "").strip())

        nonempty = [i for i, d in enumerate(diffs) if d]
        if not nonempty:
            return None
        if len(nonempty) == 1:
            return nonempty[0]

        blocks = "\n\n".join(f"### Candidate {i}\n```diff\n{diffs[i][:4000]}\n```" for i in nonempty)
        prompt = (
            f"{len(nonempty)} coding agents each attempted the same task.\n\n"
            f"Acceptance criteria:\n{ac or '(none given)'}\n\n"
            f"{blocks}\n\n"
            "Which candidate BEST satisfies every acceptance criterion (most correct, "
            "complete, and clean)? Reply with ONLY the candidate number."
        )
        try:
            from graph.sdk import complete

            verdict = (await complete(prompt, system=_MAX_MODE_JUDGE_SYS) or "").strip()
        except Exception as exc:  # noqa: BLE001 — never fail the build on the judge
            log.warning(
                "[project_board] %s max-mode judge errored (using first candidate): %s",
                feature.get("id"),
                exc,
            )
            return nonempty[0]

        for tok in re.findall(r"\d+", verdict):
            idx = int(tok)
            if idx in nonempty:
                return idx
        return nonempty[0]  # judge unclear → first non-empty candidate

    async def _candidate_diff_indices(self, base: str, worktrees: list[str]) -> list[int]:
        """Indices of candidates that produced a non-empty staged diff vs ``origin/<base>``.
        Cheap name-only check; best-effort (a candidate we can't stage/diff is skipped)."""
        out: list[int] = []
        for i, wt in enumerate(worktrees):
            try:
                await worktree.stage_all(wt)
                _rc, names, _err = await worktree._git(wt, "diff", "--cached", "--name-only", f"origin/{base}")
            except Exception:  # noqa: BLE001 — best-effort, like _judge_candidates
                names = ""
            if (names or "").strip():
                out.append(i)
        return out

    async def _select_candidate(self, feature: dict, base: str, worktrees: list[str]) -> int | None:
        """Pick the winning Max-Mode candidate — EXECUTION-GROUNDED (ADR 0064).

        When a pre-PR gate (``local_gate_cmd``) is configured, PREFER candidates whose
        gate actually PASSES: run the candidates, don't just judge their diffs. An LLM
        judge of code rewards plausible-looking diffs and can't catch subtle wrongness —
        only running the tests discriminates. The judge (``_judge_candidates``) then only
        breaks ties among the PASSING set (quality among the correct), or decides when no
        gate is configured / none pass. With no gate this is exactly the old behavior.

        Returns the winning index, or ``None`` when no candidate produced a diff."""
        # No oracle → judge exactly as before (it does its own emptiness handling and
        # returns None when every candidate is empty). Avoids a redundant diff pass.
        if not self._local_gate_cmd_for(feature):
            return await self._judge_candidates(feature, base, worktrees)

        nonempty = await self._candidate_diff_indices(base, worktrees)
        if not nonempty:
            return None
        if len(nonempty) == 1:
            return nonempty[0]

        fid = feature.get("id")
        gates = await asyncio.gather(*(self._run_local_gate(worktrees[i], feature) for i in nonempty))
        passing = [i for i, gap in zip(nonempty, gates) if gap is None]
        if not passing:
            log.info(
                "[project_board] %s execution-select: 0/%d candidates pass the gate — judging diffs", fid, len(nonempty)
            )
            return await self._judge_candidates(feature, base, worktrees)
        log.info(
            "[project_board] %s execution-select: %d/%d candidates pass the gate", fid, len(passing), len(nonempty)
        )
        if len(passing) == 1:
            return passing[0]
        # Tie-break among the PASSING (correct) candidates by quality, via the judge.
        j = await self._judge_candidates(feature, base, [worktrees[i] for i in passing])
        return passing[j] if j is not None else passing[0]

    async def _dispatch_max_mode(
        self, feature: dict, coder, prompt: str, repo: str, base: str, fid: str, tier: str = ""
    ) -> tuple[str, str, str]:
        """Max-Mode (#21): build the feature N ways in parallel and ship the best diff.

        Creates ``max_mode_n`` throwaway candidate worktrees off the same base (suffixed
        ``feat-<id>.c<k>`` so none collides with the canonical name), dispatches the coder
        into ALL of them concurrently — each keeps its own ``coder_timeout`` watchdog +
        ``finally`` subprocess teardown (``dispatch_coder``), and ``return_exceptions``
        means one candidate timing out / erroring leaves an empty tree the selector skips
        rather than sinking the batch. ``_select_candidate`` picks the winning index —
        EXECUTION-GROUNDED when a pre-PR gate is configured (prefer candidates whose tests
        pass; ADR 0064), else the best-of-N LLM judge; the winner is PROMOTED into the canonical
        ``feat-<id>`` worktree / ``feat/<id>`` branch (so the rest of the lifecycle is
        unchanged) and the losers are reaped. All-empty → ``NoChangesError``, which
        ``_drive`` escalates/blocks exactly like a single coder that produced nothing.

        Returns (canonical_wt, canonical_branch, winner_reply). The fan-out is bounded by
        ``max_concurrent`` × ``max_mode_n`` coders; size those to the host."""
        n = self.max_mode_n
        cand_ids = [f"{fid}.c{i}" for i in range(n)]
        # Create the N worktrees sequentially (git serializes worktree-list writes); the
        # slow part — the coder dispatch — is what we then fan out in parallel.
        cands: list[tuple[str, str]] = []
        for cid in cand_ids:
            cands.append(await worktree.create_worktree(repo, base, cid, self.root))
        log.info("[project_board] %s max-mode: dispatching %d parallel candidates", fid, n)
        # Tap each candidate into the live monitor as its own gen (#84) — the drawer
        # shows all N building in parallel; a tap that can't wire degrades per-candidate.
        results = await asyncio.gather(
            *(
                coder_seam.dispatch_coder_tapped(
                    coder, wt, prompt, fid=fid, gen=i + 1, tier=tier, timeout=self.coder_timeout or None
                )
                for i, (wt, _b) in enumerate(cands)
            ),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.info("[project_board] %s max-mode candidate %d failed (skipped): %s", fid, i, r)
        idx = await self._select_candidate(feature, base, [wt for wt, _b in cands])
        if idx is None:
            for cid in cand_ids:
                await worktree.reap_feature_worktree(repo, self.root, cid)
            raise worktree.NoChangesError(f"max-mode: all {n} candidates produced no diff")
        log.info("[project_board] %s max-mode: candidate %d/%d wins → promoting", fid, idx, n)
        win_wt, win_branch = cands[idx]
        canon_wt, canon_branch = await worktree.promote_worktree(
            repo, win_wt, win_branch, fid, self.root, title=feature.get("title") or ""
        )
        # Reap the losers (the winner was moved out of its candidate name by promote).
        for i, cid in enumerate(cand_ids):
            if i != idx:
                await worktree.reap_feature_worktree(repo, self.root, cid)
        winner_reply = results[idx] if not isinstance(results[idx], Exception) else ""
        return canon_wt, canon_branch, winner_reply

    async def _fetch_kg_lessons(self, feature: dict) -> str:
        """Query the knowledge graph (via graph.sdk) for lessons relevant to THIS
        feature — the read half of the flywheel. Builds the query from the feature's
        title + files (the area it touches), pulls the top-``kg_lessons_k`` chunks from
        the ``kg_lessons_domain`` bucket, and returns them as a markdown bullet list for
        ``_build_prompt`` to inject. Best-effort: returns "" if disabled, no store, no
        SDK, or any error — a retrieval hiccup must never block a build."""
        if not self.kg_lessons:
            return ""
        query = " ".join(
            p
            for p in (
                feature.get("title", ""),
                " ".join(feature.get("files_to_modify") or []),
                feature.get("difficulty", ""),
            )
            if p
        ).strip()
        if not query:
            return ""
        try:
            from graph.sdk import knowledge_search

            hits = await knowledge_search(query, k=self.kg_lessons_k, domain=self.kg_lessons_domain or None)
        except Exception as exc:  # noqa: BLE001 — retrieval is best-effort; never block a build
            log.info("[project_board] kg-lessons fetch skipped (%s)", exc)
            return ""
        lines = []
        for h in hits or []:
            text = (h.get("preview") or h.get("content") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    @staticmethod
    def _empty_result_signals(fid: str) -> tuple[bool, str]:
        """(had_tool_activity, stop_reason) for the dispatch that just failed with no
        diff, mined from the live-monitor ring buffer (#198). No tool events across
        any gen ⇒ the coder connected but never executed — the ``empty_result``
        class ``_drive`` retries once on the same tier before counting a failure
        (#2991). ``stop_reason`` is whatever
        signal the ACP adapter reported (``progress_stop_reason``), latest gen wins.
        Best-effort: an unreadable snapshot reports activity=True, so a monitor
        hiccup can never misclassify a real capability failure as empty."""
        try:
            gens = coder_seam.progress_snapshot(fid).get("gens") or []
        except Exception:  # noqa: BLE001 — a monitor read must never break the drive
            return True, ""
        if not gens:  # no record of the run at all (evicted) — can't call it empty
            return True, ""
        had_tools = any(g.get("recent_tools") or g.get("current_tool") for g in gens)
        stop = next((str(g["stop_reason"]) for g in reversed(gens) if g.get("stop_reason")), "")
        return had_tools, stop

    def _timeout_escalation_context(self, fid: str) -> str:
        """Feedback for an escalated dispatch whose PRIOR attempt TIMED OUT (#146).

        A ``CoderTimeout`` climbs the tier ladder (``_drive``), but on its own the
        stronger model would get a BYTE-IDENTICAL prompt: zero signal that a prior
        attempt ran out of time, how long it ran, or what it was doing when killed.
        Mine the progress ring buffer (``coder_seam.progress_snapshot``) for the
        timed-out gen's elapsed time, the last tool in flight, and the thought tail,
        and lead the re-dispatch with them. Returned for injection into
        ``_ci_feedback`` so it rides the exact same prompt path a CI/review bounce
        uses — no new plumbing. Best-effort: a missing/empty snapshot still yields a
        usable "prior attempt timed out, produced no diff" note — a monitor read must
        never break escalation."""
        try:
            gens = coder_seam.progress_snapshot(fid).get("gens") or []
        except Exception:  # noqa: BLE001 — a monitor read must never break escalation
            gens = []
        gen = gens[-1] if gens else {}
        elapsed = gen.get("elapsed_s")
        ran_for = f"ran ~{elapsed}s and " if elapsed is not None else ""
        lines = [
            "A PREVIOUS attempt at this feature TIMED OUT and was killed — it "
            f"{ran_for}produced NO diff (nothing was committed). You are a stronger "
            "model taking over: be decisive, avoid open-ended exploration, and make "
            "the edits early rather than reading indefinitely.",
        ]
        cur = gen.get("current_tool") or {}
        if cur.get("name"):
            locs = ", ".join(cur.get("locations") or [])
            where = f" on {locs}" if locs else ""
            lines.append(
                f"- Last tool in flight when it was killed: {cur['name']} ({cur.get('status', 'running')}){where}."
            )
        else:
            recent = gen.get("recent_tools") or []
            if recent:
                last = recent[-1]
                lines.append(f"- Last observed tool: {last.get('name', 'tool')} ({last.get('status', '')}).")
        tail = (gen.get("thought_tail") or "").strip()
        if tail:
            lines.append(f"- Its last reasoning before the timeout (it never converged):\n{tail}")
        return "\n".join(lines)

    def _build_prompt(self, feature: dict, lessons: str = "") -> str:
        """An imperative, fully-specified instruction (ProtoMaker discipline). A
        passive 'implement this feature' + a vague spec makes a coder produce
        nothing; naming the files + a direct 'make the edits now' makes it act.

        ``lessons`` (distilled gotchas from the knowledge graph, fetched async in
        ``_drive``) is injected so a coder gets this area's known failure modes on
        attempt 1 — the read half of the flywheel (retro grounds → coder heeds)."""
        files = feature.get("files_to_modify") or []
        files_block = (
            "\n".join(f"- {f}" for f in files) if files else "(none listed — create the files the task requires)"
        )
        # Repo standing gate files (#108): repo-wide obligations that a card's tight
        # `files_to_modify` would otherwise suppress — a card author can't enumerate
        # per-repo gates, so the loop carries them from config. Emitted as a SEPARATE
        # prompt block (not merged into `files_to_modify`, not a ledger item #113): a
        # standing reminder that these must stay green even if the change doesn't
        # centre on them. Empty by default → no block.
        # #90: gate files + conventions resolve from THIS feature's project, so a coder
        # on a multi-repo board gets the standing obligations of the repo it builds in.
        gate_files = self._gate_files_for(feature)
        gate_files_block = (
            "\n## Repo standing gate files (keep these green — repo-wide, not per-card)\n"
            + "\n".join(f"- {g}" for g in gate_files)
            + "\nThese are standing obligations for EVERY change in this repo (beyond the "
            "files listed above). If your change affects them, update them so the gate "
            "stays green.\n"
            if gate_files
            else ""
        )
        # Repo conventions (#108): the RULES around the repo-wide gates — what CI runs,
        # what format a required fragment takes, which files are GENERATED and must not
        # be hand-edited, and the standing "if a convention named here doesn't exist,
        # STOP and say so" guard. `gate_files` lists the paths; this carries the prose a
        # card author can't restate per-card. Emitted as a SEPARATE block right after
        # the gate files (its natural neighbour — both are repo-wide, not per-card),
        # verbatim from config. Injected unconditionally when set; empty → no block.
        repo_conventions = self._repo_conventions_for(feature)
        repo_conventions_block = (
            "\n## Repo conventions\n" + repo_conventions.strip() + "\n" if repo_conventions.strip() else ""
        )
        design = feature.get("design", "")
        design_block = f"\n## Design / context\n{design}\n" if design.strip() else ""
        # CI-feedback re-dispatch (closed-loop verify): a prior attempt's PR failed
        # CI; lead with the failure so the coder FIXES it this pass (it can't run the
        # checks itself — edit-only). Also widen scope: the fix may touch tests/files
        # the original `files_to_modify` didn't list (the #1053 lesson).
        fid = feature.get("id", "")
        # Drain any externally-queued review-bounce feedback (the /review route stashed
        # it via queue_review_feedback) into THIS run's _ci_feedback, so an operator/CI
        # review bounce rides the exact same prompt path as an in-loop CI/review bounce.
        pending = _PENDING_FEEDBACK.pop(fid, None)
        if pending:
            self._ci_feedback[fid] = pending
        ci = self._ci_feedback.get(fid)
        prior = self._ci_prior_diff.get(fid)
        prior_block = (
            f"\n### The diff that failed (your previous attempt — fix it, don't restart from scratch)\n"
            f"```diff\n{prior}\n```\n"
            if prior
            else ""
        )
        ci_block = (
            "\n## ⚠ Your previous attempt was REJECTED — fix it this attempt\n"
            f"{ci}\n"
            f"{prior_block}"
            "Address the problem above. This may require editing files beyond the list "
            "below — e.g. ADD the missing tests, or update an e2e/unit test that assumed "
            "the old behavior.\n"
            if ci
            else ""
        )
        lessons_block = (
            f"\n## Known gotchas for this area (distilled from past retros — heed them)\n{lessons.strip()}\n"
            if lessons.strip()
            else ""
        )
        # Requirement ledger (#113): the tracked items decomposed from the acceptance
        # criteria at mark_ready, WITH their current statuses — so a re-dispatch
        # re-injects the still-open items and the coder must dispose of every one
        # (done, or declined with a reason). Silence is not disposition: an
        # unreported item stays open and the completion gate refuses the PR.
        reqs = feature.get("requirements") or []
        req_lines = "\n".join(
            f"- `{r.get('id')}` [{r.get('status', 'open')}] {r.get('text', '')}"
            + (f" (reason: {r['decline_reason']})" if r.get("decline_reason") else "")
            for r in reqs
        )
        req_block = (
            "\n## Requirements ledger (dispose of EVERY item)\n"
            f"{req_lines}\n\n"
            "Each item above is tracked on the board. Address every `open` item this "
            "round, and report a per-item disposition: include a `## Requirements` "
            "section in your final message (before the `## Summary`) with ONE line "
            "per item — `- <id>: done` or `- <id>: declined — <concrete reason>`. "
            "Declining with a real reason (e.g. not reachable/not applicable) is a "
            "valid closed state; SILENCE IS NOT — an unreported item stays open, and "
            "the PR cannot open while any item is open.\n"
            if reqs
            else ""
        )
        return (
            f"You are implementing ONE feature in this repository. Your working "
            f"directory is an isolated git worktree — **make all the edits here, now**. "
            f"Do not ask questions or just describe a plan; if something is ambiguous, "
            f"make the most reasonable choice and write the code.\n\n"
            f"# {feature['title']}\n\n"
            f"{ci_block}"
            f"{lessons_block}"
            f"## Task\n{feature.get('spec', '')}\n\n"
            f"## Files to create / modify\n{files_block}\n"
            f"{gate_files_block}"
            f"{repo_conventions_block}"
            f"{design_block}\n"
            f"## Acceptance criteria (definition of done)\n{feature.get('acceptance_criteria', '')}\n"
            f"{req_block}\n"
            f"## Rules\n"
            f"- Make the edits directly in the working tree NOW — actually write the files.\n"
            f"- Touch only the files this task needs; mirror the surrounding code's style.\n"
            f"- **Write automated tests** covering the new/changed behavior (a new or "
            f"updated test file, matching the repo's existing test conventions). This is "
            f"part of the definition of done, not optional — a code change with no test "
            f"is rejected before the PR opens. If a test GENUINELY doesn't apply (a pure "
            f"refactor, config/docs-as-code, or a change with no behavior to exercise), "
            f"write a single line `NO_TEST_NEEDED: <reason>` inside the final `## Summary` "
            f"section of your final message instead — it does not count anywhere else.\n"
            f"- You cannot run shell commands (edit-only); the tests you write run in CI "
            f"on the PR, so they must be correct and self-contained.\n"
            f"- Push the branch if you can; do NOT open a PR (draft or otherwise) — the loop "
            f"opens it with the title/body it composes and owns the PR lifecycle.\n"
            f"- **Your FINAL message becomes the PR description, verbatim.** End with a "
            f"short, clean summary for a reviewer — what changed and why, 2-6 sentences "
            f"or a few bullet points. Do NOT narrate your process: no step-by-step "
            f'exploration, no "I first looked at..."/"Let me...", no restating these '
            f"instructions or the acceptance criteria back. If you used scratch "
            f"reasoning to get here, leave it out of this message entirely.\n"
            f"- You are done when the listed files exist, tests cover the change, and "
            f"every acceptance criterion is satisfied."
        )

"""The external QA panel's verdict on a board PR, read and judged (#473).

A fleet repo can run its own review bot on every PR, separate from the board's in-process
review gate. On protoAgent that is the ``protoreview`` GitHub App ("Vera"). Its verdict
arrives three ways at once, all pinned to the PR head it reviewed:

* a PR REVIEW by the bot (``protoreview[bot]``), CHANGES_REQUESTED on a FAIL, whose body
  carries a hidden head marker —
  ``<!-- protoagent-qa-review head=<sha> verdict=FAIL promoted=false diff=… -->`` — and
  the findings as a fenced ``json`` array (file, line, severity, claim, evidence, verdict);
* a ``QA panel`` CHECK RUN from the App, concluding ``failure``;
* a ``Review at head`` commit STATUS, ``failure`` — NOT read by default: it is also red on
  every head the panel has not reviewed yet, so it cannot tell "rejected" from "not yet".

The board used to read none of this. On 2026-09-27 its own gate said clean at the head the
panel failed (bd-524n, protoAgent#3698), so the card sat ``in_review`` for seven hours while
the merged-state gate re-ran on every base move until its budget was spent.

This module is the pure half: parse the config, decide from one ``gh pr view`` payload
(``worktree.pr_review_state``) whether the panel FAILED at the current head, and turn the
findings into the text a fix round leads with. The reconcile (``loop/reconcile.py``) owns
what happens next. Nothing here shells out.

Not to be confused with the board's OWN verdict: the in-process gate publishes a
``board/review-gate`` commit STATUS (#354, #512) — its own context, kept apart from the
panel's ``QA panel``. #354 first wrote that status under the ``QA panel`` name, which collided
with the panel's App check of the same name (careercoach#17): the board read its own earlier
FAIL back as the panel's verdict. #512 moved the board's status to ``board/review-gate``, so
the panel's ``QA panel`` check is matched only under the panel's name and the board can never
read its own verdict back as the panel's. The board's status is a record of ITS verdict, never
evidence of the panel's.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEFAULT_REVIEWERS: tuple[str, ...] = ("protoreview[bot]",)
DEFAULT_MARKER = "protoagent-qa-review"
DEFAULT_CHECK_RUNS: tuple[str, ...] = ("QA panel",)
# No status by default. protoAgent's `Review at head` status (scripts/review_at_head.py) is
# `failure` whenever the head has no verdict YET ("no QA panel verdict for <sha> — this head
# is unreviewed") and flips green minutes later, so reading it as a FAIL held every fresh
# head (#477 review). A status is only worth listing if its red means "the panel rejected it".
DEFAULT_STATUSES: tuple[str, ...] = ()

# A finding blocks the merge when it is serious AND the panel stood behind it — the same
# bar the in-process gate applies (blocker/major, not refuted), made explicit for the
# panel's own verification vocabulary.
BLOCKING_SEVERITIES = frozenset({"blocker", "major"})
CONFIRMED_VERDICTS = frozenset({"confirmed", "verified"})

# Check-run conclusions / commit-status states that mean the review FAILED. A check still
# running, or one that was skipped, says nothing.
_FAILED_CHECK = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED"})
_FAILED_STATUS = frozenset({"FAILURE", "ERROR"})
# The marker verdicts that reject a head — protoAgent's `review_at_head.BLOCKING_VERDICTS`.
BLOCKING_VERDICTS = frozenset({"FAIL", "BLOCK", "REJECT"})
# The panel's check-run conclusion for an incomplete pass at an OPEN PR's head (pr-reviewer
# `checks.py`, HOLD_INCOMPLETE → "Incomplete pass — not blocking"). GitHub reads it as passing.
_INCOMPLETE_CHECK = "NEUTRAL"
# The panel's own name for that hold, quoted in what the board says about it.
HOLD_INCOMPLETE_COVERAGE = "hold:incomplete-coverage"

# How much of a review body a bounce carries when no finding parses out of it.
_BODY_EXCERPT_CHARS = 4000
# How much evidence per finding the bounce quotes.
_EVIDENCE_CHARS = 1200

_JSON_FENCE_RE = re.compile(r"```json[^\n]*\n(.*?)```", re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ATTR_RE = re.compile(r"([A-Za-z_][\w-]*)=([^\s>]+)")


@dataclass(frozen=True)
class Config:
    """Who the external reviewer is and how its verdict is spelled. ``reviewers`` are
    compared without the ``[bot]`` suffix, since GraphQL (``gh pr view``) names the App
    ``protoreview`` where REST names it ``protoreview[bot]``."""

    reviewers: tuple[str, ...] = DEFAULT_REVIEWERS
    marker: str = DEFAULT_MARKER
    check_runs: tuple[str, ...] = DEFAULT_CHECK_RUNS
    statuses: tuple[str, ...] = DEFAULT_STATUSES

    def is_reviewer(self, login: str) -> bool:
        return _login(login) in {_login(r) for r in self.reviewers}


def _login(name: str) -> str:
    return str(name or "").strip().lower().removesuffix("[bot]")


def _names(value, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(s for s in (str(v).strip() for v in value) if s)


def parse_config(raw) -> Config | None:
    """``external_review`` → a :class:`Config`, or ``None`` when the check is off.

    Unset or ``true`` is the default (the protoreview panel). ``false`` turns it off. A
    mapping overrides any of ``reviewers`` / ``marker`` / ``check_runs`` / ``statuses``
    and may carry ``enabled: false``. An empty ``check_runs`` or ``statuses`` list turns
    that signal off; an empty ``reviewers`` list turns the whole check off (nobody's
    verdict can be read)."""
    if raw is False or (isinstance(raw, str) and raw.strip().lower() in ("false", "off", "no", "0")):
        return None
    if not isinstance(raw, dict):
        return Config()
    enabled = raw.get("enabled", True)
    if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in ("false", "off", "no", "0")):
        return None
    reviewers = _names(raw.get("reviewers"), DEFAULT_REVIEWERS)
    if not reviewers:
        return None
    marker = str(raw.get("marker") or DEFAULT_MARKER).strip() or DEFAULT_MARKER
    return Config(
        reviewers=reviewers,
        marker=marker,
        check_runs=_names(raw.get("check_runs"), DEFAULT_CHECK_RUNS),
        statuses=_names(raw.get("statuses"), DEFAULT_STATUSES),
    )


def parse_marker(body: str, marker: str = DEFAULT_MARKER) -> dict[str, str] | None:
    """The attributes of the first ``<!-- <marker> k=v … -->`` comment in ``body``
    (``{"head": …, "verdict": …}``), or ``None`` when there is none."""
    m = re.search(r"<!--\s*" + re.escape(marker) + r"\b(.*?)-->", body or "", re.S)
    if not m:
        return None
    return {k.lower(): v for k, v in _ATTR_RE.findall(m.group(1))}


def head_matches(marked: str, head: str) -> bool:
    """Whether a marker's ``head=`` names ``head``. A marker may carry a full or an
    abbreviated sha; fewer than 7 characters is not an identity."""
    marked, head = str(marked or "").strip().lower(), str(head or "").strip().lower()
    return len(marked) >= 7 and bool(head) and head.startswith(marked)


@dataclass
class Verdict:
    """What the external panel says about the PR's CURRENT head."""

    head: str
    # The panel failed this head, by any signal.
    failed: bool = False
    # The verdict of the latest marked review AT THIS HEAD (FAIL, PASS, WARN, …); "" when none.
    review_verdict: str = ""
    # When that review was submitted (ISO-8601, as GitHub gives it); "" when unknown.
    review_at: str = ""
    # That review's author and body (the findings source); "" when there is none.
    reviewer: str = ""
    body: str = ""
    # Human-readable evidence for each failing signal, e.g. "check run `QA panel` FAILURE".
    signals: list[str] = field(default_factory=list)
    # The panel's latest word at this head is an INCOMPLETE pass: a finder did not run, so a
    # clear verdict covers less than the whole diff. Read from the panel's ``QA panel`` check
    # concluding ``neutral`` ("Incomplete pass — not blocking", the panel's
    # ``hold:incomplete-coverage``) or the marked review carrying ``complete=false``. Not a
    # FAIL and never blocking by itself; a project's ``require_complete_review`` holds the
    # merge on it.
    incomplete: bool = False
    incomplete_signals: list[str] = field(default_factory=list)

    @property
    def has_findings_review(self) -> bool:
        """A FAIL review at this head exists — something a fix round can act on."""
        return self.review_verdict in BLOCKING_VERDICTS and bool(self.body)


def _author(r: dict) -> str:
    return str(((r.get("author") or {}).get("login")) or (r.get("user") or {}).get("login") or "")


def evaluate(view: dict, cfg: Config) -> Verdict | None:
    """Judge one ``gh pr view --json state,headRefOid,reviews,statusCheckRollup`` payload.

    Returns ``None`` when the payload names no head (nothing can be pinned). Otherwise a
    :class:`Verdict` whose ``failed`` is true when, AT THE PR'S CURRENT HEAD:

    * the latest non-dismissed review by a configured reviewer whose marker names this head
      carries a blocking verdict (``FAIL`` / ``BLOCK`` / ``REJECT``); or
    * a configured check run concluded failed (or a configured status is failure/error),
      unless the latest marked review at this head is non-blocking AND was submitted after
      that check completed — the panel re-reviewed and cleared it. A red that landed AFTER
      the clearing review counts; with no timestamp to order them by, the review wins.

    Ignored: a review whose marker names another head (it judged code that is gone), one by
    anyone but a configured reviewer, and a DISMISSED review — an operator's dismissal is
    the override. A check run is matched only as a CHECK RUN published by an App (an empty
    ``workflowName`` that is present: GitHub Actions runs always carry theirs, and the rollup
    names no app, so ``check_runs`` should name a check only the panel's App publishes),
    and a status only as a STATUS, so neither the board's own ``QA panel`` commit status nor
    an Actions job that happens to be called ``QA panel`` can read as the panel's verdict.

    The same read also sets ``incomplete`` (see ``_judge_coverage``): the panel's newest word
    at this head is a pass that did not cover the whole diff. That never sets ``failed``."""
    head = str((view or {}).get("headRefOid") or "").strip()
    if not head:
        return None
    verdict = Verdict(head=head)
    reviews = [r for r in (view.get("reviews") or []) if isinstance(r, dict)]
    # Chronological. `gh` returns them in submission order; sort anyway (stable), so a
    # reordered payload can't promote an older verdict over a newer one.
    reviews.sort(key=lambda r: str(r.get("submittedAt") or ""))
    review_complete = True  # the latest marked review at this head did not say complete=false
    for r in reviews:
        if str(r.get("state") or "").upper() == "DISMISSED" or not cfg.is_reviewer(_author(r)):
            continue
        attrs = parse_marker(str(r.get("body") or ""), cfg.marker)
        if not attrs or not head_matches(attrs.get("head", ""), head):
            continue
        verdict.review_verdict = str(attrs.get("verdict") or "").strip().upper()
        review_complete = str(attrs.get("complete") or "").strip().lower() != "false"
        verdict.review_at = str(r.get("submittedAt") or "")
        verdict.reviewer = _author(r)
        verdict.body = str(r.get("body") or "")
    blocking_review = verdict.review_verdict in BLOCKING_VERDICTS
    # The panel's newest App check run at this head (by completion time; list order breaks a
    # tie), for the incomplete-pass read below.
    latest_check: dict | None = None
    if blocking_review:
        verdict.signals.append(f"review by {verdict.reviewer}: verdict={verdict.review_verdict} at {head[:12]}")
    cleared_at = verdict.review_at if verdict.review_verdict and not blocking_review else ""
    for c in view.get("statusCheckRollup") or []:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("__typename") or "")
        if kind == "CheckRun" and str(c.get("name") or "") in cfg.check_runs:
            if "workflowName" not in c or str(c.get("workflowName") or "").strip():
                # A GitHub Actions job of that name (it names its workflow), or an entry we
                # cannot tell apart from one (an older `gh` omits the key): not the App's check.
                continue
            state, at, label = str(c.get("conclusion") or "").upper(), str(c.get("completedAt") or ""), "check run"
            name = c.get("name")
            if latest_check is None or at >= str(latest_check.get("completedAt") or ""):
                latest_check = c
            if state not in _FAILED_CHECK:
                continue
        elif kind == "StatusContext" and str(c.get("context") or "") in cfg.statuses:
            state, at, label = str(c.get("state") or "").upper(), str(c.get("startedAt") or ""), "status"
            name = c.get("context")
            if state not in _FAILED_STATUS:
                continue
        else:
            continue
        # A clearing review submitted after this red outranks it; one it can't be ordered
        # against (no timestamp on either side) still does, as before.
        if cleared_at and (not at or cleared_at > at):
            continue
        verdict.signals.append(f"{label} `{name}` {state}")
    verdict.failed = bool(verdict.signals)
    _judge_coverage(verdict, latest_check, review_complete)
    return verdict


def _judge_coverage(verdict: Verdict, latest_check: dict | None, review_complete: bool) -> None:
    """Set ``verdict.incomplete``: is the panel's newest word at this head an incomplete pass?

    * The newest App ``QA panel`` check run concluded ``neutral`` — unless a COMPLETE marked
      review at this head was submitted after that check completed (a later full pass).
    * The newest marked review at this head says ``complete=false`` — unless the newest App
      check run concluded ``success`` after that review was submitted (a later full pass
      cleared the head).

    A FAIL is not an incomplete pass: it already blocks, by its own edge."""
    if verdict.failed:
        return
    check_state = str((latest_check or {}).get("conclusion") or "").upper()
    check_at = str((latest_check or {}).get("completedAt") or "")
    review_at = verdict.review_at
    if check_state == _INCOMPLETE_CHECK:
        superseded = (
            bool(verdict.review_verdict) and review_complete and review_at and check_at and review_at > check_at
        )
        if not superseded:
            verdict.incomplete_signals.append(
                f"check run `{latest_check.get('name')}` NEUTRAL (incomplete pass, {HOLD_INCOMPLETE_COVERAGE})"
            )
    if verdict.review_verdict and not review_complete:
        superseded = check_state == "SUCCESS" and check_at and review_at and check_at > review_at
        if not superseded:
            verdict.incomplete_signals.append(
                f"review by {verdict.reviewer}: verdict={verdict.review_verdict} complete=false at {verdict.head[:12]}"
            )
    verdict.incomplete = bool(verdict.incomplete_signals)


def parse_findings(body: str) -> list[dict] | None:
    """The findings array from the first fenced ``json`` block in ``body`` that holds one
    (a list of objects, or an object with a ``findings`` list). ``None`` when none parses."""
    for block in _JSON_FENCE_RE.findall(body or ""):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            data = data.get("findings")
        if isinstance(data, list) and all(isinstance(f, dict) for f in data):
            return data
    return None


def blocking(findings: list[dict]) -> list[dict]:
    """The findings a fix round must address: blocker/major AND confirmed/verified."""
    return [
        f
        for f in findings or []
        if str(f.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        and str(f.get("verdict") or "").strip().lower() in CONFIRMED_VERDICTS
    ]


def _fence(text: str) -> str:
    runs = [len(r) for r in re.findall(r"`+", text)]
    return "`" * max(3, max(runs, default=0) + 1)


def render_findings(verdict: Verdict, pr_url: str = "") -> str:
    """The fix-round feedback for an external FAIL: each blocking finding with its
    ``file:line``, claim and evidence. When no blocking finding parses (the verdict is FAIL
    but its findings are all minor, unconfirmed or unreadable), the review's own text is
    quoted instead, so the coder still sees what the panel objected to."""
    where = f" on {pr_url}" if pr_url else ""
    said = f" ({verdict.review_verdict})" if verdict.review_verdict else ""
    head = f"External review FAILED{said} — {verdict.reviewer or 'the QA panel'} at head {verdict.head[:12]}{where}."
    findings = parse_findings(verdict.body)
    must = blocking(findings or [])
    lines = [head, ""]
    if must:
        lines.append(f"Blocking findings ({len(must)}):")
        for i, f in enumerate(must, 1):
            loc = str(f.get("file") or "?")
            if f.get("line") not in (None, ""):
                loc += f":{f.get('line')}"
            sev = str(f.get("severity") or "").lower()
            ver = str(f.get("verdict") or "").lower()
            lines.append(f"{i}. `{loc}` [{sev}, {ver}] {str(f.get('claim') or '').strip()}")
            evidence = str(f.get("evidence") or "").strip()[:_EVIDENCE_CHARS]
            if evidence:
                fence = _fence(evidence)
                lines += ["   Evidence:", f"   {fence}", *("   " + ln for ln in evidence.splitlines()), f"   {fence}"]
            note = str(f.get("note") or "").strip()
            if note:
                lines.append(f"   Reviewer's note: {note[:_EVIDENCE_CHARS]}")
        rest = len(findings or []) - len(must)
        if rest > 0:
            lines += ["", f"({rest} other finding(s) were minor, unconfirmed or refuted — not required.)"]
    else:
        text = _HTML_COMMENT_RE.sub("", verdict.body).strip()[:_BODY_EXCERPT_CHARS]
        lines.append(
            "No blocker/major confirmed finding could be read from the review's findings JSON. The review says:"
        )
        lines += ["", text or "(empty review body)"]
    return "\n".join(lines).rstrip()


def fail_summary(verdict: Verdict) -> str:
    """One line naming the failing signals, for logs and hold comments."""
    return "; ".join(verdict.signals) or "no failing signal"

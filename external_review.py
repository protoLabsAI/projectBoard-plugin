"""The external QA panel's verdict on a board PR, read and judged (#473).

A fleet repo can run its own review bot on every PR, separate from the board's in-process
review gate. On protoAgent that is the ``protoreview`` GitHub App ("Vera"). Its verdict
arrives three ways at once, all pinned to the PR head it reviewed:

* a PR REVIEW by the bot (``protoreview[bot]``), CHANGES_REQUESTED on a FAIL, whose body
  carries a hidden head marker —
  ``<!-- protoagent-qa-review head=<sha> verdict=FAIL promoted=false diff=… -->`` — and
  the findings as a fenced ``json`` array (file, line, severity, claim, evidence, verdict);
* a ``QA panel`` CHECK RUN from the App, concluding ``failure``;
* a ``Review at head`` commit STATUS, ``failure``.

The board used to read none of this. On 2026-09-27 its own gate said clean at the head the
panel failed (bd-524n, protoAgent#3698), so the card sat ``in_review`` for seven hours while
the merged-state gate re-ran on every base move until its budget was spent.

This module is the pure half: parse the config, decide from one ``gh pr view`` payload
(``worktree.pr_review_state``) whether the panel FAILED at the current head, and turn the
findings into the text a fix round leads with. The reconcile (``loop/reconcile.py``) owns
what happens next. Nothing here shells out.

Not to be confused with the board's OWN verdict: the in-process gate publishes a ``QA
panel`` commit STATUS (#354). The panel's check of the same name is a CHECK RUN, and the
two are told apart by type, so the board can never read its own verdict back as the
panel's.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEFAULT_REVIEWERS: tuple[str, ...] = ("protoreview[bot]",)
DEFAULT_MARKER = "protoagent-qa-review"
DEFAULT_CHECK_RUNS: tuple[str, ...] = ("QA panel",)
DEFAULT_STATUSES: tuple[str, ...] = ("Review at head",)

# A finding blocks the merge when it is serious AND the panel stood behind it — the same
# bar the in-process gate applies (blocker/major, not refuted), made explicit for the
# panel's own verification vocabulary.
BLOCKING_SEVERITIES = frozenset({"blocker", "major"})
CONFIRMED_VERDICTS = frozenset({"confirmed", "verified"})

# Check-run conclusions / commit-status states that mean the review FAILED. A check still
# running, or one that was skipped, says nothing.
_FAILED_CHECK = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED"})
_FAILED_STATUS = frozenset({"FAILURE", "ERROR"})

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
    # FAIL / PASS from the latest marked review AT THIS HEAD; "" when there is none.
    review_verdict: str = ""
    # That review's author and body (the findings source); "" when there is none.
    reviewer: str = ""
    body: str = ""
    # Human-readable evidence for each failing signal, e.g. "check run `QA panel` FAILURE".
    signals: list[str] = field(default_factory=list)

    @property
    def has_findings_review(self) -> bool:
        """A FAIL review at this head exists — something a fix round can act on."""
        return self.review_verdict == "FAIL" and bool(self.body)


def evaluate(view: dict, cfg: Config) -> Verdict | None:
    """Judge one ``gh pr view --json headRefOid,reviews,statusCheckRollup`` payload.

    Returns ``None`` when the payload names no head (nothing can be pinned). Otherwise a
    :class:`Verdict` whose ``failed`` is true when, AT THE PR'S CURRENT HEAD:

    * the latest review by a configured reviewer whose marker names this head says
      ``verdict=FAIL``; or
    * a configured check run concluded failed, or a configured status is failure/error —
      unless that latest marked review at this head says PASS (the panel re-reviewed and
      cleared it; a lingering red is not the verdict).

    A review whose marker names another head is ignored: it judged code that is gone. The
    rollup is the head commit's by construction, so its signals need no such filter. A
    check run is matched by name only as a CHECK RUN, and a status only as a STATUS, so the
    board's own ``QA panel`` commit status can never read as the panel's check run."""
    head = str((view or {}).get("headRefOid") or "").strip()
    if not head:
        return None
    verdict = Verdict(head=head)
    reviews = [r for r in (view.get("reviews") or []) if isinstance(r, dict)]
    # Chronological. `gh` returns them in submission order; sort anyway (stable), so a
    # reordered payload can't promote an older verdict over a newer one.
    reviews.sort(key=lambda r: str(r.get("submittedAt") or ""))
    for r in reviews:
        author = str(((r.get("author") or {}).get("login")) or (r.get("user") or {}).get("login") or "")
        if not cfg.is_reviewer(author):
            continue
        attrs = parse_marker(str(r.get("body") or ""), cfg.marker)
        if not attrs or not head_matches(attrs.get("head", ""), head):
            continue
        verdict.review_verdict = str(attrs.get("verdict") or "").strip().upper()
        verdict.reviewer = author
        verdict.body = str(r.get("body") or "")
    if verdict.review_verdict == "FAIL":
        verdict.signals.append(f"review by {verdict.reviewer}: verdict=FAIL at {head[:12]}")
    red: list[str] = []
    for c in view.get("statusCheckRollup") or []:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("__typename") or "")
        if kind == "CheckRun" and str(c.get("name") or "") in cfg.check_runs:
            state = str(c.get("conclusion") or "").upper()
            if state in _FAILED_CHECK:
                red.append(f"check run `{c.get('name')}` {state}")
        elif kind == "StatusContext" and str(c.get("context") or "") in cfg.statuses:
            state = str(c.get("state") or "").upper()
            if state in _FAILED_STATUS:
                red.append(f"status `{c.get('context')}` {state}")
    if red and verdict.review_verdict != "PASS":
        verdict.signals.extend(red)
    verdict.failed = bool(verdict.signals)
    return verdict


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
    head = f"External review FAILED — {verdict.reviewer or 'the QA panel'} at head {verdict.head[:12]}{where}."
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

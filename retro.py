"""Loop retro: mine the board's own attempt/outcome history into a digest of
recurring failure classes + flow stats, so a scheduled distill pass can turn the
lessons into durable grounding (PROTO.md today, the knowledge graph next).

The signal lives in each feature's **comments** — the loop writes
``attempt N (tier=X): <outcome>``, ``CI failed: …``, ``blocked: …`` as it drives —
and those survive even when the loop resets the ``attempt:``/``tier:`` *labels* on a
re-run. So the retro reads comments, not labels, for the failure record.

Pure functions only (raw ``br`` feature dicts in → digest out): the I/O of fetching
features-with-comments lives in the store; the LLM clustering + write-back lives in
the ``loop-retro`` skill. That keeps this layer deterministic + unit-testable."""

from __future__ import annotations

import re
from collections import Counter

# Coarse buckets for a free-text outcome — a deterministic head start for the distill
# pass (an LLM names the long tail). Order matters: first match wins, most-specific
# first. Each pattern is one recurring failure CLASS we've actually seen the loop hit.
_CLASS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("golden-map / config field", re.compile(r"golden\s*(field)?\s*map|FROM_YAML|config_to_dict|out of sync", re.I)),
    ("lint (ruff)", re.compile(r"\bF\d{3}\b|\bruff\b|unused (import|variable)|reformat|format --check", re.I)),
    ("test schema / fixture", re.compile(r"no column named|OperationalError|fixture|table .* has no", re.I)),
    ("version drift", re.compile(r"version.*(drift|lockstep)|manifest.*pyproject|pyproject.*manifest", re.I)),
    ("rebase / merge conflict", re.compile(r"rebase|merge conflict|conflicts? with|non-fast-forward", re.I)),
    ("import contracts / layering", re.compile(r"import-linter|lint-imports|forbidden_modules|import contract", re.I)),
    ("coder produced no diff", re.compile(r"no commits|no diff|nothing to PR|NoChanges", re.I)),
    ("coder timeout", re.compile(r"timed out|timeout|CoderTimeout", re.I)),
    ("CI failed (unclassified)", re.compile(r"CI fail|checks red|Python tests|E2E|pytest", re.I)),
]

_ATTEMPT_RE = re.compile(r"attempt\s+(\d+)\s*\(tier=([^)]+)\):\s*(.*)", re.I | re.S)
_BLOCKED_RE = re.compile(r"^\s*blocked:\s*(.*)", re.I | re.S)


def classify(text: str) -> str:
    """Bucket one outcome string into a coarse failure class, or ``"other"``."""
    for name, pat in _CLASS_PATTERNS:
        if pat.search(text or ""):
            return name
    return "other"


def _comment_text(c) -> str:
    if isinstance(c, dict):
        return (c.get("text") or c.get("body") or c.get("content") or "").strip()
    return str(c or "").strip()


def mine_feature(feat: dict) -> dict:
    """Extract one feature's attempt/outcome/flow signal from its raw ``br`` dict.

    Reads ``comments`` for the per-attempt outcomes + the blocked reason; ``labels``
    for the difficulty; ``status`` for the terminal state. Returns a flat dict the
    summarizer aggregates (and the distill pass reads)."""
    comments = [_comment_text(c) for c in (feat.get("comments") or [])]
    attempts: list[dict] = []
    blocked_reason: str | None = None
    for t in comments:
        m = _ATTEMPT_RE.match(t)
        if m:
            attempts.append({"n": int(m.group(1)), "tier": m.group(2).strip(), "outcome": m.group(3).strip()})
            continue
        b = _BLOCKED_RE.match(t)
        if b:
            blocked_reason = b.group(1).strip()
    # Classify the per-ATTEMPT outcomes (the granular failure signal). The terminal
    # `blocked:` summary usually just echoes the last attempt, so it's only the
    # fallback class source when a feature blocked with no attempt recorded at all.
    outcomes = [a["outcome"] for a in attempts]
    if not outcomes and blocked_reason:
        outcomes = [blocked_reason]
    classes = sorted({classify(o) for o in outcomes if o} - {"other"})
    if not classes and outcomes:
        classes = ["other"]
    tiers = [a["tier"] for a in attempts]
    # A DONE (closed) feature shipped — it is NOT blocked, even if it carries a stale
    # `blocked` label from a phase the loop parked it in. Only an OPEN feature with the
    # label (or a recorded block reason) is terminally blocked. (Its attempt comments
    # are still mined for failure classes — failures on an eventually-done feature are
    # exactly the recurring lessons worth grounding.)
    is_done = (feat.get("status") or "").lower() in ("closed", "done")
    blocked = (not is_done) and (blocked_reason is not None or "blocked" in (feat.get("labels") or []))
    return {
        "id": feat.get("id"),
        "title": feat.get("title"),
        "status": feat.get("status"),
        "diff": next((label.split(":", 1)[1] for label in (feat.get("labels") or []) if label.startswith("diff:")), ""),
        "n_attempts": len(attempts),
        "tiers": tiers,
        "escalated": len(set(tiers)) > 1,
        "blocked": blocked,
        "blocked_reason": blocked_reason if blocked else None,
        "classes": classes,
        "outcomes": outcomes,
        "created_at": feat.get("created_at"),
        "closed_at": feat.get("closed_at"),
    }


def summarize(raw_features: list[dict]) -> dict:
    """Mine + aggregate raw ``br`` feature dicts into a retro digest:
    recurring failure classes (ranked, with example outcomes) + flow stats.

    The distill pass reads ``recurring_classes`` (what to ground against) and the
    rates (whether the loop is improving run-over-run)."""
    mined = [mine_feature(f) for f in raw_features if f]
    n = len(mined)
    class_counter: Counter = Counter()
    class_examples: dict[str, str] = {}
    for f in mined:
        for c in f.get("classes") or []:
            if c == "other":
                continue
            class_counter[c] += 1
            if c not in class_examples:
                ex = next((o for o in f.get("outcomes") or [] if classify(o) == c), "")
                class_examples[c] = ex[:200]
    escalated = sum(1 for f in mined if f.get("escalated"))
    blocked = sum(1 for f in mined if f.get("blocked"))
    multi = sum(1 for f in mined if (f.get("n_attempts") or 0) > 1)
    return {
        "n_features": n,
        "recurring_classes": [
            {"class": c, "count": k, "example": class_examples.get(c, "")} for c, k in class_counter.most_common()
        ],
        "escalation_rate": round(escalated / n, 2) if n else 0.0,
        "block_rate": round(blocked / n, 2) if n else 0.0,
        "multi_attempt_rate": round(multi / n, 2) if n else 0.0,
        "blocked_features": [
            {"id": f["id"], "title": f["title"], "reason": f["blocked_reason"]} for f in mined if f.get("blocked")
        ],
        "features": mined,
    }

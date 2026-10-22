"""Versioned external-work references, persisted as append-only bead comments.

These are caller-reported facts, never filesystem, git, credential or project
configuration authority. No path normalization or probes belong at this seam.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

PREFIX = "work-location: "
VERSION = 1
MAX_RECORD_BYTES = 16384
MAX_ENTRIES = 100
MAX_REVISIONS = 1000
FIELDS = {"id", "path", "repo", "branch", "sha", "pushed", "purpose", "remote_ref", "session_id", "last_check"}


def text(value, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be text of at most {limit} characters without control characters")
    if required and not value.strip():
        raise ValueError(f"{name} is required")
    return value


def feature_id(value) -> str:
    value = text(value, "feature_id", 200, required=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("feature_id must be a bead id, not a path or option")
    return value


def validate(entry: dict) -> dict:
    """Validate a complete state (not a patch); null pushed means unknown."""
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object with id, path and purpose")
    if set(entry) - FIELDS:
        raise ValueError("unsupported entry fields: " + ", ".join(sorted(set(entry) - FIELDS)))
    result = {}
    for key, limit in (("id", 128), ("path", 4096), ("purpose", 2048), ("repo", 2048), ("branch", 1024), ("sha", 64)):
        result[key] = text(entry.get(key, ""), key, limit, required=key in {"id", "purpose"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", result["id"]):
        raise ValueError("id must be a stable location id using letters, digits, dot, underscore or hyphen")
    if result["sha"] and not re.fullmatch(r"[0-9a-fA-F]{7,64}", result["sha"]):
        raise ValueError("sha must be 7..64 hexadecimal characters or empty when unknown")
    result["sha"] = result["sha"].lower()
    pushed = entry.get("pushed")
    if pushed is not None and type(pushed) is not bool:
        raise ValueError("pushed must be true, false or null (unknown)")
    result["pushed"] = pushed
    if not result["path"].strip() and not (pushed is True and (result["sha"] or result["branch"].strip())):
        raise ValueError("path is required unless a git identity is explicitly reported pushed=true")
    for key in ("remote_ref", "session_id"):
        if key in entry:
            result[key] = text(entry[key], key, 2048 if key == "remote_ref" else 256)
    if "last_check" in entry:
        check = entry["last_check"]
        if not isinstance(check, dict) or len(json.dumps(check).encode()) > 4096:
            raise ValueError("last_check must be a bounded evidence object")
        if type(check.get("cycle", 0)) is not int or check.get("cycle", 0) < 0:
            raise ValueError("last_check cycle must be a nonnegative integer")
        result["last_check"] = check
    if len(json.dumps(result).encode()) > MAX_RECORD_BYTES - 1024:
        raise ValueError("entry is too large; shorten path, purpose or git references")
    return result


def record(entry: dict, actor: str) -> dict:
    result = {
        "version": VERSION,
        **validate(entry),
        "by": text(actor, "by", 256, required=True),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if len((PREFIX + json.dumps(result)).encode()) > MAX_RECORD_BYTES:
        raise ValueError("record is too large including attribution; shorten entry text")
    return result


def project(comments: list[str]) -> dict:
    """Project current entries and full revisions; surface corrupt data explicitly."""
    current = {}
    history = []
    errors = []
    for position, comment in enumerate(comments):
        if not comment.startswith(PREFIX):
            continue
        try:
            if len(comment.encode()) > MAX_RECORD_BYTES:
                raise ValueError("record exceeds size limit")
            raw = json.loads(comment[len(PREFIX) :])
            if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != VERSION:
                raise ValueError("unsupported record version")
            entry = validate({k: v for k, v in raw.items() if k not in {"version", "by", "at"}})
            by = text(raw.get("by"), "by", 256, required=True)
            at = text(raw.get("at"), "at", 64, required=True)
            if datetime.fromisoformat(at).tzinfo is None:
                raise ValueError("at must include timezone")
            revision = {"version": VERSION, **entry, "by": by, "at": at}
            history.append(revision)
            current[entry["id"]] = revision
        except (ValueError, TypeError, RecursionError) as exc:
            errors.append(f"comment {position}: {exc}")
    return {"version": VERSION, "entries": list(current.values()), "history": history, "errors": errors}


# Full tokens only: never extract a hash substring from an issue id, URL or word.
_TOKEN = re.compile(r"[^\s`\"'(),;<>\[\]{}]+")
_SHA = re.compile(r"(?:[0-9a-fA-F]{7,40}|[0-9a-fA-F]{64})\Z")
_MARKED = re.compile(
    r"\b(?P<kind>sha|commit|branch)\s*(?:[=:]\s*|\s+)"
    r"(?P<quote>[`\"']?)(?P<value>[^\s`\"'(),;<>\[\]{}]+)",
    re.IGNORECASE,
)


def hold_identities(reason: str) -> list[tuple[str, str]]:
    """Conservative prose recognition; branches need explicit git context.

    Use branch=<name>, branch `<name>`, branch feat/name or refs/heads/name.
    Short letter-only hashes need a SHA marker or quoting after "commit" to
    distinguish them from words such as "feedback" and "defaced".
    """
    found = []
    for match in _MARKED.finditer(reason):
        kind, value = match["kind"].lower(), match["value"].rstrip(".!?")
        if kind != "branch":
            if _SHA.fullmatch(value) and (
                kind == "sha"
                or match["quote"]
                or len(value) in {40, 64}
                or (re.search(r"[a-fA-F]", value) and re.search(r"[0-9]", value))
            ):
                found.append(("sha", value.lower()))
        elif (
            match["quote"]
            or "=" in match[0]
            or ":" in match[0]
            or "/" in value
            or value in {"main", "master", "develop"}
        ) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value):
            found.append(("branch", value.removeprefix("refs/heads/")))
    for match in _TOKEN.finditer(reason):
        token = match[0].rstrip(".:!?")
        if (
            _SHA.fullmatch(token)
            and (len(token) in {40, 64} or (re.search(r"[a-fA-F]", token) and re.search(r"[0-9]", token)))
            and not token.isdigit()
        ):
            found.append(("sha", token.lower()))
        elif token.startswith("refs/heads/") and len(token) > len("refs/heads/"):
            found.append(("branch", token.removeprefix("refs/heads/")))
    return list(dict.fromkeys(found))


def hold_reason(reason: str, ledger: dict) -> str:
    """Use current persisted reports only, without git/path/network IO.

    A true pushed report is affirmative evidence; missing/failed verification is
    null, never inferred from a remote URL or review stamp. Snapshot the evidence
    in the audit so later ledger revisions cannot erase the hold's context.
    """
    identities = hold_identities(str(reason or ""))
    if not identities:
        return reason
    if ledger.get("errors"):
        raise ValueError("work-location history is malformed; repair it before placing a git hold")
    context = []
    for kind, identity in identities:
        matches = []
        for entry in ledger.get("entries", []):
            value = entry.get(kind, "")
            if kind == "sha":
                # Only a reason abbreviation may prefix a longer recorded SHA.
                matched = bool(value) and value.lower().startswith(identity)
            else:
                matched = value.removeprefix("refs/heads/") == identity
            if matched:
                matches.append(entry)
        ambiguous = kind == "sha" and len({e.get("sha", "").lower() for e in matches}) > 1
        resolved = [e for e in matches if e.get("path", "").strip() or e.get("pushed") is True]
        if ambiguous or not resolved:
            raise ValueError(
                f"{kind} {identity!r} has no unambiguous recorded work location or pushed confirmation; "
                "call board_record_work_location (store.record_work_location) with id, path, purpose, "
                "branch, sha and pushed (false or null when unverified), then retry the hold"
            )
        context.append(
            {
                "identity": identity,
                "kind": kind,
                "locations": [
                    {key: e.get(key) for key in ("id", "path", "repo", "branch", "sha", "pushed", "remote_ref")}
                    for e in resolved
                ],
            }
        )
    return reason + "\nwork-location context: " + json.dumps(context, sort_keys=True)

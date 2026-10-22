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
FIELDS = {"id", "path", "repo", "branch", "sha", "pushed", "purpose", "remote_ref", "session_id"}


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
        result[key] = text(entry.get(key, ""), key, limit, required=key in {"id", "path", "purpose"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", result["id"]):
        raise ValueError("id must be a stable location id using letters, digits, dot, underscore or hyphen")
    if result["sha"] and not re.fullmatch(r"[0-9a-fA-F]{7,64}", result["sha"]):
        raise ValueError("sha must be 7..64 hexadecimal characters or empty when unknown")
    result["sha"] = result["sha"].lower()
    pushed = entry.get("pushed")
    if pushed is not None and type(pushed) is not bool:
        raise ValueError("pushed must be true, false or null (unknown)")
    result["pushed"] = pushed
    for key in ("remote_ref", "session_id"):
        if key in entry:
            result[key] = text(entry[key], key, 2048 if key == "remote_ref" else 256)
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

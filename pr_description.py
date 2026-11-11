"""Durable feature context, independent of the last repair reply.

Stored in the repository's common git directory, outside disposable checkouts.
GitHub remains authoritative for operator edits; only missing attempt sections are
appended to an existing body. Failed reads must never turn into destructive writes.
"""

import hashlib
import json
import os

from . import worktree


async def _path(wt: str, fid: str) -> str:
    common = await worktree._git_required(wt, "rev-parse", "--git-common-dir")
    key = hashlib.sha256(fid.encode()).hexdigest()
    return os.path.join(os.path.abspath(os.path.join(wt, common)), "board-descriptions", key + ".json")


async def read(wt: str, fid: str) -> dict:
    filename = await _path(wt, fid)
    try:
        with open(filename) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


async def record(wt: str, fid: str, summary: str, *, repair: bool = False, fallback: str = "") -> None:
    """Capture BEFORE any verification gate can bounce the coder or halt the process."""
    context = await read(wt, fid)
    if not context:
        context = {"version": 1, "feature": fallback if repair else summary, "attempts": []}
        if not repair:
            summary = ""
    if summary:
        attempts = context["attempts"]
        attempts.append({"number": len(attempts) + 1, "summary": summary})
    filename = await _path(wt, fid)
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    temporary = filename + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(context, stream)
    os.replace(temporary, filename)


async def render(wt: str, branch: str, fid: str, fallback: str) -> str:
    context = await read(wt, fid)
    existing = await existing_body(wt, branch)
    body = existing or context.get("feature", fallback)
    for attempt in context.get("attempts", []):
        number = attempt["number"]
        marker = f"<!-- board-fix-round:{fid}:{number} -->"
        if marker not in body:
            update = attempt["summary"].removeprefix("## Summary").lstrip()
            body = body.rstrip() + f"\n\n{marker}\n## Fix round {number}\n\n" + update
    return body


async def existing_body(wt: str, branch: str) -> str | None:
    rc, out, err = await worktree._gh("pr", "list", "--head", branch, "--state", "open", "--json", "body", cwd=wt)
    if rc:
        raise worktree.WorktreeError(f"cannot read existing PR description: {err[:300]}")
    prs = json.loads(out)
    return prs[0]["body"] if prs else None

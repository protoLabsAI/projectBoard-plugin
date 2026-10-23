"""What a card's coder has changed so far — the drawer's Code tab (Changes section).

``feature_changes`` reads every worktree the card owns on disk (``worktree.feature_worktrees``)
and returns, per tree, the files that differ from where the branch left its base: committed
work, uncommitted edits and untracked files alike, because a coder mid-run has usually not
committed yet. Read-only: git ``diff`` / ``merge-base`` / ``ls-files`` and plain file reads,
nothing that writes the tree or the index.

The drawer is a display surface (screen shares, screenshots pasted into issues), so a path
that looks like a secret is listed but its content never leaves here — the same name rule
the host's code pane uses (protoAgent ``tools/fs_secrets.py``, ADR 0112), plus a local
fallback for hosts without it. Diff bodies are capped per file and in total so a generated
lockfile can't flood the page.
"""

from __future__ import annotations

import fnmatch
import os

from . import worktree

MAX_FILES = 300
MAX_FILE_DIFF_BYTES = 64 * 1024
MAX_TOTAL_DIFF_BYTES = 512 * 1024
GIT_TIMEOUT_S = 30.0

_STATUS = {"A": "A", "M": "M", "D": "D", "R": "R", "C": "A", "T": "M", "U": "M"}

# Local fallback for the host's code-pane rule: path components, case-insensitive.
_SECRET_DIRS = {".ssh", ".gnupg", ".aws"}
_SECRET_NAMES = {".netrc", ".npmrc", ".pypirc", "secrets.yaml", "secrets.yml"}
_SECRET_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*secret*",
    "*credential*",
    "*.kdbx",
)
_ENV_TEMPLATES = {"example", "sample", "template"}


def _host_secret_rule():
    try:
        from tools.fs_secrets import is_secret_path  # protoAgent host (ADR 0112)
    except Exception:  # noqa: BLE001 — host without the code pane: local rule only
        return None
    return is_secret_path


def _local_secret_reason(path: str) -> str | None:
    parts = [p.lower() for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    if any(p in _SECRET_DIRS for p in parts[:-1]):
        return "inside a credentials directory"
    name = parts[-1]
    if name == ".env" or (name.startswith(".env.") and name.split(".", 2)[2] not in _ENV_TEMPLATES):
        return "an environment file (.env)"
    if name in _SECRET_NAMES:
        return "a credentials file"
    for pat in _SECRET_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return f"looks like key or credential material ({pat})"
    return None


def secret_reason(path: str) -> str | None:
    """Why ``path`` must not show its content, or ``None``. The host rule and the local
    rule both apply — either one hiding a file hides it."""
    host = _host_secret_rule()
    if host is not None:
        try:
            reason = host(path)
        except Exception:  # noqa: BLE001 — a host-rule error must not open the file
            reason = "unreadable by the secret check"
        if reason:
            return reason
    return _local_secret_reason(path)


async def _git(path: str, *args: str) -> tuple[int, str]:
    rc, out, _err = await worktree._git(path, *args, timeout=GIT_TIMEOUT_S)
    return rc, out


async def _merge_base(path: str, base: str) -> tuple[str, str]:
    """``(sha, ref)`` of where HEAD left ``base`` — the remote-tracking ref first (the loop
    branches from it), then the local branch. ``("HEAD", "")`` when neither resolves, so
    the diff still shows the uncommitted work."""
    for ref in (f"origin/{base}", base) if base else ():
        rc, out = await _git(path, "merge-base", ref, "HEAD")
        if rc == 0 and out.strip():
            return out.strip(), ref
    return "HEAD", ""


def _parse_z_pairs(out: str, *, numstat: bool) -> list[list[str]]:
    """``git diff -z --name-status`` / ``--numstat`` → one row per file. A rename carries
    two paths; ``--numstat -z`` writes it as ``a\\td\\t\\0old\\0new``."""
    tokens = out.split("\0")
    rows: list[list[str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if numstat:
            fields = tok.split("\t")
            rename = len(fields) == 3 and fields[2] == ""  # rename: paths follow as two tokens
            width, need = (3, 4) if rename else (1, 1)
            row = [fields[0], fields[1], *tokens[i + 1 : i + 3]] if rename else fields
        else:
            width = need = 3 if tok[:1] in ("R", "C") else 2
            row = tokens[i : i + width]
        if len(row) < need:
            break  # truncated output: drop the partial row rather than index past the end
        rows.append(row)
        i += width
    return rows


def _read_untracked(full: str) -> tuple[str | None, bool, bool]:
    """``(text, binary, too_big)`` for an untracked file: its content as a diff body."""
    try:
        size = os.path.getsize(full)
        if size > MAX_FILE_DIFF_BYTES:
            return None, False, True
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, False, False
    if b"\0" in data[:8192]:
        return None, True, False
    return data.decode("utf-8", errors="replace"), False, False


def _cap(diff: str, budget: int) -> tuple[str, bool]:
    limit = min(MAX_FILE_DIFF_BYTES, max(0, budget))
    raw = diff.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return diff, False
    cut = raw[:limit].decode("utf-8", errors="ignore")
    return cut[: cut.rfind("\n") + 1] if "\n" in cut else cut, True


async def tree_changes(path: str, branch: str, base: str) -> dict:
    """The files ``path`` differs in from its merge-base with ``base``: committed,
    uncommitted and untracked, one entry each, with capped diff bodies."""
    mb, base_ref = await _merge_base(path, base)
    rc, ns = await _git(path, "diff", "-z", "-M", "--name-status", mb)
    rc2, nums = await _git(path, "diff", "-z", "-M", "--numstat", mb)
    rc3, untracked = await _git(path, "ls-files", "-z", "--others", "--exclude-standard")
    if rc != 0 or rc2 != 0 or rc3 != 0:
        return {"path": path, "branch": branch, "base": base_ref or base, "error": "git could not read this worktree"}

    counts: dict[str, tuple[int | None, int | None]] = {}
    for row in _parse_z_pairs(nums, numstat=True):
        add, dele, name = row[0], row[1], row[-1]
        counts[name] = (None if add == "-" else int(add), None if dele == "-" else int(dele))

    entries: list[dict] = []
    for row in _parse_z_pairs(ns, numstat=False):
        code = _STATUS.get(row[0][:1], "M")
        entry = {"path": row[-1], "status": code}
        if code == "R" or row[0][:1] == "C":
            entry["old_path"] = row[1]
        entries.append(entry)
    for name in untracked.split("\0"):
        if name:
            entries.append({"path": name, "status": "A", "untracked": True})
    entries.sort(key=lambda e: e["path"])

    total_files = len(entries)
    files: list[dict] = []
    budget = MAX_TOTAL_DIFF_BYTES
    for e in entries[:MAX_FILES]:
        name = e["path"]
        reason = secret_reason(name) or (secret_reason(e["old_path"]) if e.get("old_path") else None)
        add, dele = counts.get(name, (0, 0))
        f = {
            **e,
            "additions": add,
            "deletions": dele,
            "binary": False,
            "redacted": bool(reason),
            "truncated": False,
            "diff": "",
        }
        if reason:
            f["redacted_reason"] = reason
            f["additions"] = f["deletions"] = None
            files.append(f)
            continue
        if e.get("untracked"):
            text, binary, too_big = _read_untracked(os.path.join(path, name))
            if binary:
                f.update(binary=True, additions=None, deletions=None)
            elif too_big:
                f.update(truncated=True, additions=None, deletions=None)
            elif text is not None:
                lines = text.splitlines()
                f["additions"], f["deletions"] = len(lines), 0
                body = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + ln + "\n" for ln in lines)
                f["diff"], f["truncated"] = _cap(body, budget)
        elif add is None:
            f["binary"] = True
        elif budget <= 0:
            f["truncated"] = True
        else:
            paths = [p for p in (e.get("old_path"), name) if p]
            rcd, body = await _git(path, "diff", "-M", "--no-color", "--no-ext-diff", mb, "--", *paths)
            if rcd == 0:
                hunk = body.find("\n@@")
                body = body[hunk + 1 :] if hunk >= 0 else ""
                f["diff"], f["truncated"] = _cap(body, budget)
        budget -= len(f["diff"].encode("utf-8", errors="replace"))
        files.append(f)

    adds = sum(f["additions"] or 0 for f in files)
    dels = sum(f["deletions"] or 0 for f in files)
    return {
        "path": path,
        "branch": branch,
        "base": base_ref or base,
        "merge_base": "" if mb == "HEAD" else mb,
        "files": files,
        "file_count": total_files,
        "files_truncated": total_files > MAX_FILES,
        "additions": adds,
        "deletions": dels,
    }


async def feature_changes(repo: str, worktrees_root: str, fid: str, base: str) -> dict:
    """Every on-disk worktree ``fid`` owns, each with :func:`tree_changes`. An empty list
    when the card has none (not started, or done and reaped)."""
    trees = []
    for path, branch in worktree.feature_worktrees(repo, worktrees_root, fid):
        if not os.path.isdir(path):
            continue
        try:
            trees.append(await tree_changes(path, branch, base))
        except worktree.WorktreeError as exc:  # a git timeout: report it on this tree, not a 500
            trees.append({"path": path, "branch": branch, "base": base, "error": str(exc)})
    return {"worktrees": trees}

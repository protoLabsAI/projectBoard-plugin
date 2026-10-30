"""Dispatch-attributed recovery facts. Reports never grant filesystem/write authority.

Consume ACP's structured locations/input fields, never commands, titles or reply
prose. The context travels into solve/max-mode tasks and ends with the drive.

Two seams, deliberately split so the live coder stream never pays for git work:

* ``observe`` — the PROGRESS seam (the coder's tool stream). Pure in-memory
  accumulation of structured paths: no git, no store, no network, no lock-held
  subprocess. It is called on every tool event, so it must stay O(1)-ish and MUST
  NOT raise — a monitoring concern must never break a build.
* ``drain`` — the RESULT/INTERRUPTION seam (the drive, once per dispatch). It probes
  each accumulated path locally, persists its attributable identity, and publishes a
  committed WIP/evidence ref early. Shielded so a durable write/publish finishes even
  under cancellation, bounded by a deadline so cancellation cannot hang, and fully
  guarded so nothing it does — a store ``validate`` error, a git/permission failure,
  a transport refusal — can ever surface into the dispatch.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from . import project_registry, work_location_sweep as sweep, work_locations

log = logging.getLogger("protoagent.plugins.project_board")

active = ContextVar("work_location_capture", default=None)

# A settled or just-probed path is not re-probed inside this window, so repeated drains
# (or a future per-event probe) cannot re-run the four-subprocess git probe in a loop.
RECHECK_SECONDS = 12.0
# Total wall-clock the drain may spend across every path, so an interruption drain (its
# shielded worker finishes before the cancel propagates) cannot hang on slow git/network.
DRAIN_BUDGET_SECONDS = 20.0
MAX_PATHS = 32


def reporting_instructions():
    return (
        "\n\nExternal work: use board_record_work_location immediately for scratch repositories or evidence "
        "outside this checkout (path, purpose, branch, SHA, pushed status), including partial/error work. "
        "Report before a hold or context compaction. Preserve sole copies. Publish committed evidence early "
        "with a normal non-force push to wip/<card> on the existing authorized project remote, only with "
        "existing write permission; never stage unrelated files or push main. If blocked, record the exact "
        "path/branch/SHA and recovery step. Plain-text mentions alone are not automatic capture.\n"
    )


def write_authorized(path):
    """A more specific read-only project must not inherit a parent's write grant."""
    host = project_registry._live_host()
    if host is None or not getattr(host, "filesystem_enabled", True):
        return False
    entries = project_registry._dict_list(getattr(host, "filesystem_projects", None))
    if not entries:
        entries = project_registry._dict_list(getattr(host, "projects", None))
    matches = [
        (Path(e["path"]).resolve(), e)
        for e in entries
        if e.get("path") and sweep.authorized(path, [Path(e["path"]).resolve()])
    ]
    if not matches:
        return False
    depth = max(len(root.parts) for root, _ in matches)
    return all(not project_registry._falsey(e.get("write", False)) for root, e in matches if len(root.parts) == depth)


def structured_paths(event):
    if not isinstance(event, dict):
        return []
    paths = []
    locations = event.get("locations")
    for loc in (locations if isinstance(locations, list) else [])[:100]:
        value = loc.get("path") if isinstance(loc, dict) else loc
        if isinstance(value, str):
            paths.append(value)
    data = event.get("input")
    if isinstance(data, str) and len(data) <= 16384:
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        for key in ("cwd", "workdir", "path", "file_path"):
            if isinstance(data.get(key), str):
                paths.append(data[key])
    return [p for p in dict.fromkeys(paths) if p.startswith("/") and len(p) <= 4096 and not any(ord(c) < 32 for c in p)]


def _internal_prefixes(value):
    """The own-checkout prefixes a path must NOT be treated as external work under:
    the value itself, its realpath (macOS hands out both ``/tmp`` and ``/private/tmp``),
    and — mirroring coder_seam's ``_display_roots`` — the checkout a ``.worktrees/<name>``
    tree was cut from. A board worktree lives under the project checkout and a coder reads
    the main checkout by absolute path, so capturing either would push the project's own
    HEAD to wip/<card> or strand a permanent publication-blocked ledger entry."""
    out = set()
    for raw in (value, os.path.realpath(value) if value else ""):
        raw = (raw or "").rstrip("/")
        if not raw:
            continue
        out.add(raw)
        marker = "/.worktrees/"
        if marker in raw:
            parent = raw.split(marker, 1)[0]
            for cut in (parent, os.path.realpath(parent) if parent else ""):
                cut = (cut or "").rstrip("/")
                if cut:
                    out.add(cut)
    return out


def project_credentials(project, target):
    """Read existing HTTPS credential helpers from trusted project/host config only.

    The external checkout never supplies executable helpers. Values stay in memory,
    never in card metadata. No new credential or approval mechanism is introduced.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(project), "config", "--get-urlmatch", "credential.helper", target],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )
    if result.returncode not in (0, 1) or len(result.stdout) > 8192:
        raise ValueError("project credential configuration could not be read")
    return result.stdout.decode("utf-8", "strict").splitlines()


class Capture:
    def __init__(self, store, fid, repo):
        self.store, self.fid, self.repo = store, work_locations.feature_id(fid), str(repo)
        self.session = uuid.uuid4().hex
        self.paths = set()
        # path -> last probe outcome, so a settled path is never re-probed/re-pushed:
        # ("published", sha) | ("blocked", sha) | ("nocommit", error).
        self.seen = {}
        self.probed_at = {}
        self.lock = asyncio.Lock()
        # The dispatch's own worktree is added lazily from observe(); the project's own
        # checkout is known now. Neither is ever external work (worktree salvage owns it).
        self.internal = _internal_prefixes(self.repo)

    def _is_internal(self, path):
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self.internal)

    async def observe(self, event=None, root=""):
        """PROGRESS seam: accumulate structured paths in memory only. No git, no store,
        no network — just string work under the lock, and it never raises."""
        async with self.lock:
            try:
                if root:
                    self.internal |= _internal_prefixes(root)
                for path in structured_paths(event):
                    if len(self.paths) >= MAX_PATHS:
                        break
                    if not self._is_internal(path) and not sweep.coder_scratch(path):
                        self.paths.add(path)
            except Exception:  # noqa: BLE001 — a monitoring concern must never break a build
                log.warning("[project_board] work-location observe failed (ignored)", exc_info=True)

    async def drain(self):
        """RESULT/INTERRUPTION seam: persist identities and publish committed refs for
        everything accumulated so far. Shielded so the durable write/publish finishes even
        while the dispatch is being cancelled; the worker itself never raises."""
        async with self.lock:
            if not self.paths:
                return
            worker = asyncio.create_task(asyncio.to_thread(self._drain))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # The cancel was already delivered and caught here, so this await now waits
                # for the shielded worker to finish its durable write before propagating.
                try:
                    await worker
                except BaseException:  # noqa: BLE001 — _drain swallows; ignore a stray worker error
                    pass
                raise

    def _drain(self):
        try:
            roots = sweep.approved_roots()
            self._discover_git_roots(roots)
            # A readable plain file outside every repository is a log or note, not a work
            # location (#650); a file inside one stays as that repo's evidence record.
            self.paths = {p for p in self.paths if not self._loose_file(Path(p), roots)}
            if not self.paths:
                return
            ledger = {e.get("path"): e for e in self.store.list_work_locations(self.fid).get("entries", [])}
            deadline = time.monotonic() + DRAIN_BUDGET_SECONDS
            for path in sorted(self.paths):
                if time.monotonic() >= deadline:
                    break
                try:
                    self.capture(path, ledger.get(path), roots, deadline)
                except Exception:  # noqa: BLE001 — one path's store/git error must not skip the rest
                    log.warning("[project_board] work-location capture failed for %s (ignored)", path, exc_info=True)
        except Exception:  # noqa: BLE001 — the whole drain is best-effort monitoring
            log.warning("[project_board] work-location drain failed (ignored)", exc_info=True)

    def _loose_file(self, path, roots):
        if not sweep.authorized(path, roots) or not path.is_file():
            return False
        for parent in path.parents:
            if not sweep.authorized(parent, roots):
                return True
            if (parent / ".git").exists():
                return False
        return True

    def _discover_git_roots(self, roots):
        # File locations retain their own evidence record. Discover an enclosing git root
        # only inside independently authorized space, never by parsing a command, and never
        # the project's own checkout (the parent walk stops at an internal prefix).
        for name in list(self.paths):
            path = Path(name)
            if not sweep.authorized(path, roots):
                continue
            for parent in (path, *path.parents):
                text = str(parent)
                if self._is_internal(text) or not sweep.authorized(parent, roots):
                    break
                if sweep.authorized(parent / ".git", roots) and (parent / ".git").exists():
                    if len(self.paths) < MAX_PATHS and not self._is_internal(text):
                        self.paths.add(text)
                    break

    def capture(self, path, old, roots, deadline):
        now = time.monotonic()
        prior = self.seen.get(path)
        # Seen-gate BEFORE any store read or git probe: a path already published, or
        # probed within the recheck window, costs nothing on a repeat pass.
        if prior and prior[0] == "published":
            return
        if path in self.probed_at and now - self.probed_at[path] < RECHECK_SECONDS:
            return
        state = {k: v for k, v in (old or {}).items() if k in work_locations.FIELDS}
        state.update(
            id=state.get("id") or "dispatch-" + hashlib.sha256(path.encode()).hexdigest()[:20],
            path=path,
            purpose=state.get("purpose") or "Dispatch-observed external work/evidence",
            session_id=self.session,
        )
        facts = sweep.probe({"path": path}, roots, min(deadline, now + 5), local_only=True)
        self.probed_at[path] = time.monotonic()
        sha = facts.get("sha", "")
        if not sha:
            key = ("nocommit", facts.get("error"))
            if prior == key:
                return
            # First save requires no filesystem access. An unauthorized location is still recoverable.
            state["last_check"] = {
                "status": "capture-pending",
                "next_step": "Preserve this path; authorize filesystem access and record the repository root with "
                "board_record_work_location before retrying publication.",
            }
            self.store.record_work_location(self.fid, state)
            self.seen[path] = key
            return
        # An already-pushed record for this exact commit needs no republish; the sweep
        # re-verifies durability from here, so carry its verdict forward untouched.
        if old and old.get("pushed") is True and old.get("sha") == sha:
            self.seen[path] = ("published", sha)
            return
        if prior == ("published", sha):
            return
        state.update(sha=sha, branch=facts.get("branch", ""), pushed=False)
        state.pop("remote_ref", None)
        ref = "refs/heads/wip/" + self.fid
        command = shlex.join(["git", "-C", path, "push", "--", "origin", f"{sha}:{ref}"])
        if len(command) > 3000:
            command = "git -C <this entry's exact path> push -- origin <this entry's exact sha>:" + ref
        state["last_check"] = {
            "status": "publication-pending",
            "next_step": "Preserve this checkout. After verifying origin matches the authorized project remote and "
            "obtaining write/network permission, run " + command + "; verify with git ls-remote origin " + ref,
        }
        # Identity must survive process death/network timeout BEFORE publication starts.
        self.store.record_work_location(self.fid, state)
        try:
            self.publish(state, ref, roots, deadline)
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
            # Never persist stderr/URLs/credentials from a transport or approval exception.
            state["last_check"].update(
                status="publication-blocked",
                error=type(exc).__name__
                + ": publication refused or unverified; retain local copy and resolve permission/network/ref conflict",
            )
        self.store.record_work_location(self.fid, state)
        self.seen[path] = ("published", sha) if state.get("pushed") else ("blocked", sha)

    def publish(self, state, ref, roots, deadline):
        path, project = Path(state["path"]), Path(self.repo)
        if not all(write_authorized(p) for p in (path, project)):
            raise PermissionError("write permission required")
        deadline = min(deadline, time.monotonic() + 15)
        # Probe validates .git/commondir authority before any git in either checkout.
        source_objects = None
        for p in (path, project):
            facts = sweep.probe({"path": str(p)}, roots, min(deadline, time.monotonic() + 5), local_only=True)
            if not facts.get("sha"):
                raise PermissionError("repository metadata not authorized")
            for key in ("git_dir", "common_dir"):
                if not facts.get(key) or not write_authorized(Path(facts[key])):
                    raise PermissionError("repository metadata write permission required")
            if p == path:
                source_objects = Path(facts["common_dir"]) / "objects"
                if not sweep.authorized(source_objects, roots):
                    raise PermissionError("object store read permission required")

        def run(p, *args):
            return sweep.git(
                p,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "push.followTags=false",
                "-c",
                "push.gpgSign=false",
                *args,
                deadline=deadline,
            )

        # No rewrite, custom receive-pack, or pushurl may redirect the authorized target.
        for p in (path, project):
            config = run(p, "config", "--local", "--list").lower().splitlines()
            if any(line.startswith("url.") or ".pushurl=" in line or ".receivepack=" in line for line in config):
                raise ValueError("remote overrides require explicit publication")
        target = run(project, "remote", "get-url", "origin")
        if run(path, "remote", "get-url", "origin") != target:
            raise ValueError("external repository origin differs from project")
        helpers = []
        if target.startswith("https://"):
            url = urlsplit(target)
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("credential-bearing URL refused")
            helpers = project_credentials(project, target)
        else:
            remote_path = Path(target.removeprefix("file://"))
            if not remote_path.is_absolute() or not write_authorized(remote_path):
                raise PermissionError("remote transport/path not authorized")

        def transport(*args):
            credentials = [item for helper in helpers for item in ("-c", "credential.helper=" + helper)]
            return sweep.git(
                project,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "push.followTags=false",
                "-c",
                "push.gpgSign=false",
                *credentials,
                *args,
                deadline=deadline,
                alternate_objects=source_objects,
                timeout=10.0,
            )

        run(path, "check-ref-format", ref)
        transport("push", "--no-verify", "--", target, state["sha"] + ":" + ref)
        rows = transport("ls-remote", "--exit-code", "--refs", "--", target, ref).splitlines()
        tips = [row.split()[0] for row in rows if row.split()[1:] == [ref]]
        if len(tips) != 1:
            raise ValueError("remote ref not verified")
        transport("merge-base", "--is-ancestor", state["sha"], tips[0])
        state.update(pushed=True, remote_ref="origin/" + ref.removeprefix("refs/heads/"))
        state["last_check"] = {
            "status": "published",
            "remote": "origin",
            "ref": ref,
            "upstream_sha": tips[0],
            "pushed": True,
        }


async def observe(event=None, root=""):
    """PROGRESS seam entry point (the coder's tool stream)."""
    capture = active.get()
    if capture is not None:
        await capture.observe(event, root)


async def drain():
    """RESULT/INTERRUPTION seam entry point (the drive, once per dispatch)."""
    capture = active.get()
    if capture is not None:
        await capture.drain()

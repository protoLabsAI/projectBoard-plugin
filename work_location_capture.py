"""Dispatch-attributed recovery facts. Reports never grant filesystem/write authority.

Consume ACP's structured locations/input fields, never commands, titles or reply
prose. The context travels into solve/max-mode tasks and ends with the drive.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from . import project_registry, work_location_sweep as sweep, work_locations

active = ContextVar("work_location_capture", default=None)


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


def project_credentials(project, target):
    """Read existing HTTPS credential helpers from trusted project/host config only.

    The external checkout never supplies executable helpers. Values stay in memory,
    never in card metadata. No new credential or approval mechanism is introduced.
    """
    import os

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
        self.seen = {}
        self.lock = asyncio.Lock()

    async def observe(self, event=None, root=""):
        async with self.lock:
            # Await the durable write even if the dispatch is being cancelled.
            worker = asyncio.create_task(asyncio.to_thread(self._observe, event, root))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise

    def _observe(self, event, root):
        for path in structured_paths(event):
            # Internal worktree recovery already belongs to worktree salvage.
            if root and (path == root or path.startswith(root.rstrip("/") + "/")):
                continue
            if len(self.paths) < 32:
                self.paths.add(path)
        roots = sweep.approved_roots()
        # File locations retain their own evidence record. Discover an enclosing git
        # root only inside independently authorized space, never by parsing a command.
        for name in list(self.paths):
            path = Path(name)
            if not sweep.authorized(path, roots):
                continue
            for parent in (path, *path.parents):
                if not sweep.authorized(parent, roots):
                    break
                if sweep.authorized(parent / ".git", roots) and (parent / ".git").exists():
                    if len(self.paths) < 32:
                        self.paths.add(str(parent))
                    break
        for path in sorted(self.paths):
            self.capture(path)

    def capture(self, path):
        old = next((e for e in self.store.list_work_locations(self.fid)["entries"] if e.get("path") == path), None)
        state = {k: v for k, v in (old or {}).items() if k in work_locations.FIELDS}
        state.update(
            id=state.get("id") or "dispatch-" + hashlib.sha256(path.encode()).hexdigest()[:20],
            path=path,
            purpose=state.get("purpose") or "Dispatch-observed external work/evidence",
            session_id=self.session,
        )
        # First save requires no filesystem access. An unauthorized location is still recoverable.
        if old is None:
            self.store.record_work_location(self.fid, state)
        roots = sweep.approved_roots()
        facts = sweep.probe({"path": path}, roots, time.monotonic() + 5, local_only=True)
        sha = facts.get("sha", "")
        if not sha:
            key = (path, facts.get("error"))
            if self.seen.get(path) == key:
                return
            state["last_check"] = {
                "status": "capture-pending",
                "next_step": "Preserve this path; authorize filesystem access and record the repository root with "
                "board_record_work_location before retrying publication.",
            }
            self.store.record_work_location(self.fid, state)
            self.seen[path] = key
            return
        if self.seen.get(path) == sha:
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
            self.publish(state, ref, roots)
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
            # Never persist stderr/URLs/credentials from a transport or approval exception.
            state["last_check"].update(
                status="publication-blocked",
                error=type(exc).__name__
                + ": publication refused or unverified; retain local copy and resolve permission/network/ref conflict",
            )
        self.store.record_work_location(self.fid, state)
        self.seen[path] = sha

    def publish(self, state, ref, roots):
        path, project = Path(state["path"]), Path(self.repo)
        if not all(write_authorized(p) for p in (path, project)):
            raise PermissionError("write permission required")
        # Probe validates .git/commondir authority before any git in either checkout.
        source_objects = None
        for p in (path, project):
            facts = sweep.probe({"path": str(p)}, roots, time.monotonic() + 5, local_only=True)
            if not facts.get("sha"):
                raise PermissionError("repository metadata not authorized")
            for key in ("git_dir", "common_dir"):
                if not facts.get(key) or not write_authorized(Path(facts[key])):
                    raise PermissionError("repository metadata write permission required")
            if p == path:
                source_objects = Path(facts["common_dir"]) / "objects"
                if not sweep.authorized(source_objects, roots):
                    raise PermissionError("object store read permission required")
        deadline = time.monotonic() + 15

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
    capture = active.get()
    if capture is not None:
        await capture.observe(event, root)

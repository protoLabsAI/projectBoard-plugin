"""Read-only, bounded checks of recorded work; ledger paths grant no authority."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from . import project_registry, work_locations, work_snapshot

log = logging.getLogger("protoagent.plugins.project_board")


def approved_roots():
    host = project_registry._live_host()
    if host is None or not getattr(host, "filesystem_enabled", True):
        return []
    entries = project_registry._dict_list(getattr(host, "filesystem_projects", None))
    if not entries:
        entries = project_registry._dict_list(getattr(host, "projects", None))
    return [Path(e["path"]).resolve() for e in entries if e.get("path")]


def authorized(path, roots):
    return path.is_absolute() and any(path.resolve().is_relative_to(root) for root in roots)


def git(path, *args, deadline):
    """REAL seam: bare-origin/clone coverage in test_work_location_sweep.py.

    No shell, optional locks, hooks, credential programs or custom transports.
    Kill the process group on timeout; bound captured output even for large dirt.
    """
    remaining = min(2.0, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("location probe budget exhausted")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    argv = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=always",
        "-c",
        "protocol.https.allow=always",
        "-C",
        str(path),
        *args,
    ]
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(argv, stdout=output, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            rc = proc.wait(timeout=remaining)
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        if rc:
            raise ValueError(f"git {args[0]} failed ({rc})")
        output.seek(0)
        data = output.read(8193)
        if len(data) > 8192:
            raise ValueError("git output exceeded evidence limit")
        return data.decode("utf-8", "replace").strip()


def probe(entry, roots, deadline):
    facts = {"status": "unknown", "pushed": None}
    try:
        path = Path(entry["path"])
        if not entry["path"] or not authorized(path, roots):
            facts["error"] = "authorization required: approve this path through filesystem/projects settings"
            return facts
        path = path.resolve()
        facts["temporary"] = any(
            path.is_relative_to(p) for p in (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
        )
        try:
            path.stat()
        except FileNotFoundError:
            facts.update(status="missing", error="recorded path is missing; restore or locate the surviving copy")
            return facts
        metadata = path / ".git"
        if not authorized(metadata, roots):
            raise ValueError("git metadata requires filesystem authorization")
        if metadata.is_file():
            with metadata.open() as pointer:
                target = pointer.read(4097)
            if len(target) > 4096 or not target.startswith("gitdir: "):
                raise ValueError("unrecognized git metadata pointer")
            metadata = (path / target[8:].strip()).resolve()
            if not authorized(metadata, roots):
                raise ValueError("git metadata requires filesystem authorization")
        common = metadata / "commondir"
        if common.is_file():
            with common.open() as pointer:
                target = pointer.read(4097).strip()
            if len(target) > 4096 or not authorized(metadata / target, roots):
                raise ValueError("shared git metadata requires filesystem authorization")
        run = lambda *args: git(path, *args, deadline=deadline)
        # Require this exact checkout, not an ancestor repo discovered by git.
        top = run("rev-parse", "--show-toplevel")
        if Path(top).resolve() != path:
            raise ValueError("recorded path is not a repository root")
        facts["repo"] = top
        facts["sha"] = run("rev-parse", "--verify", "HEAD")
        facts["branch"] = run("symbolic-ref", "--quiet", "--short", "HEAD")
        facts["dirty"] = bool(run("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=all"))
        branch = facts["branch"]
        remote = run("config", "--get", f"branch.{branch}.remote")
        ref = run("config", "--get", f"branch.{branch}.merge")
        url = run("remote", "get-url", "--", remote)
        # Local transport also needs filesystem authority; never run remote helpers
        # or executable URLs from config, much less from the caller's ledger.
        if not url.startswith("https://"):
            local = Path(url.removeprefix("file://"))
            if not local.is_absolute():
                local = path / local
            if not authorized(local, roots):
                raise ValueError("upstream requires filesystem authorization or an HTTPS remote")
            url = str(local.resolve())
        advertised = run("ls-remote", "--exit-code", "--refs", "--", url, ref).splitlines()
        matches = [line.split()[0] for line in advertised if line.split()[1:] == [ref]]
        if len(matches) != 1:
            raise ValueError("upstream ref absent or ambiguous")
        tip = matches[0]
        facts.update(remote_ref=ref, upstream_sha=tip)
        # No fetch: an advertised object absent locally is UNKNOWN, never pushed.
        ahead, behind = map(int, run("rev-list", "--left-right", "--count", f"HEAD...{tip}").split())
        facts.update(status="verified", ahead=ahead, behind=behind, pushed=ahead == 0)
    except PermissionError as exc:
        facts["error"] = (
            "authorization required: approve access through filesystem/projects settings: " + str(exc)[:200]
        )
    except (OSError, ValueError, subprocess.SubprocessError, TimeoutError) as exc:
        facts["error"] = str(exc)[:300]
    if facts.get("status") == "verified" and (
        (entry.get("sha") and not facts.get("sha", "").startswith(entry["sha"]))
        or (entry.get("branch") and entry["branch"] != facts.get("branch"))
    ):
        facts.update(
            status="identity-changed",
            pushed=None,
            error="checkout identity differs from the record; locate and verify the recorded work",
        )
    return facts


def risks(facts, blocked=False):
    result = []
    if facts["status"] != "verified":
        result.append("authorization required" if "authorization" in facts.get("error", "") else facts["status"])
    if facts.get("ahead", 0):
        result.append("unpushed: preserve and push the local commits; no remote copy verified")
        if blocked:
            result.append("blocked local commit: resolve the hold without discarding this checkout")
    if facts.get("temporary") and (facts.get("pushed") is not True or facts.get("dirty")):
        result.append("temporary sole-copy risk: preserve work in a durable authorized location")
    if facts.get("dirty"):
        result.append("uncommitted work: preserve local changes")
    return result


class Sweep:
    def __init__(self):
        self.stop = threading.Event()
        self.cursor = 0
        self.location_offsets = {}

    async def run(self, store, notify):
        worker = asyncio.create_task(asyncio.to_thread(self.check, store, notify))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            self.stop.set()
            await worker
            raise

    def check(self, store, notify):
        if self.stop.is_set():
            return
        roots = approved_roots()
        parameters = inspect.signature(store.list_features).parameters
        features = (
            store.list_features(include_archived=True) if "include_archived" in parameters else store.list_features()
        )
        if not features:
            return
        # Store reads retain the board store's own timeout; budget location work
        # separately so a large board listing cannot starve every probe forever.
        deadline = time.monotonic() + 15
        cache = {}
        checked = 0
        start = self.cursor % len(features)
        for offset in range(min(len(features), 20)):
            if self.stop.is_set() or time.monotonic() >= deadline:
                break
            feature = features[(start + offset) % len(features)]
            self.cursor = start + offset + 1
            # Listings omit comments on ordinary cards; the ledger API includes them.
            ledger = (
                store.list_work_locations(feature["id"])
                if hasattr(store, "list_work_locations")
                else feature.get("work_locations") or {}
            )
            # Pathless pushed reports are remote evidence, not external checkouts.
            entries = [entry for entry in ledger.get("entries", []) if entry.get("path", "").strip()]
            if not entries or ledger.get("errors"):
                continue
            first = self.location_offsets.get(feature["id"], 0) % len(entries)
            for index in range(min(len(entries), 20 - checked)):
                if self.stop.is_set() or time.monotonic() >= deadline:
                    return
                entry = entries[(first + index) % len(entries)]
                self.location_offsets[feature["id"]] = first + index + 1
                try:
                    self._check_entry(store, notify, feature, entry, roots, deadline, cache)
                except Exception:  # noqa: BLE001 — one location's probe/ledger error must not end the pass
                    log.warning("[project_board] work-location check failed for %s", feature["id"], exc_info=True)
                checked += 1
            if checked >= 20:
                break

    def _check_entry(self, store, notify, feature, entry, roots, deadline, cache):
        key = tuple(entry.get(k, "") for k in ("path", "repo", "branch", "sha"))
        if key not in cache:
            cache[key] = probe(entry, roots, min(deadline, time.monotonic() + 5))
        facts = cache[key]
        risk = risks(facts, feature.get("blocked"))
        old = entry.get("last_check", {})
        # Alert (and re-alert) only on a material RISK transition. A bare HEAD-sha move is
        # not a risk: the first observation of a clean, sha-less entry, or any commit-and-push
        # that advances the recorded sha, must never fire a spurious "risk resolved". The cycle
        # bumps on each transition so a recovered-then-recurring risk re-notifies past dedup.
        transition = risk != old.get("risks", [])
        cycle = old.get("cycle", 0) + int(transition)
        check = {**facts, "risks": risk, "cycle": cycle, "checked_at": time.time()}
        work_snapshot.cache_location(feature["id"], entry, check)
        # Never erase confirmed push evidence: an unverifiable probe (filesystem off, path
        # outside approved roots, non-HTTPS upstream, timeout) leaves pushed None and retains
        # the recorded verdict; only a verified probe overwrites it with a fresh True/False.
        pushed = facts["pushed"] if facts["pushed"] is not None else entry.get("pushed")
        state = {k: v for k, v in entry.items() if k in work_locations.FIELDS}
        state.update(pushed=pushed, last_check=check)
        # Identity remains the recorded identity; observed identity lives in last_check.
        if self.stop.is_set():
            return
        # Spend a permanent ledger revision only on a MATERIAL change — the risk set, the
        # coarse probe status, or the persisted pushed verdict. Probe wiggle (upstream_sha,
        # behind, error text) within the same status and risk set refreshes nothing durable,
        # so it must not accrue revisions against the card-wide MAX_REVISIONS history cap.
        material = transition or facts.get("status") != old.get("status") or pushed != entry.get("pushed")
        if material:
            store.record_work_location(feature["id"], state)
        if transition:
            incident = hashlib.sha256(json.dumps([key, cycle, risk]).encode()).hexdigest()
            notify(
                feature["id"],
                f"Work location {entry['path']}: {', '.join(risk) or 'risk resolved'}. "
                f"Evidence: {json.dumps(facts, sort_keys=True)}",
                incident="work-location:" + incident,
                store=store,
            )

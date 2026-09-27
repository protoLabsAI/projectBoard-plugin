"""Preflight gate edge of the board loop (extracted from loop.py, #268).

Behavior-preserving move: these methods were lifted verbatim from ``BoardLoop``
and run as a mixin on the assembled ``BoardLoop`` in :mod:`.core`. Cross-edge
``self.<method>()`` calls resolve through the MRO, unchanged. The shared loop
kernel (constants, helpers, process-stable state) is re-exported from
:mod:`._common`; rebindable seams are read through the live package (``_loop``)
so tests that monkeypatch ``project_board.loop.<name>`` still take effect.
"""

from __future__ import annotations

import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace
from ..projects import orphaned_cards

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


class PreflightMixin:
    async def _maybe_preflight(self) -> None:
        """Re-run each project's gate preflight while it hasn't passed, throttled per
        project (#90). Once a project passes, the pass stands for the checkout commit it
        was reached on (#456). A healthy environment doesn't lose its toolchain on its own,
        and a per-PR gate failure is handled in the drive, not here. When the checkout
        moves, one re-check runs in the background while dispatch goes on. A run that timed
        out counts as a pass here: it is indeterminate and is not repeated on the same
        commit. At most one preflight per project runs at a time. Runs for every project
        with ready work, every project still marked failed, AND every project this loop
        holds cards for. The last two cover a project whose ready work got HELD (and so
        dropped out of `ready`): it still re-checks and can recover."""
        if not self.preflight:
            return
        store = self._store()
        # Store-only scan — off the event loop (#258).
        names = list(await asyncio.to_thread(self._ready_projects, store))
        seen = set(names)
        now = time.monotonic()
        # A failed project may have no ready work left (its cards got held) — keep
        # re-checking it so it can recover and release those holds.
        for name, st in self._preflight_state.items():
            if isinstance(st, str) and name not in seen:
                seen.add(name)
                names.append(name)
        # …and so must a project whose FAILURE verdict is gone while its holds remain. A
        # registry change resets a changed project's verdict (reload). Keyed on verdicts
        # alone, that stranded its held cards: no ready work, no failed state, never
        # re-checked, held until a restart (#393). Once it has been checked, it is throttled
        # like a known failure, so a checkout that gives no verdict (not at base) is not
        # re-smoked every tick.
        for name, held in self._preflight_held.items():
            if not held or name in seen:
                continue
            if name in self._last_preflight and (now - self._last_preflight[name]) < max(self.interval, 60.0):
                continue
            seen.add(name)
            names.append(name)
        ran = False
        for name in names:
            feature = {"project": name}
            cmd = self._preflight_cmd_for(feature)
            if not cmd:
                self._preflight_state[name] = True  # nothing to smoke → runnable
                continue
            repo = self._repo_for(feature)
            base = self._base_branch_for(feature)
            state = self._preflight_state.get(name)
            if state is True:
                # A non-failing verdict stands for the commit it was reached on (#456).
                # That covers a green run and an indeterminate one (a timeout). When the
                # checkout moves, ONE re-check runs, in the background. Dispatch goes on
                # under the old verdict meanwhile, and a red result holds the project from
                # the next claim scan. A verdict with no recorded commit (git couldn't say,
                # or nothing was smoked) stays for the run, as every pass used to.
                known = self._preflight_sha.get(name)
                if not known or name in self._preflight_tasks:
                    continue
                sha = await worktree.checkout_head_sha(repo)
                if not sha or sha == known:
                    continue
                log.info(
                    "[project_board] preflight[%s]: the checkout moved (%s → %s) — re-checking in the background",
                    name,
                    known[:10],
                    sha[:10],
                )
                self._last_preflight[name] = now
                self._start_preflight(name, cmd, repo, base, sha)
                continue
            # First check runs immediately (state is None); re-checks of a KNOWN-failed
            # preflight are throttled so a slow gate isn't hammered every tick.
            if state is not None and (now - self._last_preflight.get(name, 0.0)) < max(self.interval, 60.0):
                continue
            self._last_preflight[name] = now
            ran = True
            # Single-flight (#456): a caller that finds a run already in flight for this
            # project awaits it, rather than starting a second gate in the same checkout.
            # Shielded, so a caller that is cancelled does not cancel the run the other
            # caller is waiting on. stop() cancels runs still in flight.
            await asyncio.shield(self._start_preflight(name, cmd, repo, base))
        if ran:
            # Surface the verdicts on /status (#255) — a board that stops picking work
            # up must be able to say why without the operator reading the log.
            self._publish_preflight_health()

    def _start_preflight(self, name: str, cmd: str, repo: str, base: str, sha: str | None = None) -> asyncio.Task:
        """Project ``name``'s running preflight, started if none is in flight (#456). Every
        caller shares one task per project, so two callers never run two gates at once in
        the same checkout. The task leaves ``_preflight_tasks`` when it finishes, and its
        verdict is published to /status then. That matters for a background re-check,
        which nobody awaits."""
        task = self._preflight_tasks.get(name)
        # Shared only when the run in flight smokes the SAME command in the SAME checkout. A
        # registry save that changes the project's gate or repo resets its verdict, and the
        # new routing must get its own run, not the old command's answer.
        if task is not None and not task.done() and self._preflight_task_key.get(name) == (cmd, repo):
            log.info("[project_board] preflight[%s]: a run is already in flight — waiting for its verdict", name)
            return task
        run = self._preflight_run_gen[name] = self._preflight_run_gen.get(name, 0) + 1
        task = asyncio.create_task(self._preflight(name, cmd, repo, base, sha=sha, run=run), name=f"preflight[{name}]")
        self._preflight_tasks[name] = task
        self._preflight_task_key[name] = (cmd, repo)

        def _done(t: asyncio.Task, n: str = name) -> None:
            if self._preflight_tasks.get(n) is t:
                self._preflight_tasks.pop(n, None)
                self._preflight_task_key.pop(n, None)
            if not t.cancelled() and t.exception() is not None:
                log.warning("[project_board] preflight[%s] crashed: %s", n, t.exception())
            try:
                self._publish_preflight_health()
            except Exception:  # noqa: BLE001 — publishing status must never break a preflight
                log.debug("[project_board] preflight health publish failed", exc_info=True)

        task.add_done_callback(_done)
        return task

    def _publish_preflight_health(self) -> None:
        """Send the preflight verdicts, the slow-gate flags (#456) and the unwinnable-oracle
        flags (#459) to /status."""
        health.publish_preflight(
            self._preflight_state,
            self._preflight_dirty,
            slow=self._preflight_slow,
            oracle=self._oracle_unwinnable,
        )

    async def _preflight(
        self, name: str, cmd: str, repo: str, base: str = "", *, sha: str | None = None, run: int | None = None
    ) -> None:
        """Smoke-run project ``name``'s gate on its base checkout. Sets
        ``self._preflight_state[name]``: ``True`` when the gate exits 0 (runnable), a
        reason string on a CLEAN non-zero exit or a launch failure (broken environment →
        hold THIS project's work). A TIMEOUT is indeterminate → allow (a slow gate must
        not wedge the board). Releases this project's holds on recovery.

        A DIRTY checkout yields NO VERDICT AT ALL (#255, corrected in #300). Coders only
        touch worktrees, so the main checkout normally still sits at base — but the
        OPERATOR edits it by hand, and then whatever the gate just did was about their
        uncommitted work, not about the base every worktree branches from. That cuts BOTH
        ways, which the first cut of this got wrong by only distrusting a red result:

        * a red gate on a dirty tree must not CONVICT the base (freezing real work over a
          local edit, whose only symptom on the board is an empty ``selected: []``), and
        * a green gate on a dirty tree must not ACQUIT it either — an operator's local fix
          can make a genuinely broken base pass, and releasing the holds on that evidence
          dispatches coders onto a base no gate has actually cleared.

        So on dirt this records the dirt for ``/status``, logs, and returns WITHOUT
        touching ``_preflight_state`` or this project's holds: a project already held for
        a clean red stays held (only a clean green may release it), and one that was never
        held is not newly held (state stays ``None``, so the claim scan keeps dispatching
        — the posture a timeout already had). Fail-closed and fail-open both keep their
        meaning, and each is decided only on evidence that supports it.

        ``sha`` is the checkout commit the verdict describes, read here when not given
        (#456). A non-failing verdict records it, and ``_maybe_preflight`` re-checks only
        once the checkout moves. A run cut off by ``preflight_timeout_s`` is recorded as
        SLOW: it is indeterminate, so dispatch is allowed, it is warned about once, it shows
        on /status, and it is not re-run until the checkout moves. When the command is the
        project's gate, the run's duration is kept for the coder.solve() oracle guard (#459)."""
        if sha is None:
            sha = await worktree.checkout_head_sha(repo)
        is_gate = cmd == self._local_gate_cmd_for({"project": name})
        started = time.monotonic()
        log.info("[project_board] preflight[%s]: smoking the gate on clean base — %s", name, cmd)
        try:
            proc = await worktree.spawn_shell(
                cmd,
                cwd=repo,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                # Whole-tree kill on a timeout or cancel (#423): this runs in the operator's
                # base checkout, so an orphaned install left behind here is the worst kind.
                out, _ = await worktree.communicate_or_kill(proc, timeout=self.preflight_timeout)
            except asyncio.TimeoutError:
                if self._preflight_superseded(name, run):
                    return
                self._record_preflight_timeout(name, cmd, sha, is_gate)
                return
            if self._preflight_superseded(name, run):
                return
            if is_gate:
                self._record_gate_seconds(name, time.monotonic() - started, False)
            # The dirt probe runs BEFORE the exit code is read, because it decides
            # whether the exit code means anything at all — for a pass exactly as much
            # as for a failure (see the docstring).
            dirt = await worktree.base_checkout_dirt(repo, base)
            if dirt:
                self._preflight_dirty[name] = dirt
                log.warning(
                    "[project_board] preflight[%s]: the checkout at %s is NOT at base (%s), so the gate "
                    "just ran against those local edits — no verdict either way. State and holds are "
                    "left exactly as they were (a project held for a clean red stays held; an unheld one "
                    "keeps dispatching). Commit or stash to get a real verdict. Gate exited %s.",
                    name,
                    repo,
                    dirt,
                    proc.returncode,
                )
                if sha and self._preflight_state.get(name) is True:
                    # A background re-check on a dirty checkout (#456) keeps the old verdict,
                    # and it has now looked at this commit. Without this, every tick would
                    # re-run the gate in the background until the operator commits.
                    self._preflight_sha[name] = sha
                return
            self._preflight_dirty.pop(name, None)
            if proc.returncode == 0:
                if isinstance(self._preflight_state.get(name), str):
                    log.info("[project_board] preflight[%s] RECOVERED — gate runnable again, releasing held work", name)
                self._preflight_failed_at.pop(name, None)
                self._preflight_state[name] = True
                if sha:
                    self._preflight_sha[name] = sha
                self._preflight_slow.pop(name, None)  # it finished in time on this commit
                await asyncio.to_thread(self._release_preflight_holds, name)
                return
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            text = text or f"gate exited {proc.returncode} with no output"
            if proc.returncode in _GATE_NOT_FOUND_EXITS:
                # The gate never ran (#3585). Say so on the LAST line — the one the hold
                # stamps on each card — instead of leaving a bare shell error there.
                text += "\n" + _missing_gate_command_hint(name, cmd, text)
            if self._record_preflight_failure(name, text):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — the gate does not pass on clean base; "
                    "HOLDING that project's work until the environment is fixed:\n%s",
                    name,
                    text,
                )
        except asyncio.CancelledError:
            if self._shutting_down:
                log.info("[project_board] preflight[%s] cancelled by shutdown — no verdict", name)
                return
            raise
        except Exception as exc:  # noqa: BLE001 — a gate that CANNOT LAUNCH is the broken-env case we must catch
            if self._preflight_superseded(name, run):
                return
            reason = f"gate command could not run: {exc}"
            # A missing CHECKOUT raises the same FileNotFoundError (the cwd); only name the
            # command when the checkout is there.
            if isinstance(exc, FileNotFoundError) and repo and os.path.isdir(repo):
                reason += "\n" + _missing_gate_command_hint(name, cmd, "")
            if self._record_preflight_failure(name, reason):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — %s; HOLDING that project's work until fixed.",
                    name,
                    self._preflight_state[name],
                )

    def _preflight_superseded(self, name: str, run: int | None) -> bool:
        """Whether this run was replaced by a newer one for the same project (#467 review).
        A registry save that changes a project's gate starts a fresh run while the old one
        may still be going. Only the current run may write a verdict, so a late answer to
        the OLD command never overwrites the new one, even if the new run has already
        finished. ``run`` is the number ``_start_preflight`` gave this run; a direct call
        (``run`` None) is always current."""
        if run is None or run == self._preflight_run_gen.get(name):
            return False
        log.info("[project_board] preflight[%s]: superseded by a newer run — this result is dropped", name)
        return True

    def _record_gate_seconds(self, name: str, seconds: float, lower_bound: bool) -> None:
        """Keep the gate's measured duration, and when it was measured, for the coder.solve()
        oracle guard (#459). The guard is re-evaluated from this on every call."""
        self._gate_seconds[name] = (seconds, lower_bound)
        self._gate_measured_at[name] = time.monotonic()

    def _record_preflight_timeout(self, name: str, cmd: str, sha: str, is_gate: bool) -> None:
        """A preflight cut off at ``preflight_timeout_s`` (#456). This is indeterminate, so
        dispatch is allowed, as before. But a command that can't finish inside the timeout
        will never finish inside it, and running it again only makes every dispatch wait the
        full timeout for the same non-answer. So the verdict is cached for this commit and
        not re-run until the checkout moves. The project shows as slow on /status and in the
        setup advisories, and the first time it happens the log names the fix
        (``preflight_cmd``). When the command is the gate, the timeout is also a lower bound
        on the gate's duration, which the coder.solve() oracle guard reads (#459).

        A project already held for a clean RED keeps its hold (#467 review). A timeout is no
        evidence that the gate recovered, so it can't release the hold, and it isn't cached,
        so the throttled re-check keeps running until a clean green releases it."""
        if is_gate:
            self._record_gate_seconds(name, self.preflight_timeout, True)
        self._preflight_slow[name] = {"cmd": cmd, "timeout_s": self.preflight_timeout, "sha": sha}
        if isinstance(self._preflight_state.get(name), str):
            log.warning(
                "[project_board] preflight[%s] timed out (%ss) on a project held for a failing gate — "
                "no verdict, so it stays held until the gate passes",
                name,
                self.preflight_timeout,
            )
            return
        self._preflight_state[name] = True
        if sha:
            self._preflight_sha[name] = sha
        if name in self._preflight_slow_warned:
            log.info(
                "[project_board] preflight[%s] timed out again (%ss) — indeterminate, allowing dispatch",
                name,
                self.preflight_timeout,
            )
            return
        self._preflight_slow_warned.add(name)
        log.warning(
            "[project_board] preflight[%s] timed out (%ss) — indeterminate, allowing dispatch. `%s` can't finish "
            "inside preflight_timeout_s, so the board won't run it again until the checkout moves%s. Set a cheap "
            "`preflight_cmd` for this project (lint + an import check) to get a real verdict.",
            name,
            self.preflight_timeout,
            cmd,
            f" off {sha[:10]}" if sha else "",
        )

    def _record_preflight_failure(self, name: str, reason: str) -> bool:
        """Set project ``name``'s failure ``reason`` and say whether to log it in full
        (#263). The gate tail is multi-KB diagnostic signal exactly once per DISTINCT
        failure — but a held project re-checks every ~60s, and an unchanged failure
        re-logged at ERROR each time buries the log without adding anything. First or
        DIFFERENT reason → True (caller emits the full ERROR); identical repeat →
        emits a one-line "still held" WARNING here and returns False."""
        prev = self._preflight_state.get(name)
        now = time.monotonic()
        self._preflight_state[name] = reason
        if prev != reason:
            self._preflight_failed_at[name] = now
            return True
        held = int(now - self._preflight_failed_at.get(name, now))
        log.warning(
            "[project_board] preflight[%s] still held (%ds) — same failure as last check, tail already logged",
            name,
            held,
        )
        return False

    def _hold_ready_for_preflight(self) -> None:
        """Flag every ready feature whose PROJECT's preflight failed blocked with that
        project's reason (#90), so the hold shows on the board instead of a silent stall.
        Features in projects whose gate CAN run are left alone — a broken gate in project
        A never holds project B."""
        store = self._store()
        for f in store.list_features(state="ready"):
            fid = f["id"]
            name = self._project_name(f)
            reason = self._preflight_state.get(name)
            if not isinstance(reason, str):
                continue  # this feature's project can run its gate (or hasn't been checked)
            held = self._preflight_held.setdefault(name, set())
            if fid in held or f.get("blocked"):
                continue
            tail = reason.splitlines()[-1][:200]
            short = f"{PREFLIGHT_BLOCK_PREFIX} — the coder environment can't run the gate: {tail}"
            try:
                # Its own class (#3585), not whatever `classify()` makes of the gate's
                # tail — that fell through to `terminal`, "needs a human, never clears",
                # on a card this loop releases itself once the gate runs again.
                store.flag_blocked(fid, short, category=PREFLIGHT_HOLD_CLASS)
                held.add(fid)
                log.info("[project_board] preflight hold: flagged %s blocked (project %s gate not runnable)", fid, name)
            except Exception:  # noqa: BLE001 — a hold that can't be recorded must not kill the tick
                log.warning("[project_board] preflight hold: flag_blocked failed for %s", fid, exc_info=True)

    def _release_preflight_holds(self, name: str) -> None:
        """Clear the blocks this loop placed for project ``name``'s failed preflight (only
        those — never clobber a feature blocked for another reason)."""
        held = self._preflight_held.get(name)
        if not held:
            return  # nothing to release — don't build the store (it may need a CLI/DB
            # that isn't present) just to iterate an empty set. A clean preflight (the
            # common path) must never touch the store: the resulting error would be
            # caught by _preflight's outer except and masquerade as a gate failure.
        store = self._store()
        for fid in list(held):
            try:
                store.clear_blocked(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] preflight release: clear_blocked failed for %s", fid, exc_info=True)
        self._preflight_held.pop(name, None)

    # ── base-checkout freshness (#452) ───────────────────────────────────────────
    async def _refresh_base_checkouts(self) -> None:
        """Fetch each project's base and fast-forward its MAIN checkout when that is safe
        (clean, on the base branch, nothing local the remote lacks) — the health sweep's
        step (a2). Worktrees were always cut from ``origin/<base>``, but the checkout the
        agent READS (and the gate preflight smokes) never moved: an agent read a v0.7.1
        tree an hour after v0.9.0 shipped. A checkout that can't be moved safely is left
        exactly as it is and reported ``stale`` on ``/status`` with the reason.

        Skips a checkout whose registration-time gate smoke is running right now (the
        registry's per-checkout smoke lock), so a fast-forward never lands under a gate."""
        if not self.base_refresh:
            return
        from ..project_registry import _SMOKE_LOCKS

        results: dict[str, dict] = {}
        done: dict[tuple[str, str], dict] = {}
        for name in list(self._projects):
            repo = self._repo_for({"project": name})
            base = self._base_branch_for({"project": name})
            if not repo or not os.path.exists(os.path.join(repo, ".git")):
                continue  # no checkout: the repo setup check owns that
            key = (os.path.realpath(repo), base)
            if key not in done:
                lock = _SMOKE_LOCKS.get(str(Path(repo).expanduser().resolve()))
                if lock is not None and lock.locked():
                    continue  # a registration gate smoke is running in this checkout — next sweep
                done[key] = await worktree.refresh_base_checkout(repo, base)
                state = done[key]["state"]
                # WARN once per distinct reason; the sweep re-checks every few minutes, and an
                # unchanged stale checkout re-logged each time would bury the log.
                seen = self.__dict__.setdefault("_base_stale_logged", {})
                if state == "stale" and seen.get(key) != done[key]["detail"]:
                    log.warning("[project_board] base checkout for %s is stale: %s", name, done[key]["detail"])
                if state == "stale":
                    seen[key] = done[key]["detail"]
                else:
                    seen.pop(key, None)
                if state == "unknown":
                    log.info("[project_board] base checkout for %s not refreshed: %s", name, done[key]["detail"])
            results[name] = {
                "repo": repo,
                "base": base,
                **done[key],
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        health.publish_base_checkouts(results)

    async def _publish_orphaned_cards(self, store) -> None:
        """Publish the live cards whose project label no longer resolves (#454) for
        ``/status`` — the health sweep's step (a3). Read-only: a card is never re-homed for
        the operator, only named with the call that would do it."""
        try:
            feats = await asyncio.to_thread(store.list_features)
        except store_mod.BoardTimeout:
            raise
        except BoardError as exc:
            log.warning("[project_board] sweep: could not read cards for the orphaned-project check: %s", exc)
            return
        orphans = orphaned_cards(feats, self._projects, self._default_project)
        if orphans:
            log.warning(
                "[project_board] %d card(s) carry a project that is not in project_board.projects: %s",
                len(orphans),
                ", ".join(f"{o['id']} ({o['project']})" for o in orphans[:10]),
            )
        health.publish_orphaned_cards(orphans)

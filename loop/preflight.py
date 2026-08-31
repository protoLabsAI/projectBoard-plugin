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

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


class PreflightMixin:
    async def _maybe_preflight(self) -> None:
        """Re-run each project's gate preflight while it hasn't passed, throttled per
        project (#90). Once a project passes it stays passed for the run (a healthy env
        doesn't spontaneously lose its toolchain; a per-PR gate failure is handled in the
        drive, not here). Runs for every project with ready work AND every project still
        marked failed — the latter so a project whose ready work got HELD (and so dropped
        out of `ready`) still re-checks and can recover."""
        if not self.preflight:
            return
        store = self._store()
        # Store-only scan — off the event loop (#258).
        names = list(await asyncio.to_thread(self._ready_projects, store))
        seen = set(names)
        # A failed project may have no ready work left (its cards got held) — keep
        # re-checking it so it can recover and release those holds.
        for name, st in self._preflight_state.items():
            if isinstance(st, str) and name not in seen:
                seen.add(name)
                names.append(name)
        now = time.monotonic()
        ran = False
        for name in names:
            cmd = self._local_gate_cmd_for({"project": name})
            if not cmd:
                self._preflight_state[name] = True  # nothing to smoke → runnable
                continue
            state = self._preflight_state.get(name)
            if state is True:
                continue  # already passed this run
            # First check runs immediately (state is None); re-checks of a KNOWN-failed
            # preflight are throttled so a slow gate isn't hammered every tick.
            if state is not None and (now - self._last_preflight.get(name, 0.0)) < max(self.interval, 60.0):
                continue
            self._last_preflight[name] = now
            ran = True
            await self._preflight(
                name,
                cmd,
                self._repo_for({"project": name}),
                self._base_branch_for({"project": name}),
            )
        if ran:
            # Surface the verdicts on /status (#255) — a board that stops picking work
            # up must be able to say why without the operator reading the log.
            health.publish_preflight(self._preflight_state, self._preflight_dirty)

    async def _preflight(self, name: str, cmd: str, repo: str, base: str = "") -> None:
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
        meaning, and each is decided only on evidence that supports it."""
        log.info("[project_board] preflight[%s]: smoking the gate on clean base — %s", name, cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=repo,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.preflight_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning(
                    "[project_board] preflight[%s] timed out (%ss) — indeterminate, allowing dispatch",
                    name,
                    self.preflight_timeout,
                )
                self._preflight_state[name] = True
                return
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
                return
            self._preflight_dirty.pop(name, None)
            if proc.returncode == 0:
                if isinstance(self._preflight_state.get(name), str):
                    log.info("[project_board] preflight[%s] RECOVERED — gate runnable again, releasing held work", name)
                self._preflight_failed_at.pop(name, None)
                self._preflight_state[name] = True
                await asyncio.to_thread(self._release_preflight_holds, name)
                return
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            text = text or f"gate exited {proc.returncode} with no output"
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
            if self._record_preflight_failure(name, f"gate command could not run: {exc}"):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — %s; HOLDING that project's work until fixed.",
                    name,
                    self._preflight_state[name],
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
                store.flag_blocked(fid, short)
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

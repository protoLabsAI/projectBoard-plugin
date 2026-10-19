"""Preflight gate edge of the board loop (extracted from loop.py, #268).

Behavior-preserving move: these methods were lifted verbatim from ``BoardLoop``
and run as a mixin on the assembled ``BoardLoop`` in :mod:`.core`. Cross-edge
``self.<method>()`` calls resolve through the MRO, unchanged. The shared loop
kernel (constants, helpers, process-stable state) is re-exported from
:mod:`._common`; rebindable seams are read through the live package (``_loop``)
so tests that monkeypatch ``project_board.loop.<name>`` still take effect.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace
from .. import host_friction
from ..projects import orphaned_cards

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


# ── preflight failure diagnostics (designSystem 2026-10-11) ───────────────────────
# A held card used to carry only the LAST line of the gate's output. For a pnpm chain
# that line is `Exit status 1`: it names no step and no package, and the full tail went
# only to the daemon log, which the agent cannot read. These helpers find the failing
# step, the line that says what broke, and a bounded, secret-scrubbed tail that the
# card comment, `/status` and the board tools can show.

#: The marker the preflight prints before each step of a plain `a && b && c` gate.
_STEP_MARKER = "[pb-preflight] step"
_STEP_MARKER_RE = re.compile(r"^\[pb-preflight\] step (\d+)/(\d+)(?::.*)?$")
#: How much of the output a hold comment and the tool surfaces carry.
PREFLIGHT_TAIL_LINES = 30
_TAIL_LINE_CHARS = 300
_TAIL_TOTAL_CHARS = 4000
#: Lines that end a failing run but say nothing about WHAT failed: shell and package-manager
#: sign-offs, and npm's boilerplate. npm's log-path lines carry a timestamp that changes on
#: every run, so as a headline they would make every re-check look like a new failure.
_NOISE_LINE_RE = re.compile(
    r"^(?:exit status \d+|…\(truncated\)…|(?:\S+ )?elifecycle\s+command failed(?: with exit code \d+)?\.?"
    r"|command failed with exit code \d+\.?|error: command failed with exit code \d+\.?|\s*"
    r"|npm (?:ERR!|error)\s*"
    r"|npm (?:ERR!|error)\s+(?:code|errno|path|workspace|location|command|syscall|signal|exit status)\b.*"
    r"|.*a complete log of this run can be found in.*"
    r"|.*_logs/\S*-debug(?:-\d+)?\.log\s*"
    r"|npm (?:ERR!|error)\s+(?:this is probably not a problem with npm|there is likely additional logging).*"
    r")$",
    re.I,
)
#: Lines that say what broke, most telling first: a pnpm/npm error that names the failing
#: package or script, or the #3585 missing-command hint; then a missing module, a failed
#: test or a compiler error; then any error-looking line.
_ERROR_LINE_TIERS = (
    re.compile(r"ERR_PNPM_\w+|npm (?:ERR!|error)\s|\bERR!|^gate command not found:"),
    re.compile(
        r"Cannot find module|Module not found|Can't resolve|^FAIL(?:ED)?\b|\berror TS\d+|^E\s{2,}\S"
        r"|^\w*(?:Syntax|Type|Reference|Import|ModuleNotFound|Assertion)Error\b"
    ),
    re.compile(
        r"\berror\b|Error:|MODULE_NOT_FOUND|\bnot found\b|No such file or directory|Traceback|\bfailed\b|ELIFECYCLE",
        re.I,
    ),
)
#: What varies between two runs of the same failure, taken out of a headline before it is
#: hashed into the failure key: timestamps, durations, hex addresses, temp and npm-log paths,
#: and any remaining number.
_KEY_NOISE = (
    re.compile(r"\d{4}-\d\d-\d\d[T _]\d\d[:_]\d\d(?:[:_]\d\d(?:[._]\d+)?)?Z?"),
    re.compile(r"\b\d\d:\d\d:\d\d(?:\.\d+)?\b"),
    re.compile(r"\b0x[0-9a-fA-F]+\b"),
    re.compile(r"(?:/private)?/(?:tmp|var/folders|var/tmp)/\S*|\S*_logs/\S*"),
    re.compile(r"\(?\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|[kKMG]i?B|bytes)\b\)?"),
    # A STANDALONE number (a count, a line:col, a port, an exit code). Digits inside an
    # identifier stay: `test_case[1]`, `test_v2.py`, `pkg1`, `v18` name a different thing.
    re.compile(r"(?<![\w.\[])\d+(?:\.\d+)*(?![\w\]])"),
)


def _key_text(line: str) -> str:
    """``line`` with everything that differs between two runs of the same failure taken out."""
    s = str(line or "")
    for pattern in _KEY_NOISE:
        s = pattern.sub("#", s)
    return " ".join(s.split())


#: Credential shapes scrubbed from gate output before it reaches a card, a tool or the
#: friction ledger. A key name only counts in an assignment or key/value shape, so prose
#: that merely mentions a token (`TokenExpiredError: jwt expired`) stays readable.
_SECRET_NAME = r"[\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|access[_-]?key)[\w-]*"
_SECRET_PATTERNS = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"(?i)(authorization\s*[\"']?\s*:\s*[\"']?\s*(?:bearer|basic|token)\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\"" + _SECRET_NAME + r"\"\s*:\s*\")[^\"]*(\")"), r"\1[REDACTED]\2"),
    (re.compile(r"(?i)(_authToken\s*=\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key|access-key)(?:\s+|=))(?!-)\S+"), r"\1[REDACTED]"),
    # userinfo: the password runs to the LAST `@` before the host, so `p@ss@db` is covered.
    (re.compile(r"(://[^/\s:@]+:)[^\s/]*@"), r"\1[REDACTED]@"),
    (
        re.compile(
            r"\b((?=[A-Z])[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)"
            r"(\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            r"\b((?=[A-Z])[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)"
            r"(:\s*)[\"']?[A-Za-z0-9_\-./+=]{12,}[\"']?"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+"), "[REDACTED]"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}|gh[pousr]_[A-Za-z0-9]{30,}"
            r"|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_\-]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}"
            r"|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_\-]{20,}|ya29\.[A-Za-z0-9_\-]+|npm_[A-Za-z0-9]{30,})"
        ),
        "[REDACTED]",
    ),
)


#: A lowercase or mixed-case assignment of a credential-named key (``password=hunter2``,
#: ``db_password: hunter2hunter2``, ``token: abc…``). ``key=value`` always counts (``==``
#: does not). ``key: value`` counts only when the value is quoted or is the last word on its
#: line, so prose after a colon (``TokenExpiredError: jwt expired``, ``GITHUB_TOKEN: set it
#: in .env``, ``password: must be at least 8 characters``) stays readable.
_ASSIGN_NAME = r"(?=[A-Za-z_])[\w.-]*?(?:password|passwd|secret|token|api[_-]?key|auth(?:orization)?(?![a-z]))[\w.-]*"
_ASSIGN_EQ_RE = re.compile(r"(?i)\b(" + _ASSIGN_NAME + r")(\s*(?<![=!<>])=(?!=)\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;]+)")
_ASSIGN_COLON_RE = re.compile(
    r"(?im)\b(" + _ASSIGN_NAME + r")(\s*:\s+)(\"[^\"]*\"|'[^']*'|[^\s\"',;]+)(?=\s*[,;}]?\s*$)"
)
#: Names that only LOOK like credentials: exception and type names, tokenizer settings.
_NOT_A_SECRET_NAME_RE = re.compile(r"(?i)(?:error|exception|warning|kind|type|izer|count|length|policy|expired?)$")


def _redact_assignment(m: re.Match) -> str:
    name, sep, value = m.group(1), m.group(2), m.group(3)
    if _NOT_A_SECRET_NAME_RE.search(name) or value.startswith("[REDACTED"):
        return m.group(0)
    return f"{name}{sep}[REDACTED]"


def _redact_gate_output(text: str) -> str:
    """``text`` with credential shapes replaced by ``[REDACTED]``. The plugin's own
    patterns always run; the host's audit redactor (which also knows the values its
    secrets manager handed out) runs too when this is loaded inside protoAgent. Never
    raises: the host redactor is optional and a failure there keeps the local result."""
    s = str(text or "")
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    for pattern in (_ASSIGN_EQ_RE, _ASSIGN_COLON_RE):
        s = pattern.sub(_redact_assignment, s)
    try:
        from graph.middleware.redaction import redact as host_redact
    except Exception:  # noqa: BLE001 — host-free, or an older host
        return s
    try:
        out = host_redact(s)
        return out if isinstance(out, str) else s
    except Exception:  # noqa: BLE001 — a redactor that errors must not lose the diagnostics
        return s


def _and_chain_steps(cmd: str) -> list[str]:
    """The steps of a plain ``a && b && c`` command, or ``[]`` when it is one step or
    anything more than a plain ``&&`` chain (``;``, ``||``, a subshell, a group, a
    substitution, a background ``&``, a heredoc, a newline). Quoted text is never split.
    Only a plain chain is marked: there, printing a marker before each step changes
    neither which steps run nor the exit status, so the gate means what it meant."""
    if os.name == "nt" or not cmd:
        return []  # cmd.exe quoting differs; the steps stay unmarked there
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\":
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        if cmd.startswith("&&", i):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if c == "&":
            # Only a redirection (`2>&1`, `&>file`) is fine; a lone `&` backgrounds a step.
            if (i > 0 and cmd[i - 1] == ">") or (i + 1 < n and cmd[i + 1] == ">"):
                buf.append(c)
                i += 1
                continue
            return []
        if c in ";`(){}\n\r" or cmd.startswith(("||", "<<", "[[", "]]"), i):
            return []  # `[[ a && b ]]` keeps its `&&` inside the test: never split there
        buf.append(c)
        i += 1
    if quote:
        return []
    parts.append("".join(buf))
    steps = [p.strip() for p in parts]
    if len(steps) < 2 or any(not s for s in steps):
        return []
    return steps


def _mark_steps(cmd: str) -> tuple[str, list[str]]:
    """``(command to run, steps)``. A plain ``&&`` chain gets a ``printf`` marker before
    each step, so the output says which step was running when the chain stopped. The
    markers are ``&&``-joined like the steps, so a failing step still stops the chain
    with its own exit status. Anything else runs unchanged with no steps."""
    steps = _and_chain_steps(cmd)
    if not steps:
        return cmd, []
    total = len(steps)
    run = " && ".join(
        f"printf '%s\\n' {shlex.quote(f'{_STEP_MARKER} {i}/{total}: {step}')} && {step}"
        for i, step in enumerate(steps, 1)
    )
    return run, steps


def _failing_step(output: str, steps: list[str]) -> tuple[int, int, str] | None:
    """``(index, total, step)`` of the last step whose marker the output shows — the
    step the chain stopped in — or None when the steps were not marked."""
    if not steps:
        return None
    last = 0
    for line in (output or "").splitlines():
        m = _STEP_MARKER_RE.match(line.strip())
        if m and int(m.group(2)) == len(steps):
            last = int(m.group(1))
    if not 1 <= last <= len(steps):
        return None
    return last, len(steps), steps[last - 1]


def _strip_step_markers(output: str) -> str:
    """``output`` without the preflight's own step markers."""
    return "\n".join(l for l in (output or "").splitlines() if not _STEP_MARKER_RE.match(l.strip()))


def _informative_line(text: str) -> str:
    """The one line of a failed gate's output that says the most about what broke.

    The last line is usually the shell's or pnpm's sign-off (``Exit status 1``,
    ``ELIFECYCLE Command failed``), which names nothing. Search from the bottom for a
    line that names the failing unit (a pnpm/npm error code, a missing module, the
    #3585 "gate command not found" hint), then for any error line, then for the last
    line that is not a sign-off. The last line itself is the final fallback."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return ""
    if lines[-1].startswith("gate command not found:"):
        return lines[-1]  # the #3585 hint names the command and the fix
    for tier in _ERROR_LINE_TIERS:
        for line in reversed(lines):
            if tier.search(line) and not _NOISE_LINE_RE.match(line):
                return line
    for line in reversed(lines):
        if not _NOISE_LINE_RE.match(line):
            return line
    return lines[-1]


def _output_tail(text: str, lines: int = PREFLIGHT_TAIL_LINES) -> str:
    """The last ``lines`` lines of ``text``, each cut at ``_TAIL_LINE_CHARS`` and the
    whole held to ``_TAIL_TOTAL_CHARS`` (oldest lines go first)."""
    rows = [l.rstrip() for l in (text or "").splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    rows = [r if len(r) <= _TAIL_LINE_CHARS else r[: _TAIL_LINE_CHARS - 1] + "…" for r in rows[-lines:]]
    while rows and sum(len(r) + 1 for r in rows) > _TAIL_TOTAL_CHARS:
        rows.pop(0)
    return "\n".join(rows)


def _preflight_failure_detail(text: str, *, step: tuple[int, int, str] | None, exit_code: int | None, cmd: str) -> dict:
    """What a held card, ``/status`` and the board tools show about a failed preflight.
    ``text`` is already redacted. ``key`` identifies the failure (its step and the line
    that says what broke) so the hold comments once per DISTINCT failure, not on every
    re-check whose output differs only in timings."""
    headline = _informative_line(text)
    detail = {
        "cmd": _redact_gate_output(cmd),
        "step": step[2] if step else "",
        "step_index": step[0] if step else 0,
        "steps": step[1] if step else 0,
        "exit_code": exit_code,
        "headline": headline,
        "tail": _output_tail(text),
    }
    # The key is built from the headline with timings, timestamps, addresses and numbers
    # taken out, so a re-check of the SAME failure keeps its key (one comment per card).
    raw_key = f"{detail['step_index']}|{detail['step']}|{exit_code}|{_key_text(headline)}"
    detail["key"] = hashlib.sha1(raw_key.encode("utf-8", "replace")).hexdigest()[:12]
    return detail


def _preflight_hold_line(detail: dict | None, reason: str) -> str:
    """The short text after the hold prefix on the card's ``blocked_reason``: the failing
    step when known, then the most telling line (not just the last one), cut to fit."""
    headline = (detail or {}).get("headline") or _informative_line(reason) or "gate failed with no output"
    headline = headline[:200]
    step = (detail or {}).get("step") or ""
    if not step:
        return headline
    if len(step) > 80:
        step = step[:79] + "…"
    return f"step {detail['step_index']}/{detail['steps']} `{step}` failed: {headline}"


def _preflight_hold_comment(name: str, detail: dict) -> str:
    """The card comment a preflight hold posts once per distinct failure: which step
    failed, the exit status, and the output's last lines. It does NOT start with
    ``blocked:``, so boot recovery (#186) still reads the hold's own reason as current."""
    if detail.get("step"):
        what = f"step {detail['step_index']}/{detail['steps']} `{detail['step']}` failed"
    else:
        what = f"`{detail.get('cmd') or 'the gate'}` failed"
    if detail.get("exit_code") is not None:
        what += f" (exit {detail['exit_code']})"
    tail = detail.get("tail") or "(no output)"
    return (
        f"gate preflight output (project {name!r}): {what} on the clean base checkout.\n"
        "This card is held because its project's gate can't run there, not because of the card. "
        "Nothing on the card needs to change: fix the project's gate environment (or local_gate_cmd), "
        "and the loop releases the hold once the gate passes.\n"
        f"Last {PREFLIGHT_TAIL_LINES} lines of output (secrets redacted):\n"
        f"```\n{tail}\n```\n"
        f"{_COMMENT_KEY_PREFIX}{detail.get('key', '')}"
    )


#: The last line of a hold comment: the failure key it was posted for. A restarted loop reads
#: it back off the card, so the same failure is not commented twice across a restart.
_COMMENT_KEY_PREFIX = "preflight-failure-key: "
_COMMENT_HEAD = "gate preflight output (project "


def _last_hold_comment_key(comments) -> str:
    """The failure key of the newest preflight hold comment in ``comments`` (str or dict
    rows, oldest first), or ``""``."""
    for c in reversed(list(comments or [])):
        text = (c.get("text") or c.get("body") or c.get("content") or "") if isinstance(c, dict) else str(c or "")
        if text.startswith(_COMMENT_HEAD):
            last = text.rstrip().rsplit("\n", 1)[-1]
            return last[len(_COMMENT_KEY_PREFIX) :].strip() if last.startswith(_COMMENT_KEY_PREFIX) else ""
    return ""


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
            failures=self._preflight_detail,
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
        # A plain `a && b && c` chain prints a marker before each step, so a red run says
        # which step stopped it. The chain, and so the gate's verdict, is unchanged.
        run_cmd, steps = _mark_steps(cmd)
        try:
            proc = await worktree.spawn_shell(
                run_cmd,
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
                self._preflight_detail.pop(name, None)
                self._preflight_friction.pop(name, None)
                self._preflight_state[name] = True
                if sha:
                    self._preflight_sha[name] = sha
                self._preflight_slow.pop(name, None)  # it finished in time on this commit
                await asyncio.to_thread(self._release_preflight_holds, name)
                return
            raw = (out or b"").decode("utf-8", "replace")
            # The step is read off the FULL output, before truncation can cut its marker.
            step = _failing_step(raw, steps)
            text = _strip_step_markers(raw).strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            text = text or f"gate exited {proc.returncode} with no output"
            if proc.returncode in _GATE_NOT_FOUND_EXITS:
                # The gate never ran (#3585). Say so on the LAST line — the one the hold
                # stamps on each card — instead of leaving a bare shell error there.
                text += "\n" + _missing_gate_command_hint(name, cmd, text)
            if step:
                text = f"failing step {step[0]}/{step[1]}: {step[2]}\n{text}"
            # The reason reaches /status, the card and the board tools: no credentials.
            text = _redact_gate_output(text)
            self._preflight_detail[name] = _preflight_failure_detail(
                text, step=step, exit_code=proc.returncode, cmd=cmd
            )
            self._friction_preflight_hold(name)
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
            reason = _redact_gate_output(reason)
            self._preflight_detail[name] = _preflight_failure_detail(reason, step=None, exit_code=None, cmd=cmd)
            self._friction_preflight_hold(name)
            if self._record_preflight_failure(name, reason):
                log.error(
                    "[project_board] PREFLIGHT[%s] FAILED — %s; HOLDING that project's work until fixed.",
                    name,
                    self._preflight_state[name],
                )

    def _friction_preflight_hold(self, name: str) -> None:
        """Record project ``name``'s red preflight in the host's friction ledger, once per
        DISTINCT failure (the same key the hold's card comment dedupes on), so a project
        re-checked every minute while it stays red adds one entry, not one per check."""
        detail = self._preflight_detail.get(name) or {}
        key = f"preflight|{name}|{detail.get('key', '')}"
        if self._preflight_friction.get(name) == key:
            return
        self._preflight_friction[name] = key
        where = (
            f"step {detail['step_index']}/{detail['steps']} `{detail['step']}`"
            if detail.get("step")
            else f"`{detail.get('cmd') or 'the gate'}`"
        )
        host_friction.record(
            "gate preflight fails on clean base, so the project's cards are held",
            f"project {name}: {where} failed (exit {detail.get('exit_code')}): {detail.get('headline', '')}"[:600],
            severity="major",
            key=key,
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
        A never holds project B.

        The card's ``blocked_reason`` names the failing step and the most telling line of
        the output, not just its last line (a pnpm chain ends in ``Exit status 1``). Each
        held card also gets ONE comment per distinct failure with the step, the exit status
        and the output's last lines, secret-scrubbed: the agent has no shell and cannot read
        the daemon log, so the card is where it must be able to read what broke. A card
        already held when the failure CHANGES gets the new output as another comment."""
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
            line = _preflight_hold_line(self._preflight_failure_for(name, reason), reason)
            short = f"{PREFLIGHT_BLOCK_PREFIX} — the coder environment can't run the gate: {line}"
            try:
                # Its own class (#3585), not whatever `classify()` makes of the gate's
                # tail — that fell through to `terminal`, "needs a human, never clears",
                # on a card this loop releases itself once the gate runs again.
                store.flag_blocked(fid, short, category=PREFLIGHT_HOLD_CLASS)
                held.add(fid)
                log.info("[project_board] preflight hold: flagged %s blocked (project %s gate not runnable)", fid, name)
            except Exception:  # noqa: BLE001 — a hold that can't be recorded must not kill the tick
                log.warning("[project_board] preflight hold: flag_blocked failed for %s", fid, exc_info=True)
        self._comment_preflight_holds(store)

    def _preflight_failure_for(self, name: str, reason: str) -> dict:
        """Project ``name``'s failure detail, built from ``reason`` when the run that set
        it recorded none (a verdict set some other way, as tests and older code paths do)."""
        detail = self._preflight_detail.get(name)
        if not detail:
            detail = self._preflight_detail[name] = _preflight_failure_detail(
                _redact_gate_output(reason), step=None, exit_code=None, cmd=self._preflight_cmd_for({"project": name})
            )
        return detail

    @staticmethod
    def _card_has_hold_comment(store, fid: str, key: str) -> bool:
        """Whether the card's newest hold comment was posted for failure ``key``. Read only
        for a card this process has not commented yet, i.e. after a restart; a store
        without comment reads, or a failed read, answers False (post the comment)."""
        read = getattr(store, "feature_comments", None)
        if read is None:
            return False
        try:
            return _last_hold_comment_key(read(fid)) == key
        except Exception:  # noqa: BLE001 — a failed read costs at most one repeated comment
            return False

    def _comment_preflight_holds(self, store) -> None:
        """Post the output comment on every card this loop holds for a failed preflight,
        once per distinct failure per card (``detail["key"]``: the step and the line that
        says what broke, so a re-check whose output differs only in timings stays quiet).
        Best-effort: a write that raises is retried next tick (``store.comment`` itself
        logs and swallows a failed ``br`` write), and the hold stands either way."""
        sent = self._preflight_commented
        for name, held in self._preflight_held.items():
            reason = self._preflight_state.get(name)
            if not held or not isinstance(reason, str):
                continue
            detail = self._preflight_failure_for(name, reason)
            body = ""
            for fid in sorted(held):
                if sent.get(fid) == detail["key"]:
                    continue
                if fid not in sent and self._card_has_hold_comment(store, fid, detail["key"]):
                    sent[fid] = detail["key"]  # posted before a restart: don't repeat it
                    continue
                body = body or _preflight_hold_comment(name, detail)
                try:
                    store.comment(fid, body)
                except Exception:  # noqa: BLE001 — the diagnostics comment is best-effort
                    log.warning(
                        "[project_board] preflight hold: could not comment the output on %s", fid, exc_info=True
                    )
                    continue
                sent[fid] = detail["key"]

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
                # Tracking records the original hold, not current ownership. An
                # explicit agent/operator hold may have replaced it since then.
                feature = store.get_feature(fid)
                if feature and feature.get("blocked_class") == PREFLIGHT_HOLD_CLASS:
                    store.clear_blocked(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] preflight release: clear_blocked failed for %s", fid, exc_info=True)
            self._preflight_commented.pop(fid, None)  # a later failure gets its own comment
        self._preflight_held.pop(name, None)

    # ── base-checkout freshness (#452) ───────────────────────────────────────────
    #: The most one refresh pass may take, all checkouts together. The fetches run
    #: concurrently; any still running at the cap are cancelled (their git tree killed)
    #: and reported `unknown`, so a slow remote can never hold a pass open.
    base_refresh_budget_s = 120.0

    def _start_base_refresh(self) -> None:
        """Start a refresh pass as its OWN task, off the tick (#452 review M2): a pass is
        network-bound, and run inline it would stall claims and reconciles behind every
        fetch — #462's starvation again. At most one pass runs at a time."""
        if not self.base_refresh:
            return
        task = getattr(self, "_base_refresh_task", None)
        if task is not None and not task.done():
            return
        self._base_refresh_task = asyncio.create_task(self._refresh_base_checkouts())

    def _base_refresh_owned(self, name: str, repo: str) -> tuple[bool, str]:
        """Whether the board may move project ``name``'s checkout at ``repo``, and why.

        Only a checkout the BOARD owns is refreshed by default (#452 review M2): one under
        the host's onboarding root (where onboarding and board registration clone), or one
        the board registered as a managed project. An operator's own checkout elsewhere
        (``~/dev/<repo>``, the one they work in) is never moved unless its project entry
        says ``base_refresh: true``; ``base_refresh: false`` opts any checkout out."""
        raw: dict = {}
        try:
            from ..project_registry import _host_onboarding, _live_section

            live = _live_section().get("projects")
            source = live if isinstance(live, dict) else self.cfg.get("projects")
            raw = dict((source or {}).get(name) or {}) if isinstance(source, dict) else {}
            _enabled, root = _host_onboarding()
        except Exception:  # noqa: BLE001 — no host: nothing is known to be the board's
            root = ""
        if "base_refresh" in raw:
            on = _knob_bool(raw, "base_refresh", False, strict=False)
            return on, "base_refresh set on the project"
        if str(raw.get("managed_project") or "").strip():
            return True, "registered by the board"
        if root:
            try:
                r = Path(root).expanduser().resolve()
                p = Path(repo).expanduser().resolve()
                if p == r or r in p.parents:
                    return True, "under the onboarding root"
            except OSError:
                pass
        return (
            False,
            "not a board-owned checkout (outside the onboarding root) — set base_refresh: true on the project to include it",
        )

    async def _refresh_one(self, repo: str, base: str) -> dict:
        """Refresh one checkout while HOLDING its registration smoke lock for the whole
        refresh, so neither can run under the other (#452 review M2). A checkout whose gate
        smoke is running right now is skipped for this pass, not waited on."""
        from ..project_registry import _SMOKE_LOCKS

        lock = _SMOKE_LOCKS.setdefault(str(Path(repo).expanduser().resolve()), asyncio.Lock())
        if lock.locked():
            return {"state": "unknown", "behind": 0, "detail": "a registration gate smoke is running in it — next pass"}
        async with lock:  # uncontended: acquired without yielding, so nothing slips in between
            return await worktree.refresh_base_checkout(repo, base)

    async def _refresh_base_checkouts(self) -> None:
        """Fetch each board-owned project's base and fast-forward its MAIN checkout when that
        is safe (clean, on the base branch, nothing local the remote lacks). Worktrees were
        always cut from ``origin/<base>``, but the checkout the agent READS (and the gate
        preflight smokes) never moved: an agent read a v0.7.1 tree an hour after v0.9.0
        shipped. A checkout that can't be moved safely is left exactly as it is and
        reported ``stale`` on ``/status`` with the reason; one the board doesn't own is
        reported ``skipped``."""
        if not self.base_refresh:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results: dict[str, dict] = {}
        jobs: dict[tuple[str, str], list[str]] = {}
        where: dict[str, tuple[str, str]] = {}
        for name in list(self._projects):
            repo = self._repo_for({"project": name})
            base = self._base_branch_for({"project": name})
            if not repo or not os.path.exists(os.path.join(repo, ".git")):
                continue  # no checkout: the repo setup check owns that
            where[name] = (repo, base)
            owned, why = self._base_refresh_owned(name, repo)
            if not owned:
                results[name] = {"state": "skipped", "behind": 0, "detail": why}
                continue
            jobs.setdefault((os.path.realpath(repo), base), []).append(name)
        tasks = {key: asyncio.create_task(self._refresh_one(where[names[0]][0], key[1])) for key, names in jobs.items()}
        if tasks:
            _done, pending = await asyncio.wait(tasks.values(), timeout=self.base_refresh_budget_s)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        seen = self.__dict__.setdefault("_base_stale_logged", {})
        for key, names in jobs.items():
            t = tasks[key]
            if t.cancelled():
                outcome = {
                    "state": "unknown",
                    "behind": 0,
                    "detail": f"not finished within the {self.base_refresh_budget_s:g}s refresh budget",
                }
            elif t.exception() is not None:
                outcome = {"state": "unknown", "behind": 0, "detail": f"base refresh failed: {t.exception()}"}
            else:
                outcome = t.result()
            # WARN once per distinct reason: the sweep re-checks every few minutes, and an
            # unchanged stale checkout re-logged each time would bury the log.
            if outcome["state"] == "stale" and seen.get(key) != outcome["detail"]:
                log.warning("[project_board] base checkout for %s is stale: %s", ", ".join(names), outcome["detail"])
            if outcome["state"] == "stale":
                seen[key] = outcome["detail"]
            else:
                seen.pop(key, None)
            if outcome["state"] == "unknown":
                log.info("[project_board] base checkout for %s not refreshed: %s", ", ".join(names), outcome["detail"])
            for name in names:
                results[name] = dict(outcome)
        for name, row in results.items():
            repo, base = where[name]
            row.update(repo=repo, base=base, checked_at=stamp)
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

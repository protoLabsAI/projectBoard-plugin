"""Loop-health facts the API can read without reaching into the running loop (#255).

The gate preflight is the board's most consequential piece of hidden state: one project
whose gate is red fail-closes THAT project's dispatch, and because the held cards drop
out of the ready scan the only thing an operator sees is a board that quietly stops
picking work up — ``claim_decision {"selected": []}``, tick after tick, with the reason
buried in the log. `/status` is where the board page already looks, so the loop publishes
its per-project preflight verdicts here and the route serves them.

Lives in its own module, attached to a process-stable ``sys.modules`` slot exactly like
coder_seam's monitor buffer (#178): the loop and the API routers are separate module
instances after a plugin reload, and a plain module global would leave the reader holding
an empty dict while the writer fills its own copy.
"""

from __future__ import annotations

import sys
import types

# Not prefixed with the plugin package's own name, so a host that purges `sys.modules`
# by package prefix on reload cannot purge the slot (see coder_seam._PROGRESS_SLOT_PREFIX).
_SLOT_PREFIX = "project_board.loop_health::"


def _slot_name() -> str:
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    return _SLOT_PREFIX + pkg


def _attach() -> dict:
    name = _slot_name()
    holder = sys.modules.get(name)
    prev = getattr(holder, "health", None)
    if isinstance(prev, dict):
        return prev
    holder = types.ModuleType(name)
    holder.__doc__ = "Process-stable holder for project_board's loop-health facts (#255)."
    holder.health = {}
    holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder.health


_health: dict = _attach()


def publish_preflight(
    state: dict,
    dirty: dict,
    *,
    slow: dict | None = None,
    oracle: dict | None = None,
    failures: dict | None = None,
    no_command: set[str] | None = None,
) -> None:
    """Called by the loop after each preflight pass with its live maps:
    ``state`` (project -> True | reason-string | None), ``dirty``
    (project -> why the checkout wasn't at base), ``slow`` (project -> {cmd, timeout_s,
    sha}: a preflight command that can't finish inside ``preflight_timeout_s``, #456),
    ``oracle`` (project -> why its coder.solve() oracle is unwinnable, #459) and
    ``failures`` (project -> {cmd, step, step_index, steps, exit_code, headline, tail}:
    what a held project's failed gate said, redacted). Only HELD projects keep a failure.

    ``passed`` is derived here as the ONE authoritative per-project pass signal: the
    projects whose gate ran CLEAN on the base (``state`` is True) with no outstanding
    caveat — not ``dirty`` (the checkout wasn't at base, so there is no verdict either
    way, #255/#300), not ``slow`` (a timeout is indeterminate, #456) and not an unwinnable
    ``oracle`` (#459), and not ``no_command`` (runnable without running a gate). A reader
    (the board page's checks dot) can then show green ONLY for a
    real pass instead of guessing from a board-wide "something was evaluated" flag: a held,
    dirty, slow, oracle or not-yet-checked project is simply absent from ``passed`` (#612)."""
    held = {n: r for n, r in state.items() if isinstance(r, str)}
    dirty_map = dict(dirty)
    slow_map = {n: dict(v) for n, v in (slow or {}).items()}
    oracle_map = dict(oracle or {})
    passed = sorted(
        n
        for n, r in state.items()
        if r is True
        and n not in dirty_map
        and n not in slow_map
        and n not in oracle_map
        and n not in (no_command or ())
    )
    _health["preflight"] = {
        "held": held,
        "dirty": dirty_map,
        "slow": slow_map,
        "unwinnable_oracle": oracle_map,
        "passed": passed,
        "failures": {
            n: {k: v for k, v in dict(d).items() if k != "key"} for n, d in (failures or {}).items() if n in held
        },
    }


def preflight_snapshot() -> dict:
    """``{held: {project: reason}, dirty: {project: why}, slow: {project: {cmd,
    timeout_s, sha}}, unwinnable_oracle: {project: why}, passed: [project, …], failures:
    {project: {cmd, step, step_index, steps, exit_code, headline, tail}}}``. Held projects
    are the ones whose ready work is frozen behind a red gate; ``failures`` says which step
    of their gate failed and shows the last lines of its output (secrets redacted). Slow
    ones run a preflight that can't finish in its timeout, so their verdict is only
    "indeterminate" (#456). An unwinnable oracle means that project's cards skip
    coder.solve() (#459). ``passed`` is the projects whose gate ran clean on the base with
    none of those caveats — the one positive per-project verdict (#612). Empty before the
    first preflight pass (and on a board whose loop is off), which reads correctly as
    "nothing held, nothing passed"."""
    snap = _health.get("preflight") or {}
    return {
        "held": dict(snap.get("held") or {}),
        "dirty": dict(snap.get("dirty") or {}),
        "slow": {n: dict(v) for n, v in (snap.get("slow") or {}).items()},
        "unwinnable_oracle": dict(snap.get("unwinnable_oracle") or {}),
        "passed": list(snap.get("passed") or []),
        "failures": {n: dict(v) for n, v in (snap.get("failures") or {}).items()},
    }


def preflight_failure(project: str) -> dict | None:
    """Held project ``project``'s preflight failure detail, or None when it is not held
    (or the loop has not published one)."""
    return preflight_snapshot()["failures"].get(project)


# The advisory kinds, in the order an entry list sorts them within one project (#728).
ADVISORY_KINDS: tuple[str, ...] = ("slow_gate", "slow_preflight", "unwinnable_oracle")
# The longest a plain advisory summary may run, so a banner never cuts it off (#728).
SUMMARY_MAX = 120


def _duration(seconds) -> str:
    """``600`` -> ``10 minutes``; ``90`` -> ``90 seconds``; ``0.3`` -> ``0.3 seconds``."""
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s >= 120 and s % 60 == 0:
        return f"{s / 60:.0f} minutes"
    return f"{s:g} seconds"


def _summary(name: str, template: str) -> str:
    """``template`` with ``{p}`` filled by the project name, clipped to SUMMARY_MAX by
    shortening the NAME (never the effect), so a long project name can't push the summary
    past the banner's width."""
    text = template.format(p=name)
    over = len(text) - SUMMARY_MAX
    if over > 0:
        keep = max(len(name) - over - 1, 1)
        text = template.format(p=name[:keep] + "…")
    return text[:SUMMARY_MAX]


def _fix_action(label: str, fields: list[str]) -> dict:
    """The host's closed setup-gap action shape — the same ``plugin_config`` kind as
    ``setup_check.plugin_config_action``: it opens Project Board's settings focused on the
    fields at fault. Built here (not imported) so health stays import-free of setup_check."""
    return {"kind": "plugin_config", "label": label, "fields": list(fields)}


def advisory_entries() -> list[dict]:
    """The setup advisories as structured entries, one per (project, kind), sorted by
    project then kind (#728): ``{project, kind, summary, fix, fix_label, fix_action,
    setting_path, detail}``.

    * ``slow_gate`` — the project's local pre-PR gate timed out (#483);
    * ``slow_preflight`` — its preflight can't finish inside ``preflight_timeout_s`` (#456);
    * ``unwinnable_oracle`` — its coder.solve() oracle can't pass, so solve() is off (#459).

    ``summary`` is plain words that lead with the effect, at most SUMMARY_MAX characters,
    and never carries the command — the command (often hundreds of characters) lives only
    in ``detail``. ``fix`` is the one-sentence resolution, ``fix_action`` the button that
    opens the settings named in it, ``setting_path`` the main setting to change."""
    snap = preflight_snapshot()
    entries: list[dict] = []
    for name, info in snap["slow"].items():
        timeout = info.get("timeout_s", 0) or 0
        entries.append(
            {
                "project": name,
                "kind": "slow_preflight",
                "summary": _summary(
                    name, "{p}'s preflight check takes longer than " + _duration(timeout) + ", so it gives no verdict."
                ),
                "fix": "Fix: set preflight_cmd to a cheap check (lint + an import check), or raise preflight_timeout_s.",
                "fix_label": "Set a cheaper preflight",
                "fix_action": _fix_action("Set a cheaper preflight", ["preflight_cmd", "preflight_timeout_s"]),
                "setting_path": "preflight_cmd",
                "detail": (
                    f"the preflight (`{info.get('cmd', '')}`) can't finish inside "
                    f"preflight_timeout_s={float(timeout):.0f}s, so it gives no verdict — set a cheap "
                    "`preflight_cmd` (lint + an import check)"
                ),
            }
        )
    for name, why in snap["unwinnable_oracle"].items():
        entries.append(
            {
                "project": name,
                "kind": "unwinnable_oracle",
                "summary": _summary(name, "{p}'s cards skip coder.solve(), because its test check can't pass in time."),
                "fix": (
                    "Fix: raise coder_solve_test_timeout_s, or set coder_solve_test_cmd (or coder_solve_test_paths) "
                    "to a faster test command."
                ),
                "fix_label": "Raise the solve time limit",
                "fix_action": _fix_action(
                    "Raise the solve time limit",
                    ["coder_solve_test_timeout_s", "coder_solve_test_cmd", "coder_solve_test_paths"],
                ),
                "setting_path": "coder_solve_test_timeout_s",
                "detail": f"coder.solve() is off — {why}",
            }
        )
    for name, info in slow_gates_snapshot().items():
        timeout = info.get("timeout_s", 0) or 0
        entries.append(
            {
                "project": name,
                "kind": "slow_gate",
                "summary": _summary(
                    name, "{p}'s pre-PR check takes longer than " + _duration(timeout) + ", so it is skipped."
                ),
                "fix": "Fix: raise local_gate_timeout_s, or set local_gate_cmd to lint only — CI runs the tests.",
                "fix_label": "Raise the time limit",
                "fix_action": _fix_action("Raise the time limit", ["local_gate_timeout_s", "local_gate_cmd"]),
                "setting_path": "local_gate_timeout_s",
                "detail": (
                    f"the local gate (`{info.get('cmd', '')}`) timed out at "
                    f"local_gate_timeout_s={float(timeout):.0f}s, so it verifies nothing and the merged-state "
                    "re-verify is skipped — set a cheaper `local_gate_cmd` (CI is the test gate) or raise the timeout"
                ),
            }
        )
    entries.sort(key=lambda e: (str(e["project"]), ADVISORY_KINDS.index(e["kind"])))
    return entries


def advisory_fix_action() -> dict | None:
    """The first active advisory's fix button (#728), or None when there is none — the
    action ``GapReporter`` attaches to the preflight setup gap."""
    entries = advisory_entries()
    return dict(entries[0]["fix_action"], fields=list(entries[0]["fix_action"]["fields"])) if entries else None


def advisory_hint() -> str:
    """One operator line for the setup advisories (#456/#459/#483), derived from
    :func:`advisory_entries`: per entry, the plain summary, then the fix, then the detail
    (which is where the command appears) — so a banner that cuts the line off still shows
    what is wrong and how to fix it (#728). "" when there are none."""
    return "; ".join(f"{e['summary']} {e['fix']} Detail: {e['detail']}" for e in advisory_entries())


def publish_slow_gates(slow: dict) -> None:
    """Called by the loop when a project's ``local_gate_cmd`` times out (or recovers):
    ``{project: {cmd, timeout_s}}`` (#483). A timed-out gate is a fail-open "pass" that
    verified nothing, so the merged-state re-verify stops re-running it."""
    _health["slow_gates"] = {n: dict(v) for n, v in (slow or {}).items()}


def slow_gates_snapshot() -> dict:
    """``{project: {cmd, timeout_s}}`` — the projects whose local gate can't finish in time."""
    return {n: dict(v) for n, v in (_health.get("slow_gates") or {}).items()}


def publish_base_checkouts(results: dict) -> None:
    """Called by the health sweep with ``{project: {repo, base, state, behind, detail,
    checked_at}}`` — how each project's MAIN checkout compares with ``origin/<base>`` after
    the sweep's refresh (#452; see ``worktree.refresh_base_checkout``)."""
    _health["base_checkouts"] = {n: dict(r) for n, r in (results or {}).items()}


def base_checkouts_snapshot() -> dict:
    """``{project: {repo, base, state, behind, detail, checked_at}}`` — empty before the
    first sweep, on a board whose loop is off, or with ``base_refresh: false``."""
    return {n: dict(r) for n, r in (_health.get("base_checkouts") or {}).items()}


def publish_orphaned_cards(cards: list) -> None:
    """Called by the health sweep with the live cards whose project label no longer
    resolves (#454; see ``projects.orphaned_cards``)."""
    _health["orphaned_cards"] = [dict(c) for c in cards or ()]


def orphaned_cards_snapshot() -> list:
    """The orphaned cards the last sweep found — ``[]`` before the first sweep."""
    return [dict(c) for c in _health.get("orphaned_cards") or ()]


def publish_merge_holds(cards: list) -> None:
    """Called by the reconcile pass with the in_review cards whose merge-state hold (#495)
    has stood longer than ``merge_hold_alert_s`` — a PR GitHub has read not-clean for hours,
    stuck the same way a claim stall leaves ready work unclaimed. Each is ``{id, hold, age_s,
    status, pr_url}``: ``hold`` is the operator text (``held: PR not clean on GitHub
    (UNSTABLE) — QA panel: in progress``) and ``age_s`` how long it has been held. ``[]``
    clears the entry, so a hold that cleared, merged or closed drops off the next pass."""
    _health["merge_holds"] = [dict(c) for c in cards or ()]


def merge_holds_snapshot() -> list:
    """The long-held merge-state holds the last reconcile pass found — ``[]`` before the
    first pass, on a board whose loop is off, or when none exceed ``merge_hold_alert_s``."""
    return [dict(c) for c in _health.get("merge_holds") or ()]


def publish_claim_stall(reason: str) -> None:
    """Called by the loop when its claim-stall signal changes (#462): the reason the board
    is idle with ready work and a free slot, or "" once the claim scan runs again."""
    _health["claim_stall"] = str(reason or "")


def claim_stall_hint() -> str:
    """The operator-facing line for a claim stall, "" when there is none (or no loop). It
    rides ``/status`` as a setup/health gap: a board that stops claiming says why."""
    reason = str(_health.get("claim_stall") or "")
    if not reason:
        return ""
    return (
        f"the board loop is not claiming ready work: {reason}. A per-card gate or review may be hung "
        "(see the agent log for that phase); `board_dispatch` runs a claim scan now"
    )

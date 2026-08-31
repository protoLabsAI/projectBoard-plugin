"""Setup preflight (v0.42.0) — ``setup_status`` + ``GapReporter`` + the places they
surface: ``register()``, the loop's pause/resume gate, ``GET /status``, the board page.

Every probe is injected (``which``/``delegates``/``run``/``isdir``) — no PATH, no
subprocess, no host. The point of the feature is that a board which CANNOT run (no
``br``, no resolvable coder, no ``gh``, no repo) says so where the operator looks
instead of booting green and ticking into tracebacks, so these tests pin:

* each check's pass/fail branch and its operator hint,
* that an UNSET coder is a failure (there is no ``proto`` default any more),
* the edge-triggered host reporting (and the guard for a host without the seam),
* that the loop pauses — no recovery, no ticks, ONE warning — and resumes on its
  own once the gap closes, and
* the ``setup`` block on ``/status`` + the board page's setup card.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, projects, setup_check
from project_board.loop import BoardLoop
from project_board.setup_check import GapReporter, setup_status

LOGGER = "protoagent.plugins.project_board"


def _which_all(name):
    return f"/usr/local/bin/{name}"


def _which_none(_name):
    return None


def _which_only(*present):
    return lambda name: f"/usr/local/bin/{name}" if name in present else None


def _delegates(*known):
    return lambda name: object() if name in known else None


def _ok_cfg(tmp_path):
    return {"coder": "proto", "repo": str(tmp_path)}


def _all_ok(tmp_path, **over):
    kw = dict(which=_which_all, delegates=_delegates("proto"), run=_fake_run("br 0.1.23"))
    kw.update(over)
    return setup_status(_ok_cfg(tmp_path), **kw)


def _fake_run(stdout="", returncode=0, raise_exc=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    run.calls = calls
    return run


# ── setup_status: the four checks ────────────────────────────────────────────────


def test_everything_present_is_ready_with_no_blockers(tmp_path):
    s = _all_ok(tmp_path)
    assert s["ready"] is True
    assert s["loop_blockers"] == []
    assert {k: s["br"][k] for k in ("ok", "path", "version", "hint")} == {
        "ok": True,
        "path": "/usr/local/bin/br",
        "version": "br 0.1.23",
        "hint": "",
    }
    assert s["br"]["source"] == "path" and s["br"]["fetch"]["state"] in ("idle", "done")
    assert s["gh"] == {"ok": True, "path": "/usr/local/bin/gh", "hint": ""}
    assert s["coder"]["ok"] is True and s["coder"]["name"] == "proto" and s["coder"]["missing"] == []
    assert s["repo"] == {"ok": True, "path": str(tmp_path), "hint": ""}
    assert s["loop_enabled"] is False


def test_br_missing_fails_with_the_install_hint_and_blocks_the_loop(tmp_path):
    s = _all_ok(tmp_path, which=_which_only("gh"))
    assert s["br"]["ok"] is False and s["br"]["path"] == "" and s["br"]["version"] == ""
    assert "cargo install beads_rust" in s["br"]["hint"] and "paused" in s["br"]["hint"]
    assert s["ready"] is False
    assert s["loop_blockers"] == ["br"]


def test_br_honors_br_bin_override(tmp_path, monkeypatch):
    """store.py resolves the binary through BR_BIN; the preflight must probe the SAME
    name, or an operator who set BR_BIN sees a false 'br missing'."""
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "BR", "/opt/beads/br")  # the store's own constant, read at call time
    probed = []

    def which(name):
        probed.append(name)
        return "/opt/beads/br" if name == "/opt/beads/br" else "/usr/local/bin/gh"

    s = _all_ok(tmp_path, which=which)
    assert "/opt/beads/br" in probed and s["br"]["ok"] is True
    # …and a missing override is named in the hint (not a generic 'br')
    s = _all_ok(tmp_path, which=_which_only("gh"))
    assert s["br"]["ok"] is False and "'/opt/beads/br'" in s["br"]["hint"]


def test_br_version_is_sampled_once_per_path_with_a_short_timeout(tmp_path):
    run = _fake_run("br 0.2.16\nextra")
    s1 = _all_ok(tmp_path, run=run)
    s2 = _all_ok(tmp_path, run=run)
    assert s1["br"]["version"] == "br 0.2.16" == s2["br"]["version"]
    assert len(run.calls) == 1  # cached per path — the per-tick re-check is a `which` only
    argv, kw = run.calls[0]
    assert argv == ["/usr/local/bin/br", "--version"]
    assert kw["timeout"] == 3.0


def test_br_version_failure_is_informational_never_a_gate(tmp_path):
    s = _all_ok(tmp_path, run=_fake_run(raise_exc=TimeoutError("slow")))
    assert s["br"]["ok"] is True and s["br"]["version"] == ""
    s = _all_ok(tmp_path, run=_fake_run("boom", returncode=1))
    assert s["br"]["ok"] is True and s["br"]["version"] == ""


def test_gh_missing_is_reported_but_does_not_block_the_loop(tmp_path):
    s = _all_ok(tmp_path, which=_which_only("br"))
    assert s["gh"]["ok"] is False and "gh auth login" in s["gh"]["hint"]
    assert s["ready"] is False
    assert s["loop_blockers"] == []  # the PR edge fails per-build; the ticks themselves are fine


def test_unset_coder_is_a_failure_with_the_pick_a_delegate_hint(tmp_path):
    """v0.42.0: NO default coder. Empty → not ok, and the roster is never asked
    about a phantom name."""
    asked = []

    def delegates(name):
        asked.append(name)
        return object()

    for cfg in ({"repo": str(tmp_path)}, {"repo": str(tmp_path), "coder": ""}, {"repo": str(tmp_path), "coder": "  "}):
        s = setup_status(cfg, which=_which_all, delegates=delegates, run=_fake_run())
        assert s["coder"]["ok"] is False, cfg
        assert s["coder"]["name"] == "" and s["coder"]["names"] == []
        assert s["coder"]["hint"] == setup_check.NO_CODER_HINT
        assert "pick a delegate in Settings ▸ Project Board" in s["coder"]["hint"]
        assert "propose_delegate" in s["coder"]["hint"]
        assert "former implicit default `proto` no longer applies" in s["coder"]["hint"]
        assert "coder" in s["loop_blockers"]
    assert asked == []


def test_unresolvable_coder_names_the_missing_delegate(tmp_path):
    s = _all_ok(tmp_path, delegates=_delegates("other"))
    assert s["coder"]["ok"] is False and s["coder"]["missing"] == ["proto"]
    assert "'proto'" in s["coder"]["hint"] and "Settings ▸ Delegates" in s["coder"]["hint"]
    assert s["loop_blockers"] == ["coder"]


def test_every_configured_coder_name_must_resolve(tmp_path):
    """The flat `coder`, the `coders` tier map AND each `projects:` entry's own
    `coders` all dispatch — one unresolvable rung blocks a whole escalation ladder."""
    cfg = {
        "coder": "proto",
        "coders": {"fast": "proto", "smart": "proto-smart"},
        "projects": {"web": {"repo": str(tmp_path), "coders": {"reasoning": "claude-acp"}}},
    }
    assert setup_check.coder_names(cfg) == ["proto", "proto-smart", "claude-acp"]
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto", "claude-acp"), run=_fake_run())
    assert s["coder"]["ok"] is False and s["coder"]["missing"] == ["proto-smart"]
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto", "proto-smart", "claude-acp"), run=_fake_run())
    assert s["coder"]["ok"] is True


def test_coders_ladder_alone_passes_only_when_every_tier_is_mapped(tmp_path):
    """Review on #212: with `coder` blank the ladder is the ONLY dispatch path and
    dispatch resolves `coders.get(tier, "")` — an unmapped TIER_LADDER rung blocks
    the card with "coder delegate '' not configured". So blank `coder` needs
    escalation ON and smart/reasoning/opus all mapped."""
    full = {"smart": "a", "reasoning": "b", "opus": "c"}
    s = setup_status(
        {"repo": str(tmp_path), "coders": full}, which=_which_all, delegates=_delegates("a", "b", "c"), run=_fake_run()
    )
    assert s["coder"]["ok"] is True and s["coder"]["name"] == "" and s["coder"]["names"] == ["a", "b", "c"]
    # the manifest's old example shape (fast/smart/reasoning, no opus) → every
    # `architectural` card would dispatch to '' — not ok, and the hint names the rung
    s = setup_status(
        {"repo": str(tmp_path), "coders": {"fast": "a", "smart": "b", "reasoning": "c"}},
        which=_which_all,
        delegates=_delegates("a", "b", "c"),
        run=_fake_run(),
    )
    assert s["coder"]["ok"] is False
    assert "`coder:` is unset and the coders ladder doesn't cover tier(s) opus" in s["coder"]["hint"]
    assert "set `coder:` as the fallback" in s["coder"]["hint"]
    assert "coder" in s["loop_blockers"]
    # `coder` SET → the ladder may have holes (the fallback covers them)
    s = setup_status(
        {"repo": str(tmp_path), "coder": "a", "coders": {"smart": "a", "reasoning": "c"}},
        which=_which_all,
        delegates=_delegates("a", "c"),
        run=_fake_run(),
    )
    assert s["coder"]["ok"] is True


def test_single_delegate_ladder_with_blank_coder_is_not_ok(tmp_path):
    """`coders: {smart: a}` + no `coder:` — escalation_enabled needs >1 distinct
    delegate, so the loop dispatches `coder:` = '' and every card blocks. The
    reviewer's repro, pinned."""
    s = setup_status(
        {"repo": str(tmp_path), "coders": {"smart": "a"}}, which=_which_all, delegates=_delegates("a"), run=_fake_run()
    )
    assert s["coder"]["ok"] is False
    assert "only one distinct delegate" in s["coder"]["hint"] and "set `coder:`" in s["coder"]["hint"]
    # map a to all three rungs — still ONE distinct delegate → still not ok
    s = setup_status(
        {"repo": str(tmp_path), "coders": {"smart": "a", "reasoning": "a", "opus": "a"}},
        which=_which_all,
        delegates=_delegates("a"),
        run=_fake_run(),
    )
    assert s["coder"]["ok"] is False


def test_blank_coder_requires_every_project_ladder_to_cover_every_tier(tmp_path):
    """`_coders_for(feature)` prefers the feature's PROJECT map, so a project map
    with a hole dispatches to '' for that rung even when the instance map is full."""
    full = {"smart": "a", "reasoning": "b", "opus": "c"}
    cfg = {
        "coders": full,
        "projects": {
            "web": {"repo": str(tmp_path), "coders": {"smart": "a", "reasoning": "b"}},
            "api": {"repo": str(tmp_path)},
        },
    }
    s = setup_status(cfg, which=_which_all, delegates=_delegates("a", "b", "c"), run=_fake_run())
    assert s["coder"]["ok"] is False
    assert "opus (project 'web')" in s["coder"]["hint"]
    cfg["projects"]["web"]["coders"]["opus"] = "c"
    s = setup_status(cfg, which=_which_all, delegates=_delegates("a", "b", "c"), run=_fake_run())
    assert s["coder"]["ok"] is True  # `api` has no map → falls back to the (full) instance map
    assert setup_check.uncovered_tiers({"smart": "a", "opus": ""}) == ["reasoning", "opus"]


def test_delegate_lookup_errors_count_as_unresolved(tmp_path):
    def boom(_name):
        raise RuntimeError("delegates plugin disabled")

    s = _all_ok(tmp_path, delegates=boom)
    assert s["coder"]["ok"] is False and s["coder"]["missing"] == ["proto"]


def test_unbound_default_repo_fails_unless_cwd_already_has_a_beads_workspace():
    """The shipped `repo: "."` only works when the process cwd IS the target repo —
    honor exactly that case (a `.beads/` there) and nothing looser."""
    common = dict(which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    s = setup_status({"coder": "proto"}, isdir=lambda p: False, **common)
    assert s["repo"]["ok"] is False and s["repo"]["hint"] == setup_check.REPO_UNBOUND_HINT
    assert s["loop_blockers"] == ["repo"]
    s = setup_status({"coder": "proto"}, isdir=lambda p: p.endswith(".beads"), **common)
    assert s["repo"]["ok"] is True and s["repo"]["path"] == "."


def test_bound_repo_must_exist_on_disk(tmp_path):
    s = setup_status(
        {"coder": "proto", "repo": "/nowhere/at/all"},
        which=_which_all,
        delegates=_delegates("proto"),
        run=_fake_run(),
    )
    assert s["repo"]["ok"] is False and "/nowhere/at/all" in s["repo"]["hint"] and "does not exist" in s["repo"]["hint"]


def test_projects_map_names_the_missing_project_repo(tmp_path):
    cfg = {"coder": "proto", "projects": {"web": {"repo": str(tmp_path)}, "api": {"repo": "/gone"}}}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"]["ok"] is False and "(project 'api')" in s["repo"]["hint"]


def test_malformed_projects_map_is_the_repo_finding_not_a_raise():
    cfg = {"coder": "proto", "projects": {"web": {"base_branch": "main"}}}  # no repo → resolve_projects raises
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"]["ok"] is False and "invalid" in s["repo"]["hint"] and "'web'" in s["repo"]["hint"]
    assert s["coder"]["ok"] is True  # the coder check still ran (names from the flat key)


# ── the multi-project + explicit-blank db_path ADVISORY (D3, #260) ────────────────
# A blank db_path resolves to the instance store (store.default_db_path — pinned in
# test_store and store_db_path below), so a multi-project board shares one db whether
# the key is absent OR explicitly blank. The operator who EXPLICITLY wrote
# `db_path: ""` (the pre-D3 per-repo-discovery override — distinguishable because the
# host hands a plugin its config section verbatim, not a per-key merge with manifest
# defaults) next to a multi-entry projects: map gets a knob that silently does
# nothing — surfaced as a non-blocking advisory (its own `db` report key), NEVER a
# failing check or a pause: the board runs correctly on the one shared store.


def _two_projects(tmp_path):
    return {"web": {"repo": str(tmp_path)}, "api": {"repo": str(tmp_path)}}


def test_multi_project_map_with_explicit_blank_db_path_is_a_nonblocking_advisory(tmp_path):
    cfg = {"coder": "proto", "db_path": "", "projects": _two_projects(tmp_path)}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["db_override_ignored"] is True
    assert s["db_override_hint"] == setup_check.MULTI_PROJECT_DB_HINT
    assert "ignored" in s["db_override_hint"] and "one instance store" in s["db_override_hint"]
    # the board is NOT paused: the blank resolves to the same instance store as an
    # absent key (store_db_path below), so every check passes and the loop runs
    assert s["repo"] == {"ok": True, "path": ".", "hint": ""}
    assert s["ready"] is True and s["loop_blockers"] == []


def test_whitespace_only_db_path_is_the_same_override(tmp_path):
    cfg = {"coder": "proto", "db_path": "   ", "projects": _two_projects(tmp_path)}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["db_override_ignored"] is True and s["repo"]["ok"] is True


def test_multi_project_map_without_a_db_path_key_rides_the_instance_default(tmp_path):
    """The #260 acceptance shape: two projects, NO db_path key — every card lands in
    the instance-default store (r1; the store half is pinned in test_store), so the
    repo check stays green, the loop runs, and no advisory fires (nothing is stale)."""
    cfg = {"coder": "proto", "projects": _two_projects(tmp_path)}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"] == {"ok": True, "path": ".", "hint": ""}
    assert s["ready"] is True and s["loop_blockers"] == []
    assert s["db_override_ignored"] is False and s["db_override_hint"] == ""


def test_multi_project_map_with_a_shared_db_path_passes_quietly(tmp_path):
    cfg = {"coder": "proto", "db_path": str(tmp_path / "board" / "beads.db"), "projects": _two_projects(tmp_path)}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"]["ok"] is True and s["db_override_ignored"] is False


def test_single_project_map_with_explicit_blank_db_path_is_not_the_advisory(tmp_path):
    """One entry never had anything to fragment: the explicit blank simply rides the
    instance default, and there is no multi-project intent to warn about."""
    cfg = {"coder": "proto", "db_path": "", "projects": {"web": {"repo": str(tmp_path)}}}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"]["ok"] is True and s["db_override_ignored"] is False


def test_the_advisory_and_a_missing_project_dir_surface_independently(tmp_path):
    """Orthogonal findings, orthogonal channels: the missing dir is the repo check's
    failure (the loop pauses on it), the inert blank override is the advisory — one
    never masks the other."""
    cfg = {"coder": "proto", "db_path": "", "projects": {"web": {"repo": str(tmp_path)}, "api": {"repo": "/gone"}}}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["repo"]["ok"] is False and "/gone" in s["repo"]["hint"]
    assert "repo" in s["loop_blockers"]
    assert s["db_override_ignored"] is True and s["db_override_hint"] == setup_check.MULTI_PROJECT_DB_HINT


# ── the pre-D3 per-repo workspace MIGRATION advisory (D3, #260) ───────────────────
# Before D3, a board with no db_path kept its cards IN the configured repo (`br`
# per-repo discovery / `br init` in the repo). Post-D3 the same config reads the
# instance store, so a repo still carrying `.beads/` is the upgrade signature: any
# cards left there are invisible until db_path pins back to that file or they are
# migrated — surfaced under its own `db_legacy` report key so the switch is never a
# silently empty board. Non-blocking: the board itself is healthy on the instance
# store, so the advisory never fails a check or pauses the loop.


def test_repo_with_a_beads_workspace_and_no_db_path_is_the_migration_advisory(tmp_path):
    (tmp_path / ".beads").mkdir()
    cfg = {"coder": "proto", "repo": str(tmp_path)}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["legacy_store_repos"] == [str(tmp_path)]
    assert str(tmp_path) in s["legacy_store_hint"] and "db_path" in s["legacy_store_hint"]
    # non-blocking: the board runs (on the instance store) — the advisory only informs
    assert s["ready"] is True and s["loop_blockers"] == []


def test_an_explicit_db_path_pin_quiets_the_migration_advisory(tmp_path):
    """The pin decides, wherever it points — an operator who set db_path (to the
    legacy file or anywhere else) has already made the migration choice."""
    (tmp_path / ".beads").mkdir()
    cfg = {"coder": "proto", "repo": str(tmp_path), "db_path": str(tmp_path / ".beads" / "old.db")}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["legacy_store_repos"] == [] and s["legacy_store_hint"] == ""


def test_a_repo_without_a_beads_workspace_is_not_the_migration_advisory(tmp_path):
    s = setup_status(
        {"coder": "proto", "repo": str(tmp_path)}, which=_which_all, delegates=_delegates("proto"), run=_fake_run()
    )
    assert s["legacy_store_repos"] == [] and s["legacy_store_hint"] == ""


def test_an_explicitly_blank_db_path_still_gets_the_migration_advisory(tmp_path):
    """`db_path: ""` is NOT a pin — it rides the instance store (store_db_path), so a
    legacy workspace next to it is exactly as invisible as with the key absent."""
    (tmp_path / ".beads").mkdir()
    cfg = {"coder": "proto", "repo": str(tmp_path), "db_path": ""}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["legacy_store_repos"] == [str(tmp_path)]


def test_migration_advisory_names_only_the_project_repos_that_carry_a_workspace(tmp_path):
    legacy, fresh = tmp_path / "legacy", tmp_path / "fresh"
    (legacy / ".beads").mkdir(parents=True)
    fresh.mkdir()
    cfg = {"coder": "proto", "projects": {"old": {"repo": str(legacy)}, "new": {"repo": str(fresh)}}}
    s = setup_status(cfg, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s["legacy_store_repos"] == [str(legacy)]
    assert str(legacy) in s["legacy_store_hint"] and str(fresh) not in s["legacy_store_hint"]
    assert s["ready"] is True and s["loop_blockers"] == []
    # composes with the stale-override advisory — independent channels, neither masks
    s2 = setup_status({**cfg, "db_path": ""}, which=_which_all, delegates=_delegates("proto"), run=_fake_run())
    assert s2["db_override_ignored"] is True and s2["legacy_store_repos"] == [str(legacy)]


def test_legacy_store_repos_dedupes_pins_and_guards():
    probed = []

    def isdir(p):
        probed.append(p)
        return True

    shared = {"a": {"repo": "/x"}, "b": {"repo": "/x"}}
    assert setup_check.legacy_store_repos({"projects": shared}, isdir=isdir) == ["/x"]  # de-duplicated
    assert probed == [os.path.join("/x", ".beads")]  # one probe per distinct repo
    assert setup_check.legacy_store_repos({"db_path": "/pin.db", "repo": "/x"}, isdir=isdir) == []  # pinned
    # a malformed projects: map is the repo check's finding, not a raise here
    assert setup_check.legacy_store_repos({"projects": {"a": {"base_branch": "m"}}}, isdir=isdir) == []
    assert setup_check.legacy_store_hint([]) == ""


# ── the config-seam helpers the wiring rides (projects.py, D3 #260) ───────────────


def test_blank_db_override_means_key_present_and_blank():
    assert projects.blank_db_override({"db_path": ""}) is True
    assert projects.blank_db_override({"db_path": "   "}) is True
    assert projects.blank_db_override({"db_path": None}) is True
    assert projects.blank_db_override({}) is False  # key absent = no choice = the instance default
    assert projects.blank_db_override({"db_path": "/x/beads.db"}) is False
    assert projects.blank_db_override(None) is False


def test_multi_project_is_an_explicit_map_with_more_than_one_entry():
    assert projects.multi_project({"projects": {"a": {"repo": "/x"}, "b": {"repo": "/y"}}}) is True
    assert projects.multi_project({"projects": {"a": {"repo": "/x"}}}) is False
    assert projects.multi_project({"projects": {}}) is False
    assert projects.multi_project({}) is False
    assert projects.multi_project(None) is False


def test_store_db_path_resolves_explicit_else_instance_default(monkeypatch):
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    # explicit pins pass through VERBATIM — the same raw value the loop's store_kw
    # carries, so both resolutions land on ONE cached board (get_store keys on db)
    assert projects.store_db_path({"db_path": "/x/board.db"}) == "/x/board.db"
    assert projects.store_db_path({"db_path": "~/boards/b.db"}) == "~/boards/b.db"
    for cfg in ({}, {"db_path": ""}, {"db_path": "  "}, {"db_path": None}, None):
        assert projects.store_db_path(cfg) == "/inst/project_board/.beads/beads.db"


def test_setup_status_never_raises_on_a_broken_which(tmp_path):
    def which(_name):
        raise OSError("PATH is cursed")

    s = _all_ok(tmp_path, which=which)
    assert s["br"]["ok"] is False and s["gh"]["ok"] is False


def test_loop_enabled_is_echoed_and_blockers_are_ordered(tmp_path):
    s = setup_status(
        {"loop_enabled": True},
        which=_which_none,
        delegates=_delegates(),
        run=_fake_run(),
        isdir=lambda p: False,
    )
    assert s["loop_enabled"] is True
    assert s["ready"] is False
    assert s["loop_blockers"] == ["br", "coder", "repo"]  # gh is reported, not a blocker
    assert setup_check.loop_blockers(s) == ["br", "coder", "repo"]
    summary = setup_check.blocker_summary(s)
    assert summary.startswith("br: ") and "; coder: " in summary and "; repo: " in summary


# ── GapReporter: the host seam, edge-triggered, guarded ──────────────────────────


class _HostWithSeam:
    def __init__(self):
        self.calls = []

    def report_setup_gap(self, key, message):
        self.calls.append((key, message))


class _HostWithoutSeam:
    pass


def _status(**ok):
    base = {k: {"ok": True, "hint": ""} for k in setup_check.SETUP_KEYS}
    for k, v in ok.items():
        base[k] = {"ok": v, "hint": f"{k} hint"}
    return base


ALL_CLEAR = {k: None for k in setup_check.REPORT_KEYS}


def test_reporter_sends_failing_hints_once_and_clears_on_recovery():
    host = _HostWithSeam()
    rep = GapReporter(host)
    assert rep.available is True

    # FIRST evaluation: every key, unconditionally — the failing hints AND a clear
    # for the passing checks (review on #212: a reload builds a fresh reporter that
    # must not leave the previous instance's warning standing; the clear is idempotent).
    first = rep.report(_status(br=False, coder=False))
    assert first == {**ALL_CLEAR, "br": "br hint", "coder": "coder hint"}
    assert host.calls == [
        ("br", "br hint"),
        ("gh", None),
        ("coder", "coder hint"),
        ("repo", None),
        ("loop", None),
        ("db", None),
        ("db_legacy", None),
        ("review_status", None),
    ]
    # steady state → nothing forwarded (a 30 s tick must not spam the host)
    assert rep.report(_status(br=False, coder=False)) == {}
    assert len(host.calls) == 8
    # br installed → ONE clear for br, coder still standing → silent
    assert rep.report(_status(coder=False)) == {"br": None}
    assert host.calls[-1] == ("br", None)
    # everything passes → coder cleared
    assert rep.report(_status()) == {"coder": None}
    assert host.calls[-1] == ("coder", None)
    assert rep.reported == ALL_CLEAR


def test_fresh_reporter_clears_a_previous_instances_warning():
    """The reload scenario: boot with no coder → gap reported; operator fixes it →
    reload → a NEW reporter with empty memory. Its first report must send the clear
    (None) for the now-passing check, or the host banner outlives the gap."""
    host = _HostWithSeam()
    GapReporter(host).report(_status(coder=False))  # the previous instance
    assert ("coder", "coder hint") in host.calls
    fresh = GapReporter(host)
    fresh.report(_status())  # the reloaded instance sees a passing coder
    assert ("coder", None) in host.calls[7:]


def test_reporter_forwards_the_loop_stale_key():
    host = _HostWithSeam()
    rep = GapReporter(host)
    rep.report({**_status(), "loop_cfg_stale_hint": "config changed since the loop started (repo) — restart"})
    assert ("loop", "config changed since the loop started (repo) — restart") in host.calls
    assert rep.report({**_status(), "loop_cfg_stale_hint": ""}) == {"loop": None}


def test_reporter_forwards_the_db_override_advisory_key():
    """The D3 advisory (#260) rides the seam under its own `db` key — edge-triggered
    like every other key: the hint when the inert override appears, one clear (None)
    when the operator removes it."""
    host = _HostWithSeam()
    rep = GapReporter(host)
    rep.report({**_status(), "db_override_hint": setup_check.MULTI_PROJECT_DB_HINT})
    assert ("db", setup_check.MULTI_PROJECT_DB_HINT) in host.calls
    assert rep.report({**_status(), "db_override_hint": setup_check.MULTI_PROJECT_DB_HINT}) == {}  # steady
    assert rep.report({**_status(), "db_override_hint": ""}) == {"db": None}
    assert host.calls[-1] == ("db", None)


def test_reporter_forwards_the_migration_advisory_key():
    """The pre-D3 workspace advisory (D3, #260) rides the seam under its own
    `db_legacy` key — edge-triggered like every other key: the hint when a legacy
    workspace appears, one clear (None) once the operator pins or migrates."""
    host = _HostWithSeam()
    rep = GapReporter(host)
    hint = setup_check.legacy_store_hint(["/old/repo"])
    assert "/old/repo" in hint and "db_path" in hint
    rep.report({**_status(), "legacy_store_hint": hint})
    assert ("db_legacy", hint) in host.calls
    assert rep.report({**_status(), "legacy_store_hint": hint}) == {}  # steady
    assert rep.report({**_status(), "legacy_store_hint": ""}) == {"db_legacy": None}
    assert host.calls[-1] == ("db_legacy", None)


def test_reporter_is_a_guarded_noop_on_a_host_without_the_seam():
    rep = GapReporter(_HostWithoutSeam())
    assert rep.available is False
    assert rep.report(_status(br=False)) == {**ALL_CLEAR, "br": "br hint"}  # still tracked locally
    assert rep.report(_status()) == {"br": None}
    assert GapReporter(None).available is False
    # a non-callable attribute is NOT the seam
    assert GapReporter(SimpleNamespace(report_setup_gap="nope")).available is False


def test_reporter_swallows_a_host_side_failure(caplog):
    class _Broken:
        def report_setup_gap(self, key, message):
            raise RuntimeError("host exploded")

    rep = GapReporter(_Broken())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert rep.report(_status(gh=False)) == {**ALL_CLEAR, "gh": "gh hint"}
    assert any("report_setup_gap('gh') failed" in r.message for r in caplog.records)


# ── register(): reports at mount time, logs coder=<unset> ────────────────────────


class _Registry:
    def __init__(self, config):
        self.config = config
        self.tools, self.routers, self.surfaces = [], [], []
        self.subagents, self.skill_dirs = [], []

    def register_tool(self, t):
        self.tools.append(t)

    def register_router(self, router, prefix):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None, reload=None):
        self.surfaces.append(name)
        self.loop_start = start

    def register_subagent(self, config):
        self.subagents.append(config)

    def register_skill_dir(self, path):
        self.skill_dirs.append(path)


class _RegistryWithSeam(_Registry):
    def __init__(self, config):
        super().__init__(config)
        self.gaps = []

    def report_setup_gap(self, key, message):
        self.gaps.append((key, message))


def _pin_probes(monkeypatch, *, which, delegates):
    """Pin the preflight's live probes for register()/route tests (which otherwise
    read the real PATH + the host roster)."""
    monkeypatch.setattr(setup_check.shutil, "which", which)
    monkeypatch.setattr(setup_check, "_default_delegates", lambda: delegates)  # one resolver per call
    monkeypatch.setattr(setup_check, "_subprocess_run", _fake_run("br 0.1.23"))


def test_register_reports_every_failing_check_to_a_host_with_the_seam(monkeypatch, caplog):
    _pin_probes(monkeypatch, which=_which_only("br"), delegates=_delegates())
    reg = _RegistryWithSeam({"coder": "", "repo": "/nowhere"})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        pb.register(reg)
    msgs = dict(reg.gaps)
    # every key on the first evaluation (br + loop + db + db_legacy + review_status as clears), in render order
    assert [k for k, _ in reg.gaps] == ["br", "gh", "coder", "repo", "loop", "db", "db_legacy", "review_status"]
    assert msgs["br"] is None and msgs["loop"] is None and msgs["db"] is None
    assert msgs["coder"] == setup_check.NO_CODER_HINT
    assert "gh auth login" in msgs["gh"]
    assert "/nowhere" in msgs["repo"]
    # each gap also lands in the server log, and the registration line says <unset>
    assert any("setup gap (coder)" in r.message for r in caplog.records)
    assert any("coder=<unset>" in r.message and "registered board API" in r.message for r in caplog.records)
    # the loop surface still registers — the board API serves regardless
    assert "project-board-loop" in reg.surfaces


def test_register_on_a_host_without_the_seam_still_mounts_everything(monkeypatch, caplog):
    """The guard: `getattr(registry, "report_setup_gap", None)` — an older host has no
    seam, so the gaps go to the log only and registration is unaffected."""
    _pin_probes(monkeypatch, which=_which_none, delegates=_delegates())
    reg = _Registry({})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        pb.register(reg)  # must not raise
    assert "/plugins/project_board" in reg.routers and "project-board-loop" in reg.surfaces
    assert any("setup gap (br)" in r.message for r in caplog.records)


def test_register_reports_nothing_when_setup_is_complete(monkeypatch, tmp_path):
    _pin_probes(monkeypatch, which=_which_all, delegates=_delegates("proto"))
    reg = _RegistryWithSeam({"coder": "proto", "repo": str(tmp_path)})
    pb.register(reg)
    assert reg.gaps == [(k, None) for k in setup_check.REPORT_KEYS]  # all clears, nothing standing


def test_register_log_names_the_coder_when_set(monkeypatch, tmp_path, caplog):
    _pin_probes(monkeypatch, which=_which_all, delegates=_delegates("proto"))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        pb.register(_Registry({"coder": "proto", "repo": str(tmp_path)}))
    assert any("coder=proto reviewer=quinn" in r.message for r in caplog.records)


# ── the loop: pause without tracebacks, resume without a restart ─────────────────


class _Probe:
    """A switchable PATH + roster the loop's preflight reads each check."""

    def __init__(self, *, br=True, coder=True):
        self.br, self.coder = br, coder

    def which(self, name):
        if name == "br":
            return "/usr/local/bin/br" if self.br else None
        return f"/usr/local/bin/{name}"

    def resolver(self):
        return lambda name: object() if (self.coder and name == "proto") else None


def _wire(monkeypatch, loop, probe, reporter=None):
    """Instrument a loop: fake probes, a recorder for recovery + the tick steps (so
    a paused loop provably runs NONE of them), and a sub-second interval."""
    loop._which = probe.which
    monkeypatch.setattr(loop, "_delegate_resolver", probe.resolver)
    monkeypatch.setattr(loop, "_ensure_br", lambda: {"state": "disabled"})  # the fetch has its own tests
    if reporter is not None:
        loop._gap_reporter = reporter
    loop.interval = 0.02
    calls = []

    async def _recover():
        calls.append("recover")

    async def _reconcile():
        calls.append("reconcile")

    async def _sweep():
        calls.append("sweep")

    async def _preflight():
        calls.append("preflight")

    async def _spawn():  # _spawn_ready is a coroutine since #258 (store calls offloaded)
        calls.append("spawn")
        return False

    monkeypatch.setattr(loop, "_recover", _recover)
    monkeypatch.setattr(loop, "_maybe_reconcile", _reconcile)
    monkeypatch.setattr(loop, "_maybe_sweep", _sweep)
    monkeypatch.setattr(loop, "_maybe_preflight", _preflight)
    monkeypatch.setattr(loop, "_spawn_ready", _spawn)
    return calls


async def _settle(n=6):
    for _ in range(n):
        await asyncio.sleep(0.02)


async def test_loop_pauses_on_missing_br_then_resumes_when_it_appears(monkeypatch, tmp_path, caplog):
    """No `br` → NO recovery, NO tick, ONE warning (not a traceback per tick); install
    it → recovery + ticks start on their own, one resume line, no restart."""
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    probe = _Probe(br=False)
    host = _HostWithSeam()
    calls = _wire(monkeypatch, loop, probe, GapReporter(host))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle()
        assert calls == []  # paused: nothing ran — no "crash recovery failed", no tick
        paused = [r for r in caplog.records if "loop paused:" in r.message]
        assert len(paused) == 1 and paused[0].levelno == logging.WARNING
        assert "br:" in paused[0].message and "cargo install beads_rust" in paused[0].message
        assert not any("crash recovery failed" in r.message or "loop tick failed" in r.message for r in caplog.records)
        assert ("br", setup_check.BR_HINT) in host.calls and len(host.calls) == 8  # first eval: all keys

        probe.br = True  # operator installs beads → the next re-check passes
        await _settle()
        assert calls[:2] == ["recover", "reconcile"]  # recovery ran ONCE, then the tick steps
        assert "spawn" in calls
        assert len([r for r in caplog.records if "loop paused:" in r.message]) == 1  # still ONE warning
        assert any("loop resumed — setup gaps cleared" in r.message for r in caplog.records)
        assert any("recovery done — entering tick loop" in r.message for r in caplog.records)
        assert host.calls[-1] == ("br", None)  # the gap was CLEARED on the host

    loop._stop.set()
    await asyncio.wait_for(task, 1)
    assert calls.count("recover") == 1


async def test_loop_pauses_on_an_unresolvable_coder_then_resumes_when_declared(monkeypatch, tmp_path, caplog):
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    probe = _Probe(coder=False)
    calls = _wire(monkeypatch, loop, probe)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle()
        assert calls == []
        paused = [r.message for r in caplog.records if "loop paused:" in r.message]
        assert len(paused) == 1 and "coder:" in paused[0] and "'proto'" in paused[0]

        probe.coder = True  # delegate declared in Settings ▸ Delegates — no restart
        await _settle()
        assert calls and calls[0] == "recover"

    loop._stop.set()
    await asyncio.wait_for(task, 1)


async def test_loop_with_no_coder_at_all_pauses_instead_of_dispatching(monkeypatch, tmp_path, caplog):
    """The fresh-archetype case: `loop_enabled: true` and NO `coder:` key. Before
    v0.42.0 this resolved to the phantom 'proto' and blocked every card at first
    dispatch; now it pauses with the pick-a-delegate hint."""
    loop = BoardLoop({"repo": str(tmp_path), "loop_enabled": True})
    assert loop.coder_name == ""
    calls = _wire(monkeypatch, loop, _Probe())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle()
    assert calls == []
    paused = [r.message for r in caplog.records if "loop paused:" in r.message]
    assert len(paused) == 1 and "no coder configured" in paused[0]
    loop._stop.set()
    await asyncio.wait_for(task, 1)


async def test_passing_preflight_runs_recovery_then_ticks_exactly_as_before(monkeypatch, tmp_path, caplog):
    """A healthy board: no pause line, recovery first, then the tick steps in order."""
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    calls = _wire(monkeypatch, loop, _Probe())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle(3)
    assert calls[:5] == ["recover", "reconcile", "sweep", "preflight", "spawn"]
    assert not any("loop paused" in r.message or "loop resumed" in r.message for r in caplog.records)
    loop._stop.set()
    await asyncio.wait_for(task, 1)


async def test_a_gap_opening_mid_run_pauses_the_ticks_again(monkeypatch, tmp_path, caplog):
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    probe = _Probe()
    host = _HostWithSeam()
    calls = _wire(monkeypatch, loop, probe, GapReporter(host))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle(3)
        assert "spawn" in calls
        probe.br = False  # br removed under a running loop
        await _settle(3)
        n = len(calls)
        await _settle(3)
        assert len(calls) == n  # no further ticks while paused
        assert host.calls[-1] == ("br", setup_check.BR_HINT)
        assert [c for c in host.calls if c[1] is not None] == [("br", setup_check.BR_HINT)]
        assert len([r for r in caplog.records if "loop paused:" in r.message]) == 1
    loop._stop.set()
    await asyncio.wait_for(task, 1)
    assert calls.count("recover") == 1  # recovery is a boot step, not re-run on resume


async def test_stop_while_paused_exits_cleanly(monkeypatch, tmp_path):
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    calls = _wire(monkeypatch, loop, _Probe(br=False))
    task = asyncio.create_task(loop._run())
    await _settle(2)
    loop._stop.set()
    await asyncio.wait_for(task, 1)  # returns (no hang, no CancelledError needed)
    assert calls == []


async def test_preflight_probe_error_fails_open(monkeypatch, tmp_path, caplog):
    """A broken probe must never wedge the loop — log and proceed (the per-tick
    BoardError path is still the backstop)."""
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    calls = _wire(monkeypatch, loop, _Probe())

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(loop, "_setup_status", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle(2)
    assert calls and calls[0] == "recover"
    assert any("setup preflight errored — proceeding" in r.message for r in caplog.records)
    loop._stop.set()
    await asyncio.wait_for(task, 1)


async def test_start_log_says_coder_unset(monkeypatch, caplog):
    async def _noop():
        pass

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        loop = BoardLoop({"loop_enabled": True})
        loop.start()
    if loop._task:
        loop._task.cancel()
    started = [r.message for r in caplog.records if "loop started" in r.message]
    assert started and "coder=<unset>" in started[0]


def test_loop_setup_status_builds_one_resolver_per_check(tmp_path, monkeypatch):
    """One roster read per preflight (review nit on #212): `_delegate_resolver()` is
    called once per `_setup_status`, and every configured name goes through it."""
    loop = BoardLoop({"coder": "proto", "coders": {"smart": "proto", "reasoning": "x"}, "repo": str(tmp_path)})
    loop._which = _which_all
    built, seen = [], []

    def _resolver():
        built.append(1)
        return lambda name: seen.append(name) or object()

    monkeypatch.setattr(loop, "_delegate_resolver", _resolver)
    s = loop._setup_status()
    assert built == [1] and seen == ["proto", "x"] and s["coder"]["ok"] is True
    assert s["loop_cfg_stale"] is False  # compared to its own config — never stale


def test_coder_seam_delegate_resolver_answers_none_without_a_roster():
    from project_board import coder_seam

    resolve = coder_seam.delegate_resolver("acp")  # no `plugins.delegates` in the host-free suite
    assert callable(resolve) and resolve("proto") is None


async def test_reload_naming_the_coder_resumes_the_paused_loop_without_a_restart(monkeypatch, tmp_path, caplog):
    """The fresh-archetype flow end to end: boot with NO coder → paused; the operator
    types the delegate into Settings ▸ Project Board → the host fires reload() → the
    running loop's next check passes → recovery + ticks. No restart."""
    loop = BoardLoop({"repo": str(tmp_path), "loop_enabled": True})
    host = _HostWithSeam()
    calls = _wire(monkeypatch, loop, _Probe(), GapReporter(host))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(loop._run())
        await _settle()
        assert calls == [] and ("coder", setup_check.NO_CODER_HINT) in host.calls
        assert loop.reload({"coder": "proto"}) == {"coder": ("", "proto")}
        await _settle()
    assert calls and calls[0] == "recover"
    assert host.calls[-1] == ("coder", None)
    loop._stop.set()
    await asyncio.wait_for(task, 1)


async def test_setup_gate_runs_the_preflight_off_the_event_loop(monkeypatch, tmp_path):
    """Review on #212: `br --version` + the roster read must not block the loop —
    `_setup_gate` awaits the status via `asyncio.to_thread`."""
    import threading

    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path), "loop_enabled": True})
    _wire(monkeypatch, loop, _Probe())
    threads = []
    real = loop._setup_status

    def _status():
        threads.append(threading.current_thread())
        return real()

    monkeypatch.setattr(loop, "_setup_status", _status)
    assert await loop._setup_gate() is True
    assert threads and threads[0] is not threading.main_thread()


# ── GET /status carries the setup block ──────────────────────────────────────────


class _Store:
    def list_features(self, **_kw):
        return []


def _client(monkeypatch, cfg, *, gap_reporter=None):
    monkeypatch.setattr(api, "get_store", lambda **_kw: _Store())
    app = FastAPI()
    app.include_router(api.build_data_router(cfg, gap_reporter=gap_reporter), prefix="/api/plugins/project_board")
    return TestClient(app)


def test_status_route_adds_the_setup_block_and_keeps_the_existing_keys(monkeypatch, tmp_path):
    _pin_probes(monkeypatch, which=_which_only("br"), delegates=_delegates())
    c = _client(monkeypatch, {"repo": str(tmp_path), "coder": "proto", "loop_enabled": True})
    body = c.get("/api/plugins/project_board/status").json()
    # the v0.40.0 keys are untouched — the view still branches on `bound`
    assert body["bound"] is True and body["repo"] == str(tmp_path) and body["db_path"] is False
    assert body["projects"] == []
    setup = body["setup"]
    assert set(setup) >= {"br", "gh", "coder", "repo", "loop_enabled", "loop_blockers", "ready"}
    assert setup["ready"] is False
    assert setup["br"]["ok"] is True and setup["br"]["version"] == "br 0.1.23"
    assert setup["gh"]["ok"] is False and "gh" in setup["gh"]["hint"]
    assert setup["coder"]["ok"] is False and setup["coder"]["missing"] == ["proto"]
    assert setup["repo"]["ok"] is True
    assert setup["loop_enabled"] is True and setup["loop_blockers"] == ["coder"]


def test_status_route_is_ready_when_setup_is_complete(monkeypatch, tmp_path):
    _pin_probes(monkeypatch, which=_which_all, delegates=_delegates("proto"))
    c = _client(monkeypatch, {"repo": str(tmp_path), "coder": "proto"})
    setup = c.get("/api/plugins/project_board/status").json()["setup"]
    assert setup["ready"] is True and setup["loop_blockers"] == []


def test_status_route_resyncs_the_host_gap_through_the_shared_reporter(monkeypatch, tmp_path):
    """A board with the loop OFF has no tick to clear a host warning once the operator
    fixes the gap; the board page polls /status, which re-reports through the SAME
    reporter register() handed the loop — so the warning clears from the page alone."""
    host = _HostWithSeam()
    rep = GapReporter(host)
    _pin_probes(monkeypatch, which=_which_only("br"), delegates=_delegates("proto"))
    c = _client(monkeypatch, {"repo": str(tmp_path), "coder": "proto"}, gap_reporter=rep)
    assert c.get("/api/plugins/project_board/status").json()["setup"]["gh"]["ok"] is False
    assert ("gh", setup_check.GH_HINT) in host.calls and len(host.calls) == 8  # first eval: all keys
    c.get("/api/plugins/project_board/status")
    assert len(host.calls) == 8  # steady state: no re-send per poll
    monkeypatch.setattr(setup_check.shutil, "which", _which_all)  # gh installed
    assert c.get("/api/plugins/project_board/status").json()["setup"]["ready"] is True
    assert host.calls[-1] == ("gh", None)


def test_status_route_reports_the_running_loops_stale_restart_knobs(monkeypatch, tmp_path):
    """Reload drift (review on #212): the routers see the NEW config, the running
    loop keeps its construction-time `repo`/`coders`. /status compares the live
    config to the loop's published snapshot and says "restart to apply" — on its own
    line and on the affected failing hint — instead of reporting the new config as
    the loop's state. `coder` is live, so it is never a stale key."""
    _pin_probes(monkeypatch, which=_which_all, delegates=_delegates("proto"))
    old = {"repo": str(tmp_path), "coder": "proto", "loop_enabled": True}
    running = BoardLoop(old)
    setup_check.publish_loop_snapshot(running.cfg)  # what start() does when the loop actually runs
    try:
        # a reload changed repo (restart knob) + coder (live — applied via reload(),
        # which republishes the snapshot)
        running.reload({"coder": "other"})
        assert setup_check.live_loop_snapshot()["coder"] == "other"
        new = {**old, "repo": "/moved/elsewhere", "coder": "other"}
        setup = _client(monkeypatch, new).get("/api/plugins/project_board/status").json()["setup"]
        assert setup["loop_cfg_stale"] is True and setup["loop_cfg_stale_keys"] == ["repo"]
        assert "restart the agent to apply" in setup["loop_cfg_stale_hint"]
        assert setup["repo"]["ok"] is False and "restart the agent to apply: repo" in setup["repo"]["hint"]
        assert setup["coder"]["ok"] is False  # 'other' unresolvable — but NOT stale (live knob)
        assert "restart" not in setup["coder"]["hint"]
        # same config as the loop → not stale
        setup = _client(monkeypatch, {**old, "coder": "other"}).get("/api/plugins/project_board/status").json()["setup"]
        assert setup["loop_cfg_stale"] is False and setup["loop_cfg_stale_hint"] == ""
    finally:
        setup_check.publish_loop_snapshot(None)
    # no running loop (stopped / never started) → nothing to be stale against
    setup_check.publish_loop_snapshot(None)
    setup = _client(monkeypatch, new).get("/api/plugins/project_board/status").json()["setup"]
    assert setup["loop_cfg_stale"] is False


def test_stale_loop_keys_compare_only_restart_knobs():
    snap = setup_check.snapshot_of(
        {"repo": "/a", "coders": {"smart": "x"}, "coder": "p", "projects": {"old": {"repo": "/old"}}}
    )
    assert (
        setup_check.stale_loop_keys(
            {"repo": "/a", "coders": {"smart": "x"}, "coder": "q", "projects": {"new": {"repo": "/new"}}},
            snap,
        )
        == []
    )
    assert setup_check.stale_loop_keys({"repo": "/b", "coders": {"smart": "y"}}, snap) == ["coders", "repo"]
    assert setup_check.stale_loop_keys({"repo": "/b"}, None) == []


def test_status_route_runs_the_preflight_off_the_event_loop(monkeypatch, tmp_path):
    import threading

    seen = []

    def _status(cfg, **kw):
        seen.append(threading.current_thread())
        return {"ready": True, "loop_cfg_stale_hint": ""}

    monkeypatch.setattr(setup_check, "setup_status", _status)
    _client(monkeypatch, {"repo": str(tmp_path)}).get("/api/plugins/project_board/status")
    assert seen and seen[0] is not threading.main_thread()


def test_status_route_never_500s_on_a_preflight_error(monkeypatch, tmp_path):
    def boom(_cfg):
        raise RuntimeError("preflight exploded")

    monkeypatch.setattr(setup_check, "setup_status", boom)
    c = _client(monkeypatch, {"repo": str(tmp_path)})
    r = c.get("/api/plugins/project_board/status")
    assert r.status_code == 200
    assert r.json()["bound"] is True
    assert r.json()["setup"] == {"ready": False, "error": "preflight exploded"}


# ── the board page renders each failing check ────────────────────────────────────


def test_board_page_renders_setup_gaps_from_the_status_block():
    from project_board.board_view import BOARD_PAGE

    # one label per check, rendered in the preflight's order, via the esc()'d hint
    assert (
        'const SETUP_CHECKS = [["br", "beads CLI (br)"], ["gh", "GitHub CLI (gh)"], ["coder", "coder delegate"], ["repo", "repo"]];'
        in BOARD_PAGE
    )
    assert "function setupGapItems(setup)" in BOARD_PAGE
    assert "if (!c || c.ok !== false) continue;" in BOARD_PAGE  # only FAILING checks render
    assert 'esc(String(c.hint || (key + " check failed")))' in BOARD_PAGE
    # a bound-but-broken board gets the gap card (warning), both on a read failure and
    # on a successful read whose preflight fails — never a silent green. Both call
    # sites hand the full /status body through so held projects ride along (#261).
    assert "function renderSetupGaps(setup, e, s)" in BOARD_PAGE
    assert "if (s && s.setup && s.setup.ready === false) renderSetupGaps(s.setup, null, s);" in BOARD_PAGE
    assert "if (s && s.setup && s.setup.ready === false) { renderSetupGaps(s.setup, e, s); return; }" in BOARD_PAGE
    assert '"pl-callout pl-callout--warning"' in BOARD_PAGE
    # the loop line tells paused-vs-off, and says "restart" when the running loop's
    # config snapshot lags the live config (review on #212)
    assert "The build loop is <b>paused</b> on: " in BOARD_PAGE
    assert "loop_enabled: false" in BOARD_PAGE
    assert "if (setup && setup.loop_cfg_stale) html += " in BOARD_PAGE
    assert "<b>Running loop is stale:</b> " in BOARD_PAGE
    assert "function renderLoopStale(setup)" in BOARD_PAGE
    assert (
        "else if (s && s.setup && (s.setup.loop_cfg_stale || s.setup.db_override_ignored || "
        "s.setup.legacy_store_hint)) renderLoopStale(s.setup);" in BOARD_PAGE
    )
    # the D3 advisories (#260) ride the same info callout: an inert db_path override
    # on a multi-project board, and a repo still carrying the pre-D3 per-repo
    # workspace — lines, never a failing check or a pause
    assert "if (setup && setup.db_override_ignored) html += " in BOARD_PAGE
    assert "<b>db_path override ignored:</b> " in BOARD_PAGE
    assert "if (setup && setup.legacy_store_hint) html += " in BOARD_PAGE
    assert "<b>Pre-D3 repo workspace detected:</b> " in BOARD_PAGE
    # a gh-only gap is NOT "can't run" (gh isn't a loop blocker) — softer copy
    assert "function setupBlocking(setup)" in BOARD_PAGE
    assert 'key !== "gh" && setup && setup[key] && setup[key].ok === false' in BOARD_PAGE
    assert "GitHub CLI missing — PRs can&#39;t open or merge until it is installed." in BOARD_PAGE
    # the unbound card now carries the other gaps too, and says there is no default coder
    assert "function renderSetup(e, setup)" in BOARD_PAGE
    assert "<b>Also missing:</b>" in BOARD_PAGE
    assert "there is no default" in BOARD_PAGE
    # status is fetched through the kit's authed fetch only (rules 2+3) — the page
    # never hand-rolls a fetch for it
    assert BOARD_PAGE.count('api("/api/plugins/project_board/status")') == 2
    assert 'fetch("/api/plugins/project_board/status")' not in BOARD_PAGE


# ── the manifest no longer ships a phantom coder ─────────────────────────────────


def test_manifest_config_has_no_default_coder():
    from pathlib import Path

    import yaml

    m = yaml.safe_load((Path(pb.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert m["config"]["coder"] == ""


# ── __init__ wires every store construction onto the resolved db (D3, #260) ───────
# The tool store_kw and register()'s coder-monitor persist factory resolve the db at
# BUILD time through projects.store_db_path — a blank/absent db_path lands on the ONE
# instance-default store (never per-repo discovery), an explicit path stays the pin.


def test_board_tools_construct_stores_on_the_instance_default_db(monkeypatch, tmp_path):
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    seen = {}
    monkeypatch.setattr("project_board.store.get_store", lambda **kw: seen.update(kw) or _Store())
    tools = {t.name: t for t in pb._board_tools({"coder": "proto", "projects": _two_projects(tmp_path)})}
    assert tools["board_list"].invoke({}) == "[]"
    assert seen["db"] == "/inst/project_board/.beads/beads.db"


def test_board_tools_keep_an_explicit_db_path_pin(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("project_board.store.get_store", lambda **kw: seen.update(kw) or _Store())
    tools = {t.name: t for t in pb._board_tools({"db_path": str(tmp_path / "pin.db")})}
    tools["board_list"].invoke({})
    assert seen["db"] == str(tmp_path / "pin.db")


def test_register_persist_store_factory_rides_the_resolved_db(monkeypatch, tmp_path):
    """register()'s coder-monitor persist factory (#226) is built from the same
    resolved db — a blank db_path lands its snapshots in the instance store, never a
    repo `.beads/`."""
    from project_board import coder_seam
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    _pin_probes(monkeypatch, which=_which_all, delegates=_delegates("proto"))
    seen = {}
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: seen.update(kw) or _Store())
    captured = {}
    monkeypatch.setattr(coder_seam, "set_store_factory", lambda fn: captured.update(fn=fn))
    pb.register(_Registry({"coder": "proto", "repo": str(tmp_path)}))
    captured["fn"]()
    assert seen["db"] == "/inst/project_board/.beads/beads.db"
    assert seen["repo"] == str(tmp_path)


# ── #354: review-status publication capability probe ──────────────────────────────


def _probe_cfg(tmp_path, **over):
    cfg = {"coder": "proto", "repo": str(tmp_path), "review_gate": True}
    cfg.update(over)
    return cfg


def _probe_status(tmp_path, *, probe, which=_which_all, gate=True, run=None):
    return setup_status(
        _probe_cfg(tmp_path, review_gate=gate),
        which=which,
        delegates=_delegates("proto"),
        run=run or _fake_run("br 0.1.23"),
        status_probe=probe,
    )


def test_review_status_quiet_when_the_gate_is_off(tmp_path):
    """r6: with the review gate OFF there is nothing to publish — the advisory is quiet and the
    probe is never even consulted."""
    consulted = []

    def _probe(run, cwd):
        consulted.append(1)
        return (False, "would warn if asked")

    s = _probe_status(tmp_path, probe=_probe, gate=False)
    assert s["review_status_ok"] is True and s["review_status_hint"] == ""
    assert consulted == []  # gate off → never probed


def test_review_status_warns_on_an_incapable_credential(tmp_path):
    """r6: gate on + gh present + a proven-incapable credential → a DISTINCT actionable warning
    (not a failing check, not a pause) naming the fix; `ready` is unaffected."""
    s = _probe_status(tmp_path, probe=lambda run, cwd: (False, "App-only token detail"))
    assert s["review_status_ok"] is False
    assert "App-only token detail" in s["review_status_hint"]
    assert "statuses:write" in s["review_status_hint"] and "QA panel" in s["review_status_hint"]
    assert s["ready"] is True  # advisory only — it never fails the preflight or pauses the loop


def test_review_status_quiet_when_capable(tmp_path):
    """r6: a status-capable credential → the advisory is quiet."""
    s = _probe_status(tmp_path, probe=lambda run, cwd: (True, ""))
    assert s["review_status_ok"] is True and s["review_status_hint"] == ""


def test_review_status_quiet_when_gh_missing_no_double_warn(tmp_path):
    """r6/r7: gate on but gh MISSING — the `gh` check already owns that failure, so the status
    advisory stays quiet (no double-warn) and the probe is not consulted."""
    consulted = []

    def _probe(run, cwd):
        consulted.append(1)
        return (False, "unused")

    s = _probe_status(tmp_path, probe=_probe, which=_which_only("br"))
    assert s["gh"]["ok"] is False  # the gh check reports it
    assert s["review_status_ok"] is True and s["review_status_hint"] == ""
    assert consulted == []


def test_review_status_probe_runs_once_per_process(tmp_path):
    """r6: the capability probe is a BOUNDED startup cost — sampled once and cached, so the
    per-tick / per-page-load re-check never re-shells gh (it does not silently degrade on every
    card)."""
    calls = []

    def _probe(run, cwd):
        calls.append(1)
        return (True, "")

    _probe_status(tmp_path, probe=_probe)
    _probe_status(tmp_path, probe=_probe)
    assert calls == [1]  # cached across evaluations


def _multi_run(*, user=None, repo=None, headers=None):
    """An argv-aware fake ``run`` for the two/three-step status probe (#354/bd-doo0): routes
    ``gh api user --jq`` (credential-shape), ``gh api repos/{owner}/{repo} --jq .permissions.push``
    (per-repo capability), and ``gh api user --include`` (the scope-header fallback) to their own
    responses. Each of ``user`` / ``repo`` / ``headers`` is ``(stdout, rc[, stderr])`` or ``None``
    (→ rc 1, empty). Records ``(argv, kw)`` on ``.calls``."""

    def _resp(spec):
        if spec is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        stdout, rc = spec[0], spec[1]
        stderr = spec[2] if len(spec) > 2 else ""
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if "--include" in argv:
            return _resp(headers)
        if any(str(a).startswith("repos/") for a in argv):
            return _resp(repo)
        return _resp(user)

    run.calls = calls
    return run


def test_default_status_probe_user_read_discriminates_app_vs_user():
    """r6: the FIRST read (`gh api user`) still discriminates credential SHAPE — a GitHub App
    installation token (either 403 signature) is proven incapable; any OTHER `gh api user` failure
    (offline / unauthenticated) fails open (the gh/auth checks own those); a raised probe fails
    open. A user/PAT rc-0 is necessary but NOT sufficient — it defers to the per-repo read."""
    ok, detail = setup_check._default_status_probe(
        _multi_run(user=("HTTP 403: You must authenticate via a GitHub App", 1))
    )
    assert ok is False and "installation token" in detail
    # the symmetric App-installation signature, arriving via stderr
    ok, detail = setup_check._default_status_probe(_multi_run(user=("", 1, "not accessible by integration")))
    assert ok is False and "installation token" in detail
    # an unrelated `gh api user` failure never manufactures the status warning
    ok, _ = setup_check._default_status_probe(_multi_run(user=("could not connect", 1)))
    assert ok is True
    ok, _ = setup_check._default_status_probe(_fake_run(raise_exc=TimeoutError("slow")))
    assert ok is True


def test_default_status_probe_per_repo_push_decides_a_user_pat():
    """r1/r2: a user/PAT-shaped credential (rc 0 on `gh api user`) is decided by the PER-REPOSITORY
    `.permissions.push` read on the checkout's own repo — `push` true ⇒ capable, `push` false ⇒
    the former-`gh api user` false-positive is now caught with an actionable, repo-specific
    warning (a token valid elsewhere but not on THIS repo)."""
    ok, detail = setup_check._default_status_probe(_multi_run(user=("octocat", 0), repo=("true", 0)))
    assert ok is True and detail == ""

    ok, detail = setup_check._default_status_probe(_multi_run(user=("octocat", 0), repo=("false", 0)))
    assert ok is False
    assert "no write access to this repository" in detail


def test_default_status_probe_targets_the_checkout_repo():
    """r1: the per-repo read uses gh's templated `repos/{owner}/{repo}` path (gh fills the slug
    from the checkout's remote) run in the supplied `cwd`, so it assesses the SAME repository the
    reviewed status publishes to — not the process cwd."""
    run = _multi_run(user=("octocat", 0), repo=("true", 0))
    setup_check._default_status_probe(run, cwd="/some/repo")
    repo_calls = [(argv, kw) for argv, kw in run.calls if any(str(a).startswith("repos/") for a in argv)]
    assert repo_calls, "the per-repo capability read never fired"
    argv, kw = repo_calls[0]
    assert "repos/{owner}/{repo}" in argv and ".permissions.push" in argv
    assert kw.get("cwd") == "/some/repo"


def test_default_status_probe_ambiguous_per_repo_falls_back_to_scope_headers():
    """r3: when the per-repo `.permissions.push` read is AMBIGUOUS (rc 0 but no `.permissions`, or
    unreadable), the probe consults the `X-OAuth-Scopes` response header as a SECONDARY fallback —
    a scope list carrying `repo`/`public_repo` is capable, a non-empty list without them is a
    proven-incapable classic PAT, and an EMPTY header (a fine-grained token) proves nothing → fail
    open."""
    # ambiguous per-repo (empty `.permissions.push`) + a header carrying `repo` → capable
    ok, _ = setup_check._default_status_probe(
        _multi_run(user=("octocat", 0), repo=("", 0), headers=("HTTP/2 200\r\nX-OAuth-Scopes: repo, gist\r\n", 0))
    )
    assert ok is True
    # ambiguous per-repo + a non-empty scope list WITHOUT a status-write scope → proven incapable
    ok, detail = setup_check._default_status_probe(
        _multi_run(user=("octocat", 0), repo=("", 0), headers=("X-OAuth-Scopes: gist, read:org\r\n", 0))
    )
    assert ok is False and "OAuth scopes" in detail
    # ambiguous per-repo + an EMPTY scope header (fine-grained token) → prove nothing → fail open
    ok, _ = setup_check._default_status_probe(
        _multi_run(user=("octocat", 0), repo=("", 0), headers=("X-OAuth-Scopes: \r\n", 0))
    )
    assert ok is True


def test_default_status_probe_fails_open_when_capability_is_unreadable():
    """r3: neither the per-repo read NOR the scope fallback is readable (offline / SSO wall / no
    scope header at all) — capability cannot be proven either way, so the probe fails open to
    `(True, "")` rather than a startup false alarm."""
    # user ok, repo read fails (offline), scope header present but no X-OAuth-Scopes line
    ok, _ = setup_check._default_status_probe(
        _multi_run(user=("octocat", 0), repo=None, headers=("HTTP/2 200\r\ncontent-type: application/json\r\n", 0))
    )
    assert ok is True
    # user ok, repo read fails, scope fallback also fails → still fail open
    ok, _ = setup_check._default_status_probe(_multi_run(user=("octocat", 0), repo=None, headers=None))
    assert ok is True
    # a per-repo read that surfaces the App-only signature (rc 0 user, App 403 on the repo) warns
    ok, detail = setup_check._default_status_probe(
        _multi_run(user=("octocat", 0), repo=("", 1, "You must authenticate as a GitHub App"))
    )
    assert ok is False and "installation token" in detail


def test_default_status_probe_never_mutates_anything():
    """r4: every read the probe issues is a bounded, read-only GET — no `--method POST/PATCH`, no
    write path — so proving capability can never create a live status or mutate a resource."""
    run = _multi_run(user=("octocat", 0), repo=("false", 0))
    setup_check._default_status_probe(run)
    for argv, _kw in run.calls:
        assert "--method" not in argv and "statuses" not in " ".join(str(a) for a in argv)


def test_setup_status_probes_the_board_repo_as_cwd(tmp_path):
    """r1: setup_status runs the default probe in the BOARD's own checkout (cfg['repo']) so the
    per-repo capability read assesses the repository the reviewed status will be published to."""
    seen = {}

    def _probe(run, cwd):
        seen["cwd"] = cwd
        return (True, "")

    _probe_status(tmp_path, probe=_probe)
    assert seen["cwd"] == str(tmp_path)


def test_reporter_forwards_the_review_status_warning_and_clears_it():
    """r6: the capability warning reaches the operator through the same gap seam as the other
    checks — sent once when it appears, cleared (None) once the credential becomes capable."""
    host = _HostWithSeam()
    rep = GapReporter(host)
    base = _status()  # every SETUP_KEY ok
    warned = {**base, "review_status_hint": "cannot publish QA panel status — fix the token"}
    rep.report(warned)
    assert (setup_check.REVIEW_STATUS_KEY, "cannot publish QA panel status — fix the token") in host.calls
    host.calls.clear()
    rep.report({**base, "review_status_hint": ""})  # capability restored
    assert (setup_check.REVIEW_STATUS_KEY, None) in host.calls


def test_the_preflight_validates_every_sibling_of_a_list_rung():
    """#362's loop half shipped without this and GATED A LIVE BOARD: `coders.smart`
    became `[codex, sonnet]`, the preflight stringified the list to
    `"['codex', 'sonnet']"`, reported it as one missing delegate, and `loop_blockers`
    went to `['coder']`.

    Every sibling must be validated individually — that is the point of the gap check:
    a typo'd provider fails at STARTUP, not on the card that happens to rotate onto it."""
    names = setup_check.coder_names({"coders": {"smart": ["codex", "sonnet"], "reasoning": "opus"}})
    assert names == ["codex", "sonnet", "opus"]
    # a string rung is unchanged — every pre-#362 config reads identically
    assert setup_check.coder_names({"coders": {"smart": "sonnet"}}) == ["sonnet"]
    # blanks never become phantom names, and duplicates across rungs collapse
    assert setup_check.coder_names({"coders": {"smart": ["a", "", "b"], "opus": "a"}}) == ["a", "b"]


def test_a_list_rung_is_not_reported_as_an_uncovered_tier():
    """The other half of the same bug: `uncovered_tiers` must see a populated list rung
    as COVERED, or the board reports a ladder gap and pauses on a perfectly valid config."""
    assert setup_check.uncovered_tiers({"smart": ["codex", "sonnet"], "reasoning": "opus", "opus": "opus"}) == []
    assert "smart" in setup_check.uncovered_tiers({"smart": [], "reasoning": "opus", "opus": "opus"})

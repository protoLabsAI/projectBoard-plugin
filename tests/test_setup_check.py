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
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, setup_check
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
    assert host.calls == [("br", "br hint"), ("gh", None), ("coder", "coder hint"), ("repo", None), ("loop", None)]
    # steady state → nothing forwarded (a 30 s tick must not spam the host)
    assert rep.report(_status(br=False, coder=False)) == {}
    assert len(host.calls) == 5
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
    assert ("coder", None) in host.calls[5:]


def test_reporter_forwards_the_loop_stale_key():
    host = _HostWithSeam()
    rep = GapReporter(host)
    rep.report({**_status(), "loop_cfg_stale_hint": "config changed since the loop started (repo) — restart"})
    assert host.calls[-1] == ("loop", "config changed since the loop started (repo) — restart")
    assert rep.report({**_status(), "loop_cfg_stale_hint": ""}) == {"loop": None}


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
    # every key on the first evaluation (br + loop as clears), in render order
    assert [k for k, _ in reg.gaps] == ["br", "gh", "coder", "repo", "loop"]
    assert msgs["br"] is None and msgs["loop"] is None
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
        assert ("br", setup_check.BR_HINT) in host.calls and len(host.calls) == 5  # first eval: all keys

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
    assert ("gh", setup_check.GH_HINT) in host.calls and len(host.calls) == 5  # first eval: all keys
    c.get("/api/plugins/project_board/status")
    assert len(host.calls) == 5  # steady state: no re-send per poll
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
    # on a successful read whose preflight fails — never a silent green
    assert "function renderSetupGaps(setup, e)" in BOARD_PAGE
    assert "if (s && s.setup && s.setup.ready === false) renderSetupGaps(s.setup, null);" in BOARD_PAGE
    assert "if (s && s.setup && s.setup.ready === false) { renderSetupGaps(s.setup, e); return; }" in BOARD_PAGE
    assert '"pl-callout pl-callout--warning"' in BOARD_PAGE
    # the loop line tells paused-vs-off, and says "restart" when the running loop's
    # config snapshot lags the live config (review on #212)
    assert "The build loop is <b>paused</b> on: " in BOARD_PAGE
    assert "loop_enabled: false" in BOARD_PAGE
    assert "if (setup && setup.loop_cfg_stale) html += " in BOARD_PAGE
    assert "<b>Running loop is stale:</b> " in BOARD_PAGE
    assert "function renderLoopStale(setup)" in BOARD_PAGE
    assert "else if (s && s.setup && s.setup.loop_cfg_stale) renderLoopStale(s.setup);" in BOARD_PAGE
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

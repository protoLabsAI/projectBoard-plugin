"""Host-free strict routing contracts (#576)."""

import json

import pytest

from project_board import setup_check, store, worktree
from project_board.loop import BoardLoop, prefer_live_sibling
from tests.test_loop import FEATURE, FakeLoopStore, _drive_with


LADDER = {"standard": ["sonnet", "codex"], "reasoning": ["sonnet"], "advanced": ["codex", "sonnet"]}


class LadderStore(FakeLoopStore):
    def __init__(self, tier="standard"):
        super().__init__()
        self.tier = tier

    def current_tier(self, fid):
        return self.tier

    def escalate(self, fid, reason):
        self.calls.append(("escalate", fid, reason))
        index = store.TIER_LADDER.index(self.tier) + 1
        if index == len(store.TIER_LADDER):
            return None
        self.tier = store.TIER_LADDER[index]
        return self.tier


def test_project_override_and_empty_disable():
    loop = BoardLoop(
        {
            "routing": {"exclusive": ["codex"]},
            "coders": LADDER,
            "projects": {
                "app": {"repo": "/app", "routing": {"exclusive": ["sonnet"]}},
                "free": {"repo": "/free", "routing": {"exclusive": []}},
                "inherit": {"repo": "/inherit"},
            },
        }
    )
    assert loop._coders_for({"project": "app"}) == {tier: ["sonnet"] for tier in LADDER}
    assert loop._coders_for({"project": "free"}) == LADDER
    assert loop._coders_for({"project": "inherit"}) == {
        "standard": ["codex"],
        "reasoning": [],
        "advanced": ["codex"],
    }


@pytest.mark.parametrize("tier", list(LADDER))
@pytest.mark.parametrize("live", [[], ["codex"], ["sonnet"], ["codex", "sonnet"]])
def test_absent_and_empty_decisions_are_byte_identical(tier, live):
    decisions = []
    for routing in [None, {}, {"exclusive": []}]:
        cfg = {"coder": "sonnet", "coders": LADDER}
        if routing is not None:
            cfg["routing"] = routing
        loop = BoardLoop(cfg)
        loop._resolve_delegate = lambda name, _: name if name in live else None
        coders = loop._coders_for({})
        siblings = loop._live_rung(tier, coders[tier], coders, exclusive=loop._exclusive_for({}))
        decisions.append(json.dumps([coders, siblings, [prefer_live_sibling(siblings, i, set()) for i in range(3)]]))
    assert decisions[0] == decisions[1] == decisions[2]


@pytest.mark.parametrize("feedback", ["", "CI failed: repair the tests", "REQUESTED CHANGES: fix the review findings"])
@pytest.mark.parametrize("start", ["standard", "reasoning"])
async def test_implement_and_fix_dispatch_only_allowed(monkeypatch, feedback, start):
    dispatched = []
    board = LadderStore(start)

    async def dispatch(coder, *args, **kwargs):
        dispatched.append(coder)
        return "implemented with tests"

    async def open_pr(*args, **kwargs):
        return "https://github.com/o/r/pull/1"

    def seed(loop):
        loop._resolve_delegate = lambda name, _: name
        loop._dispatch_reached_model = lambda fid: True
        loop.decompose_after_timeouts = 0
        if feedback:
            loop._ci_feedback[FEATURE["id"]] = feedback

    await _drive_with(
        monkeypatch,
        cfg={"coder": "sonnet", "coders": LADDER, "routing": {"exclusive": ["codex"]}},
        store=board,
        seed=seed,
        dispatch=dispatch,
        open_pr=open_pr,
    )
    assert dispatched == ["codex"]
    assert "open_review" in board.names()
    assert board.tier == ("advanced" if start == "reasoning" else "standard")


@pytest.mark.parametrize("ladder", [LADDER, {}, {"standard": "codex"}])
async def test_unavailable_allowed_delegate_blocks_without_base_fallback(monkeypatch, ladder):
    board = LadderStore()
    dispatched = []

    async def dispatch(*args, **kwargs):
        dispatched.append(args)

    def seed(loop):
        loop._resolve_delegate = lambda name, _: "sonnet" if name == "sonnet" else None

    await _drive_with(
        monkeypatch,
        cfg={"coder": "sonnet", "coders": ladder, "routing": {"exclusive": ["codex"]}},
        store=board,
        seed=seed,
        dispatch=dispatch,
        open_pr=dispatch,
    )
    assert dispatched == []
    blocked = [c for c in board.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    assert "routing.exclusive [codex]" in blocked[0][2]


@pytest.mark.parametrize("failure", ["timeout", "empty"])
async def test_capability_escalation_skips_disallowed_rung(monkeypatch, failure):
    board = LadderStore()
    dispatched = []

    async def dispatch(coder, *args, **kwargs):
        dispatched.append((board.tier, coder))
        if failure == "timeout":
            raise worktree.CoderTimeout("coder timed out after 1800s")
        raise worktree.NoChangesError("no diff produced")

    def seed(loop):
        loop._resolve_delegate = lambda name, _: name
        loop._dispatch_reached_model = lambda fid: True
        loop.decompose_after_timeouts = 0

    await _drive_with(
        monkeypatch,
        cfg={"coder": "sonnet", "coders": LADDER, "routing": {"exclusive": ["codex"]}},
        store=board,
        seed=seed,
        dispatch=dispatch,
        open_pr=dispatch,
    )
    repeats = 2 if failure == "empty" else 1
    assert dispatched == [("standard", "codex")] * repeats + [("advanced", "codex")] * repeats
    assert "routing.exclusive [codex]" in next(c[2] for c in board.calls if c[0] == "flag_blocked")


def test_setup_hint_is_only_the_real_problem_in_plain_words():
    # The ONE real problem — a project's coder limit names an agent that isn't set up — is
    # reported in plain operator words with the project and agent filled in. No internal routing
    # names (routing.exclusive / coder.solve / Max-Mode / decompose), and no "not covered" note.
    cfg = {
        "routing": {"exclusive": ["codex"]},
        "projects": {
            "app": {"repo": "/app", "routing": {"exclusive": ["missing"]}},
        },
    }
    hint = setup_check.routing_exclusive_hint(cfg, lambda name: name if name == "codex" else None)
    assert hint == (
        "app cards can only use missing, but missing isn't set up, so those cards can't start. "
        "Set up missing, or remove it from app's coder limit."
    )
    for internal in ["not covered", "routing.exclusive", "coder.solve", "Max-Mode", "decompose"]:
        assert internal not in hint
    assert setup_check.routing_exclusive_hint({}, lambda _: None) == ""


def test_setup_hint_empty_when_every_limited_agent_is_available():
    # With a coder limit set and every named agent available there is NO gap at all.
    cfg = {"coder": "codex", "routing": {"exclusive": ["codex", "sonnet"]}}
    assert setup_check.routing_exclusive_hint(cfg, lambda name: name) == ""


def test_limit_names_multiple_agents_lists_them_in_plain_words():
    cfg = {"routing": {"exclusive": ["codex", "missing"]}, "projects": {"app": {"repo": "/app"}}}
    hint = setup_check.routing_exclusive_hint(cfg, lambda name: name if name == "codex" else None)
    assert hint == (
        "app cards can only use codex and missing, but missing isn't set up, so those cards "
        "can't start. Set up missing, or remove it from app's coder limit."
    )


def test_setup_status_reports_limit_gap_and_keeps_coder_rungs_clean(tmp_path):
    from tests.test_setup_check import _fake_run, _which_all

    status = setup_check.setup_status(
        {"coder": "codex", "repo": str(tmp_path), "routing": {"exclusive": ["codex", "missing"]}},
        which=_which_all,
        delegates=lambda name: name if name == "codex" else None,
        run=_fake_run("br 0.1.23"),
        loop_snapshot={},
    )
    # The coder-limit gap rides its OWN key with the plain copy; it is NEVER folded into
    # coder_rungs_hint (so the informational note can't masquerade as a coder-rungs banner).
    assert "missing isn't set up" in status["routing_exclusive_hint"]
    assert "coder limit" in status["routing_exclusive_hint"]
    assert "not covered" not in status["routing_exclusive_hint"]
    assert "missing" not in status["coder_rungs_hint"]
    assert status["coder_rungs_hint"] == ""


def test_limit_gap_reports_one_setup_gap_with_a_configure_action():
    from project_board.setup_check import ROUTING_EXCLUSIVE_KEY, GapReporter

    class _Host:
        def __init__(self):
            self.calls = []

        def report_setup_gap(self, key, message, *, label=None, action=None):
            self.calls.append((key, message, action))

    host = _Host()
    reporter = GapReporter(host)
    assert reporter.action_supported is True
    hint = setup_check.routing_exclusive_hint(
        {"routing": {"exclusive": ["missing"]}, "projects": {"app": {"repo": "/app"}}},
        lambda _name: None,
    )
    reporter.report({"routing_exclusive_hint": hint})

    gaps = [c for c in host.calls if c[0] == ROUTING_EXCLUSIVE_KEY]
    assert len(gaps) == 1
    _key, message, action = gaps[0]
    assert message == (
        "app cards can only use missing, but missing isn't set up, so those cards can't start. "
        "Set up missing, or remove it from app's coder limit."
    )
    assert action == {"kind": "plugin_config", "label": "Open Project Board settings", "fields": ["routing"]}


def test_effective_review_and_helpers_rows_carry_the_operator_copy():
    from project_board import routing

    result = routing.effective_routes({"coders": LADDER, "routing": {"exclusive": ["codex"]}})
    proj = next(iter(result["projects"].values()))
    paths = {p["path"]: p for p in proj["paths"]}
    assert paths["review"]["covered"] is False
    assert paths["helpers"]["covered"] is False
    assert (
        paths["review"]["detail"]
        == "Reviews keep using the board's reviewer. A coder limit doesn't change who reviews."
    )
    assert (
        paths["helpers"]["detail"]
        == "Splitting big cards, judging and other helper steps keep using their usual agents."
    )
    assert (
        proj["exclusive_note"] == "Only the coders that build and fix this project's cards are limited to these agents."
    )
    # No internal routing names leak into any operator-facing row.
    for row in proj["paths"]:
        for internal in ["routing.exclusive", "coder.solve", "Max-Mode", "decompose"]:
            assert internal not in row["detail"]


@pytest.mark.parametrize("base", ["codex", "sonnet"])
@pytest.mark.parametrize("coders", [{}, {"standard": ["codex"]}, dict.fromkeys(store.TIER_LADDER, "codex")])
@pytest.mark.parametrize("difficulty", ["small", "medium", "large", "architectural"])
@pytest.mark.parametrize("available", [True, False])
async def test_single_coder_requires_allowlist_membership(monkeypatch, base, coders, difficulty, available):
    dispatched = []
    board = LadderStore(store.DIFFICULTY_TIER[difficulty])

    def unexpected_tier_read(fid):
        pytest.fail("single-coder routing must not consult difficulty/tier")

    board.current_tier = unexpected_tier_read

    async def dispatch(coder, *args, **kwargs):
        dispatched.append(coder)
        return "implemented with tests"

    async def open_pr(*args, **kwargs):
        return "https://github.com/o/r/pull/1"

    _, board = await _drive_with(
        monkeypatch,
        cfg={"coder": base, "coders": coders, "routing": {"exclusive": ["codex"]}},
        store=board,
        feature={**FEATURE, "difficulty": difficulty},
        seed=lambda loop: setattr(loop, "_resolve_delegate", lambda name, _: name if available else None),
        dispatch=dispatch,
        open_pr=open_pr,
    )
    allowed = base == "codex" and available
    assert dispatched == (["codex"] if allowed else [])
    assert ("flag_blocked" in board.names()) == (not allowed)
    assert "escalate" not in board.names()
    assert "record_attempt" not in board.names()
    if not allowed:
        assert "routing.exclusive [codex]" in next(c[2] for c in board.calls if c[0] == "flag_blocked")


def test_top_level_policy_change_is_live():
    before = {"routing": {"exclusive": []}}
    after = {"routing": {"exclusive": ["codex"]}}
    assert setup_check.stale_loop_keys(after, setup_check.snapshot_of(before)) == []

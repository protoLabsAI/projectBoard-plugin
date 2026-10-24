"""#581 (ADR 0122 D8): `routing.usage_policy` — keep a named account under a target %.

The board can steer a matching-tier card onto a specific delegate when a watched account
is at or above a used-% line — the LOWEST-precedence routing reorder (exclusive > gates >
down/blocked > headroom > policy). The decision is a PURE function (``apply_usage_policy``
/ ``policy_decision``) so it is tested on snapshot dicts, plus a drive-level check with a
fake SDK and a validation check.

The invariant it inherits from #579 (ADR 0122 D4): UNKNOWN == today. An account with no
snapshot never fires, so dispatch is byte-for-byte the headroom behaviour. A rule only ever
reorders WITHIN the already exclusive-filtered rung — it never adds a delegate the rung
lacks, and never a spent/down/blocked one.
"""

from __future__ import annotations

import pytest

from project_board import worktree
import project_board.loop as loop_mod
from project_board.loop import BoardLoop, apply_usage_policy
from project_board.routing import validate_routing

from test_loop import _rung_env


def _snap(headroom, *, usable=True, resets_at=None):
    """One ``graph.sdk.provider_usage`` snapshot with ``headroom`` percent of the binding
    window still free — so the account is ``100 - headroom`` percent used."""
    return {"usable": usable, "headroom_pct": headroom, "binding_window": "5h", "resets_at": resets_at}


_RULE = {"account": "anthropic-oauth", "keep_under": 70, "prefer": "codex", "tiers": ["standard"]}


# ── r1: at/above keep_under prefers the rule delegate; under it uses today's choice ───


def test_at_or_above_keep_under_prefers_the_rule_delegate():
    """anthropic-oauth at 75% used (headroom 25) with keep_under 70: a standard card on
    sonnet moves to codex. At 60% used it stays on today's choice."""
    sibs = ["sonnet", "codex"]
    over = {"anthropic-oauth": _snap(25), "sonnet": _snap(80), "codex": _snap(80)}
    idx, reason = apply_usage_policy(sibs, 0, "standard", over, [_RULE])
    assert idx == 1 and reason and "codex" in reason and "anthropic-oauth" in reason
    under = {**over, "anthropic-oauth": _snap(40)}  # 60% used
    assert apply_usage_policy(sibs, 0, "standard", under, [_RULE]) == (0, None)


def test_keep_under_is_the_boundary():
    """At exactly keep_under% used the rule fires; one point under it does not."""
    sibs = ["sonnet", "codex"]
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(30)}, [_RULE])[0] == 1  # 70% used
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(31)}, [_RULE])[0] == 0  # 69% used


def test_already_on_prefer_reports_the_rule_without_moving():
    """The pick doesn't change when the card already opens on ``prefer`` — but the rule
    still fires (the view wants to show it), so ``index`` is that sibling."""
    sibs = ["codex", "sonnet"]
    idx, reason = apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(10)}, [_RULE])
    assert idx == 0 and reason is not None


# ── r2: never outside the rung, and never a spent / down / blocked sibling ────────────


def test_prefer_outside_the_rung_never_fires():
    """``prefer`` absent from the (exclusive-filtered) rung ⇒ the rule can't add it."""
    sibs = ["sonnet", "opus"]  # no codex
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(1)}, [_RULE]) == (0, None)


def test_a_spent_prefer_is_never_selected():
    sibs = ["sonnet", "codex"]
    usage = {"anthropic-oauth": _snap(1)}
    assert apply_usage_policy(sibs, 0, "standard", usage, [_RULE], spent={"codex"}) == (0, None)


def test_a_blocked_prefer_is_never_selected():
    loop_mod._PROVIDER_DOWN.clear()
    sibs = ["sonnet", "codex"]
    usage = {"anthropic-oauth": _snap(1), "codex": _snap(90, usable=False, resets_at=2000)}
    assert apply_usage_policy(sibs, 0, "standard", usage, [_RULE], now=1000) == (0, None)
    # once codex's reset passes it is eligible again and the rule fires
    assert apply_usage_policy(sibs, 0, "standard", usage, [_RULE], now=2000)[0] == 1


def test_a_down_marked_prefer_is_never_selected():
    loop_mod._PROVIDER_DOWN.clear()
    loop_mod.mark_provider_down("codex", now=1000)
    sibs = ["sonnet", "codex"]
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(1)}, [_RULE], now=1000) == (0, None)
    loop_mod._PROVIDER_DOWN.clear()


# ── r3: unknown account == today (never fires) ────────────────────────────────────────


def test_unknown_account_never_fires():
    """An account with no snapshot (None / missing / a whole-None map) leaves the pick
    exactly where B5 left it — byte-for-byte."""
    sibs = ["sonnet", "codex"]
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": None}, [_RULE]) == (0, None)
    assert apply_usage_policy(sibs, 0, "standard", {}, [_RULE]) == (0, None)
    assert apply_usage_policy(sibs, 0, "standard", None, [_RULE]) == (0, None)
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(10)}, []) == (0, None)


# ── tier matching, aliases, rule order ────────────────────────────────────────────────


def test_tier_must_match_the_rule():
    sibs = ["sonnet", "codex"]
    off_tier = {"account": "anthropic-oauth", "keep_under": 70, "prefer": "codex", "tiers": ["reasoning"]}
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(1)}, [off_tier]) == (0, None)
    any_tier = {"account": "anthropic-oauth", "keep_under": 70, "prefer": "codex"}  # no tiers ⇒ all tiers
    assert apply_usage_policy(sibs, 0, "advanced", {"anthropic-oauth": _snap(1)}, [any_tier])[0] == 1


def test_tier_alias_is_canonicalised():
    """A rule written against a legacy alias (``smart`` → ``standard``) matches the
    canonical tier the card carries."""
    sibs = ["sonnet", "codex"]
    aliased = {"account": "anthropic-oauth", "keep_under": 70, "prefer": "codex", "tiers": ["smart"]}
    assert apply_usage_policy(sibs, 0, "standard", {"anthropic-oauth": _snap(1)}, [aliased])[0] == 1


def test_first_matching_rule_wins():
    sibs = ["sonnet", "codex", "opus"]
    rules = [
        {"account": "acctA", "keep_under": 70, "prefer": "codex", "tiers": ["standard"]},
        {"account": "acctB", "keep_under": 70, "prefer": "opus", "tiers": ["standard"]},
    ]
    usage = {"acctA": _snap(10), "acctB": _snap(10)}  # both over target
    assert apply_usage_policy(sibs, 0, "standard", usage, rules)[0] == 1  # codex, from the first rule


# ── policy_decision: the shared seam B7b's view calls ─────────────────────────────────


def test_policy_decision_exposes_the_fired_rule_for_the_view():
    sibs = ["sonnet", "codex"]
    d = loop_mod.policy_decision(sibs, 0, "standard", {"anthropic-oauth": _snap(25)}, [_RULE])
    assert d["index"] == 1 and d["rule"] is _RULE
    assert d["account"] == "anthropic-oauth" and d["prefer"] == "codex" and d["used_pct"] == 75
    none = loop_mod.policy_decision(sibs, 0, "standard", {"anthropic-oauth": None}, [_RULE])
    assert none == {"index": 0, "rule": None, "reason": None, "account": None, "used_pct": None, "prefer": None}


# ── drive level: a fake SDK, the account over/under the line, exclusive confinement ───

_LADDER_POLICY = {
    "coders": {"standard": ["sonnet", "codex"], "reasoning": ["opus"]},
    "routing": {"usage_policy": [_RULE]},
}


def _fake_sdk(monkeypatch, usage):
    """Install a fake ``graph.sdk.provider_usage`` and record which names it was asked
    about (so a test can prove an excluded delegate was never even queried)."""
    queried: list[str] = []

    def _provider_usage(delegate):
        queried.append(delegate)
        return usage.get(delegate)

    monkeypatch.setattr("graph.sdk.provider_usage", _provider_usage, raising=False)
    return queried


async def test_drive_opens_on_prefer_when_the_account_is_over_target(monkeypatch):
    """r1 end to end: anthropic-oauth at 75% used, the round-robin cursor points at
    sonnet, and both siblings have headroom — the card still opens on codex."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_LADDER_POLICY, cursor=0)
    _fake_sdk(monkeypatch, {"anthropic-oauth": _snap(25), "sonnet": _snap(80), "codex": _snap(80)})
    await loop._drive({"id": "bd-pol", "title": "t", "spec": "s"})
    assert seen[0] == "codex", f"expected the policy to open on codex, got {seen}"


async def test_drive_under_target_keeps_todays_choice(monkeypatch):
    """r1: at 60% used the rule does not fire, so the card opens on the cursor sibling."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_LADDER_POLICY, cursor=0)
    _fake_sdk(monkeypatch, {"anthropic-oauth": _snap(40), "sonnet": _snap(80), "codex": _snap(80)})
    await loop._drive({"id": "bd-pol2", "title": "t", "spec": "s"})
    assert seen[0] == "sonnet", f"under target should keep today's choice, got {seen}"


async def test_drive_policy_never_leaves_routing_exclusive(monkeypatch):
    """r2: codex is the rule's ``prefer`` but is not in ``routing.exclusive`` — the rung is
    filtered to sonnet before usage is gathered, so codex is never queried or dispatched."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    cfg = {
        "coders": {"standard": ["sonnet", "codex"], "reasoning": ["opus"]},
        "routing": {"exclusive": ["sonnet"], "usage_policy": [_RULE]},
    }
    loop, store = _rung_env(monkeypatch, _dispatch, cfg=cfg, cursor=0)
    queried = _fake_sdk(monkeypatch, {"anthropic-oauth": _snap(1), "sonnet": _snap(80), "codex": _snap(99)})
    await loop._drive({"id": "bd-polex", "title": "t", "spec": "s"})
    assert seen == ["sonnet"], f"policy must not add a delegate outside routing.exclusive, got {seen}"
    assert "codex" not in queried, f"an excluded delegate must not even be queried, got {queried}"


# ── r4: an invalid rule is refused and leaves the current policy unchanged ─────────────


def test_validate_routing_accepts_a_good_rule_and_resolves_its_names():
    cfg = {"coders": {"standard": ["codex"]}, "routing": {"usage_policy": [_RULE]}}
    validate_routing(cfg, delegates=lambda n: n if n in {"codex", "anthropic-oauth"} else None)  # no raise
    validate_routing(cfg, resolve=False)  # structural pass without name resolution


def test_validate_routing_refuses_bad_rules():
    with pytest.raises(ValueError, match="keep_under"):
        validate_routing(
            {"routing": {"usage_policy": [{"account": "a", "prefer": "b", "keep_under": 0}]}}, resolve=False
        )
    with pytest.raises(ValueError, match="keep_under"):
        validate_routing(
            {"routing": {"usage_policy": [{"account": "a", "prefer": "b", "keep_under": 150}]}}, resolve=False
        )
    with pytest.raises(ValueError, match="unknown tier"):
        validate_routing(
            {"routing": {"usage_policy": [{"account": "a", "prefer": "b", "keep_under": 70, "tiers": ["bogus"]}]}},
            resolve=False,
        )
    with pytest.raises(ValueError, match="usage_policy must be a list"):
        validate_routing({"routing": {"usage_policy": {"account": "a"}}}, resolve=False)
    with pytest.raises(ValueError, match="must name a prefer delegate"):
        validate_routing({"routing": {"usage_policy": [{"account": "a", "keep_under": 70}]}}, resolve=False)
    # an unknown delegate/account is refused by the resolve pass
    with pytest.raises(ValueError, match="not configured/enabled"):
        validate_routing(
            {"routing": {"usage_policy": [{"account": "ghost", "prefer": "codex", "keep_under": 70}]}},
            delegates=lambda n: n if n == "codex" else None,
        )


def test_invalid_reload_keeps_the_current_policy():
    """r4 through B3's set path: a reload carrying a malformed rule is refused and the
    running loop keeps the policy it already had, no restart, no partial apply."""
    loop = BoardLoop(dict(_LADDER_POLICY))
    feature = {"id": "bd-live", "title": "t"}
    assert loop._usage_policy_for(feature) == [_RULE]
    loop.reload(
        {
            "project_board": {
                "coders": {"standard": ["sonnet", "codex"], "reasoning": ["opus"]},
                "routing": {"usage_policy": [{"account": "anthropic-oauth", "keep_under": 150, "prefer": "codex"}]},
            }
        }
    )
    assert loop._usage_policy_for(feature) == [_RULE]


def test_project_scope_overrides_the_board_policy():
    """A project's own ``usage_policy`` overrides the board's, including an explicit empty
    list that disables it for that project; a project without one inherits the board's."""
    loop = BoardLoop(
        {
            "coders": {"standard": ["sonnet", "codex"], "reasoning": ["opus"]},
            "routing": {"usage_policy": [_RULE]},
            "projects": {
                "app": {"repo": "/tmp/app", "routing": {"usage_policy": []}},
                "web": {"repo": "/tmp/web"},
            },
        }
    )
    assert loop._usage_policy_for({"project": "app"}) == []  # explicit empty disables it here
    assert loop._usage_policy_for({"project": "web"}) == [_RULE]  # inherits the board policy


def test_usage_policy_reloads_live():
    """A valid reload lands the new policy on the running loop, no restart (B3)."""
    loop = BoardLoop(dict(_LADDER_POLICY))
    feature = {"id": "bd-x", "title": "t"}
    assert loop._usage_policy_for(feature) == [_RULE]
    new_rule = {"account": "anthropic-oauth", "keep_under": 50, "prefer": "codex", "tiers": ["standard"]}
    loop.reload(
        {
            "project_board": {
                "coders": {"standard": ["sonnet", "codex"], "reasoning": ["opus"]},
                "routing": {"usage_policy": [new_rule]},
            }
        }
    )
    assert loop._usage_policy_for(feature) == [new_rule]

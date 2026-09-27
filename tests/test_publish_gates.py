"""Publish gates (`waits_for`) and the release-freeze merge guard — the unit tier.

The network seams (``gates._http_get_json`` / ``gates._gh_json`` and the three freeze reads
in ``worktree``) are faked here; tests/test_publish_gate_real.py drives the real ones.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from project_board import gates, release_freeze, worktree
from project_board import store as store_mod
from project_board.gates import GateSpecError, SemVer, max_satisfying, parse_spec, satisfies
from project_board.loop import BoardLoop
from project_board.store import BoardError, annotate_next_action

# ── spec grammar ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, kind, target, constraint, is_range, canonical",
    [
        ("npm:@protolabsai/ui@>=0.63.0", "npm", "@protolabsai/ui", ">=0.63.0", True, "npm:@protolabsai/ui@>=0.63.0"),
        ("npm:@protolabsai/ui", "npm", "@protolabsai/ui", "", True, "npm:@protolabsai/ui"),
        ("NPM: left-pad@^1.2", "npm", "left-pad", "^1.2", True, "npm:left-pad@^1.2"),
        ("npm:@Scope/Pkg@>=1.0.0   <2.0.0-0", "npm", "@scope/pkg", ">=1.0.0 <2.0.0-0", True, None),
        ("release:protoLabsAI/protoContent@v0.63.0", "release", "protoLabsAI/protoContent", "v0.63.0", False, None),
        ("release:o/r@@protolabsai/ui@0.63.0", "release", "o/r", "@protolabsai/ui@0.63.0", False, None),
        ("release:o/r@>=0.63.0", "release", "o/r", ">=0.63.0", True, None),
        ("release:o/r@0.63.x", "release", "o/r", "0.63.x", True, None),
        ("release:o/r@*", "release", "o/r", "*", True, None),
        (
            "pr:protoLabsAI/protoContent#042",
            "pr",
            "protoLabsAI/protoContent",
            "42",
            False,
            "pr:protoLabsAI/protoContent#42",
        ),
    ],
)
def test_parse_spec(raw, kind, target, constraint, is_range, canonical):
    s = parse_spec(raw)
    assert (s.kind, s.target, s.constraint, s.is_range) == (kind, target, constraint, is_range)
    if canonical:
        assert s.raw == canonical
    assert parse_spec(s.raw) == s  # the persisted text re-parses to the same gate


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "card:bd-1@merged",  # depends_on already covers a card on this board
        "npm:",
        "npm:../etc/passwd",
        "npm:@protolabsai/ui@>=banana",
        "npm:left pad@1",
        "release:o/r",
        "release:not-a-slug@v1",
        "release:o/r@v1;rm -rf",
        "pr:o/r",
        "pr:o/r#0",
        "pr:o/r#abc",
    ],
)
def test_parse_spec_refuses_bad_specs_by_name(raw):
    with pytest.raises(GateSpecError, match="waits_for spec"):
        parse_spec(raw)


def test_parse_specs_splits_commas_and_dedupes_in_order():
    got = gates.normalize_specs("npm:a@>=1, pr:o/r#2 ,, npm:a@>=1, release:o/r@v1")
    assert got == ["npm:a@>=1", "pr:o/r#2", "release:o/r@v1"]
    assert gates.normalize_specs(["pr:o/r#2"]) == ["pr:o/r#2"]
    assert gates.normalize_specs(None) == [] and gates.normalize_specs("") == []


def test_describe_is_the_human_text_on_the_card():
    assert parse_spec("npm:@protolabsai/ui@>=0.63.0").describe() == "npm @protolabsai/ui >=0.63.0"
    assert parse_spec("pr:o/r#7").describe() == "pr o/r#7"
    assert parse_spec("release:o/r@v1.0.0").describe() == "release o/r v1.0.0"


# ── semver ────────────────────────────────────────────────────────────────────────


def test_semver_precedence_matches_the_spec_example_chain():
    # semver.org §11: 1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta
    # < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0
    chain = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    parsed = [SemVer.parse(v) for v in chain]
    assert all(a < b for a, b in zip(parsed, parsed[1:]))
    assert SemVer.parse("v1.2.3+build.5") == SemVer.parse("1.2.3")  # v-prefix ok, build ignored
    for bad in ("1.2", "01.2.3", "1.2.3-", "latest", ""):
        assert SemVer.parse(bad) is None


@pytest.mark.parametrize(
    "rng, yes, no",
    [
        (">=0.63.0", ["0.63.0", "0.63.1", "1.0.0"], ["0.62.0", "0.64.0-next.1", "0.63.0-rc.1"]),
        (">0.62.0", ["0.62.1", "0.63.0"], ["0.62.0", "0.63.0-rc.1"]),
        ("^1.2.3", ["1.2.3", "1.9.9"], ["1.2.2", "2.0.0", "2.0.0-alpha", "1.3.0-beta"]),
        ("^0.2.3", ["0.2.3", "0.2.9"], ["0.3.0", "0.2.2"]),
        ("^0.0.3", ["0.0.3"], ["0.0.4", "0.0.2"]),
        ("^0.0", ["0.0.0", "0.0.9"], ["0.1.0"]),
        ("^1.x", ["1.0.0", "1.9.0"], ["2.0.0", "0.9.0"]),
        ("~1.2.3", ["1.2.3", "1.2.9"], ["1.3.0", "1.2.2"]),
        ("~1.2", ["1.2.0", "1.2.9"], ["1.3.0"]),
        ("~1", ["1.0.0", "1.9.9"], ["2.0.0"]),
        ("1.2.x", ["1.2.0", "1.2.7"], ["1.3.0"]),
        ("1", ["1.0.0", "1.99.0"], ["2.0.0"]),
        ("*", ["0.0.1", "9.9.9"], ["1.0.0-rc.1"]),
        ("", ["1.0.0"], ["1.0.0-rc.1"]),
        ("1.2.3", ["1.2.3"], ["1.2.4"]),
        ("=1.2.3", ["1.2.3"], ["1.2.4"]),
        (">1.2", ["1.3.0"], ["1.2.9"]),
        ("<=1.2", ["1.2.9", "1.0.0"], ["1.3.0"]),
        ("<1.2", ["1.1.9"], ["1.2.0"]),
        (">= 1.2.3 < 2", ["1.5.0"], ["2.0.0", "1.2.2"]),
        ("1.2.3 - 2.3.4", ["1.2.3", "2.3.4"], ["2.3.5", "1.2.2"]),
        ("1.2 - 2.3", ["1.2.0", "2.3.9"], ["2.4.0"]),
        ("<1.0.0 || >=3.0.0", ["0.5.0", "3.1.0"], ["2.0.0"]),
        # The prerelease rule: a prerelease only matches a comparator naming one on the
        # SAME major.minor.patch.
        (">=1.2.3-beta.1", ["1.2.3-beta.2", "1.2.3", "1.3.0"], ["1.3.0-beta.1", "1.2.3-alpha"]),
        ("^1.2.3-beta.2", ["1.2.3-beta.3", "1.2.4"], ["1.2.4-beta.1"]),
    ],
)
def test_satisfies_follows_node_semver(rng, yes, no):
    for v in yes:
        assert satisfies(v, rng), f"{v} should satisfy {rng!r}"
    for v in no:
        assert not satisfies(v, rng), f"{v} should NOT satisfy {rng!r}"


def test_max_satisfying_skips_prereleases_and_junk():
    versions = ["0.61.0", "0.62.0", "0.63.0-next.0", "0.63.0", "0.63.1", "not-a-version"]
    assert str(max_satisfying(versions, ">=0.62.0 <0.64.0")) == "0.63.1"
    assert max_satisfying(versions, ">=1.0.0") is None


def test_parse_range_refuses_nonsense():
    for bad in (">=banana", "^", "1.2.3.4", "~x.y"):
        with pytest.raises(GateSpecError):
            gates.parse_range(bad)


# ── evaluation with the HTTP / gh seams faked ─────────────────────────────────────


def _npm(monkeypatch, versions, latest="", status=200, calls=None):
    def _get(url, *, token="", timeout=0.0):
        if calls is not None:
            calls.append((url, token))
        if isinstance(status, Exception):
            raise status
        if status != 200:
            return status, None
        return 200, {"versions": {v: {} for v in versions}, "dist-tags": {"latest": latest}}

    monkeypatch.setattr(gates, "_http_get_json", _get)


def test_eval_npm_met_names_the_version_and_encodes_the_scope(monkeypatch):
    calls = []
    _npm(monkeypatch, ["0.62.0", "0.63.0"], latest="0.63.0", calls=calls)
    out = gates.eval_npm(parse_spec("npm:@protolabsai/ui@>=0.63.0"), token="tok")
    assert out["met"] is True and out["detail"] == "npm @protolabsai/ui >=0.63.0 (0.63.0 published)"
    assert calls == [("https://registry.npmjs.org/@protolabsai%2Fui", "tok")]


def test_eval_npm_unmet_names_the_latest(monkeypatch):
    _npm(monkeypatch, ["0.61.0", "0.62.0", "0.63.0-next.1"], latest="0.62.0")
    out = gates.eval_npm(parse_spec("npm:@protolabsai/ui@>=0.63.0"))
    assert out["met"] is False
    assert out["detail"] == "npm @protolabsai/ui >=0.63.0 (latest 0.62.0)"


def test_eval_npm_404_is_not_yet_and_401_is_an_error_naming_the_token(monkeypatch):
    _npm(monkeypatch, [], status=404)
    assert "not published yet" in gates.eval_npm(parse_spec("npm:@x/y@>=1.0.0"))["detail"]
    _npm(monkeypatch, [], status=401)
    with pytest.raises(gates.GateCheckError, match="npm_token"):
        gates.eval_npm(parse_spec("npm:@x/y@>=1.0.0"))


def _gh(monkeypatch, answers, calls=None):
    """``answers``: path → (rc, data, err)."""

    def _fake(path, *, timeout=0.0):
        if calls is not None:
            calls.append(path)
        for key, val in answers.items():
            if path.startswith(key):
                return val
        raise AssertionError(f"unexpected gh api {path}")

    monkeypatch.setattr(gates, "_gh_json", _fake)


def test_eval_pr_merged_open_closed_and_missing(monkeypatch):
    _gh(monkeypatch, {"repos/o/r/pulls/1": (0, {"merged": True, "state": "closed"}, "")})
    assert gates.eval_pr(parse_spec("pr:o/r#1")) == {"met": True, "detail": "pr o/r#1 (merged)"}
    _gh(monkeypatch, {"repos/o/r/pulls/1": (0, {"merged": False, "state": "open"}, "")})
    assert gates.eval_pr(parse_spec("pr:o/r#1"))["detail"] == "pr o/r#1 (open)"
    _gh(monkeypatch, {"repos/o/r/pulls/1": (0, {"merged": False, "state": "closed"}, "")})
    assert "closed without merging" in gates.eval_pr(parse_spec("pr:o/r#1"))["detail"]
    _gh(monkeypatch, {"repos/o/r/pulls/1": (1, {"status": "404"}, "gh: Not Found (HTTP 404)")})
    assert gates.eval_pr(parse_spec("pr:o/r#1"))["detail"] == "pr o/r#1 (no such PR)"
    _gh(monkeypatch, {"repos/o/r/pulls/1": (1, None, "HTTP 403: API rate limit exceeded")})
    with pytest.raises(gates.GateCheckError, match="rate limit"):
        gates.eval_pr(parse_spec("pr:o/r#1"))


def test_eval_release_tag_and_range(monkeypatch):
    calls = []
    releases = [
        {"tag_name": "v0.62.0"},
        {"tag_name": "@protolabsai/ui@0.63.0"},
        {"tag_name": "v0.64.0", "draft": True},  # a draft is not released
        {"tag_name": "nightly"},
    ]
    _gh(
        monkeypatch,
        {
            "repos/o/r/git/ref/tags/v0.63.0": (0, {"ref": "refs/tags/v0.63.0"}, ""),
            "repos/o/r/git/ref/tags/v9": (1, {"status": "404"}, "gh: Not Found (HTTP 404)"),
            "repos/o/r/releases": (0, releases, ""),
        },
        calls,
    )
    assert gates.eval_release(parse_spec("release:o/r@v0.63.0"))["met"] is True
    assert gates.eval_release(parse_spec("release:o/r@v9"))["detail"] == "release o/r v9 (no such tag yet)"
    assert gates.eval_release(parse_spec("release:o/r@>=0.63.0"))["met"] is True
    unmet = gates.eval_release(parse_spec("release:o/r@>=0.64.0"))
    assert unmet["met"] is False and "latest release 0.63.0" in unmet["detail"]
    assert calls[0] == "repos/o/r/git/ref/tags/v0.63.0"


# ── cache, TTL, backoff, fail-closed ──────────────────────────────────────────────


def test_evaluate_caches_per_spec_across_cards_and_respects_the_ttl(monkeypatch):
    calls = []
    _npm(monkeypatch, ["0.62.0"], latest="0.62.0", calls=calls)
    spec = "npm:@protolabsai/ui@>=0.63.0"
    t0 = 1_000.0
    for _card in range(20):  # twenty consumer cards on one package …
        (r,) = gates.evaluate([spec], now=t0)
        assert r["met"] is False
    assert len(calls) == 1  # … one registry read
    gates.evaluate([spec], now=t0 + gates.UNMET_TTL_S - 1)
    assert len(calls) == 1
    _npm(monkeypatch, ["0.62.0", "0.63.0"], latest="0.63.0", calls=calls)
    (r,) = gates.evaluate([spec], now=t0 + gates.UNMET_TTL_S)
    assert r["met"] is True and len(calls) == 2
    gates.evaluate([spec], now=t0 + gates.UNMET_TTL_S + gates.MET_TTL_S - 1)
    assert len(calls) == 2  # a met gate is re-read rarely


def test_a_failed_check_is_unmet_with_the_error_and_backs_off(monkeypatch, caplog):
    calls = []
    _npm(monkeypatch, [], status=gates.GateCheckError("registry unreachable: boom"), calls=calls)
    spec = "npm:@x/y@>=1.0.0"
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        (r,) = gates.evaluate([spec], now=0.0)
    assert r["met"] is False and "boom" in r["error"] and "check failed" in r["detail"]
    assert "check failed" in caplog.text
    gates.evaluate([spec], now=gates.ERROR_BACKOFF_BASE_S - 1)
    assert len(calls) == 1  # backing off
    gates.evaluate([spec], now=gates.ERROR_BACKOFF_BASE_S)
    assert len(calls) == 2  # second failure: the next wait doubles
    gates.evaluate([spec], now=gates.ERROR_BACKOFF_BASE_S * 2.5)
    assert len(calls) == 2
    gates.evaluate([spec], now=gates.ERROR_BACKOFF_BASE_S * 3)
    assert len(calls) == 3


def test_force_skips_the_ttl_but_not_the_floor(monkeypatch):
    calls = []
    _npm(monkeypatch, ["1.0.0"], latest="1.0.0", calls=calls)
    gates.evaluate(["npm:a@>=2"], now=100.0)
    gates.evaluate(["npm:a@>=2"], now=100.0 + gates.FORCE_MIN_INTERVAL_S - 1, force=True)
    assert len(calls) == 1
    gates.evaluate(["npm:a@>=2"], now=100.0 + gates.FORCE_MIN_INTERVAL_S, force=True)
    assert len(calls) == 2


def test_evaluate_never_raises_on_a_bad_spec_or_a_crashing_seam(monkeypatch):
    def _crash(url, **_kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gates, "_http_get_json", _crash)
    out = gates.evaluate(["nonsense", "npm:a@>=1"])
    assert [r["met"] for r in out] == [False, False]
    assert out[0]["error"] and "kaboom" in out[1]["error"]


def test_card_status_reads_the_cache_only(monkeypatch):
    f = {"waits_for": ["pr:o/r#3"]}
    (s,) = gates.card_status(f)
    assert s["met"] is False and s["detail"] == "pr o/r#3 (not checked yet)"
    _gh(monkeypatch, {"repos/o/r/pulls/3": (0, {"merged": False, "state": "open"}, "")})
    gates.evaluate(["pr:o/r#3"])
    assert gates.card_status(f)[0]["detail"] == "pr o/r#3 (open)"
    assert gates.unmet_sentence(gates.card_status(f)) == "waiting on publish: pr o/r#3 (open)"


# ── the store: persistence in notes, never a label ────────────────────────────────


def _recording_board(make_board, bead=None):
    calls = []
    state = {"bead": dict(bead or {})}

    def _run(*args, want_json=False, **_kw):
        calls.append(args)
        if args[0] == "create":
            state["bead"] = {"id": "bd-1", "title": args[1], "status": "open", "issue_type": "feature", "labels": []}
            return "bd-1"
        if args[0] == "update":
            for a in args[2:]:
                if str(a).startswith("--notes="):
                    state["bead"]["notes"] = str(a)[len("--notes=") :]
            return ""
        if args[0] == "show":
            return [dict(state["bead"])]
        return []

    return make_board(_run), calls, state


def test_create_feature_persists_gates_in_notes_not_labels(make_board):
    b, calls, state = _recording_board(make_board)
    f = b.create_feature("t", spec="s", files_to_modify=["a.py"], waits_for="npm:@protolabsai/ui@>=0.63.0, pr:o/r#5")
    notes = state["bead"]["notes"]
    assert notes == "a.py\nwaits-for: npm:@protolabsai/ui@>=0.63.0\nwaits-for: pr:o/r#5"
    assert f["waits_for"] == ["npm:@protolabsai/ui@>=0.63.0", "pr:o/r#5"]
    assert f["files_to_modify"] == ["a.py"]  # a gate line is never a file path
    assert not any("waits-for" in str(a) and a != f"--notes={notes}" for c in calls for a in c)


def test_a_bad_spec_refuses_create_before_br_runs(make_board):
    b, calls, _state = _recording_board(make_board)
    with pytest.raises(BoardError, match="waits_for spec"):
        b.create_feature("t", spec="s", waits_for="npm:@x/y@>=banana")
    assert calls == []


def test_update_feature_replaces_and_clears_gates_keeping_files_and_source(make_board):
    bead = {
        "id": "bd-1",
        "title": "t",
        "status": "open",
        "issue_type": "feature",
        "labels": [],
        "notes": "a.py\nwaits-for: pr:o/r#1\nsource-issue: o/r#9",
    }
    b, _calls, state = _recording_board(make_board, bead)
    b.update_feature("bd-1", waits_for="release:o/r@v1.0.0")
    assert state["bead"]["notes"] == "a.py\nwaits-for: release:o/r@v1.0.0\nsource-issue: o/r#9"
    b.update_feature("bd-1", files_to_modify=["b.py"])  # untouched gates ride a files rewrite
    assert state["bead"]["notes"] == "b.py\nwaits-for: release:o/r@v1.0.0\nsource-issue: o/r#9"
    b.update_feature("bd-1", waits_for=[])
    assert state["bead"]["notes"] == "b.py\nsource-issue: o/r#9"


# ── visibility: next_action, board_list, get_feature ─────────────────────────────


def _card(**over):
    f = {"id": "bd-9", "board_state": "ready", "blocked": False, "labels": ["ready"], "waits_for": ["pr:o/r#3"]}
    f.update(over)
    return f


def test_next_action_says_what_the_card_waits_on(monkeypatch):
    (f,) = annotate_next_action([_card()], {})
    assert f["next_action"] == "waiting on publish: pr o/r#3 (not checked yet)"
    assert "board_check_gates bd-9" in f["next_action_hint"]
    assert f["gates"][0]["spec"] == "pr:o/r#3"
    _npm(monkeypatch, ["0.62.0"], latest="0.62.0")
    gates.evaluate(["npm:@protolabsai/ui@>=0.63.0"])
    (f,) = annotate_next_action([_card(waits_for=["npm:@protolabsai/ui@>=0.63.0"])], {})
    assert f["next_action"] == "waiting on publish: npm @protolabsai/ui >=0.63.0 (latest 0.62.0)"


def test_a_met_gate_or_a_card_past_the_claim_owes_no_waiting_action(monkeypatch):
    _gh(monkeypatch, {"repos/o/r/pulls/3": (0, {"merged": True}, "")})
    gates.evaluate(["pr:o/r#3"])
    (f,) = annotate_next_action([_card()], {})
    assert "next_action" not in f and f["gates"][0]["met"] is True
    (g,) = annotate_next_action([_card(board_state="in_progress", waits_for=["pr:o/r#4"])], {})
    assert "next_action" not in g


def test_backlog_card_hint_says_it_still_needs_mark_ready():
    (f,) = annotate_next_action([_card(board_state="backlog", labels=[])], {})
    assert f["next_action"].startswith("waiting on publish:")
    assert "board_mark_ready" in f["next_action_hint"]


def test_board_list_and_get_feature_carry_the_gate_state(monkeypatch):
    import project_board as pb

    card = {
        "id": "bd-9",
        "title": "Adopt token",
        "board_state": "ready",
        "blocked": False,
        "labels": ["ready"],
        "waits_for": ["pr:o/r#3"],
        "pr_url": "",
        "priority": 2,
        "difficulty": "",
        "spec": "",
        "acceptance_criteria": "",
        "design": "",
    }

    class _S:
        def list_features(self, state=None, include_archived=False):
            return [dict(card)]

        def get_feature(self, fid):
            return dict(card)

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _S())
    tools = {t.name: t for t in pb._board_tools({})}
    (row,) = json.loads(tools["board_list"].invoke({}))
    assert row["waits_for"] == ["pr:o/r#3"] and row["next_action"].startswith("waiting on publish: pr o/r#3")
    got = json.loads(tools["board_get_feature"].invoke({"feature_id": "bd-9"}))
    assert got["waits_for"] == ["pr:o/r#3"] and got["gates"][0]["met"] is False
    assert got["next_action"] == "waiting on publish: pr o/r#3 (not checked yet)"

    _gh(monkeypatch, {"repos/o/r/pulls/3": (0, {"merged": True}, "")})
    (checked,) = json.loads(tools["board_check_gates"].invoke({"feature_id": "bd-9"}))
    assert checked["clear"] is True and checked["gates"][0]["detail"] == "pr o/r#3 (merged)"
    (row,) = json.loads(tools["board_list"].invoke({}))
    assert "next_action" not in row  # the on-demand check filled the shared cache


def test_update_tool_none_clears_gates(monkeypatch):
    import project_board as pb

    seen = {}

    class _S:
        def update_feature(self, fid, **kw):
            seen.update(kw)
            return {"id": fid, "board_state": "backlog", "title": "t"}

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _S())
    tools = {t.name: t for t in pb._board_tools({})}
    tools["board_update_feature"].invoke({"feature_id": "bd-1", "waits_for": "none"})
    assert seen["waits_for"] == []
    seen.clear()
    tools["board_update_feature"].invoke({"feature_id": "bd-1", "title": "x"})
    assert "waits_for" not in seen  # empty = untouched


# ── the claim scan: excluded while unmet, claimed when clear ──────────────────────


class _GateStore:
    def __init__(self, features):
        self._features = [dict(f) for f in features]
        self.claimed = []
        self.comments = []

    def ready_queue(self, relaxed=False):
        return [dict(f) for f in self._features if f["id"] not in self.claimed]

    def claim(self, fid, assignee=""):
        self.claimed.append(fid)
        return next(dict(f) for f in self._features if f["id"] == fid)

    def list_features(self, state=None):
        return []

    def comment(self, fid, text):
        self.comments.append((fid, text))

    def flag_blocked(self, fid, reason, category=""):
        raise AssertionError(f"a publish wait must never be flagged as a livelock: {fid} {reason}")


async def _hold(loop, monkeypatch):
    release = asyncio.Event()

    async def _drive(feature):
        await release.wait()

    monkeypatch.setattr(loop, "_drive", _drive)

    async def _finish():
        release.set()
        await asyncio.gather(*loop._drives, return_exceptions=True)

    return _finish


async def test_claim_scan_holds_a_gated_card_and_claims_it_when_the_gate_clears(monkeypatch, caplog):
    gated = {"id": "bd-2", "board_state": "ready", "files_to_modify": ["b.py"], "waits_for": ["pr:o/r#3"]}
    free = {"id": "bd-3", "board_state": "ready", "files_to_modify": ["c.py"]}
    store = _GateStore([gated, free])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 5, "ready_skip_max": 2})
    finish = await _hold(loop, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(gates.time, "time", lambda: now[0])
    _gh(monkeypatch, {"repos/o/r/pulls/3": (0, {"merged": False, "state": "open"}, "")})
    try:
        with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
            for _tick in range(4):  # well past ready_skip_max: a publish wait is not a livelock
                await loop._spawn_ready()
        assert store.claimed == ["bd-3"]  # the ungated sibling is unaffected
        skip = loop._last_claim_decision["skipped"]
        assert skip == [{"fid": "bd-2", "reason": "waiting-on-publish", "gates": ["pr:o/r#3"]}]
        assert caplog.text.count("bd-2 held out of the claim") == 1  # logged once, not per tick

        _gh(monkeypatch, {"repos/o/r/pulls/3": (0, {"merged": True, "state": "closed"}, "")})
        now[0] += gates.UNMET_TTL_S
        with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
            await loop._spawn_ready()
        assert store.claimed == ["bd-3", "bd-2"]
        assert "bd-2 publish gates cleared (pr o/r#3 (merged))" in caplog.text
        assert store.comments == [("bd-2", "publish gates cleared: pr o/r#3 (merged)")]
    finally:
        await finish()


async def test_claim_scan_fails_closed_when_the_check_errors(monkeypatch):
    store = _GateStore([{"id": "bd-2", "board_state": "ready", "files_to_modify": [], "waits_for": ["npm:a@>=1"]}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    _npm(monkeypatch, [], status=gates.GateCheckError("registry unreachable"))
    loop = BoardLoop({"max_concurrent": 1})
    await loop._spawn_ready()
    assert store.claimed == []
    (f,) = annotate_next_action([dict(store._features[0], labels=["ready"], blocked=False)], {})
    assert "check failed: registry unreachable" in f["next_action"]


def test_held_summary_names_publish_waits():
    from project_board.loop.drive import _held_summary

    held = _held_summary([_card()])
    assert held["waiting-on-publish"]["ids"] == ["bd-9"]


# ── release freeze: config, detection per signal, fail closed ─────────────────────


def test_parse_freeze_config_shapes():
    d = release_freeze.parse_config(None)
    assert d == release_freeze.DEFAULT_PATTERNS
    assert release_freeze.parse_config(True) == d and release_freeze.parse_config("default") == d
    assert release_freeze.parse_config(False) is None and release_freeze.parse_config("off") is None
    assert release_freeze.parse_config(["release/*", "workflow:release.yml", "cut.yaml"]) == {
        "branches": ["release/*"],
        "pr_heads": ["release/*"],
        "workflows": ["release.yml", "cut.yaml"],
    }
    assert release_freeze.parse_config("release/*, workflow:r.yml")["workflows"] == ["r.yml"]
    assert release_freeze.parse_config({"pr_heads": ["prepare-release*"]}) == {
        "branches": [],
        "pr_heads": ["prepare-release*"],
        "workflows": [],
    }
    assert release_freeze.parse_config({}) is None and release_freeze.parse_config([]) is None


def _freeze_seams(monkeypatch, *, branches=(), prs=(), runs=(), error=None, calls=None):
    async def _b(repo, patterns):
        if calls is not None:
            calls.append(("branches", tuple(patterns or ())))
        if error:
            raise worktree.WorktreeError(error)
        return list(branches)

    async def _p(slug, patterns, *, cwd="."):
        if calls is not None:
            calls.append(("prs", slug))
        return list(prs)

    async def _r(slug, wf, *, cwd="."):
        if calls is not None:
            calls.append(("runs", wf))
        return list(runs)

    monkeypatch.setattr(worktree, "remote_branches", _b)
    monkeypatch.setattr(worktree, "open_pr_heads", _p)
    monkeypatch.setattr(worktree, "active_workflow_runs", _r)


@pytest.mark.parametrize(
    "kw, want",
    [
        ({}, ""),
        ({"branches": ["prepare-release/v0.173.0"]}, "branch prepare-release/v0.173.0"),
        ({"prs": [(3565, "prepare-release/v0.173.0")]}, "PR #3565 (prepare-release/v0.173.0)"),
        ({"runs": [{"status": "in_progress"}]}, "prepare-release.yml run in_progress"),
        ({"error": "gh: HTTP 502"}, "freeze check failed: gh: HTTP 502"),
    ],
)
async def test_freeze_check_per_signal(monkeypatch, kw, want):
    _freeze_seams(monkeypatch, **kw)
    got = await release_freeze.check("o/r", "/repo", release_freeze.parse_config(None), now=1.0)
    assert got == want


async def test_freeze_check_is_cached_per_repo(monkeypatch):
    calls = []
    _freeze_seams(monkeypatch, calls=calls)
    pats = release_freeze.parse_config(None)
    await release_freeze.check("o/r", "/repo", pats, now=1.0)
    await release_freeze.check("o/r", "/repo", pats, now=1.0 + release_freeze.CHECK_TTL_S - 1)
    assert len(calls) == 3  # one of each read, once
    await release_freeze.check("o/r", "/repo", pats, now=1.0 + release_freeze.CHECK_TTL_S)
    assert len(calls) == 6


# ── the merge edge: held during a freeze, merged when it lifts ────────────────────


class _MergeStore:
    def __init__(self, feature):
        self.feature = dict(feature)
        self.comments = []

    def get_feature(self, fid):
        return dict(self.feature)

    def comment(self, fid, text):
        self.comments.append((fid, text))


def _merge_env(monkeypatch):
    calls = {"merge": []}

    async def _info(pr_url, *, cwd="."):
        return {"mergeStateStatus": "CLEAN", "isDraft": False}

    async def _merge(pr_url, *, method="squash", cwd=".", expected_head=""):
        calls["merge"].append(pr_url)
        return True, ""

    async def _ok(*_a, **_k):
        return "MERGED"

    async def _true(*_a, **_k):
        return True

    async def _reap(*_a, **_k):
        return True

    monkeypatch.setattr(worktree, "pr_merge_info", _info)
    monkeypatch.setattr(worktree, "merge_pr", _merge)
    monkeypatch.setattr(worktree, "pr_state", _ok)
    monkeypatch.setattr(worktree, "delete_remote_branch", _true)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    return calls


CARD = {
    "id": "bd-1",
    "title": "Adopt token",
    "board_state": "in_review",
    "blocked": False,
    "labels": ["in-review"],
    "pr_url": "https://github.com/protoLabsAI/protoAgent/pull/7",
}


async def test_merge_is_held_during_a_freeze_and_lands_when_it_lifts(monkeypatch):
    calls = _merge_env(monkeypatch)
    _freeze_seams(monkeypatch, prs=[(3565, "prepare-release/v0.173.0")])
    loop = BoardLoop({"auto_merge": True, "auto_rebase": False})
    store = _MergeStore(CARD)
    pr = CARD["pr_url"]
    for _poll in range(3):
        release_freeze._CHECKS.clear()  # each poll is a fresh read (past the TTL)
        assert await loop._maybe_auto_merge(store, "bd-1", pr, "/repo") is False
    assert calls["merge"] == []
    assert loop._auto_merge_failures.get("bd-1", 0) == 0  # a hold never spends a merge attempt
    assert len(store.comments) == 1 and "release freeze (PR #3565 (prepare-release/v0.173.0))" in store.comments[0][1]
    (f,) = annotate_next_action([dict(CARD)], {"auto_merge": True})
    assert f["next_action"] == "held: release freeze (PR #3565 (prepare-release/v0.173.0))"

    _freeze_seams(monkeypatch)  # the release PR merged and its branch is gone
    release_freeze._CHECKS.clear()
    assert await loop._maybe_auto_merge(store, "bd-1", pr, "/repo") is True
    assert calls["merge"] == [pr]
    assert release_freeze.hold_for("bd-1") is None
    (f,) = annotate_next_action([dict(CARD)], {"auto_merge": True})
    assert f["next_action"] == store_mod.NEXT_ACTION_AUTO_MERGE_PENDING


async def test_a_freeze_check_error_holds_the_merge(monkeypatch):
    calls = _merge_env(monkeypatch)
    _freeze_seams(monkeypatch, error="gh: HTTP 502")
    loop = BoardLoop({"auto_merge": True, "auto_rebase": False})
    assert await loop._maybe_auto_merge(_MergeStore(CARD), "bd-1", CARD["pr_url"], "/repo") is False
    assert calls["merge"] == []
    assert release_freeze.hold_for("bd-1")["evidence"] == "freeze check failed: gh: HTTP 502"


async def test_release_freeze_false_skips_the_check_per_project(monkeypatch):
    calls = _merge_env(monkeypatch)
    seen = []
    _freeze_seams(monkeypatch, prs=[(1, "prepare-release/v1")], calls=seen)
    loop = BoardLoop(
        {
            "auto_merge": True,
            "auto_rebase": False,
            "projects": {"protoContent": {"repo": "/pc", "release_freeze": False}, "protoAgent": {"repo": "/pa"}},
            "default_project": "protoAgent",
        }
    )
    card = dict(CARD, project="protoContent")
    assert await loop._maybe_auto_merge(_MergeStore(card), "bd-1", CARD["pr_url"], "/pc") is True
    assert seen == [] and calls["merge"] == [CARD["pr_url"]]
    # …while the default project still checks (and here, holds).
    assert (
        await loop._maybe_auto_merge(_MergeStore(dict(CARD, project="protoAgent")), "bd-1", CARD["pr_url"], "/pa")
        is False
    )
    assert seen

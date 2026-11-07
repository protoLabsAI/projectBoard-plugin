"""Goal verifier contract, with the host result type supplied at its import seam."""

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

import project_board
from project_board import store
from test_packaging import _Registry


@dataclass
class VerifyResult:
    met: bool
    reason: str
    evidence: str


@pytest.fixture
def verifier(monkeypatch):
    types = ModuleType("graph.goals.types")
    types.VerifyResult = VerifyResult
    monkeypatch.setitem(sys.modules, "graph.goals.types", types)
    card = {"board_state": "in_review", "pr_url": "https://github.com/org/repo/pull/1", "labels": []}
    calls = []

    def get_store(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(get_feature=lambda fid: card if fid == "bd-x" else None)

    monkeypatch.setattr(store, "get_store", get_store)
    reg = _Registry()
    registered = {}
    reg.register_goal_verifier = lambda name, fn, description: registered.update({name: (fn, description)})
    project_board.register(reg)
    fn, description = registered["board:card_state"]
    assert description
    return fn, card, reg, calls


async def check(verifier, **args):
    return await verifier[0]({"args": {"feature_id": "bd-x", "state": "in_review", **args}}, None)


async def test_registered_and_uses_tool_store(verifier):
    result = await check(verifier)
    assert result.met
    assert result.reason == "bd-x is in_review"
    tool = next(t for t in verifier[2].tools if t.name == "board_get_feature")
    assert "unknown feature" in tool.invoke({"feature_id": "bd-no"})
    assert verifier[3][-1] == verifier[3][-2]


@pytest.mark.parametrize("states,met", [("in_review", True), ("done", False), (["ready", "in_review"], True)])
async def test_state_match(verifier, states, met):
    assert (await check(verifier, state=states)).met is met


@pytest.mark.parametrize(
    "state,labels,pr_url,met",
    [
        ("done", ["merged-verified:abc123"], "pr", True),
        ("in_review", ["merged-verified:abc123"], "pr", False),
        ("done", [], "pr", False),
        ("done", ["merged-verified:"], "pr", False),
        ("done", ["merged-verified:abc123"], "", False),
    ],
)
async def test_merged(verifier, state, labels, pr_url, met):
    verifier[1].update(board_state=state, labels=labels, pr_url=pr_url)
    assert (await check(verifier, state=["done", "in_review"], merged=True)).met is met


@pytest.mark.parametrize(
    "fid,reason", [(None, "feature_id is required"), ("", "feature_id is required"), ("bd-no", "unknown feature")]
)
async def test_missing_unknown(verifier, fid, reason):
    result = await check(verifier, feature_id=fid)
    assert not result.met
    assert reason in result.reason


async def test_absent_id(verifier):
    result = await verifier[0]({"args": {"state": "done"}}, None)
    assert not result.met
    assert "feature_id is required" in result.reason


async def test_evidence(verifier):
    first = await check(verifier)
    verifier[1]["board_state"] = "done"
    second = await check(verifier)
    assert second.evidence != first.evidence
    verifier[1]["labels"] = ["z", "a"]
    third = await check(verifier)
    assert third.evidence != second.evidence
    verifier[1]["labels"].reverse()
    assert (await check(verifier)).evidence == third.evidence
    verifier[1]["pr_url"] = "another-pr"
    assert (await check(verifier)).evidence != third.evidence


@pytest.mark.parametrize("spec", [None, {}, {"args": None}, {"args": {"feature_id": "bd-x", "state": [42]}}])
async def test_malformed_spec(verifier, spec):
    assert not (await verifier[0](spec, None)).met


async def test_read_error(verifier, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store, "get_store", fail)
    result = await check(verifier)
    assert not result.met
    assert "store unavailable" in result.reason


def test_older_host():
    project_board.register(_Registry())


def test_registration_failure_logged(caplog):
    reg = _Registry()

    def fail(*args):
        raise RuntimeError("host failure")

    reg.register_goal_verifier = fail
    project_board.register(reg)
    assert "registering card state goal verifier failed" in caplog.text

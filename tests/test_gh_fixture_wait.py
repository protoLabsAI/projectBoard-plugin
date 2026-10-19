"""Deterministic coverage of the real-GitHub tier's bounded fixture setup wait."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_publish_gate_real as real_tier


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]

    async def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(real_tier, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(real_tier, "asyncio", SimpleNamespace(sleep=sleep))
    return elapsed


async def test_completed_fixture_needs_no_wait(monkeypatch, clock):
    facts = Mock(return_value=("completed", 46))
    monkeypatch.setattr(real_tier, "_run_facts", facts)
    assert await real_tier._wait_for_completed_fixture("fixture") == 46
    facts.assert_called_once_with("fixture")
    assert clock[0] == 0


async def test_fixture_can_finish_after_old_three_minute_deadline(monkeypatch, clock):
    def facts(run_id):
        assert run_id == "fixture"
        if clock[0] < 200:
            return "queued", 45
        if clock[0] < 250:
            return "in_progress", 46
        return "completed", 46

    monkeypatch.setattr(real_tier, "_run_facts", facts)
    assert await real_tier._wait_for_completed_fixture("fixture") == 46
    assert clock[0] == 250


@pytest.mark.parametrize("status", ["queued", "in_progress"])
async def test_stuck_fixture_still_fails_at_bounded_deadline(monkeypatch, clock, status):
    monkeypatch.setattr(real_tier, "_run_facts", Mock(return_value=(status, 46)))
    with pytest.raises(AssertionError, match=rf"fixture.*600s.*status={status}, attempt=46"):
        await real_tier._wait_for_completed_fixture("fixture")
    assert clock[0] == 600


@pytest.mark.parametrize("attempt", [1, 44])
async def test_usable_pinned_fixture_does_not_dispatch(monkeypatch, clock, attempt):
    monkeypatch.setattr(real_tier, "_run_facts", Mock(return_value=("completed", attempt)))
    command = Mock(side_effect=AssertionError("no dispatch needed"))
    monkeypatch.setattr(real_tier.subprocess, "run", command)
    assert await real_tier._rerunnable_fixture("123") == ("123", attempt)
    command.assert_not_called()


@pytest.mark.parametrize("attempt", [45, 50, 51])
async def test_exhausted_fixture_dispatches_and_waits_for_new_run(monkeypatch, clock, attempt):
    import json

    facts = Mock(side_effect=[("completed", attempt), ("queued", 1), ("completed", 1)])
    monkeypatch.setattr(real_tier, "_run_facts", facts)
    commands = []
    listings = iter([[123, 122], [123, 122], [124, 123, 122]])

    def command(args, **kwargs):
        commands.append(args)
        assert args[-2:] == ["--repo", real_tier.PLUGIN_SLUG]
        if args[1:3] == ["run", "list"]:
            assert "ci-fixture-fails.yml" in args and "workflow_dispatch" in args
            return SimpleNamespace(
                returncode=0, stdout=json.dumps([{"databaseId": rid} for rid in next(listings)]), stderr=""
            )
        assert args[1:4] == ["workflow", "run", "ci-fixture-fails.yml"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(real_tier.subprocess, "run", command)
    assert await real_tier._rerunnable_fixture("123") == ("124", 1)
    assert [c.args[0] for c in facts.call_args_list] == ["123", "124", "124"]
    assert sum(args[1:3] == ["workflow", "run"] for args in commands) == 1
    assert clock[0] == 10


async def test_fixture_dispatch_refusal_fails_setup(monkeypatch, clock):
    monkeypatch.setattr(real_tier, "_run_facts", Mock(return_value=("completed", 50)))
    command = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout="[]", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="dispatch forbidden"),
        ]
    )
    monkeypatch.setattr(real_tier.subprocess, "run", command)
    with pytest.raises(AssertionError, match="dispatch forbidden"):
        await real_tier._rerunnable_fixture("123")


async def test_missing_new_fixture_fails_at_bounded_deadline(monkeypatch, clock):
    monkeypatch.setattr(real_tier, "_run_facts", Mock(return_value=("completed", 50)))
    monkeypatch.setattr(
        real_tier.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout='[{"databaseId": 123}]', stderr="")),
    )
    with pytest.raises(AssertionError, match="did not appear within 600s"):
        await real_tier._rerunnable_fixture("123")
    assert clock[0] == 600

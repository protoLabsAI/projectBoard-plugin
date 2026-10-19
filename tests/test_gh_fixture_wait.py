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

"""Deterministic coverage of setup for the real GitHub rerun assertion."""

import json
from types import SimpleNamespace

import pytest

import test_publish_gate_real as tier


@pytest.mark.parametrize("existing", [[500, 499], []])
async def test_dispatch_discovers_only_new_runs(monkeypatch, existing):
    listings = iter([existing, existing, [501, *existing]])
    commands = []
    sleeps = []

    def run(args, **kwargs):
        commands.append(args)
        output = json.dumps([{"databaseId": n} for n in next(listings)]) if args[1:3] == ["run", "list"] else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(tier.subprocess, "run", run)
    monkeypatch.setattr(tier.asyncio, "sleep", sleep)
    assert await tier._fresh_failed_fixture_run() == "501"
    assert sum(cmd[1:3] == ["workflow", "run"] for cmd in commands) == 1
    assert sleeps == [2]


@pytest.mark.parametrize("fail_at", ["list", "dispatch"])
async def test_fixture_setup_errors_fail_instead_of_skipping(monkeypatch, fail_at):
    def run(args, **kwargs):
        listing = args[1:3] == ["run", "list"]
        failed = listing if fail_at == "list" else not listing
        return SimpleNamespace(returncode=1 if failed else 0, stdout="[]", stderr="permission denied" if failed else "")

    monkeypatch.setattr(tier.subprocess, "run", run)
    with pytest.raises(AssertionError, match="permission denied"):
        await tier._fresh_failed_fixture_run()


async def test_fixture_discovery_has_a_deadline(monkeypatch):
    ticks = iter([0, 121])
    monkeypatch.setattr(tier, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(
        tier.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr="")
    )
    with pytest.raises(pytest.fail.Exception, match="did not appear"):
        await tier._fresh_failed_fixture_run()


async def test_rerun_waits_for_the_fresh_fixture(monkeypatch):
    from unittest.mock import AsyncMock

    fresh = AsyncMock(return_value="501")
    wait = AsyncMock(return_value=1)
    rerun = AsyncMock(return_value=["501"])
    monkeypatch.setattr(tier, "_WRITES_ALLOWED", True)
    monkeypatch.setattr(tier, "FAILED_RUN", "499")
    monkeypatch.setattr(tier, "_fresh_failed_fixture_run", fresh)
    monkeypatch.setattr(tier, "_wait_for_completed_fixture", wait)
    monkeypatch.setattr(tier.worktree, "rerun_failed_ci", rerun)
    monkeypatch.setattr(tier.asyncio, "sleep", AsyncMock())

    def facts(run_id):
        assert run_id == "501"
        return "completed", 2

    monkeypatch.setattr(tier, "_run_facts", facts)
    await tier.test_rerun_failed_ci_reruns_the_pinned_failed_fixture_run(None)
    fresh.assert_awaited_once_with()
    wait.assert_awaited_once_with("501")
    rerun.assert_awaited_once_with(cwd=str(tier.ROOT), run_ids=["501"], slug=tier.PLUGIN_SLUG)

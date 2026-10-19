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

"""Env sanitization tests (#78).

The loop must never hand a subprocess the HOST agent's identity/credentials —
``AGENT_NAME``, ``PROTOAGENT_*``, ``A2A_*``. These tests prove the strip on all
three spawn paths named in the issue (gate preflight, ``local_gate_cmd``, and the
coder — the last via the process-env scrub the ACP adapter's subprocess inherits),
and prove the ``env_passthrough`` whitelist lets a named var survive the strip.
"""

from __future__ import annotations

import os

import pytest

from project_board import coder_seam, config
from project_board.loop import BoardLoop


@pytest.fixture(autouse=True)
def _restore_environ():
    """Snapshot and fully restore ``os.environ`` around every test in this module.

    Kept as belt-and-braces even though the in-place ``scrub_process_env`` was
    reverted (it mutated the HOST's env — the regression that killed a live restart):
    snapshotting the whole env guarantees these tests can never leak host vars into
    the rest of the suite."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class _FakeProc:
    """Stand-in for an asyncio subprocess — enough surface for the gate/preflight paths."""

    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


# ── config.py: the sanitization contract ──────────────────────────────────────────


def test_is_host_identity_var_matches_blacklist():
    assert config.is_host_identity_var("AGENT_NAME")
    assert config.is_host_identity_var("PROTOAGENT_ID")
    assert config.is_host_identity_var("A2A_TOKEN")
    # Not host-identity — ordinary environment.
    assert not config.is_host_identity_var("PATH")
    assert not config.is_host_identity_var("HOME")
    assert not config.is_host_identity_var("AGENT")  # AGENT_NAME is exact, AGENT is not


def test_sanitized_env_strips_host_identity_and_credentials():
    src = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "AGENT_NAME": "host-agent",
        "PROTOAGENT_ID": "abc",
        "PROTOAGENT_SESSION": "xyz",
        "A2A_TOKEN": "secret",
        "A2A_URL": "https://bus",
    }
    out = config.sanitized_env(environ=src)
    # Host identity/credentials are gone …
    assert "AGENT_NAME" not in out
    assert "PROTOAGENT_ID" not in out
    assert "PROTOAGENT_SESSION" not in out
    assert "A2A_TOKEN" not in out
    assert "A2A_URL" not in out
    # … but ordinary env survives so the child still works.
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/root"


def test_sanitized_env_does_not_mutate_source():
    src = {"A2A_TOKEN": "secret", "PATH": "/usr/bin"}
    config.sanitized_env(environ=src)
    assert src == {"A2A_TOKEN": "secret", "PATH": "/usr/bin"}


def test_sanitized_env_passthrough_whitelist_wins():
    src = {"A2A_TOKEN": "secret", "AGENT_NAME": "host", "PROTOAGENT_ID": "abc", "PATH": "/usr/bin"}
    out = config.sanitized_env(passthrough=["A2A_TOKEN", "AGENT_NAME"], environ=src)
    # Whitelisted host vars pass through …
    assert out["A2A_TOKEN"] == "secret"
    assert out["AGENT_NAME"] == "host"
    # … non-whitelisted host vars are still stripped.
    assert "PROTOAGENT_ID" not in out
    assert out["PATH"] == "/usr/bin"


def test_parse_env_passthrough_accepts_list_and_string():
    assert config.parse_env_passthrough({}) == ()
    assert config.parse_env_passthrough({"env_passthrough": ["A2A_TOKEN", "AGENT_NAME"]}) == ("A2A_TOKEN", "AGENT_NAME")
    # A single comma/space-separated string is accepted and de-duplicated, order kept.
    assert config.parse_env_passthrough({"env_passthrough": "A2A_TOKEN, AGENT_NAME A2A_TOKEN"}) == (
        "A2A_TOKEN",
        "AGENT_NAME",
    )


# ── loop.py: wiring the sanitizer into the spawn paths ─────────────────────────────


def test_env_passthrough_config_parsed():
    assert BoardLoop({}).env_passthrough == ()  # nothing passes through by default
    assert BoardLoop({"env_passthrough": ["A2A_TOKEN"]}).env_passthrough == ("A2A_TOKEN",)


def test_child_env_strips_host_vars_and_honors_passthrough(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PROTOAGENT_ID", "abc")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    stripped = BoardLoop({})._child_env()
    assert "AGENT_NAME" not in stripped and "PROTOAGENT_ID" not in stripped and "A2A_TOKEN" not in stripped
    assert stripped["PATH"] == "/usr/bin"

    kept = BoardLoop({"env_passthrough": ["A2A_TOKEN"]})._child_env()
    assert kept["A2A_TOKEN"] == "secret"  # whitelisted
    assert "AGENT_NAME" not in kept and "PROTOAGENT_ID" not in kept  # still stripped


async def test_local_gate_spawns_with_sanitized_env(monkeypatch):
    """local_gate_cmd runs with the host identity/credentials stripped (#78 criterion 1)."""
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PROTOAGENT_ID", "abc")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"local_gate_cmd": "ruff check ."})._run_local_gate("/wt")

    env = captured["env"]
    assert "AGENT_NAME" not in env
    assert "PROTOAGENT_ID" not in env
    assert "A2A_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


async def test_local_gate_passes_through_whitelisted_var(monkeypatch):
    """A var listed in env_passthrough reaches the gate subprocess (#78 criterion 2)."""
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("AGENT_NAME", "host-agent")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"local_gate_cmd": "ruff check .", "env_passthrough": ["A2A_TOKEN"]})._run_local_gate("/wt")

    env = captured["env"]
    assert env["A2A_TOKEN"] == "secret"  # whitelisted → present
    assert "AGENT_NAME" not in env  # not whitelisted → stripped


async def test_preflight_spawns_with_sanitized_env(monkeypatch):
    """The gate preflight runs with host identity/credentials stripped (#78 criterion 1)."""
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PROTOAGENT_ID", "abc")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._store_kw = {"repo": "/repo"}
    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    env = captured["env"]
    assert "AGENT_NAME" not in env
    assert "PROTOAGENT_ID" not in env
    assert "A2A_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


async def test_fixups_spawns_with_sanitized_env(monkeypatch):
    """The pre-PR auto-fix command is also sanitized (#78)."""
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"format_cmd": "ruff format ."})._run_fixups("/wt")

    env = captured["env"]
    assert "A2A_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


# ── coder_seam.py: the solve() acceptance-test (verify) subprocess (#86) ────────────


class _FakeVerdict:
    """Minimal stand-in for ``coder.solve.Verdict`` — ``verify()`` only constructs one."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _verify_adapter(**overrides):
    """A ``_WorktreeSolveAdapter`` with just the fields ``verify()`` reads."""
    kw = dict(
        repo="/repo",
        base="main",
        root="/root",
        fid="bd-1",
        coder=None,
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=60.0,
        verdict_cls=_FakeVerdict,
    )
    kw.update(overrides)
    return coder_seam._WorktreeSolveAdapter(**kw)


async def test_solve_verify_spawns_with_sanitized_env(monkeypatch):
    """coder.solve()'s acceptance-test (verify) subprocess runs with the host
    identity/credentials stripped — the leak that burned 15 solve gens (#86). With no
    ``env=`` the child inherited ``os.environ`` verbatim (PROTOAGENT_*/A2A_*/AGENT_NAME)."""
    monkeypatch.setenv("PROTOAGENT_HOME", "/host/home")
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "host-instance")
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await _verify_adapter().verify("/wt")

    env = captured["env"]
    assert "PROTOAGENT_HOME" not in env
    assert "PROTOAGENT_INSTANCE" not in env
    assert "AGENT_NAME" not in env
    assert "A2A_AUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


async def test_solve_verify_passes_through_whitelisted_var(monkeypatch):
    """A var listed in env_passthrough reaches the verify subprocess (#86 criterion 2),
    threaded from the loop so the verify strip matches the gate's whitelist exactly."""
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret")
    monkeypatch.setenv("AGENT_NAME", "host-agent")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await _verify_adapter(env_passthrough=["A2A_AUTH_TOKEN"]).verify("/wt")

    env = captured["env"]
    assert env["A2A_AUTH_TOKEN"] == "secret"  # whitelisted → present
    assert "AGENT_NAME" not in env  # not whitelisted → stripped


async def test_dispatch_threads_env_passthrough_to_verify(monkeypatch):
    """The loop's env_passthrough reaches the verify subprocess through
    ``coder_seam.dispatch`` → the adapter constructor (#86 criterion 3). A fake
    ``solve`` drives the adapter's REAL ``verify()``, then returns a non-passing
    result so dispatch exhausts without needing the promote/reap machinery."""
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret")
    monkeypatch.setenv("AGENT_NAME", "host-agent")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)

    class _Budget:
        def __init__(self, n):
            self.n = n

    class _Result:
        passed = False  # → dispatch raises SolveExhausted (no promote path to fake)
        solution = None
        rung = "greedy"
        gens_spent = 1
        note = "no candidate passed"
        verdict = None

    async def _fake_solve(task, *, generate, verify, **kw):
        await verify("/wt/win")  # exercise the adapter's real verify() + threaded env
        return _Result()

    with pytest.raises(coder_seam.SolveExhausted):
        await coder_seam.dispatch(
            task="do it",
            coder=None,
            repo="/repo",
            base="main",
            root="/root",
            fid="bd-1",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=60.0,
            budget=1,
            k=1,
            tree_depth=1,
            env_passthrough=["A2A_AUTH_TOKEN"],
            _solve=_fake_solve,
            _budget_cls=_Budget,
            _verdict_cls=_FakeVerdict,
        )

    env = captured["env"]
    assert env["A2A_AUTH_TOKEN"] == "secret"  # whitelisted → present in verify()
    assert "AGENT_NAME" not in env  # not whitelisted → stripped

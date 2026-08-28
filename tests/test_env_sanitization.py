"""Env sanitization tests (#78, tightened by F8a).

The loop must never hand a subprocess the HOST agent's identity/credentials —
``AGENT_NAME``, ``PROTOAGENT_*``, ``A2A_*``. These tests prove the strip on all
three spawn paths named in the issue (gate preflight, ``local_gate_cmd``, and the
coder — the last via the process-env scrub the ACP adapter's subprocess inherits),
and prove the ``env_passthrough`` whitelist lets a named var survive the strip.

F8a adds a second, stricter tier for the loop's OWN children (gate preflight,
``local_gate_cmd``, ``format_cmd``): a narrow allowlist — the baseline a build/test
toolchain needs plus ``env_passthrough``, dropping everything else. F8b extends
that tier to the fourth spawn site: the ``coder.solve()`` seam's acceptance-test
(verify) subprocess. The ACP/coder path deliberately stays blacklist-only; those
tests are unchanged below.
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


# ── config.py: the allowlist tier for gate/format/preflight children (F8a) ─────────


def test_is_allowlisted_var_matches_baseline():
    for name in ("PATH", "HOME", "LANG", "TMPDIR", "TERM", "SHELL", "USER", "CI"):
        assert config.is_allowlisted_var(name), name
    # The Windows mirror of the same baseline — SYSTEMROOT above all: subprocess
    # requires it in an explicit child environment for children to start on Windows.
    for name in (
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        assert config.is_allowlisted_var(name), name
    # Locale is a prefix match — every LC_* var is baseline.
    assert config.is_allowlisted_var("LC_ALL")
    assert config.is_allowlisted_var("LC_CTYPE")
    # Outside the baseline — ordinary vars and host-identity vars alike.
    assert not config.is_allowlisted_var("EDITOR")
    assert not config.is_allowlisted_var("VIRTUAL_ENV")
    assert not config.is_allowlisted_var("AGENT_NAME")
    assert not config.is_allowlisted_var("A2A_TOKEN")
    # Exact names are exact, not prefixes.
    assert not config.is_allowlisted_var("HOMEBREW_PREFIX")
    assert not config.is_allowlisted_var("TERMINFO")


def test_sanitized_env_allowlist_keeps_only_baseline():
    baseline = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "TERM": "xterm",
        "SHELL": "/bin/zsh",
        "USER": "op",
        "CI": "true",
    }
    src = dict(
        baseline,
        # Everything below is outside the baseline and must be dropped — ordinary
        # vars and the #78 host-identity block alike.
        EDITOR="vim",
        VIRTUAL_ENV="/venv",
        AGENT_NAME="host-agent",
        PROTOAGENT_ID="abc",
        A2A_TOKEN="secret",
    )
    assert config.sanitized_env(environ=src, mode="allowlist") == baseline


def test_sanitized_env_allowlist_keeps_windows_system_baseline():
    """A Windows-shaped environment keeps the system block with NO env_passthrough —
    SYSTEMROOT above all: subprocess requires a valid SystemRoot in an explicit
    child environment, so dropping it can stop children from starting at all
    (the review finding on the first F8a cut)."""
    baseline = {
        "PATH": r"C:\Windows\system32",
        "SYSTEMROOT": r"C:\Windows",
        "SYSTEMDRIVE": "C:",
        "WINDIR": r"C:\Windows",
        "COMSPEC": r"C:\Windows\system32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": r"C:\Users\op\AppData\Local\Temp",
        "TMP": r"C:\Users\op\AppData\Local\Temp",
        "USERPROFILE": r"C:\Users\op",
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\Users\op",
        "USERNAME": "op",
        "APPDATA": r"C:\Users\op\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\op\AppData\Local",
    }
    # A Windows var outside the baseline and a host-identity var are still dropped.
    src = dict(baseline, PROCESSOR_LEVEL="6", AGENT_NAME="host-agent")
    assert config.sanitized_env(environ=src, mode="allowlist") == baseline


def test_sanitized_env_allowlist_passthrough_wins():
    src = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "EDITOR": "vim", "AGENT_NAME": "host"}
    out = config.sanitized_env(passthrough=["VIRTUAL_ENV"], environ=src, mode="allowlist")
    # The passed-through var survives; everything else outside the baseline is dropped.
    assert out == {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv"}


def test_sanitized_env_allowlist_does_not_mutate_source():
    src = {"PATH": "/usr/bin", "EDITOR": "vim"}
    config.sanitized_env(environ=src, mode="allowlist")
    assert src == {"PATH": "/usr/bin", "EDITOR": "vim"}


def test_sanitized_env_rejects_unknown_mode():
    with pytest.raises(ValueError):
        config.sanitized_env(environ={}, mode="denylist")


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

    # #90: preflight is now per-project and only smokes projects with ready work — give
    # it one ready feature (default project) so the gate actually spawns.
    class _Store:
        def list_features(self, state=None):
            return [{"id": "bd-1"}] if state == "ready" else []

    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _Store())
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


# ── loop.py: gate/format/preflight children see ONLY the allowlist (F8a) ───────────
#
# The #78 tests above prove the host-identity block never reaches these children.
# F8a is stronger: NOTHING outside the baseline allowlist reaches them unless it is
# named in ``env_passthrough`` — proven against the child's ENTIRE environment, not
# just the vars a test happened to set.


def _assert_allowlist_only(env: dict, passthrough: tuple = ()):
    """Every var the child sees is baseline-allowlisted or explicitly passed through."""
    leaked = [k for k in env if not (config.is_allowlisted_var(k) or k in passthrough)]
    assert not leaked, f"vars outside the allowlist reached the child: {leaked}"


async def test_local_gate_env_is_allowlist_only(monkeypatch):
    """The gate child sees no variable outside the allowlist unless passed through (F8a r1)."""
    monkeypatch.setenv("EDITOR", "vim")  # ordinary, NOT host-identity — still must not leak
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "C")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"local_gate_cmd": "ruff check ."})._run_local_gate("/wt")

    env = captured["env"]
    assert "EDITOR" not in env and "VIRTUAL_ENV" not in env and "AGENT_NAME" not in env
    assert env["PATH"] == "/usr/bin"  # baseline survives …
    assert env["LC_ALL"] == "C"  # … including the LC_* prefix
    _assert_allowlist_only(env)


async def test_local_gate_allowlist_honors_passthrough(monkeypatch):
    """env_passthrough is the only door through the allowlist for the gate child (F8a r1)."""
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    monkeypatch.setenv("EDITOR", "vim")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"local_gate_cmd": "ruff check .", "env_passthrough": ["VIRTUAL_ENV"]})._run_local_gate("/wt")

    env = captured["env"]
    assert env["VIRTUAL_ENV"] == "/venv"  # passed through → present
    assert "EDITOR" not in env  # not passed through → dropped
    _assert_allowlist_only(env, passthrough=("VIRTUAL_ENV",))


async def test_preflight_env_is_allowlist_only(monkeypatch):
    """The gate preflight child sees only the allowlist baseline (F8a r1)."""
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})

    # #90: preflight only smokes projects with ready work — give it one ready feature.
    class _Store:
        def list_features(self, state=None):
            return [{"id": "bd-1"}] if state == "ready" else []

    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _Store())
    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    env = captured["env"]
    assert "EDITOR" not in env and "VIRTUAL_ENV" not in env
    assert env["PATH"] == "/usr/bin"
    _assert_allowlist_only(env)


async def test_fixups_env_is_allowlist_only(monkeypatch):
    """The pre-PR format_cmd child sees only the allowlist baseline (F8a r1)."""
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await BoardLoop({"format_cmd": "ruff format ."})._run_fixups("/wt")

    env = captured["env"]
    assert "EDITOR" not in env
    assert env["PATH"] == "/usr/bin"
    _assert_allowlist_only(env)


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


# ── coder_seam.py: the verify child sees ONLY the allowlist (F8b) ──────────────────
#
# The #86 tests above prove the host-identity block never reaches the verify child.
# F8b is stronger: the verify child runs a repo-defined command over coder-written
# code — the same posture as the loop's gate/format/preflight children (F8a) — so
# NOTHING outside the baseline allowlist reaches it unless named in
# ``env_passthrough``, proven against the child's ENTIRE environment.


async def test_solve_verify_env_is_allowlist_only(monkeypatch):
    """The verify child sees no variable outside the allowlist unless passed through (F8b r1)."""
    monkeypatch.setenv("EDITOR", "vim")  # ordinary, NOT host-identity — still must not leak
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "C")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await _verify_adapter().verify("/wt")

    env = captured["env"]
    assert "EDITOR" not in env and "VIRTUAL_ENV" not in env and "AGENT_NAME" not in env
    assert env["PATH"] == "/usr/bin"  # baseline survives …
    assert env["LC_ALL"] == "C"  # … including the LC_* prefix
    _assert_allowlist_only(env)


async def test_solve_verify_allowlist_honors_passthrough(monkeypatch):
    """env_passthrough is the only door through the allowlist for the verify child (F8b r1)."""
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    monkeypatch.setenv("EDITOR", "vim")

    captured = {}

    async def _shell(cmd, **kw):
        captured.update(kw)
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await _verify_adapter(env_passthrough=["VIRTUAL_ENV"]).verify("/wt")

    env = captured["env"]
    assert env["VIRTUAL_ENV"] == "/venv"  # passed through → present
    assert "EDITOR" not in env  # not passed through → dropped
    _assert_allowlist_only(env, passthrough=("VIRTUAL_ENV",))


async def test_dispatch_verify_env_is_allowlist_only(monkeypatch):
    """The allowlist holds on the REAL dispatch wiring too — ``coder_seam.dispatch`` →
    adapter constructor → ``verify()`` (F8b r1), reusing the #86 fake-solve harness."""
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

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
    assert env["A2A_AUTH_TOKEN"] == "secret"  # passed through → present
    assert "EDITOR" not in env  # ordinary var outside the baseline → dropped
    _assert_allowlist_only(env, passthrough=("A2A_AUTH_TOKEN",))


# ── worktree.dispatch_coder / coder_seam.dispatch_coder_tapped: Delegate env overlay (#142) ──────


def _inject_fake_acp_adapter(monkeypatch, captured: dict):
    """Inject a fake ``plugins.delegates.adapters`` module so ``dispatch_coder``'s
    local import resolves without the host plugins installed. The fake adapter
    records the scoped Delegate on ``forget_session``."""
    import sys
    import types

    class _FakeAdapter:
        async def forget_session(self, scoped):
            captured["scoped"] = scoped

        async def dispatch(self, scoped, prompt, timeout=None):
            return "done"

        async def teardown(self, scoped):
            pass

    fake_mod = types.ModuleType("plugins.delegates.adapters")
    fake_mod.ADAPTERS = {"acp": _FakeAdapter()}
    fake_mod.DelegateError = Exception

    for _ns in ("plugins", "plugins.delegates"):
        if _ns not in sys.modules:
            monkeypatch.setitem(sys.modules, _ns, types.ModuleType(_ns))
    monkeypatch.setitem(sys.modules, "plugins.delegates.adapters", fake_mod)


async def test_dispatch_coder_scoped_env_excludes_host_vars(monkeypatch):
    """dispatch_coder populates the scoped Delegate's env overlay via sanitized_env —
    host identity vars are absent from the overlay (#142)."""
    import dataclasses as _dc

    @_dc.dataclass
    class _FakeDelegate:
        workdir: str = ""
        env: dict = _dc.field(default_factory=dict)
        manage_git: bool = False

    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("PROTOAGENT_ID", "abc")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}
    _inject_fake_acp_adapter(monkeypatch, captured)

    from project_board import worktree as wt_mod

    await wt_mod.dispatch_coder(_FakeDelegate(), "/wt", "do it", env_passthrough=())

    scoped = captured["scoped"]
    assert "AGENT_NAME" not in scoped.env
    assert "PROTOAGENT_ID" not in scoped.env
    assert scoped.env["PATH"] == "/usr/bin"


async def test_dispatch_coder_scoped_env_honors_passthrough(monkeypatch):
    """env_passthrough whitelist reaches the scoped Delegate's env overlay (#142)."""
    import dataclasses as _dc

    @_dc.dataclass
    class _FakeDelegate:
        workdir: str = ""
        env: dict = _dc.field(default_factory=dict)
        manage_git: bool = False

    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}
    _inject_fake_acp_adapter(monkeypatch, captured)

    from project_board import worktree as wt_mod

    await wt_mod.dispatch_coder(_FakeDelegate(), "/wt", "do it", env_passthrough=["A2A_TOKEN"])

    scoped = captured["scoped"]
    assert scoped.env["A2A_TOKEN"] == "secret"  # whitelisted → present
    assert "AGENT_NAME" not in scoped.env


async def test_dispatch_coder_no_env_field_unchanged(monkeypatch):
    """When the Delegate has no env field, dispatch_coder does not add one (#142, r2)."""
    import dataclasses as _dc

    @_dc.dataclass
    class _FakeDelegate:
        workdir: str = ""
        manage_git: bool = False

    monkeypatch.setenv("AGENT_NAME", "host-agent")

    captured = {}
    _inject_fake_acp_adapter(monkeypatch, captured)

    from project_board import worktree as wt_mod

    await wt_mod.dispatch_coder(_FakeDelegate(), "/wt", "do it", env_passthrough=())

    scoped = captured["scoped"]
    assert not hasattr(scoped, "env")  # no env field on the no-env delegate


async def test_dispatch_coder_tapped_threads_env_passthrough_to_fallback(monkeypatch):
    """dispatch_coder_tapped's fallback path threads env_passthrough to dispatch_coder,
    which strips host identity vars from the scoped Delegate's env overlay (#142)."""
    import dataclasses as _dc

    @_dc.dataclass
    class _FakeDelegate:
        workdir: str = ""
        env: dict = _dc.field(default_factory=dict)
        manage_git: bool = False

    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured = {}
    _inject_fake_acp_adapter(monkeypatch, captured)

    from project_board import coder_seam as cs

    # dispatch_coder_tapped's tap path fails (plugins.coding_agent absent) → fallback to
    # worktree.dispatch_coder, which builds the scoped Delegate with the sanitized env.
    await cs.dispatch_coder_tapped(_FakeDelegate(), "/wt", "do it", fid="bd-1", gen=1, env_passthrough=["A2A_TOKEN"])

    scoped = captured["scoped"]
    assert scoped.env["A2A_TOKEN"] == "secret"  # whitelisted → present
    assert "AGENT_NAME" not in scoped.env

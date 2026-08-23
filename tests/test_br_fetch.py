"""`br` fetched on first run (v0.43.0) — br_fetch.py and the places it surfaces.

No network, no real `br`: the downloader is injected (a fake that returns a tarball
built in-test), the data dir is a tmp dir (conftest), the fetch runs inline
(``background=False``). Pins: the pin/checksum table itself (one sha per supported
platform, CI runs the shape tier on exactly that version), the resolution order
(``BR_BIN`` > fetched > PATH), and every failure turning into a ``br`` setup gap with
the error in the hint — never a traceback.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import stat
import tarfile
from pathlib import Path

import pytest
import yaml

import project_board as pb
from project_board import br_fetch, setup_check
from project_board import store as store_mod
from project_board.loop import BoardLoop

ROOT = Path(pb.__file__).resolve().parent
LOGGER = "protoagent.plugins.project_board"


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


FAKE_BR = b"#!/bin/sh\necho br 9.9.9-fake\n"
ARCHIVE = _tarball({"br": FAKE_BR, "README.md": b"hi", "LICENSE": b"mit"})
ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()


def _spec(sha=ARCHIVE_SHA, platform="darwin_arm64"):
    return br_fetch.FetchSpec(version="9.9.9", platform=platform, url="https://github.com/x/y/br.tar.gz", sha256=sha)


def _downloader(payload=ARCHIVE, *, calls=None, raise_exc=None):
    calls = [] if calls is None else calls

    def dl(url, timeout=0.0):
        calls.append((url, timeout))
        if raise_exc is not None:
            raise raise_exc
        return payload

    dl.calls = calls
    return dl


def _no_br(name):
    return (
        None
        if os.path.basename(name) == "br" and not os.path.isabs(name)
        else (name if os.path.isabs(name) and os.path.exists(name) else None)
    )


@pytest.fixture(autouse=True)
def _restore_store_br():
    before = store_mod.BR
    yield
    store_mod.BR = before


# ── the pin table ────────────────────────────────────────────────────────────────


def test_pin_table_covers_the_four_supported_platforms_with_real_shas():
    assert set(br_fetch.BR_SHA256) == {"darwin_arm64", "darwin_amd64", "linux_amd64", "linux_arm64"}
    for key, sha in br_fetch.BR_SHA256.items():
        assert re.fullmatch(r"[0-9a-f]{64}", sha), key
    assert re.fullmatch(r"\d+\.\d+\.\d+", br_fetch.BR_VERSION)
    spec = br_fetch.fetch_spec("linux_amd64")
    assert spec.url == (
        f"https://github.com/Dicklesworthstone/beads_rust/releases/download/v{br_fetch.BR_VERSION}/"
        f"br-{br_fetch.BR_VERSION}-linux_amd64.tar.gz"
    )
    assert spec.sha256 == br_fetch.BR_SHA256["linux_amd64"]


def test_ci_runs_the_real_br_tier_on_exactly_the_pinned_version():
    """The pin moves WITH a CI leg: the workflow's matrix must carry an entry for
    BR_VERSION whose asset + sha256 equal the plugin's linux_amd64 pin, so the
    binary a fresh member fetches is the one CI proved the store against."""
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    entries = wf["jobs"]["test"]["strategy"]["matrix"]["include"]
    leg = next((e for e in entries if str(e["br_version"]) == br_fetch.BR_VERSION), None)
    assert leg is not None, f"ci.yml has no matrix leg for the auto-fetch pin v{br_fetch.BR_VERSION}"
    assert leg["br_asset"] == f"br-{br_fetch.BR_VERSION}-linux_amd64.tar.gz"
    assert leg["br_sha256"] == br_fetch.BR_SHA256["linux_amd64"]


def test_platform_key_maps_machine_aliases_and_rejects_the_rest():
    assert br_fetch.platform_key("Darwin", "arm64") == "darwin_arm64"
    assert br_fetch.platform_key("darwin", "aarch64") == "darwin_arm64"
    assert br_fetch.platform_key("Darwin", "x86_64") == "darwin_amd64"
    assert br_fetch.platform_key("Linux", "x86_64") == "linux_amd64"
    assert br_fetch.platform_key("linux", "amd64") == "linux_amd64"
    assert br_fetch.platform_key("Linux", "aarch64") == "linux_arm64"
    assert br_fetch.platform_key("Windows", "AMD64") is None
    assert br_fetch.platform_key("linux", "riscv64") is None
    assert br_fetch.fetch_spec("windows_amd64") is None


def test_platform_key_refuses_musl_linux(monkeypatch):
    """Alpine: the pinned linux assets are glibc builds — a musl host is `unsupported`
    with its own hint, not a binary that fails to exec."""
    assert br_fetch.platform_key("linux", "x86_64", musl=True) is None
    assert br_fetch.platform_key("linux", "x86_64", musl=False) == "linux_amd64"
    assert br_fetch.platform_key("darwin", "arm64", musl=True) == "darwin_arm64"  # musl is a linux question
    monkeypatch.setattr(br_fetch, "is_musl", lambda: True)
    assert br_fetch.platform_key("linux", "aarch64") is None
    monkeypatch.setattr(br_fetch._platform, "system", lambda: "Linux")
    monkeypatch.setattr(br_fetch._platform, "machine", lambda: "x86_64")
    st = br_fetch.ensure_br({}, which=_no_br, downloader=_downloader(), background=False)
    assert st["state"] == "unsupported" and st["error"] == br_fetch.MUSL_HINT
    assert br_fetch.hint_for(st) == br_fetch.MUSL_HINT


def test_is_musl_detection(monkeypatch):
    monkeypatch.setattr(br_fetch._platform, "libc_ver", lambda: ("glibc", "2.35"))
    assert br_fetch.is_musl() is False
    monkeypatch.setattr(br_fetch._platform, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(br_fetch.Path, "glob", lambda self, pat: iter([Path("/lib/ld-musl-x86_64.so.1")]))
    assert br_fetch.is_musl() is True
    monkeypatch.setattr(br_fetch.Path, "glob", lambda self, pat: iter([]))
    assert br_fetch.is_musl() is False


# ── resolution order: BR_BIN > fetched > PATH ───────────────────────────────────


def test_resolve_br_bin_precedence(tmp_path):
    fetched = tmp_path / "bin" / "br"
    assert br_fetch.resolve_br_bin(env={}, fetched=fetched) == "br"  # nothing fetched → PATH name
    fetched.parent.mkdir()
    fetched.write_bytes(FAKE_BR)
    fetched.chmod(0o755)
    assert br_fetch.resolve_br_bin(env={}, fetched=fetched) == str(fetched)  # fetched beats PATH
    assert br_fetch.resolve_br_bin(env={"BR_BIN": "/opt/my/br"}, fetched=fetched) == "/opt/my/br"  # env wins
    fetched.chmod(0o644)
    assert br_fetch.resolve_br_bin(env={}, fetched=fetched) == "br"  # not executable → ignored


def test_data_dir_honors_the_env_override_and_never_the_plugin_source_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(br_fetch.ENV_DATA_DIR, str(tmp_path / "data"))
    assert br_fetch.data_dir() == tmp_path / "data"
    assert br_fetch.fetched_br_path() == tmp_path / "data" / "bin" / br_fetch.BR_VERSION / "br"
    monkeypatch.delenv(br_fetch.ENV_DATA_DIR)
    d = br_fetch.data_dir()  # host-free fallback (no infra.paths in the suite)
    assert ROOT not in d.parents and d != ROOT
    assert d.parts[-2:] == ("plugin-data", "project_board")


def test_fetched_path_is_keyed_by_version_so_a_pin_bump_refetches(tmp_path, monkeypatch):
    """Review on #216: the fetched binary was `bin/br`, unversioned — bumping BR_VERSION
    never re-fetched, and fetch_state()["version"] reported the NEW pin over the OLD
    binary. Now `bin/<version>/br`: the old binary is simply not the resolved path any
    more, and the version the state reports is the one the path was built for."""
    monkeypatch.setenv(br_fetch.ENV_DATA_DIR, str(tmp_path))
    old = br_fetch.fetched_br_path(version="0.2.16")
    old.parent.mkdir(parents=True)
    old.write_bytes(FAKE_BR)
    old.chmod(0o755)
    assert br_fetch.resolve_br_bin(env={}) == "br"  # the old pin's binary is not "fetched" for THIS pin
    dl = _downloader()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(br_fetch.BR_SHA256, "linux_amd64", ARCHIVE_SHA)
        st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform="linux_amd64", background=False)
    assert st["state"] == "done" and len(dl.calls) == 1  # re-fetched into the new version dir
    assert Path(st["path"]) == tmp_path / "bin" / br_fetch.BR_VERSION / "br"
    assert st["version"] == br_fetch.BR_VERSION
    assert old.exists()  # the old one is left alone (delete <data>/bin to clean up)


def test_fetch_state_version_is_never_the_bare_pin_over_an_unknown_binary(tmp_path, monkeypatch):
    """A `br` on PATH: version "" (setup_check samples `br --version` for it). A
    previously fetched binary found on restart: the version its path was built for."""
    monkeypatch.setenv(br_fetch.ENV_DATA_DIR, str(tmp_path))
    assert br_fetch.fetch_state()["version"] == ""  # idle: no spec, no path
    st = br_fetch.ensure_br({}, which=lambda n: "/usr/local/bin/br", downloader=_downloader(), background=False)
    assert st["state"] == "done" and st["path"] == "" and st["version"] == ""
    br_fetch.reset_state()
    fetched = br_fetch.fetched_br_path()
    fetched.parent.mkdir(parents=True)
    fetched.write_bytes(FAKE_BR)
    fetched.chmod(0o755)
    st = br_fetch.ensure_br(
        {}, which=lambda n: n if os.path.isabs(n) else None, downloader=_downloader(), background=False
    )
    assert st["state"] == "done" and st["path"] == str(fetched) and st["version"] == br_fetch.BR_VERSION
    assert br_fetch._version_from_path("/x/bin/0.1.23/br") == "0.1.23"
    assert br_fetch._version_from_path("/x/bin/br") == "" and br_fetch._version_from_path("") == ""


# ── fetch_br: the download → verify → extract → atomic install pipeline ─────────


def test_fetch_br_verifies_sha_extracts_only_br_and_installs_executable(tmp_path):
    dest = tmp_path / "bin" / "br"
    dl = _downloader()
    out = br_fetch.fetch_br(_spec(), dest, downloader=dl, timeout=12.5)
    assert out == dest and dest.read_bytes() == FAKE_BR
    assert dest.stat().st_mode & stat.S_IXUSR
    assert sorted(p.name for p in dest.parent.iterdir()) == ["br"]  # README/LICENSE never land, no temp left
    assert dl.calls == [("https://github.com/x/y/br.tar.gz", 12.5)]
    # a temp a previous process died in is swept before the new one is written
    (dest.parent / ".br-deadbeef").write_bytes(b"x")
    br_fetch.fetch_br(_spec(), dest, downloader=_downloader())
    assert sorted(p.name for p in dest.parent.iterdir()) == ["br"]


def test_fetch_br_sha_mismatch_refuses_to_install(tmp_path):
    dest = tmp_path / "bin" / "br"
    with pytest.raises(ValueError, match="sha256 mismatch"):
        br_fetch.fetch_br(_spec(sha="0" * 64), dest, downloader=_downloader())
    assert not dest.exists()


def test_fetch_br_archive_without_br_member_fails(tmp_path):
    bad = _tarball({"README.md": b"no binary here"})
    with pytest.raises(ValueError, match="no `br` binary"):
        br_fetch.fetch_br(_spec(sha=hashlib.sha256(bad).hexdigest()), tmp_path / "br", downloader=_downloader(bad))


def test_fetch_br_honors_the_hosts_egress_allowlist(tmp_path, monkeypatch):
    import sys
    import types

    sec = types.ModuleType("security")
    sec.__path__ = []
    eg = types.ModuleType("security.egress")
    eg.check_url = lambda url, **kw: "Error: egress to github.com is blocked — not in the egress allowlist"
    monkeypatch.setitem(sys.modules, "security", sec)
    monkeypatch.setitem(sys.modules, "security.egress", eg)
    dl = _downloader()
    with pytest.raises(PermissionError, match="egress blocked"):
        br_fetch.fetch_br(_spec(), tmp_path / "br", downloader=dl)
    assert dl.calls == []  # never even tried


def test_redirect_hops_are_pinned_and_egress_checked(monkeypatch):
    """Review on #216: check_url saw the initial URL only — urllib followed the 302 to
    release-assets.githubusercontent.com unchecked. Every hop now passes the host's
    allowlist AND must stay on https *.githubusercontent.com."""
    import sys
    import types

    ok = "https://release-assets.githubusercontent.com/github-production-release-asset/x/br.tar.gz"
    br_fetch.check_redirect_target(ok)  # the real target
    br_fetch.check_redirect_target("https://objects.githubusercontent.com/x")  # the older one, still allowed
    with pytest.raises(PermissionError, match="refused"):
        br_fetch.check_redirect_target("https://evil.example.com/br.tar.gz")
    with pytest.raises(PermissionError, match="refused"):
        br_fetch.check_redirect_target("http://release-assets.githubusercontent.com/x")  # no plaintext hop
    with pytest.raises(PermissionError, match="refused"):
        br_fetch.check_redirect_target("https://githubusercontent.com.evil.example/x")
    # the host's allowlist verdict applies to the hop too
    sec = types.ModuleType("security")
    sec.__path__ = []
    eg = types.ModuleType("security.egress")
    seen = []
    eg.check_url = lambda url, **kw: (
        seen.append(url) or "Error: egress to release-assets.githubusercontent.com is blocked"
    )
    monkeypatch.setitem(sys.modules, "security", sec)
    monkeypatch.setitem(sys.modules, "security.egress", eg)
    with pytest.raises(PermissionError, match="egress blocked on redirect"):
        br_fetch.check_redirect_target(ok)
    assert seen == [ok]


def test_urllib_download_installs_the_pinned_redirect_handler(monkeypatch):
    """The opener urllib uses for the GET carries _PinnedRedirects, whose
    redirect_request runs check_redirect_target on each Location."""
    import urllib.request

    installed = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b""

    class _Opener:
        def open(self, req, timeout=None):
            installed.append(("open", req.full_url, timeout))
            return _Resp()

    def _build(*handlers):
        installed.extend(type(h) for h in handlers)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", _build)
    real = br_fetch._urllib_download.real  # conftest swaps the attribute and keeps the real one on the stub
    assert real("https://github.com/x/br.tar.gz", timeout=12.0) == b""
    assert br_fetch._PinnedRedirects in installed
    assert ("open", "https://github.com/x/br.tar.gz", 12.0) in installed
    # the handler refuses a bad hop before urllib builds the follow-up request
    h = br_fetch._PinnedRedirects()
    with pytest.raises(PermissionError):
        h.redirect_request(urllib.request.Request("https://github.com/x"), None, 302, "Found", {}, "https://evil/x")


# ── ensure_br: the once-per-process state machine ───────────────────────────────


def test_ensure_br_fetches_once_activates_the_store_and_reports_done(tmp_path, caplog):
    dest = tmp_path / "bin" / "br"
    dl = _downloader()
    spec = _spec()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform=spec.platform, dest=dest, background=False)
    # the pinned spec for the platform is what's fetched — swap the pin for the fake
    # archive's sha via the public table
    assert st["state"] == "failed"  # real pin sha ≠ fake archive → mismatch (the table is real)
    assert "sha256 mismatch" in st["error"]
    # now with the table pointing at the fake archive
    br_fetch.reset_state()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(br_fetch.BR_SHA256, spec.platform, ARCHIVE_SHA)
        dl = _downloader()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            st = br_fetch.ensure_br(
                {}, which=_no_br, downloader=dl, platform=spec.platform, dest=dest, background=False
            )
        assert st["state"] == "done" and st["path"] == str(dest) and dest.exists()
        assert store_mod.BR == str(dest)  # the store is re-pointed in place
        assert any("fetched to" in r.message for r in caplog.records)
        assert any("fetching beads-rust v" in r.message for r in caplog.records)
        # ONCE: a second call is a no-op (and `which` now finds the fetched binary anyway)
        st2 = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform=spec.platform, dest=dest, background=False)
        assert st2["state"] == "done" and len(dl.calls) == 1


def test_ensure_br_failure_is_a_gap_not_a_traceback_and_stays_failed(tmp_path, caplog):
    dest = tmp_path / "bin" / "br"
    dl = _downloader(raise_exc=TimeoutError("download exceeded 60s"))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest, background=False)
    assert st["state"] == "failed" and "TimeoutError: download exceeded 60s" in st["error"]
    assert not dest.exists()
    warn = [r for r in caplog.records if "auto-fetch failed" in r.message]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and "cargo install beads_rust" in warn[0].message
    assert not any(r.exc_info for r in caplog.records)  # no traceback logged
    # once per process: no retry on the next call
    st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest, background=False)
    assert st["state"] == "failed" and len(dl.calls) == 1
    hint = br_fetch.hint_for(st)
    assert "br auto-fetch failed: TimeoutError" in hint and "install beads-rust" in hint and "restart to retry" in hint
    # "restart to retry" is TRUE: a restart starts from idle and fetches again (knob toggles never do)
    br_fetch.reset_state()
    dl2 = _downloader(raise_exc=TimeoutError("again"))
    st = br_fetch.ensure_br({}, which=_no_br, downloader=dl2, platform="linux_amd64", dest=dest, background=False)
    assert st["state"] == "failed" and len(dl2.calls) == 1
    assert "~60s cap" in br_fetch.hint_for({"state": "fetching", "version": "9", "platform": "p", "started": 0})


def test_ensure_br_disabled_is_the_old_install_hint(tmp_path):
    dl = _downloader()
    for off in (False, "false", "off", 0):
        br_fetch.reset_state()
        st = br_fetch.ensure_br(
            {"br_autofetch": off},
            which=_no_br,
            downloader=dl,
            platform="linux_amd64",
            dest=tmp_path / "br",
            background=False,
        )
        assert st["state"] == "disabled", off
    assert dl.calls == []
    hint = br_fetch.hint_for(st)
    assert "br_autofetch is off" in hint and "cargo install beads_rust" in hint


def test_knob_off_mid_download_never_clobbers_fetching_and_knob_on_never_doubles(tmp_path):
    """Review on #216: ensure_br with the knob off wrote `disabled` over a `fetching`
    state, and flipping back on started a SECOND concurrent download. The knob check
    now runs under the holder lock and only ever moves idle → disabled."""
    import threading

    gate = threading.Event()
    calls = []

    def dl(url, timeout=0.0):
        calls.append(url)
        gate.wait(5)
        return ARCHIVE

    dest = tmp_path / "bin" / "br"
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(br_fetch.BR_SHA256, "linux_amd64", ARCHIVE_SHA)
        st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest)
        assert st["state"] == "fetching"
        # knob off while the download is in flight: state stays `fetching`
        st = br_fetch.ensure_br({"br_autofetch": False}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest)
        assert st["state"] == "fetching"
        # knob back on: no second download
        st = br_fetch.ensure_br({"br_autofetch": True}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest)
        assert st["state"] == "fetching"
        gate.set()
        for t in threading.enumerate():
            if t.name == "project-board-br-fetch":
                t.join(5)
        assert br_fetch.fetch_state()["state"] == "done" and len(calls) == 1
        # …and a knob-off AFTER done/failed never rewrites those either
        st = br_fetch.ensure_br({"br_autofetch": False}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest)
        assert st["state"] == "done"
    dest2 = tmp_path / "bin2" / "br"  # a dest that does NOT exist yet (dest landed above)
    br_fetch._set(state="failed", error="x")
    st = br_fetch.ensure_br({"br_autofetch": False}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest2)
    assert st["state"] == "failed"
    # idle → disabled → (knob on) → fetching is still the normal path
    br_fetch.reset_state()
    st = br_fetch.ensure_br({"br_autofetch": False}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest2)
    assert st["state"] == "disabled"
    st = br_fetch.ensure_br(
        {"br_autofetch": True}, which=_no_br, downloader=dl, platform="linux_amd64", dest=dest2, background=False
    )
    assert st["state"] in ("done", "failed") and len(calls) == 2


def test_ensure_br_present_on_path_never_fetches(tmp_path):
    dl = _downloader()
    st = br_fetch.ensure_br(
        {},
        which=lambda n: "/usr/local/bin/br",
        downloader=dl,
        platform="linux_amd64",
        dest=tmp_path / "br",
        background=False,
    )
    assert st["state"] == "done" and dl.calls == []
    assert store_mod.BR == "br"  # untouched


def test_ensure_br_explicit_br_bin_wins_even_over_a_fetched_binary(tmp_path, monkeypatch):
    fetched = tmp_path / "bin" / "br"
    fetched.parent.mkdir()
    fetched.write_bytes(FAKE_BR)
    fetched.chmod(0o755)
    monkeypatch.setenv(br_fetch.ENV_BR_BIN, "/opt/custom/br")
    assert br_fetch.resolve_br_bin(fetched=fetched) == "/opt/custom/br"
    dl = _downloader()
    # BR_BIN points at nothing resolvable → NO fetch at all (the operator's explicit
    # choice is theirs to fix; a download resolve_br_bin would refuse to use is 10 MB
    # of nothing) — the gap hint names the override
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(br_fetch.BR_SHA256, "linux_amd64", ARCHIVE_SHA)
        st = br_fetch.ensure_br(
            {}, which=lambda n: None, downloader=dl, platform="linux_amd64", dest=fetched, background=False
        )
    assert dl.calls == []
    assert st["state"] == "disabled" and st["error"] == "BR_BIN=/opt/custom/br is set — not fetching"
    hint = br_fetch.hint_for(st, br_bin="/opt/custom/br")
    assert hint.startswith("beads CLI '/opt/custom/br' not found on PATH — BR_BIN=/opt/custom/br is set — not fetching")
    assert "cargo install beads_rust" in hint
    # and _activate never overrides an explicit BR_BIN
    br_fetch._activate(fetched)
    assert store_mod.BR != str(fetched)


def test_ensure_br_unsupported_platform_says_so(tmp_path):
    dl = _downloader()
    st = br_fetch.ensure_br(
        {}, which=_no_br, downloader=dl, platform="windows_amd64", dest=tmp_path / "br", background=False
    )
    assert st["state"] == "unsupported" and dl.calls == []
    hint = br_fetch.hint_for({"state": "unsupported", "error": ""})
    assert "no build for this platform" in hint


def test_windows_gets_the_explicit_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(br_fetch._platform, "system", lambda: "Windows")
    monkeypatch.setattr(br_fetch._platform, "machine", lambda: "AMD64")
    st = br_fetch.ensure_br({}, which=_no_br, downloader=_downloader(), dest=tmp_path / "br", background=False)
    assert st["state"] == "unsupported" and "Windows" in st["error"]
    assert br_fetch.hint_for(st) == br_fetch.WINDOWS_HINT


def test_ensure_br_runs_in_a_daemon_thread_by_default(tmp_path):
    import threading

    seen = []

    def dl(url, timeout=0.0):
        seen.append(threading.current_thread())
        return ARCHIVE

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(br_fetch.BR_SHA256, "linux_amd64", ARCHIVE_SHA)
        st = br_fetch.ensure_br({}, which=_no_br, downloader=dl, platform="linux_amd64", dest=tmp_path / "bin" / "br")
        assert st["state"] in ("fetching", "done")
        for t in threading.enumerate():
            if t.name == "project-board-br-fetch":
                t.join(5)
    assert seen and seen[0] is not threading.main_thread() and seen[0].daemon
    assert br_fetch.fetch_state()["state"] == "done"


def test_fetch_state_survives_a_module_reload():
    """The state lives in a process-stable sys.modules slot (the #178 pattern): a
    plugin reload re-imports br_fetch but must neither re-fetch nor forget a
    failure/success."""
    import importlib

    br_fetch._set(state="done", path="/x/br")
    fresh = importlib.reload(br_fetch)
    assert fresh.fetch_state()["state"] == "done" and fresh.fetch_state()["path"] == "/x/br"


# ── where it surfaces: setup_check, register(), the loop, the manifest ──────────


def test_setup_status_br_hint_follows_the_fetch_state(tmp_path):
    which = lambda n: None  # noqa: E731
    common = dict(which=which, delegates=lambda n: object(), run=lambda *a, **k: None)
    br_fetch._set(state="fetching", spec=_spec(platform="linux_amd64"), started=__import__("time").time())
    s = setup_check.setup_status({"coder": "p", "repo": str(tmp_path)}, **common)
    assert s["br"]["ok"] is False and "fetching beads-rust v9.9.9 for linux_amd64" in s["br"]["hint"]
    assert s["br"]["fetch"]["state"] == "fetching" and s["br"]["source"] == "path"
    br_fetch._set(state="failed", error="ValueError: sha256 mismatch for …")
    s = setup_check.setup_status({"coder": "p", "repo": str(tmp_path)}, **common)
    assert "br auto-fetch failed: ValueError: sha256 mismatch" in s["br"]["hint"]
    br_fetch._set(state="disabled", error="")
    s = setup_check.setup_status({"coder": "p", "repo": str(tmp_path)}, **common)
    assert "br_autofetch is off" in s["br"]["hint"]
    br_fetch.reset_state()  # idle → the plain install hint — hint_for's idle copy IS BR_HINT
    s = setup_check.setup_status({"coder": "p", "repo": str(tmp_path)}, **common)
    assert s["br"]["hint"] == setup_check.BR_HINT == br_fetch.hint_for({"state": "idle"})
    assert "beads CLI 'br' not found on PATH — install beads-rust (cargo install beads_rust)" in setup_check.BR_HINT
    assert setup_check.BR_HINT.endswith("the board is paused until then")
    # a custom BR_BIN name rides through the same renderer
    store_mod.BR = "/opt/custom/br"
    s = setup_check.setup_status({"coder": "p", "repo": str(tmp_path)}, **common)
    assert s["br"]["hint"].startswith("beads CLI '/opt/custom/br' not found on PATH")


def test_setup_status_reports_a_fetched_binary_as_its_source(tmp_path):
    fetched = tmp_path / "bin" / "br"
    fetched.parent.mkdir()
    fetched.write_bytes(FAKE_BR)
    fetched.chmod(0o755)
    store_mod.BR = str(fetched)
    br_fetch._set(state="done", path=str(fetched))
    s = setup_check.setup_status(
        {"coder": "p", "repo": str(tmp_path)},
        which=lambda n: n if os.path.isabs(n) else None,
        delegates=lambda n: object(),
        run=lambda *a, **k: None,
    )
    assert s["br"]["ok"] is True and s["br"]["path"] == str(fetched) and s["br"]["source"] == "fetched"
    assert s["br"]["fetch"]["path"] == str(fetched)


class _Registry:
    def __init__(self, config):
        self.config = config
        self.host = None
        self.routers, self.surfaces = [], []

    def register_tool(self, t):
        pass

    def register_router(self, router, prefix):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None, reload=None):
        self.surfaces.append(name)

    def register_subagent(self, config):
        pass

    def register_skill_dir(self, path):
        pass


def test_register_arms_the_fetch_when_br_is_missing(monkeypatch, tmp_path, caplog):
    """register() kicks ensure_br — a fresh member with no br starts the fetch in the
    background and boots on. Here the downloader is the conftest's no-network stub,
    so the state lands as `failed` with the stub's error — proving the wiring AND that
    a failure is a gap, not a boot failure."""
    import threading

    monkeypatch.setattr(setup_check.shutil, "which", lambda n: None if os.path.basename(n) == "br" else "/usr/bin/x")
    monkeypatch.setattr(setup_check, "_default_delegates", lambda: lambda n: object())
    reg = _Registry({"coder": "p", "repo": str(tmp_path)})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        pb.register(reg)
    for t in threading.enumerate():
        if t.name == "project-board-br-fetch":
            t.join(5)
    st = br_fetch.fetch_state()
    assert st["state"] == "failed" and "inject a fake downloader" in st["error"]
    assert any("fetching beads-rust v" in r.message for r in caplog.records)
    assert "project-board-loop" in reg.surfaces  # registration unaffected


def test_register_does_not_fetch_when_disabled_or_present(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_check.shutil, "which", lambda n: None if os.path.basename(n) == "br" else "/usr/bin/x")
    monkeypatch.setattr(setup_check, "_default_delegates", lambda: lambda n: object())
    pb.register(_Registry({"coder": "p", "repo": str(tmp_path), "br_autofetch": False}))
    assert br_fetch.fetch_state()["state"] == "disabled"
    br_fetch.reset_state()
    monkeypatch.setattr(setup_check.shutil, "which", lambda n: "/usr/local/bin/" + os.path.basename(n))
    pb.register(_Registry({"coder": "p", "repo": str(tmp_path)}))
    assert br_fetch.fetch_state()["state"] == "done" and br_fetch.fetch_state()["path"] == ""  # PATH, no fetch


def test_loop_gate_rearms_the_fetch_and_br_autofetch_is_live(tmp_path, monkeypatch):
    loop = BoardLoop({"coder": "p", "repo": str(tmp_path), "br_autofetch": False})
    assert loop.br_autofetch is False
    assert loop.reload({"br_autofetch": True}) == {"br_autofetch": (False, True)}
    assert loop.cfg["br_autofetch"] is True
    armed = []
    monkeypatch.setattr(
        br_fetch, "ensure_br", lambda cfg, **kw: armed.append(cfg.get("br_autofetch")) or {"state": "fetching"}
    )
    loop._ensure_br()
    assert armed == [True]


def test_manifest_declares_br_autofetch_on_by_default_and_live():
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert m["config"]["br_autofetch"] is True
    assert m["version"] == "0.43.0"
    field = next(s for s in m["settings"] if s["key"] == "br_autofetch")
    assert field["type"] == "bool" and "github.com" in field["description"]

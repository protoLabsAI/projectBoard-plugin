"""First-use `br init` never adopts an ancestor `.beads` (the `~/.beads` refusal), and an
OLDER `br` on PATH never creates a fresh board store in place of the pinned release.

The demo-day failure: an instance under ``$HOME`` bootstrapped its store with a bare
``br init`` in ``<instance>/project_board``. br's workspace discovery walked UP to the
user's own ``~/.beads/beads.db`` (made by another br version), refused ("ordinary commands
never migrate an existing tracker database"), and every board read failed after. The init
now names its ``--db`` explicitly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from project_board import br_fetch
from project_board import store as store_mod
from project_board.store import BeadsBoard


@pytest.fixture(autouse=True)
def _restore_store_br():
    before = store_mod.BR
    yield
    store_mod.BR = before


def _board(monkeypatch, db: str) -> BeadsBoard:
    monkeypatch.setattr(store_mod, "default_db_path", lambda: db)
    return BeadsBoard(db=db, repo=os.path.dirname(os.path.dirname(os.path.dirname(db))))


# ── unit: the init command itself ─────────────────────────────────────────────────


def test_default_store_init_names_its_db_explicitly(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    db = str(tmp_path / "inst" / "project_board" / ".beads" / "beads.db")
    board = _board(monkeypatch, db)
    calls: list[tuple[list[str], str]] = []

    def fake_run(cmd, *, cwd, args):
        calls.append((list(cmd), cwd))
        os.makedirs(os.path.dirname(db), exist_ok=True)
        open(db, "w").close()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(store_mod, "_run_br_process", fake_run)
    board._ensure_workspace()

    (cmd, cwd), *rest = calls
    assert not rest
    assert cmd[1] == "init"
    # The explicit --db is the fix: without it br discovers an ancestor .beads (~/.beads).
    assert cmd[cmd.index("--db") + 1] == db
    assert "--prefix" in cmd and cmd[cmd.index("--prefix") + 1] == "bd"
    # …and the cwd stays the store root, so the `.beads/` that `init --db` also drops in
    # the cwd is the store's own, never the project repo.
    assert cwd == str(tmp_path / "inst" / "project_board")


# ── integration: a real br under a "home" that already has a .beads ────────────────

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH",
)


@pytest.mark.br_shape
@requires_br
def test_first_use_init_under_an_ancestor_beads_workspace_creates_its_own_store(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    br = shutil.which(store_mod.BR)
    env = {**os.environ, "HOME": str(home)}
    # The user's own beads workspace at ~/.beads — what a bare `br init` would discover.
    subprocess.run([br, "init", "--prefix", "zz", "--actor", "me"], cwd=home, env=env, check=True, capture_output=True)
    ancestor = sorted(p.name for p in (home / ".beads").iterdir())
    ancestor_db = next((home / ".beads").glob("*.db"))
    before = ancestor_db.read_bytes()
    monkeypatch.setenv("HOME", str(home))

    db = str(home / "inst" / "project_board" / ".beads" / "beads.db")
    board = _board(monkeypatch, db)
    created = board.create_feature("first card", spec="s", acceptance_criteria="a")
    fid = created["id"] if isinstance(created, dict) else created

    assert os.path.isfile(db), "the board store was not created at its own --db"
    assert [f["id"] for f in board.list_features()] == [fid]
    assert fid.startswith("bd-")
    # The ancestor workspace is untouched: same files, same db bytes.
    assert sorted(p.name for p in (home / ".beads").iterdir()) == ancestor
    assert ancestor_db.read_bytes() == before


# ── an older br on PATH must not create a FRESH store ──────────────────────────────


def _downloader(calls):
    def dl(url, timeout=0.0):
        calls.append(url)
        raise OSError("offline")

    return dl


def test_older_path_br_yields_to_the_pinned_fetch_on_a_fresh_store(tmp_path, monkeypatch):
    calls: list[str] = []
    dest = tmp_path / "bin" / br_fetch.BR_VERSION / "br"
    seen = {}

    def fake_fetch(spec, d, *, downloader, timeout):
        seen["store_br_during_fetch"] = store_mod.BR
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("#!/bin/sh\necho br " + spec.version + "\n")
        d.chmod(0o755)
        return d

    monkeypatch.setattr(br_fetch, "fetch_br", fake_fetch)
    st = br_fetch.ensure_br(
        {},
        which=lambda n: "/home/me/.cargo/bin/br" if n == "br" else (str(dest) if dest.exists() else None),
        downloader=_downloader(calls),
        platform="darwin_arm64",
        dest=dest,
        background=False,
        version_of=lambda _p: "br 0.2.16",
        store_db=str(tmp_path / "nope" / "beads.db"),
    )
    assert st["state"] == "done" and st["path"] == str(dest)
    # While the pin was landing the store pointed at the (not yet present) pinned path,
    # never at the old PATH br, so the old br had no window to create the store.
    assert seen["store_br_during_fetch"] == str(dest)
    assert store_mod.BR == str(dest)


@pytest.mark.parametrize(
    "version,store_exists,cfg",
    [
        ("br 0.3.2", False, {}),  # the pin itself (or newer) on PATH is fine
        ("br 0.4.0", False, {}),
        ("", False, {}),  # unknown version: never a verdict
        ("br 0.2.16", True, {}),  # an existing store was made by that br — the pin would refuse it
        ("br 0.2.16", False, {"br_autofetch": False}),  # the operator turned the fetch off
    ],
)
def test_path_br_is_kept_when_yielding_would_be_wrong(tmp_path, version, store_exists, cfg):
    calls: list[str] = []
    db = tmp_path / "beads.db"
    if store_exists:
        db.write_text("")
    st = br_fetch.ensure_br(
        cfg,
        which=lambda n: "/usr/local/bin/br" if n == "br" else None,
        downloader=_downloader(calls),
        platform="darwin_arm64",
        dest=tmp_path / "bin" / "br",
        background=False,
        version_of=lambda _p: version,
        store_db=str(db),
    )
    assert calls == [] and st["state"] in ("done", "disabled")
    assert store_mod.BR == "br"


def test_a_failed_pinned_fetch_falls_back_to_the_older_path_br(tmp_path):
    calls: list[str] = []
    st = br_fetch.ensure_br(
        {},
        which=lambda n: "/home/me/.cargo/bin/br" if n == "br" else None,
        downloader=_downloader(calls),
        platform="darwin_arm64",
        dest=tmp_path / "bin" / "br",
        background=False,
        version_of=lambda _p: "br 0.2.16",
        store_db=str(tmp_path / "absent.db"),
    )
    assert calls, "the pinned release was never attempted"
    # Better an older br than no board: the store is handed back to PATH, and the state
    # says why so the operator can see it.
    assert store_mod.BR == "br"
    assert st["state"] == "done" and st["path"] == "/home/me/.cargo/bin/br"
    assert "0.3.2" in st["error"] or br_fetch.BR_VERSION in st["error"]


def test_parse_version():
    assert br_fetch.parse_version("br 0.2.16") == (0, 2, 16)
    assert br_fetch.parse_version("br 0.3.2\n") == (0, 3, 2)
    assert br_fetch.parse_version("nothing") == ()

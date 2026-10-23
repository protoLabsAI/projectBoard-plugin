"""GET /features/{fid}/changes — the drawer's Code tab diff, against real git worktrees.

A temp repo with a ``main`` that keeps moving after the card branched, and the card's
worktree under ``.worktrees/feat-<id>-<slug>`` holding committed, uncommitted and untracked
work. The route must diff against the merge-base (so main's later commits don't show as the
card's), include all three kinds of change, cap bodies, and never return a secret's content.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, changes, worktree

FID = "bd-ch1"


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _commit(cwd, msg):
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("def a():\n    return 1\n")
    (r / "old_name.py").write_text("x = 1\n" * 20)
    (r / "gone.py").write_text("bye\n")
    (r / ".gitignore").write_text(".worktrees/\nbuild/\n")
    _commit(r, "base")
    _git(r, "worktree", "add", "-q", "-b", f"feat/{FID}-thing", str(r / ".worktrees" / f"feat-{FID}-thing"))
    # main moves on after the card branched — none of this is the card's change
    (r / "main_only.py").write_text("later\n")
    _commit(r, "main moves")
    return r


@pytest.fixture
def wt(repo):
    w = repo / ".worktrees" / f"feat-{FID}-thing"
    (w / "app.py").write_text("def a():\n    return 2\n\n\ndef b():\n    return 3\n")
    _git(w, "mv", "old_name.py", "new_name.py")
    _git(w, "rm", "-q", "gone.py")
    _commit(w, "card work")
    (w / "app.py").write_text("def a():\n    return 2\n\n\ndef b():\n    return 4\n")  # uncommitted edit
    (w / "notes.md").write_text("# untracked\nline two\n")  # untracked
    (w / ".env").write_text("API_KEY=sk-live-not-for-you\n")  # untracked secret
    (w / "build").mkdir()
    (w / "build" / "out.bin").write_text("ignored\n")  # ignored → not listed
    return w


class _Store:
    def __init__(self, features):
        self.features = features

    def get_feature(self, fid):
        return self.features.get(fid)


def _client(monkeypatch, repo, features):
    monkeypatch.setattr(api, "get_store", lambda **_kw: _Store(features))
    app = FastAPI()
    app.include_router(api.build_data_router({"repo": str(repo)}), prefix="/api/plugins/project_board")
    return TestClient(app)


def _get(client, fid=FID):
    return client.get(f"/api/plugins/project_board/features/{fid}/changes")


def _files(body):
    (tree,) = body["worktrees"]
    return tree, {f["path"]: f for f in tree["files"]}


def test_lists_committed_uncommitted_and_untracked_work_against_the_merge_base(monkeypatch, repo, wt):
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    r = _get(c)
    assert r.status_code == 200
    tree, files = _files(r.json())
    assert tree["branch"] == f"feat/{FID}-thing"
    assert tree["base"] == "main"
    assert "main_only.py" not in files  # main's later commit is not the card's change
    assert files["app.py"]["status"] == "M"
    assert "+    return 4" in files["app.py"]["diff"]  # the uncommitted edit is included
    assert files["app.py"]["diff"].startswith("@@")  # hunks only, no git header lines
    assert files["new_name.py"]["status"] == "R" and files["new_name.py"]["old_path"] == "old_name.py"
    assert files["gone.py"]["status"] == "D"
    assert files["notes.md"]["status"] == "A" and files["notes.md"]["untracked"] is True
    assert files["notes.md"]["additions"] == 2 and "+line two" in files["notes.md"]["diff"]
    assert "build/out.bin" not in files  # gitignored stays out
    assert tree["additions"] >= 6 and tree["file_count"] == len(files)


def test_a_secret_path_is_listed_but_its_content_never_returned(monkeypatch, repo, wt):
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    r = _get(c)
    _tree, files = _files(r.json())
    env = files[".env"]
    assert env["redacted"] is True and env["diff"] == "" and env["redacted_reason"]
    assert "sk-live-not-for-you" not in r.text


def test_unknown_feature_is_404_and_a_card_without_a_worktree_is_empty(monkeypatch, repo):
    c = _client(monkeypatch, repo, {"bd-none": {"id": "bd-none", "base_branch": "main"}})
    assert _get(c, "bd-nope").status_code == 404
    assert _get(c, "bd-none").json() == {"worktrees": []}


def test_bodies_are_capped_per_file_and_marked_truncated(monkeypatch, repo, wt):
    monkeypatch.setattr(changes, "MAX_FILE_DIFF_BYTES", 200)
    (wt / "big.txt").write_text("".join(f"line {i}\n" for i in range(50)))
    _commit(wt, "big")
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    _tree, files = _files(_get(c).json())
    big = files["big.txt"]
    assert big["truncated"] is True
    assert len(big["diff"].encode()) <= 200 and big["diff"].endswith("\n")
    assert big["additions"] == 50  # the counts stay whole


def test_the_file_list_is_capped(monkeypatch, repo, wt):
    monkeypatch.setattr(changes, "MAX_FILES", 2)
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    tree, files = _files(_get(c).json())
    assert len(files) == 2 and tree["files_truncated"] is True and tree["file_count"] > 2


def test_the_total_budget_stops_further_bodies(monkeypatch, repo, wt):
    monkeypatch.setattr(changes, "MAX_TOTAL_DIFF_BYTES", 1)
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    _tree, files = _files(_get(c).json())
    bodies = [f for f in files.values() if f["diff"]]
    assert len(bodies) <= 1
    assert any(f["truncated"] for f in files.values())


def test_a_binary_file_has_no_body(monkeypatch, repo, wt):
    (wt / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    _commit(wt, "logo")
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    _tree, files = _files(_get(c).json())
    assert files["logo.png"]["binary"] is True and files["logo.png"]["diff"] == ""


@pytest.mark.parametrize(
    "path,secret",
    [
        (".env", True),
        ("config/.env.production", True),
        (".env.example", False),
        ("deploy/server.pem", True),
        ("home/.ssh/config", True),
        ("my_secret_notes.txt", True),
        ("aws_credentials.json", True),
        ("src/app.py", False),
        ("README.md", False),
    ],
)
def test_the_local_secret_rule(path, secret):
    assert bool(changes._local_secret_reason(path)) is secret


def test_truncated_z_output_drops_the_partial_row_instead_of_raising():
    assert changes._parse_z_pairs("1\t2\t\0old.py", numstat=True) == []
    assert changes._parse_z_pairs("R100\0old.py", numstat=False) == []
    assert changes._parse_z_pairs("M\0a.py\0R100\0b.py", numstat=False) == [["M", "a.py"]]


def test_a_git_timeout_is_reported_on_the_worktree_not_a_500(monkeypatch, repo, wt):
    async def _timeout(*_a, **_kw):
        raise worktree.WorktreeError("git diff timed out after 30s")

    monkeypatch.setattr(changes.worktree, "_git", _timeout)
    c = _client(monkeypatch, repo, {FID: {"id": FID, "base_branch": "main"}})
    r = _get(c)
    assert r.status_code == 200
    (tree,) = r.json()["worktrees"]
    assert "timed out" in tree["error"]

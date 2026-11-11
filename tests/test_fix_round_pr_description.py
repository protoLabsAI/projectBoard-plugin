"""Feature context survives gate bounces, publication, and fresh loop instances."""

import importlib
import json
import subprocess

import pytest

from project_board import pr_description, worktree
from project_board.loop import BoardLoop
from test_loop import _drive_with


async def test_initial_gate_environment_repair_keeps_feature(monkeypatch):
    replies = iter(
        [
            "## Summary\n\nAdd compact picker and progressive panes.",
            "## Summary\n\nRepaired lint-imports launcher; all checks pass.",
        ]
    )
    gates = iter(["stale interpreter", None])
    bodies = []

    async def dispatch(*a, **kw):
        return next(replies)

    async def gate(*a, **kw):
        return next(gates)

    async def publish(*a, **kw):
        bodies.append(kw["body"])
        return "https://github.com/acme/widgets/pull/1"

    await _drive_with(
        monkeypatch,
        dispatch=dispatch,
        gate=gate,
        open_pr=publish,
        cfg={"coder": "proto", "local_gate_cmd": "check", "local_gate_max": 2},
    )
    assert len(bodies) == 1
    assert bodies[0].startswith("## Summary\n\nAdd compact picker")
    assert "## Fix round 1" in bodies[0]
    assert "Repaired lint-imports" in bodies[0]


@pytest.mark.parametrize("repair", ["CI", "review", "conflict"])
async def test_restart_publication_preserves_operator_and_issue(tmp_path, monkeypatch, repair):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    wt = str(tmp_path)
    fid = "bd-feature"
    await pr_description.record(wt, fid, "## Summary\n\nCompact picker.")
    await pr_description.record(wt, fid, f"{repair}: environment-only repair", repair=True)
    # Reload removes process state; the common git directory is the source of truth.
    importlib.reload(pr_description)
    existing = "## Summary\n\nCompact picker.\n\nFixes #97\n\n## Operator\n\nKeep this rollout note."
    calls = []

    async def gh(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("pr", "list"):
            return 0, json.dumps([{"body": existing}]), ""
        if args[:2] == ("pr", "create"):
            return 1, "", "already exists"
        if args[:2] == ("pr", "view"):
            return 0, "https://github.com/acme/widgets/pull/1", ""
        return 0, "", ""

    monkeypatch.setattr(worktree, "_gh", gh)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_store", lambda: type("Store", (), {"list_features": lambda *a, **k: []})())

    async def slug(**kw):
        return "acme/widgets"

    monkeypatch.setattr(worktree, "repo_slug", slug)
    body = await pr_description.render(wt, "feat/x", fid, "fallback")
    body, _ = await loop._with_source_issue_ref({"id": fid, "source_issue": "#97"}, wt, body)

    async def git(path, *args, **kw):
        return 0, "1" if args[0] == "rev-list" else "", ""

    # Exercise actual existing-PR publication, including the body write.
    async def no_recovery(*a):
        return {}

    monkeypatch.setattr(worktree, "_git", git)
    monkeypatch.setattr(worktree, "recovery_context", no_recovery)
    await worktree.open_pr(wt, "feat/x", title="Feature", body=body, promote_draft=False)
    posted = next(c[-1] for c in calls if c[:2] == ("pr", "edit"))
    assert existing in posted
    assert f"{repair}: environment-only repair" in posted
    assert posted.count("Fixes #97") == 1
    # Publishing the same journal twice does not duplicate repair sections.
    existing = posted

    async def context(*a):
        return {"attempts": [{"number": 1, "summary": f"{repair}: environment-only repair"}]}

    monkeypatch.setattr(pr_description, "read", context)
    assert await pr_description.render(wt, "feat/x", fid, "fallback") == posted


async def test_failed_body_read_does_not_publish(tmp_path, monkeypatch):
    importlib.reload(pr_description)

    async def gh(*a, **k):
        return 1, "", "network unavailable"

    monkeypatch.setattr(worktree, "_gh", gh)
    with pytest.raises(worktree.WorktreeError, match="cannot read existing"):
        await pr_description.existing_body(str(tmp_path), "feat/x")


def test_moved_venv_launchers_and_activation_are_relocated(tmp_path):
    old = tmp_path / "candidate"
    new = tmp_path / "canonical"
    bindir = new / ".venv" / "bin"
    bindir.mkdir(parents=True)
    launcher = bindir / "lint-imports"
    launcher.write_text(f"#!{old}/.venv/bin/python\nprint('ok')\n")
    launcher.chmod(0o755)
    activation = bindir / "activate"
    activation.write_text(f'VIRTUAL_ENV="{old}/.venv"\n')
    external = tmp_path / "shared"
    external.write_text(str(old / ".venv"))
    (bindir / "shared").symlink_to(external)
    worktree._relocate_venv(str(new), str(old))
    assert launcher.read_text().startswith(f"#!{new}/.venv/bin/python")
    assert launcher.stat().st_mode & 0o111
    assert str(new / ".venv") in activation.read_text()
    assert external.read_text() == str(old / ".venv")


async def test_salvage_after_restart_uses_persisted_feature(tmp_path, monkeypatch):
    wt = str(tmp_path)
    await pr_description.record(wt, "bd-1", "## Summary\n\nFeature implementation.")
    await pr_description.record(wt, "bd-1", "Fixed environment.", repair=True)
    loop = BoardLoop({"coder": "proto"})
    bodies = []

    async def publish(*a, **kw):
        bodies.append(kw["body"])
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "open_pr", publish)
    await loop._open_tree_pr({"id": "bd-1", "title": "Feature"}, wt, "feat/bd-1", note="Recovered after restart.")
    assert bodies[0].startswith("## Summary\n\nFeature implementation.")
    assert "## Fix round 1" in bodies[0]
    assert "Recovered after restart." in bodies[0]


async def test_existing_body_is_restamped_for_open_sibling(tmp_path, monkeypatch):
    wt = str(tmp_path)
    await pr_description.record(wt, "bd-1", "environment repair", repair=True, fallback="Feature")

    async def existing(*a):
        return "## Summary\n\nFeature.\n\nFixes #97\n\n## Operator\nKeep me."

    monkeypatch.setattr(pr_description, "existing_body", existing)
    loop = BoardLoop({"coder": "proto"})
    sibling = {"id": "bd-2", "source_issue": "#97", "board_state": "ready"}
    monkeypatch.setattr(loop, "_store", lambda: type("Store", (), {"list_features": lambda *a, **k: [sibling]})())

    async def slug(**kw):
        return "acme/widgets"

    monkeypatch.setattr(worktree, "repo_slug", slug)
    body = await pr_description.render(wt, "feat/bd-1", "bd-1", "fallback")
    body, refs_only = await loop._with_source_issue_ref({"id": "bd-1", "source_issue": "#97"}, wt, body)
    assert refs_only
    assert "Fixes #97" not in body
    assert "Refs #97" in body
    assert "## Operator\nKeep me." in body


async def test_corrupt_journal_is_not_silently_replaced(tmp_path):
    wt = str(tmp_path)
    await pr_description.record(wt, "bd-1", "Feature")
    filename = await pr_description._path(wt, "bd-1")
    with open(filename, "w") as stream:
        stream.write("broken journal")
    with pytest.raises(json.JSONDecodeError):
        await pr_description.record(wt, "bd-1", "repair")
    with open(filename) as stream:
        assert stream.read() == "broken journal"


async def test_promotion_preserves_journal_and_executable_venv(tmp_path):
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base")
    candidate = repo / ".worktrees" / "feat-bd-1.c1"
    git("worktree", "add", "-b", "feat/bd-1.c1", str(candidate))
    bindir = candidate / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(sys.executable)
    launcher = bindir / "lint-imports"
    launcher.write_text(f"#!{bindir}/python\nprint('contracts passed')\n")
    launcher.chmod(0o755)
    await pr_description.record(str(candidate), "bd-1", "## Summary\nFeature.")
    promoted, _ = await worktree.promote_worktree(str(repo), str(candidate), "feat/bd-1.c1", "bd-1")
    result = subprocess.run([promoted + "/.venv/bin/lint-imports"], check=True, capture_output=True, text=True)
    assert result.stdout == "contracts passed\n"
    assert (await pr_description.read(promoted, "bd-1"))["feature"] == "## Summary\nFeature."
    # Even removing the disposable tree leaves the feature context in the repository.
    git("worktree", "remove", "--force", promoted)
    assert (await pr_description.read(str(repo), "bd-1"))["feature"] == "## Summary\nFeature."


async def test_existing_pr_body_write_failure_is_reported(monkeypatch):
    from test_worktree import FakeGit, FakeGh

    monkeypatch.setattr(worktree, "_git", FakeGit({"rev-list": (0, "1", "")}))
    gh = FakeGh(
        {
            "create": (1, "", "already exists"),
            "view": (0, "https://example/pr/1", ""),
            "edit": (1, "", "permission denied"),
        }
    )
    monkeypatch.setattr(worktree, "_gh", gh)
    with pytest.raises(worktree.WorktreeError, match="could not refresh PR description"):
        await worktree.open_pr("/wt", "feat/bd-1", title="Feature", body="Preserved feature + repair")


@pytest.mark.parametrize("feedback", ["CI failed", "Review findings", "Merge conflict repair"])
async def test_fresh_drive_appends_repair_to_existing_feature(monkeypatch, feedback):
    from test_loop import FEATURE
    import project_board.loop as loop_mod

    remote = {"body": None}

    async def current_body(*a):
        return remote["body"]

    async def publish(*a, **kw):
        remote["body"] = kw["body"]
        return "https://example/pr/1"

    async def implementation(*a, **kw):
        return "## Summary\n\nCompact picker and progressive panes."

    async def repair(*a, **kw):
        return "## Summary\n\nFixed local interpreter; product unchanged."

    async def issue_open(*a):
        return True

    async def slug(**kw):
        return "acme/widgets"

    monkeypatch.setattr(pr_description, "existing_body", current_body)
    monkeypatch.setattr(loop_mod, "_source_issue_still_open", issue_open)
    monkeypatch.setattr(worktree, "repo_slug", slug)
    feature = {**FEATURE, "source_issue": "#97"}
    await _drive_with(monkeypatch, dispatch=implementation, open_pr=publish, feature=feature)
    assert "Fixes #97" in remote["body"]
    remote["body"] += "\n\n## Operator\nRoll out gradually."
    original = remote["body"]
    # _drive_with creates a fresh BoardLoop; only disk and GitHub carry context.
    _, store = await _drive_with(
        monkeypatch,
        dispatch=repair,
        open_pr=publish,
        feature={**feature, "pr_url": "https://example/pr/1"},
        seed=lambda loop: loop._ci_feedback.__setitem__(feature["id"], feedback),
    )
    assert ("open_review", feature["id"], "https://example/pr/1") in store.calls
    assert original in remote["body"]
    assert "## Fix round 1" in remote["body"]
    assert "Fixed local interpreter" in remote["body"]

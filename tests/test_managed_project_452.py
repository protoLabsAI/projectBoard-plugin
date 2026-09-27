"""Registering a board project also registers it as a READ-ONLY managed project (#452).

Before this, ``board_register_project`` / ``PUT /projects/{name}`` wrote only
``project_board.projects``. The host's ADR 0095 ``projects:`` registry — what the lead
agent's filesystem tools read through — never heard of the repo, so the agent could not read
the very checkout its coders branch from, and went to the GitHub API instead (the default
branch, not the clone).

The host write goes through ``HOST.apply_settings`` in the SAME patch as the board entry.
The host is a double here (``tests/_host_apply.py``) that holds the config as the YAML
document and applies the host's own merge rules; ``tests/test_host_apply_conformance.py``
checks those rules against the real host function when protoAgent is importable. The
``github`` field comes from the checkout's real ``origin`` remote, read through real git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _host_apply import HostConfig, wire_host

from project_board import worktree
from project_board.projects import parse_github_remote
from project_board.project_registry import build_register_tool, delete_project, upsert_project


def _checkout(path: Path, remote: str | None = "https://github.com/protoLabsAI/design-system-plugin.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def _host(tmp_path: Path, **doc) -> HostConfig:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    base = {"onboarding": {"enabled": True, "root": str(root)}, "project_board": {"projects": {}}}
    base.update(doc)
    return HostConfig(base)


def _registry(host: HostConfig) -> list[dict]:
    return list(host.doc.get("projects") or [])


# ── the origin remote → `github` (real git) ────────────────────────────────────────
@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/protoLabsAI/protoContent.git", "protoLabsAI/protoContent"),
        ("https://github.com/protoLabsAI/protoContent", "protoLabsAI/protoContent"),
        ("https://x-access-token:abc@github.com/o/r.git", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
        ("https://gitlab.com/o/r.git", ""),
        ("/local/path/origin.git", ""),
        ("", ""),
    ],
)
def test_parse_github_remote(url, slug):
    assert parse_github_remote(url) == slug


async def test_origin_github_slug_reads_the_real_remote(tmp_path):
    repo = _checkout(tmp_path / "a", "git@github.com:protoLabsAI/protoContent.git")
    assert await worktree.origin_github_slug(str(repo)) == "protoLabsAI/protoContent"


async def test_origin_github_slug_is_empty_without_origin_or_off_github(tmp_path):
    assert await worktree.origin_github_slug(str(_checkout(tmp_path / "none", None))) == ""
    assert await worktree.origin_github_slug(str(_checkout(tmp_path / "gl", "https://gitlab.com/o/r.git"))) == ""
    assert await worktree.origin_github_slug(str(tmp_path / "not-a-repo")) == ""


# ── register → a read-only managed project ─────────────────────────────────────────
async def test_register_adds_a_read_only_managed_project_in_the_same_patch(monkeypatch, tmp_path):
    host = _host(tmp_path)
    repo = _checkout(tmp_path / "workspace" / "design-system-plugin")
    wire_host(monkeypatch, host)

    result = await upsert_project("design-system-plugin", str(repo), base_branch="main")

    assert result["managed_project"] == {"action": "added", "name": "design-system-plugin", "detail": ""}
    assert len(host.patches) == 1  # ONE apply: one reload, one outcome
    assert _registry(host) == [
        {
            "name": "design-system-plugin",
            "path": str(repo.resolve()),
            "default_branch": "main",
            "github": "protoLabsAI/design-system-plugin",
            "write": False,
        }
    ]
    # ownership is recorded on the BOARD entry, never on the host's
    board = host.doc["project_board"]["projects"]["design-system-plugin"]
    assert board["managed_project"] == "design-system-plugin"


async def test_default_branch_follows_the_base_branch(monkeypatch, tmp_path):
    host = _host(tmp_path)
    repo = _checkout(tmp_path / "workspace" / "legacy")
    wire_host(monkeypatch, host)
    await upsert_project("legacy", str(repo), base_branch="master")
    assert _registry(host)[0]["default_branch"] == "master"

    # a later base-branch change keeps the board-added entry in step
    result = await upsert_project("legacy", str(repo), base_branch="main")
    assert result["managed_project"]["action"] == "updated"
    assert _registry(host)[0]["default_branch"] == "main"


async def test_a_repo_already_managed_is_left_alone_and_never_removed(monkeypatch, tmp_path):
    """onboard_project registered it first (read-write, its own name). The board neither
    rewrites that entry nor claims it, so unregistering the board project leaves it."""
    repo = _checkout(tmp_path / "workspace" / "protoContent")
    onboarded = {"name": "protoContent", "path": str(repo), "github": "o/pc", "default_branch": "main", "write": True}
    host = _host(tmp_path, projects=[dict(onboarded)])
    wire_host(monkeypatch, host)

    result = await upsert_project("pc", str(repo))
    assert result["managed_project"]["action"] == "present"
    assert result["managed_project"]["name"] == "protoContent"
    assert "projects" not in host.patches[-1]  # nothing written to the registry
    assert "managed_project" not in host.doc["project_board"]["projects"]["pc"]

    deleted = await delete_project("pc")
    assert deleted["managed_project"]["action"] == "none"
    assert _registry(host) == [onboarded]


async def test_a_name_already_taken_by_another_path_is_skipped_not_clobbered(monkeypatch, tmp_path):
    repo = _checkout(tmp_path / "workspace" / "web")
    other = {"name": "web", "path": str(tmp_path / "elsewhere"), "write": True}
    host = _host(tmp_path, projects=[dict(other)])
    wire_host(monkeypatch, host)

    result = await upsert_project("web", str(repo))
    assert result["managed_project"]["action"] == "skipped"
    assert "already points at" in result["managed_project"]["detail"]
    assert _registry(host) == [other]


async def test_siblings_in_the_registry_survive(monkeypatch, tmp_path):
    sibling = {"name": "protoAgent", "path": "/somewhere/protoAgent", "write": False}
    host = _host(tmp_path, projects=[dict(sibling)])
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    await upsert_project("web", str(repo))
    assert [e["name"] for e in _registry(host)] == ["protoAgent", "web"]
    assert _registry(host)[0] == sibling


async def test_unregister_removes_only_what_the_board_added(monkeypatch, tmp_path):
    sibling = {"name": "protoAgent", "path": "/somewhere/protoAgent", "write": False}
    host = _host(tmp_path, projects=[dict(sibling)])
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    await upsert_project("web", str(repo))

    deleted = await delete_project("web")
    assert deleted["managed_project"] == {"action": "removed", "name": "web", "detail": ""}
    assert _registry(host) == [sibling]
    assert "web" not in host.doc["project_board"]["projects"]


async def test_an_entry_the_operator_repointed_is_handed_back_not_removed(monkeypatch, tmp_path):
    host = _host(tmp_path)
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    await upsert_project("web", str(repo))
    host.doc["projects"][0]["path"] = "/operator/moved/it"  # the operator took it over

    deleted = await delete_project("web")
    assert deleted["managed_project"]["action"] == "kept"
    assert _registry(host)[0]["path"] == "/operator/moved/it"


async def test_an_explicit_fence_override_gets_the_mirror_and_loses_it_on_unregister(monkeypatch, tmp_path):
    """With an explicit `filesystem.projects` override the fence ignores the registry
    (ADR 0095 D2), so the entry is mirrored there too — as onboard_project does — and the
    mirror goes with it."""
    fence = [{"name": "notes", "path": "/n", "write": True}]
    host = _host(tmp_path, filesystem={"projects": [dict(f) for f in fence], "enabled": True})
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)

    await upsert_project("web", str(repo))
    assert host.doc["filesystem"]["projects"][-1] == {
        "name": "web",
        "path": str(repo.resolve()),
        "write": False,
        "github": "protoLabsAI/design-system-plugin",
    }
    assert host.doc["filesystem"]["enabled"] is True

    await delete_project("web")
    assert host.doc["filesystem"]["projects"] == fence


async def test_the_filesystem_switch_is_never_flipped_but_the_reply_says_so(monkeypatch, tmp_path):
    host = _host(tmp_path, filesystem={"enabled": False})
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    tool = build_register_tool({})
    reply = await tool.ainvoke({"name": "web", "repo": str(repo), "repo_conventions": "x"})
    assert host.doc["filesystem"] == {"enabled": False}
    assert "read-only managed project 'web'" in reply
    assert "filesystem tools are switched off" in reply


async def test_a_host_without_the_registry_registers_the_board_half_only(monkeypatch, tmp_path):
    host = _host(tmp_path)
    host.registry = False
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    result = await upsert_project("web", str(repo))
    assert result["managed_project"]["action"] == "unsupported"
    assert set(host.patches[0]) == {"project_board"}
    assert "web" in host.doc["project_board"]["projects"]


async def test_the_register_tool_says_what_it_did(monkeypatch, tmp_path):
    host = _host(tmp_path)
    repo = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    reply = await build_register_tool({}).ainvoke({"name": "web", "repo": str(repo), "repo_conventions": "x"})
    assert "Registered board project 'web'" in reply
    assert "read-only managed project 'web'" in reply and "filesystem tools" in reply


async def test_a_readback_that_misses_the_managed_entry_is_not_reported_as_success(monkeypatch, tmp_path):
    from project_board.project_registry import ProjectRegistryError

    host = _host(tmp_path)
    repo = _checkout(tmp_path / "workspace" / "web")
    real_apply = host.apply_settings

    def drops_the_registry(patch):
        patch = {k: v for k, v in patch.items() if k != "projects"}
        return real_apply(patch)

    host.apply_settings = drops_the_registry
    wire_host(monkeypatch, host)
    with pytest.raises(ProjectRegistryError, match="managed project 'web' is not live"):
        await upsert_project("web", str(repo))


async def test_moving_the_board_project_moves_the_entry_it_owns(monkeypatch, tmp_path):
    host = _host(tmp_path)
    old = _checkout(tmp_path / "workspace" / "web-old")
    new = _checkout(tmp_path / "workspace" / "web-new", "git@github.com:o/web.git")
    wire_host(monkeypatch, host)
    await upsert_project("web", str(old))
    result = await upsert_project("web", str(new))
    assert result["managed_project"]["action"] == "updated"
    assert _registry(host) == [
        {"name": "web", "path": str(new.resolve()), "default_branch": "main", "github": "o/web", "write": False}
    ]


async def test_moving_onto_an_already_managed_checkout_drops_the_board_entry(monkeypatch, tmp_path):
    host = _host(tmp_path)
    old = _checkout(tmp_path / "workspace" / "web-old")
    new = _checkout(tmp_path / "workspace" / "web")
    wire_host(monkeypatch, host)
    await upsert_project("web", str(old))
    onboarded = {"name": "web-onboarded", "path": str(new), "write": True}
    host.doc["projects"].append(dict(onboarded))

    result = await upsert_project("web", str(new))
    assert result["managed_project"] == {"action": "present", "name": "web-onboarded", "detail": ""}
    assert _registry(host) == [onboarded]  # one entry per checkout, and it isn't the board's
    assert "managed_project" not in host.doc["project_board"]["projects"]["web"]
    assert (await delete_project("web"))["managed_project"]["action"] == "none"
    assert _registry(host) == [onboarded]

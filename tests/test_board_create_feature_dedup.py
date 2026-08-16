"""board_create_feature dedup — the same class of bug portfolio_dispatch's #25
dedup fixed one tier up, but here: an agent's OWN reasoning (onboarding, or its
own read of a dispatched task) calling this tool twice in one turn for the same
work, live-caught in dogfooding (two board_create_feature calls, ~9s apart, same
title, no force) with no guard to stop it."""

from __future__ import annotations

import json
import logging

import project_board as pb


def test_norm_title_collapses_whitespace_and_case():
    assert pb._norm_title("  Fix   the Thing  ") == "fix the thing"
    assert pb._norm_title("Fix the Thing") == pb._norm_title("fix   the    thing")


def test_open_duplicate_finds_same_title_open_feature():
    features = [{"id": "bd-1", "title": "Add X", "board_state": "backlog"}]
    dup = pb._open_duplicate(features, "add   x")
    assert dup is not None and dup["id"] == "bd-1"


def test_open_duplicate_ignores_terminal_states():
    features = [
        {"id": "bd-1", "title": "Add X", "board_state": "done"},
        {"id": "bd-2", "title": "Add X", "board_state": "cancelled"},
    ]
    assert pb._open_duplicate(features, "Add X") is None


def test_open_duplicate_none_when_no_match():
    features = [{"id": "bd-1", "title": "Add X", "board_state": "backlog"}]
    assert pb._open_duplicate(features, "Add Y") is None


class _FakeStore:
    """Records create_feature calls; list_features returns whatever's been created
    so far — enough to exercise board_create_feature's dedup decision without a
    real BeadsBoard/br CLI (store.py's own projection is tested in test_store.py)."""

    def __init__(self):
        self.created: list[dict] = []
        self._next = 1

    def list_features(self, state=None):
        return list(self.created)

    def create_feature(self, title, **kw):
        fid = f"bd-{self._next}"
        self._next += 1
        f = {"id": fid, "title": title, "board_state": "backlog", **kw}
        self.created.append(f)
        return f


def _get_tool(name: str, cfg: dict | None = None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


def test_board_create_feature_refuses_a_same_title_open_dup(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    first = json.loads(create.invoke({"title": "Rollup surfacing", "spec": "s1"}))
    assert first["id"] == "bd-1"

    second = create.invoke({"title": "Rollup surfacing", "spec": "s2 — reconsidered"})
    assert "Skipped" in second
    assert "bd-1" in second
    assert len(fake.created) == 1  # the dup was refused, not created


def test_board_create_feature_force_true_creates_a_second_copy(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "Rollup surfacing", "spec": "s1"})
    second = create.invoke({"title": "Rollup surfacing", "spec": "s2", "force": True})
    assert json.loads(second)["id"] == "bd-2"
    assert len(fake.created) == 2


def test_board_create_feature_allows_recreating_after_the_first_is_done(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "Rollup surfacing", "spec": "s1"})
    fake.created[0]["board_state"] = "done"  # the first shipped — this is legit new work

    second = create.invoke({"title": "Rollup surfacing", "spec": "s2"})
    assert json.loads(second)["id"] == "bd-2"


# ── PR-side dedup (#174): source_issue with an open PR already referencing it ──────
# The real incident: a card was created (and a coder dispatched) for an issue that
# already had an open, CI-passing PR. The check is mechanical (gh pr list + a scan of
# each PR's title/body for the issue number) and fail-open — a gh failure warns and
# never blocks creation, same posture as the title dedup's store-read failure.


def test_pr_references_issue_matches_hash_number():
    pr = {"title": "fix the crash", "body": "Fixes #12"}
    assert pb._pr_references_issue(pr, 12, "acme/widgets")


def test_pr_references_issue_matches_number_in_title():
    pr = {"title": "fix the crash (#12)", "body": ""}
    assert pb._pr_references_issue(pr, 12, "acme/widgets")


def test_pr_references_issue_never_partial_matches_a_longer_number():
    pr = {"title": "", "body": "Fixes #123"}
    assert not pb._pr_references_issue(pr, 12, "acme/widgets")


def test_pr_references_issue_matches_full_issue_url():
    pr = {"title": "", "body": "See https://github.com/acme/widgets/issues/12 for context"}
    assert pb._pr_references_issue(pr, 12, "acme/widgets")


def test_pr_references_issue_url_never_partial_matches():
    pr = {"title": "", "body": "See https://github.com/acme/widgets/issues/123"}
    assert not pb._pr_references_issue(pr, 12, "acme/widgets")


def test_pr_references_issue_false_when_unmentioned():
    pr = {"title": "unrelated work", "body": "no issue refs here"}
    assert not pb._pr_references_issue(pr, 12, "acme/widgets")


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_find_open_pr_for_issue_returns_the_matching_pr(monkeypatch):
    prs = [
        {"number": 7, "title": "unrelated", "body": "", "url": "https://github.com/acme/widgets/pull/7"},
        {"number": 41, "title": "fix crash", "body": "Fixes #12", "url": "https://github.com/acme/widgets/pull/41"},
    ]
    captured = {}

    def _run(cmd, **_kw):
        captured["cmd"] = cmd
        return _FakeProc(stdout=json.dumps(prs))

    monkeypatch.setattr(pb.subprocess, "run", _run)
    # A full issue URL normalizes to acme/widgets#12 — the search targets the issue's OWN repo.
    pr = pb._find_open_pr_for_issue("https://github.com/acme/widgets/issues/12")
    assert pr is not None and pr["number"] == 41
    assert "--repo" in captured["cmd"] and "acme/widgets" in captured["cmd"]
    assert "--state" in captured["cmd"] and "open" in captured["cmd"]


def test_find_open_pr_for_issue_none_when_no_pr_references_it(monkeypatch):
    prs = [{"number": 7, "title": "unrelated", "body": "Fixes #99", "url": "u7"}]
    monkeypatch.setattr(pb.subprocess, "run", lambda *_a, **_kw: _FakeProc(stdout=json.dumps(prs)))
    assert pb._find_open_pr_for_issue("acme/widgets#12") is None


def test_find_open_pr_for_issue_gh_error_exit_warns_and_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(pb.subprocess, "run", lambda *_a, **_kw: _FakeProc(returncode=1, stderr="rate limited"))
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        assert pb._find_open_pr_for_issue("acme/widgets#12") is None
    assert any("PR-dedup" in r.message for r in caplog.records)


def test_find_open_pr_for_issue_gh_exception_warns_and_returns_none(monkeypatch, caplog):
    def _boom(*_a, **_kw):
        raise OSError("gh not installed")

    monkeypatch.setattr(pb.subprocess, "run", _boom)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        assert pb._find_open_pr_for_issue("acme/widgets#12") is None
    assert any("PR-dedup" in r.message for r in caplog.records)


def test_find_open_pr_for_issue_unnormalizable_ref_skips_gh_entirely(monkeypatch):
    def _fail(*_a, **_kw):
        raise AssertionError("gh must not run for a ref with no owner/repo")

    monkeypatch.setattr(pb.subprocess, "run", _fail)
    assert pb._find_open_pr_for_issue("#12") is None  # create_feature rejects it with its own error


def test_board_create_feature_refuses_when_open_pr_references_source_issue(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    monkeypatch.setattr(
        "project_board._find_open_pr_for_issue",
        lambda _src: {"number": 41, "url": "https://github.com/acme/widgets/pull/41"},
    )
    create = _get_tool("board_create_feature")

    out = create.invoke({"title": "Fix the crash", "spec": "s", "source_issue": "acme/widgets#12"})
    assert "Skipped" in out
    assert "#41" in out and "https://github.com/acme/widgets/pull/41" in out
    assert "force=true" in out
    assert fake.created == []  # refused, not created


def test_board_create_feature_force_true_bypasses_pr_dedup(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    def _must_not_run(_src):
        raise AssertionError("force=true must skip the PR-dedup check entirely")

    monkeypatch.setattr("project_board._find_open_pr_for_issue", _must_not_run)
    create = _get_tool("board_create_feature")

    out = create.invoke({"title": "Fix the crash", "spec": "s", "source_issue": "acme/widgets#12", "force": True})
    assert json.loads(out)["id"] == "bd-1"
    assert len(fake.created) == 1


def test_board_create_feature_without_source_issue_never_queries_github(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    calls = []
    monkeypatch.setattr(
        "project_board._find_open_pr_for_issue",
        lambda src: calls.append(src) or {"number": 1, "url": "u"},
    )
    create = _get_tool("board_create_feature")

    out = create.invoke({"title": "No issue attached", "spec": "s"})
    assert json.loads(out)["id"] == "bd-1"
    assert calls == []  # no source_issue → no GitHub round-trip, no refusal


def test_board_create_feature_gh_failure_does_not_block_creation(monkeypatch, caplog):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)

    def _boom(*_a, **_kw):
        raise OSError("network down")

    monkeypatch.setattr(pb.subprocess, "run", _boom)  # the REAL check path, gh unreachable
    create = _get_tool("board_create_feature")

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        out = create.invoke({"title": "Fix the crash", "spec": "s", "source_issue": "acme/widgets#12"})
    assert json.loads(out)["id"] == "bd-1"  # created despite the gh failure
    assert any("PR-dedup" in r.message for r in caplog.records)

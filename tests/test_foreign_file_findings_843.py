"""A review finding on a file outside the PR's repository cannot block the card (#843)."""

import json
from types import SimpleNamespace

import pytest

from project_board import worktree
from project_board.loop._common import is_foreign_file_finding, partition_by_grounding, repo_file_checker
from tests.test_loop import _GateStore, _grounding_loop, _inject_fake_findings


PR = "https://github.com/o/r/pull/839"
EVIDENCE = "entries = history_store.search(match, limit=fetch_limit)"
DIFF = (
    "diff --git a/search.py b/search.py\n--- a/search.py\n+++ b/search.py\n@@ -0,0 +1 @@\n"
    f"+{EVIDENCE}\n"
    "diff --git a/added.py b/added.py\nnew file mode 100644\n--- /dev/null\n+++ b/added.py\n"
    "@@ -0,0 +1 @@\n+print('new')\n"
)


def _f(file, evidence="some untouched code line here", category=""):
    return SimpleNamespace(file=file, evidence=evidence, category=category, verdict="confirmed")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "search.py").write_text(EVIDENCE + "\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "untouched.py").write_text("x = 1\n")
    return tmp_path


# ── r1 / r2: partition_by_grounding with an existence check ──────────────────


def test_file_absent_from_diff_and_repo_is_ungrounded(repo):
    foreign = _f("graph/fleet/proxy.py")
    assert partition_by_grounding([foreign], DIFF, repo_file_checker(repo)) == ([], [foreign])
    assert is_foreign_file_finding(foreign, DIFF, repo_file_checker(repo))


def test_file_absent_from_diff_but_present_in_repo_stays_grounded(repo):
    local = _f("pkg/untouched.py")
    assert partition_by_grounding([local], DIFF, repo_file_checker(repo)) == ([local], [])
    assert not is_foreign_file_finding(local, DIFF, repo_file_checker(repo))


def test_file_the_pr_adds_is_never_foreign(repo):
    # In the diff, not on disk (the checkout predates the PR).
    added = _f("added.py", evidence="print('new')")
    assert not (repo / "added.py").exists()
    assert partition_by_grounding([added], DIFF, repo_file_checker(repo)) == ([added], [])
    assert not is_foreign_file_finding(added, DIFF, repo_file_checker(repo))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "pkg/../../outside.py", ".."])
def test_absolute_or_escaping_paths_are_not_in_the_repo(repo, path):
    (repo.parent / "outside.py").write_text("x = 1\n")
    check = repo_file_checker(repo)
    assert check(path) is False
    finding = _f(path)
    assert partition_by_grounding([finding], DIFF, check) == ([], [finding])


def test_unknown_or_missing_repo_disables_the_check(tmp_path):
    assert repo_file_checker("") is None
    assert repo_file_checker(None) is None
    assert repo_file_checker(tmp_path / "nope") is None


def test_empty_file_and_absent_argument_behave_as_before(repo):
    empty = _f("", evidence=EVIDENCE)
    foreign = _f("graph/fleet/proxy.py")
    bad_quote = _f("search.py", evidence="this line is not anywhere in the diff at all")
    findings = [empty, foreign, bad_quote]
    baseline = partition_by_grounding(findings, DIFF)
    assert baseline == ([empty, foreign], [bad_quote])
    # The empty-file finding is unaffected by the check.
    assert partition_by_grounding([empty], DIFF, repo_file_checker(repo)) == ([empty], [])


def test_a_raising_check_demotes_nothing():
    def boom(path):
        raise OSError("unreadable")

    foreign = _f("graph/fleet/proxy.py")
    assert partition_by_grounding([foreign], DIFF, boom) == ([foreign], [])


# ── r4: the review gate ──────────────────────────────────────────────────────


@pytest.fixture
def gate_seams(monkeypatch):
    _inject_fake_findings(monkeypatch)

    async def head(*args, **kwargs):
        return "a" * 40

    async def published(*args, **kwargs):
        return True

    monkeypatch.setattr(worktree, "pr_head_sha", head)
    monkeypatch.setattr(worktree, "post_review_status", published)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", published)


def _gate_finding(file, claim, evidence="def proxy(request): return forward(request)"):
    return dict(file=file, line=12, severity="blocker", claim=claim, evidence=evidence, verdict="confirmed")


async def test_foreign_file_finding_is_demoted_reported_and_never_bounces(monkeypatch, gate_seams, repo):
    payload = json.dumps([_gate_finding("graph/fleet/proxy.py", "Proxy drops auth header")])
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    store = _GateStore()

    await loop._review_gate(store, "bd-1", PR, str(repo))

    assert store.review_states[-1][0] == "review-clean"
    assert ("requeue", "bd-1") not in store.calls
    assert loop._review_fix_attempts.get("bd-1", 0) == 0
    saved = loop._review_history("bd-1")[0]["findings"][0]
    assert saved["verdict"] == "uncertain"
    assert saved["claim"] == "Proxy drops auth header"
    comments = [c[2] for c in store.calls if c[0] == "comment"]
    foreign = [c for c in comments if "not in this repository" in c]
    assert len(foreign) == 1 and "graph/fleet/proxy.py" in foreign[0]
    assert "absent from the PR diff" not in foreign[0]
    assert all("Review findings (blocking)" not in c for c in comments)


async def test_foreign_finding_alongside_a_real_one_only_the_real_one_bounces(monkeypatch, gate_seams, repo):
    payload = json.dumps(
        [
            _gate_finding("settings_schema.py", "Schema key missing"),
            _gate_finding("search.py", "Real defect", evidence=EVIDENCE),
        ]
    )
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    store = _GateStore()

    await loop._review_gate(store, "bd-1", PR, str(repo))

    assert store.review_states[-1][0] == "changes-requested"
    note = store.review_states[-1][1]
    assert "Real defect" in note
    assert "Schema key missing" not in note
    verdicts = {f["file"]: f["verdict"] for f in loop._review_history("bd-1")[0]["findings"]}
    assert verdicts == {"settings_schema.py": "uncertain", "search.py": "confirmed"}


async def test_finding_on_an_untouched_repo_file_still_blocks(monkeypatch, gate_seams, repo):
    payload = json.dumps([_gate_finding("pkg/untouched.py", "Caller breaks")])
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    store = _GateStore()

    await loop._review_gate(store, "bd-1", PR, str(repo))

    assert store.review_states[-1][0] == "changes-requested"
    assert ("requeue", "bd-1") in store.calls


async def test_unknown_repo_keeps_todays_pass_through(monkeypatch, gate_seams, tmp_path):
    payload = json.dumps([_gate_finding("graph/fleet/proxy.py", "Proxy drops auth header")])
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    store = _GateStore()

    await loop._review_gate(store, "bd-1", PR, str(tmp_path / "missing-checkout"))

    assert store.review_states[-1][0] == "changes-requested"
    comments = [c[2] for c in store.calls if c[0] == "comment"]
    assert all("not in this repository" not in c for c in comments)

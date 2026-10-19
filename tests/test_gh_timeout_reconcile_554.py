"""Read timeouts are unknown evidence, with friction only for consecutive outages."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from project_board import host_friction, worktree
from project_board.loop import BoardLoop
from project_board.store import BoardError, BoardTimeout

REAL_REVIEW_STATE = worktree.pr_review_state


@pytest.fixture(autouse=True)
def real_reader(monkeypatch):
    monkeypatch.setattr(worktree, "pr_review_state", REAL_REVIEW_STATE)


PR = "https://github.com/example/project/pull/554"


@pytest.mark.parametrize(
    "reader,args,unknown",
    [
        ("pr_merge_info", (PR,), {"mergeStateStatus": "", "isDraft": None}),
        ("pr_merge_state", (PR,), ""),
        ("pr_state", (PR,), ""),
        ("pr_head_sha", (PR,), ""),
        ("pr_identity", (PR,), {}),
        ("pr_review_state", (PR,), None),
        ("pr_ci_status", (PR,), ("none", "")),
        ("pr_diff", (PR,), ""),
        ("pr_url_for_branch", ("feature",), ""),
        ("repo_slug", (), ""),
        ("_find_marked_comment", ("example/project", "554", "marker"), ("", "")),
        ("read_review_status_result", ("example/project", "abc"), (worktree.STATUS_READ_UNREADABLE, None)),
        ("read_review_status", ("example/project", "abc"), None),
    ],
)
async def test_read_contracts(monkeypatch, reader, args, unknown):
    monkeypatch.setattr(worktree, "_gh", AsyncMock(side_effect=worktree.GhTimeout("gh read timed out")))
    with worktree.capture_gh_timeouts() as evidence:
        assert await getattr(worktree, reader)(*args, cwd=".") == unknown
    assert len(evidence) == 1


async def test_gh_raises_dedicated_compatible_timeout(monkeypatch):
    monkeypatch.setattr(worktree, "spawn_exec", AsyncMock(return_value=object()))
    monkeypatch.setattr(worktree, "communicate_or_kill", AsyncMock(side_effect=asyncio.TimeoutError))
    with pytest.raises(worktree.GhTimeout, match="gh pr view.*timed out") as exc:
        await worktree._gh("pr", "view", PR, cwd=".")
    assert isinstance(exc.value, worktree.WorktreeError)


@pytest.fixture
def reconcile(monkeypatch):
    loop = BoardLoop({"coder": "proto", "auto_merge": True, "auto_rebase": True, "review_gate": False, "ci_poll": True})
    card = {"id": "card-a", "pr_url": PR, "board_state": "in_review", "labels": ["ci-passing"]}
    store = SimpleNamespace(get_feature=lambda fid: card)
    monkeypatch.setattr(loop, "_repo_for", lambda f: ".")
    monkeypatch.setattr(loop, "_reconcile_external_review", AsyncMock(return_value=False))
    monkeypatch.setattr(loop, "_verify_merged_state", AsyncMock(return_value=False))
    monkeypatch.setattr(loop, "_reconcile_ci", AsyncMock())
    monkeypatch.setattr(loop, "_budget_get", AsyncMock(return_value=0))
    monkeypatch.setattr(loop, "_release_freeze_evidence", AsyncMock(return_value=""))
    monkeypatch.setattr(worktree, "merge_pr", AsyncMock())
    rows = Mock()
    monkeypatch.setattr(host_friction, "record", rows)
    failing = [True]

    async def gh(*args, **kwargs):
        fields = args[args.index("--json") + 1]
        if fields == "isDraft,mergeStateStatus":
            if failing[0]:
                raise worktree.GhTimeout("gh pr view timed out after 60s")
            return 0, json.dumps({"mergeStateStatus": "BLOCKED", "isDraft": False}), ""
        return 0, json.dumps({"state": "OPEN", "headRefOid": "abc"}), ""

    async def rebase(*args):
        # Real reader: its timeout must not abort the later verify/CI/merge edges.
        await worktree.pr_merge_state(PR)
        return False

    monkeypatch.setattr(worktree, "_gh", gh)
    monkeypatch.setattr(loop, "_maybe_rebase", rebase)
    return loop, card, store, rows, failing


async def test_consecutive_passes_are_bounded_and_recovery_resets(reconcile):
    loop, card, store, rows, failing = reconcile
    await loop._reconcile_pr(store, card)
    rows.assert_not_called()
    loop._verify_merged_state.assert_awaited_once()
    loop._reconcile_ci.assert_awaited_once()
    assert "unavailable" in " ".join(await loop._auto_merge_blockers(store, card, PR, "."))
    worktree.merge_pr.assert_not_awaited()
    await loop._reconcile_pr(store, card)
    rows.assert_not_called()  # multiple reads in one pass count only once
    for _ in range(8):
        await loop._reconcile_pr(store, card)
    assert rows.call_count == 1
    assert "gh timed out" in rows.call_args.args[0]
    assert "WorktreeError" not in rows.call_args.args[0]
    failing[0] = False
    await loop._reconcile_pr(store, card)
    assert card["id"] not in loop._gh_timeout_streaks
    failing[0] = True
    for _ in range(2):
        await loop._reconcile_pr(store, card)
    assert rows.call_count == 1
    await loop._reconcile_pr(store, card)
    assert rows.call_count == 2


async def test_card_and_pr_identity_do_not_share_streaks(reconcile):
    loop, card, store, rows, _ = reconcile
    await loop._reconcile_pr(store, card)
    await loop._reconcile_pr(store, card)
    await loop._reconcile_pr(store, {**card, "id": "card-b"})
    await loop._reconcile_pr(store, {**card, "pr_url": PR + "0"})
    rows.assert_not_called()


@pytest.mark.parametrize("error", [worktree.WorktreeError("git failed"), BoardError("store failed")])
async def test_ordinary_errors_still_report(reconcile, monkeypatch, error):
    loop, card, store, rows, _ = reconcile
    monkeypatch.setattr(loop, "_reconcile_external_review", AsyncMock(side_effect=error))
    await loop._reconcile_pr(store, card)
    assert rows.call_count == 1
    assert type(error).__name__ in rows.call_args.args[0]


async def test_store_stall_still_propagates(reconcile, monkeypatch):
    loop, card, store, rows, _ = reconcile
    monkeypatch.setattr(loop, "_reconcile_external_review", AsyncMock(side_effect=BoardTimeout("stalled")))
    with pytest.raises(BoardTimeout):
        await loop._reconcile_pr(store, card)
    rows.assert_not_called()


async def test_timeout_context_is_task_local(monkeypatch):
    async def gh(*args, **kwargs):
        await asyncio.sleep(0)
        raise worktree.GhTimeout(args[2])

    monkeypatch.setattr(worktree, "_gh", gh)

    async def read(url):
        with worktree.capture_gh_timeouts() as evidence:
            await worktree.pr_merge_info(url)
        return [str(e) for e in evidence]

    assert await asyncio.gather(read("a"), read("b")) == [["a"], ["b"]]


async def test_ci_log_timeout_preserves_known_failure(monkeypatch):
    checks = [
        {
            "__typename": "CheckRun",
            "name": "test",
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/example/project/actions/runs/12",
        }
    ]
    monkeypatch.setattr(
        worktree,
        "_gh",
        AsyncMock(side_effect=[(0, json.dumps(checks), ""), worktree.GhTimeout("gh run view timed out")]),
    )
    with worktree.capture_gh_timeouts() as evidence:
        state, detail = await worktree.pr_ci_status(PR)
    assert state == "failing" and "test: FAILURE" in detail
    assert len(evidence) == 1


async def test_escaping_timeout_is_classified_by_cause(reconcile, monkeypatch):
    loop, card, store, rows, _ = reconcile
    monkeypatch.setattr(loop, "_reconcile_external_review", AsyncMock(side_effect=worktree.GhTimeout("gh timeout")))
    for _ in range(2):
        await loop._reconcile_pr(store, card)
    rows.assert_not_called()
    await loop._reconcile_pr(store, card)
    assert rows.call_count == 1
    assert "gh timed out" in rows.call_args.args[0]


async def test_grounding_timeout_keeps_review_pending(monkeypatch):
    loop = BoardLoop({"review_gate": True})
    store = Mock()
    finding = SimpleNamespace(verdict="confirmed", severity="major", evidence="broken()")
    monkeypatch.setattr(loop, "_parse_findings", lambda output: [finding])
    monkeypatch.setattr(loop, "_run_review_workflow", AsyncMock(return_value=("findings", "")))
    monkeypatch.setattr(loop, "_budget_reset", AsyncMock())
    monkeypatch.setattr(loop, "_publish_gate_verdict", AsyncMock())
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value="abc"))
    monkeypatch.setattr(worktree, "_gh", AsyncMock(side_effect=worktree.GhTimeout("gh pr diff timed out")))

    with worktree.capture_gh_timeouts() as evidence:
        await loop._review_gate(store, "card-a", PR, ".")

    assert len(evidence) == 1  # nested grounding capture reaches the per-card classifier
    store.set_review_substate.assert_called_once_with("card-a", "review-pending")
    assert finding.verdict == "confirmed"
    assert loop._publish_gate_verdict.call_args.kwargs["state"] == "pending"
    loop._publish_gate_verdict.assert_awaited_once()
    store.requeue.assert_not_called()
    store.add_comment.assert_not_called()
    assert "card-a" not in loop._review_inflight


@pytest.mark.parametrize("recovered_url", ["", PR])
async def test_orphan_timeout_defers_recovery_until_lookup_succeeds(monkeypatch, recovered_url):
    loop = BoardLoop({})
    store = Mock()
    store.get_feature.return_value = {"id": "card-a", "title": "Feature", "board_state": "in_progress"}
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_repo_for", lambda f: ".")
    salvage = AsyncMock(return_value=False)
    monkeypatch.setattr(loop, "_salvage_verified_candidate", salvage)
    rows = Mock()
    monkeypatch.setattr(host_friction, "record", rows)
    gh = AsyncMock(side_effect=worktree.GhTimeout("gh pr view timed out"))
    monkeypatch.setattr(worktree, "_gh", gh)

    await loop._reconcile_orphan("card-a")
    rows.assert_not_called()
    for _ in range(5):
        await loop._reconcile_orphan("card-a")
    assert rows.call_count == 1
    assert "gh timed out" in rows.call_args.args[0]
    salvage.assert_not_awaited()
    store.requeue.assert_not_called()
    store.open_review.assert_not_called()

    gh.side_effect = None
    gh.return_value = (0, recovered_url, "") if recovered_url else (1, "", "no PR found")
    await loop._reconcile_orphan("card-a")
    assert "card-a" not in loop._gh_timeout_streaks
    if recovered_url:
        store.open_review.assert_called_once_with("card-a", pr_url=PR)
        salvage.assert_not_awaited()
        store.requeue.assert_not_called()
    else:
        salvage.assert_awaited_once_with(store, "card-a")
        store.requeue.assert_called_once_with("card-a")

    gh.side_effect = worktree.GhTimeout("gh pr view timed out")
    await loop._reconcile_orphan("card-a")
    assert rows.call_count == 1  # recovered read reset the consecutive-pass streak

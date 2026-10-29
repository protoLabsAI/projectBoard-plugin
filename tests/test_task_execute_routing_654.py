"""Execution tasks use shell-capable coder routing without changing writing tasks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import project_board.loop.drive as drive_mod
from project_board.loop import BoardLoop
from project_board.loop.prompt import is_execution_task


@pytest.mark.parametrize(
    "feature, expected",
    [
        ({"labels": ["execute"]}, True),
        ({"labels": ["execution"]}, False),
        ({"spec": "```sh\necho hi\n```"}, True),
        ({"spec": "~~~bash\necho hi\n~~~"}, True),
        ({"spec": "$ echo hi"}, True),
        ({"spec": "Run UV RUN check.py"}, True),
        ({"acceptance_criteria": "Run pytest -q"}, True),
        ({"spec": "python check.py"}, True),
        ({"spec": "npm test"}, True),
        ({"spec": "npx check"}, True),
        ({"spec": "curl https://example.org"}, True),
        ({"spec": "Call run_eval"}, True),
        ({"spec": "make test"}, True),
        ({"spec": "Run make check"}, True),
        ({"spec": "Verify with `make check`."}, True),
        ({"spec": "Write a decision document."}, False),
        ({"spec": "Make the edits early rather than reading indefinitely."}, False),
        ({"spec": "Decide how to make the system simpler."}, False),
        ({"spec": "Discuss mypytest, run_evaluation, pythonic and remake."}, False),
        ({"title": "pytest", "spec": None, "acceptance_criteria": None}, False),
    ],
)
def test_classifier(feature, expected):
    assert is_execution_task(feature) is expected


def setup_routing(monkeypatch, cfg=None, live=("lead", "small", "large", "sister")):
    loop = BoardLoop(cfg or {"coder": "lead", "coders": {"standard": "small", "advanced": "large"}})
    delegates = {name: SimpleNamespace(name=name, type="acp") for name in live}
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, kind: delegates.get(name))
    drive = AsyncMock()
    own = AsyncMock(return_value="self")
    monkeypatch.setattr(loop, "_drive_task", drive)
    monkeypatch.setattr(loop, "_dispatch_self", own)
    store = SimpleNamespace(claim_task=Mock(side_effect=lambda cid, **kw: {"id": cid, **kw}))
    return loop, store, drive, own


async def drain(loop):
    await asyncio.gather(*list(loop._drives))
    await asyncio.sleep(0)  # let the drive registry's done callback clear its entry


@pytest.mark.parametrize("assignee", ["", "self", "lead", "agent", " LEAD "])
async def test_execution_goes_to_first_rung(monkeypatch, assignee, caplog):
    loop, store, drive, own = setup_routing(monkeypatch)
    with caplog.at_level("INFO"):
        result = await loop._dispatch_task(store, {"id": "exec-654", "assignee": assignee, "spec": "uv run eval.py"})
    await drain(loop)
    assert result == "acp"
    store.claim_task.assert_called_once_with("exec-654", assignee="small")
    assert drive.await_args.args[1].name == "small"
    own.assert_not_awaited()
    assert "execution task exec-654 → small (not self)" in caplog.text


@pytest.mark.parametrize(
    "assignee, expected, execute",
    [
        ("self", "self", False),
        ("human", "parked", False),
        ("sister", "acp", False),
        ("", "parked", False),
        ("human", "parked", True),
        ("sister", "acp", True),
    ],
)
async def test_existing_targets(monkeypatch, assignee, expected, execute):
    loop, store, drive, own = setup_routing(monkeypatch)
    card = {"id": "target-654", "assignee": assignee, "labels": ["execute"] if execute else []}
    assert await loop._dispatch_task(store, card) == expected
    await drain(loop)
    if expected == "self":
        own.assert_awaited_once_with(store, card, assignee)
        store.claim_task.assert_not_called()
    else:
        store.claim_task.assert_called_once_with(card["id"], assignee=assignee)
        own.assert_not_awaited()
    if expected == "acp":
        assert drive.await_args.args[1].name == "sister"
    else:
        drive.assert_not_awaited()


async def test_project_exclusive_and_policy(monkeypatch):
    cfg = {
        "coder": "lead",
        "coders": {"standard": "small", "advanced": "large"},
        "projects": {
            "other": {
                "repo": "/unused",
                "coders": {"standard": ["small", "sister"], "advanced": "large"},
                "routing": {
                    "exclusive": ["sister", "large"],
                    "headroom": False,
                    "usage_policy": [{"account": "sister", "keep_under": 50, "prefer": "small"}],
                },
            }
        },
    }
    loop, store, drive, own = setup_routing(monkeypatch, cfg)
    monkeypatch.setattr(loop, "_policy_usage", lambda *args: {"sister": {"headroom_pct": 10}})
    assert await loop._dispatch_task(store, {"id": "project-654", "project": "other", "labels": ["execute"]}) == "acp"
    await drain(loop)
    store.claim_task.assert_called_once_with("project-654", assignee="sister")
    assert drive.await_args.args[1].name == "sister"
    own.assert_not_awaited()


async def test_policy_can_choose_an_eligible_sibling(monkeypatch):
    cfg = {
        "coder": "lead",
        "coders": {"standard": ["small", "sister"], "advanced": "large"},
        "routing": {"headroom": False, "usage_policy": [{"account": "small", "keep_under": 50, "prefer": "sister"}]},
    }
    loop, store, drive, _ = setup_routing(monkeypatch, cfg)
    monkeypatch.setattr(drive_mod, "_next_rung_cursor", lambda: 0)
    monkeypatch.setattr(loop, "_policy_usage", lambda *args: {"small": {"headroom_pct": 10}})
    assert await loop._dispatch_task(store, {"id": "policy-654", "labels": ["execute"]}) == "acp"
    await drain(loop)
    assert drive.await_args.args[1].name == "sister"


async def test_no_eligible_coder_parks_and_never_invokes_self(monkeypatch, caplog):
    cfg = {"coder": "lead", "coders": {"standard": "small", "advanced": "large"}, "routing": {"exclusive": ["missing"]}}
    loop, store, drive, own = setup_routing(monkeypatch, cfg)
    with caplog.at_level("INFO"):
        assert (
            await loop._dispatch_task(store, {"id": "missing-654", "assignee": "self", "labels": ["execute"]})
            == "parked"
        )
    store.claim_task.assert_called_once_with("missing-654", assignee="self")
    drive.assert_not_awaited()
    own.assert_not_awaited()
    assert "no coder resolves" in caplog.text


async def test_lost_claim_does_not_dispatch(monkeypatch):
    loop, store, drive, own = setup_routing(monkeypatch)
    store.claim_task.side_effect = None
    store.claim_task.return_value = None
    assert await loop._dispatch_task(store, {"id": "race-654", "labels": ["execute"]}) == "race"
    drive.assert_not_awaited()
    own.assert_not_awaited()


def test_shell_prompt_only_for_execution():
    loop = BoardLoop({})
    prompt = loop._build_task_prompt({"spec": "pytest -q"}, cwd="/scratch/task")
    assert "You have a shell in your working directory (/scratch/task —" in prompt
    assert "Report real numbers, or an honest FAIL with the command and its output." in prompt
    assert "Never decline for lack of a shell." in prompt
    assert "Do not commit, push or open a PR." in prompt
    assert "There is no code change, worktree" not in prompt
    writing = loop._build_task_prompt({"spec": "Write a decision document"})
    assert "You have a shell" not in writing
    assert "There is no code change, worktree, or PR" in writing


async def test_explicit_a2a_execution_target_is_unchanged(monkeypatch):
    loop, store, drive, own = setup_routing(monkeypatch)
    sister = SimpleNamespace(name="remote", type="a2a")
    monkeypatch.setattr(loop, "_resolve_task_delegate", lambda name: sister)
    assert await loop._dispatch_task(store, {"id": "a2a-654", "assignee": "remote", "labels": ["execute"]}) == "a2a"
    await drain(loop)
    assert drive.await_args.args[1] is sister
    store.claim_task.assert_called_once_with("a2a-654", assignee="remote")
    own.assert_not_awaited()


async def test_drive_prompt_uses_the_allocated_workdir(monkeypatch):
    from project_board import coder_seam

    loop = BoardLoop({})
    feature = {"id": "cwd-654", "spec": "pytest -q"}
    delegate = SimpleNamespace(type="acp")
    monkeypatch.setattr(loop, "_store", lambda: object())
    monkeypatch.setattr(loop, "_task_workdir", AsyncMock(return_value=("/scratch/actual", None)))
    monkeypatch.setattr(loop, "_record_task_reply", AsyncMock())
    dispatch = AsyncMock(return_value="3 passed")
    monkeypatch.setattr(coder_seam, "dispatch_task", dispatch)
    await loop._drive_task(feature, delegate)
    assert "(/scratch/actual —" in dispatch.await_args.args[1]
    assert dispatch.await_args.kwargs["cwd"] == "/scratch/actual"

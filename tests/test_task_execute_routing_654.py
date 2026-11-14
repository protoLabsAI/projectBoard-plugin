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
        ({"spec": "Run `pytest`."}, True),
        ({"spec": "Execute `uv run check.py`."}, True),
        ({"spec": "Write a decision document."}, False),
        ({"spec": "Make the edits early rather than reading indefinitely."}, False),
        ({"spec": "Decide how to make the system simpler."}, False),
        ({"spec": "Discuss mypytest, run_evaluation, pythonic and remake."}, False),
        ({"title": "pytest", "spec": None, "acceptance_criteria": None}, False),
        # Review regressions: a tool NAMED in prose, or a non-shell/example fence, is not a
        # command to run — these used to trip the loose `\b`-anywhere / any-fence match.
        ({"spec": "The target runtime is Python 3.12 for this service."}, False),
        ({"spec": "Python 3.12 is the required runtime for the service."}, False),
        ({"spec": "Document how pytest fixtures work and write up the findings."}, False),
        ({"spec": "Make this proposal simpler and clearer."}, False),
        ({"spec": 'Return exactly this shape:\n```json\n{"ok": true}\n```'}, False),
        ({"spec": "Here is an illustrative snippet:\n```python\nx = compute()\n```"}, False),
    ],
)
def test_classifier(feature, expected):
    assert is_execution_task(feature) is expected


WRITING_TOOL_MENTIONS = [
    "Describe how the service is deployed via npm.",
    "Describe the utility implemented via Python scripts.",
    "Document how `pytest` fixtures work.",
    "Document how `python` scripts work.",
    "Document the `npm test` command.",
    "Document how the service runs pytest.",
    "Explain why running npm is useful.",
    "Document `examples using pytest`.",
]


@pytest.mark.parametrize("text", WRITING_TOOL_MENTIONS)
@pytest.mark.parametrize("field", ["spec", "acceptance_criteria"])
def test_tool_mentions_remain_writing(text, field):
    card = {field: text}
    assert not is_execution_task(card)
    prompt = BoardLoop({})._build_task_prompt(card)
    assert "You have a shell" not in prompt
    assert "There is no code change, worktree, or PR" in prompt


@pytest.mark.parametrize("text", WRITING_TOOL_MENTIONS)
@pytest.mark.parametrize("assignee", ["self", ""])
async def test_tool_mentions_preserve_dispatch(monkeypatch, text, assignee):
    loop, store, drive, own = setup_routing(monkeypatch)
    card = {"id": "writing-654", "assignee": assignee, "spec": text}
    assert await loop._dispatch_task(store, card) == ("self" if assignee else "parked")
    await drain(loop)
    drive.assert_not_awaited()
    if assignee:
        own.assert_awaited_once_with(store, card, assignee)
        store.claim_task.assert_not_called()
    else:
        own.assert_not_awaited()
        store.claim_task.assert_called_once_with(card["id"], assignee="")


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
    store.claim_task.assert_called_once_with("exec-654", assignee="small", expected_assignee=assignee.strip())
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
    store.claim_task.assert_called_once_with("project-654", assignee="sister", expected_assignee="")
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
    store.claim_task.assert_called_once_with("missing-654", assignee="self", expected_assignee="self")
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


@pytest.mark.parametrize("labels", [[], ["execute"]])
async def test_decompose_task_stays_self_despite_embedded_commands(monkeypatch, labels):
    """Copied commands must affect neither the decompose routing nor its prompt."""
    loop, store, drive, own = setup_routing(monkeypatch)
    card = {
        "id": "bd-split",
        "labels": labels,
        "assignee": "agent",
        "title": "Decompose bd-9 — timed out 2x, too wide to build",
        "spec": (
            "ORIGINAL SPEC\nRun `uv run pytest -q` and report the result.\n\n"
            "1. Create the slices with board_create_feature and leave them in backlog.\n"
            "3. Cancel bd-9 with board_cancel_feature."
        ),
    }
    assert is_execution_task(card) is False
    prompt = loop._build_task_prompt(card)
    assert "You have a shell" not in prompt
    assert "Never decline for lack of a shell" not in prompt
    assert "There is no code change, worktree, or PR" in prompt
    assert "board_create_feature" in prompt
    assert await loop._dispatch_task(store, card) == "self"
    await drain(loop)
    own.assert_awaited_once_with(store, card, "agent")  # …it stays first-party
    store.claim_task.assert_not_called()
    drive.assert_not_awaited()


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


@pytest.fixture
def execution_board(tmp_path):
    import shutil

    from project_board import store as store_mod

    if not shutil.which(store_mod.BR):
        pytest.skip("real br required for claim ownership regression")
    return store_mod.BeadsBoard(repo=str(tmp_path), actor="test")


def ready_execution(board, assignee):
    task = board.create_feature(
        "Execution claim identity",
        spec="Run pytest -q",
        acceptance_criteria="Report the result",
        issue_type="task",
        assignee=assignee,
        execute=True,
    )
    return board.mark_ready(task["id"])


def codex_routing(monkeypatch):
    return setup_routing(
        monkeypatch,
        {"coder": "lead", "coders": {"standard": "codex", "advanced": "lead"}, "routing": {"exclusive": ["codex"]}},
        live=("codex",),
    )


@pytest.mark.parametrize("owner", ["agent", "self", "lead", "LEAD", ""])
async def test_real_execution_claim_preserves_owner_and_dispatches_once(monkeypatch, execution_board, owner):
    loop, _, drive, own = codex_routing(monkeypatch)
    card = ready_execution(execution_board, owner)
    assert await loop._dispatch_task(execution_board, card) == "acp"
    await drain(loop)
    assert await loop._dispatch_task(execution_board, card) == "race"
    drive.assert_awaited_once()
    assert drive.await_args.args[1].name == "codex"
    stored = execution_board.get_feature(card["id"])
    assert stored["assignee"] == (owner or "codex")
    assert stored["board_state"] == "in_progress"
    own.assert_not_awaited()


@pytest.mark.parametrize("owner", ["agent", ""])
@pytest.mark.parametrize("timing", ["before_read", "at_claim"])
async def test_real_execution_claim_rejects_concurrent_reassignment(monkeypatch, execution_board, owner, timing):
    loop, _, drive, _ = codex_routing(monkeypatch)
    card = ready_execution(execution_board, owner)
    run = execution_board._run
    if timing == "before_read":
        run("update", card["id"], "--assignee", "operator")
    else:

        def race(*args, **kwargs):
            if "--claim" in args:
                run("update", card["id"], "--assignee", "operator")
            return run(*args, **kwargs)

        monkeypatch.setattr(execution_board, "_run", race)
    assert await loop._dispatch_task(execution_board, card) == "race"
    drive.assert_not_awaited()
    stored = execution_board.get_feature(card["id"])
    assert stored["assignee"] == "operator"
    assert stored["board_state"] == "ready"


async def test_unchanged_owner_rejection_has_diagnostic(monkeypatch, execution_board, caplog):
    from project_board.store import BoardError

    loop, _, drive, _ = codex_routing(monkeypatch)
    card = ready_execution(execution_board, "agent")
    run = execution_board._run

    def reject(*args, **kwargs):
        if "--claim" in args:
            raise BoardError(f"claim: issue {card['id']} already assigned to agent")
        return run(*args, **kwargs)

    monkeypatch.setattr(execution_board, "_run", reject)
    assert await loop._dispatch_task(execution_board, card) == "claim-ownership-rejected"
    assert "owner='agent', claim actor='agent', destination='codex'" in caplog.text
    assert "Inspect the stored owner and routing" in caplog.text
    assert drive_mod._is_livelock_skip_reason("claim-ownership-rejected")
    drive.assert_not_awaited()


@pytest.mark.parametrize("state", [None, "in_progress"])
def test_execution_claim_missing_or_claimed_card_does_not_write(monkeypatch, state):
    from project_board.store import BeadsBoard

    board = BeadsBoard(repo="/unused", actor="test")
    card = None if state is None else {"board_state": state, "assignee": "agent"}
    monkeypatch.setattr(board, "get_feature", Mock(return_value=card))
    run = Mock()
    monkeypatch.setattr(board, "_run", run)
    assert board.claim_task("exec-760", assignee="codex", expected_assignee="agent") is None
    run.assert_not_called()


@pytest.mark.parametrize("edge", ["read", "claim", "diagnostic_read"])
def test_execution_claim_timeout_is_not_a_race(monkeypatch, edge):
    from project_board.store import BeadsBoard, BoardError, BoardTimeout

    board = BeadsBoard(repo="/unused", actor="test")
    card = {"board_state": "ready", "assignee": "agent"}
    read = Mock(return_value=card)
    run = Mock(side_effect=BoardTimeout("store unavailable"))
    if edge == "read":
        read.side_effect = BoardTimeout("store unavailable")
    elif edge == "diagnostic_read":
        run.side_effect = BoardError("already assigned to agent")
        read.side_effect = [card, BoardTimeout("store unavailable")]
    monkeypatch.setattr(board, "get_feature", read)
    monkeypatch.setattr(board, "_run", run)
    with pytest.raises(BoardTimeout):
        board.claim_task("exec-760", assignee="codex", expected_assignee="agent")

"""The adversarial review of #432: seven ways the task delivery path still lied or broke.

Each test pins one gap the review reproduced on real `br` against #432's head (4d27925):

1. a REJECTION left every requirement item closed, so the next round, the verifier and
   the verify note all read "done" on work the verifier had just refused — and the task
   prompt never carried the rejection feedback board_verify's docstring promised;
2. two deliveries racing on one card BOTH landed (8/8 trials) — the #399 timeline, the
   self-drive and a PM "repair" three seconds apart in one process;
3. requirement rows rode only the WINNING delivery: an agent's in-turn board_deliver
   without the section won, its reply carrying the section was refused, and the ledger
   could never close;
4. the shared `## Requirements` parser closed items on a quoted example inside a code
   fence, and on hedged rows (`done? not yet`, `done-ish`);
5. an agent that delivered in-turn and then timed out had its DELIVERED card blocked, and
   the verifier could no longer approve it;
6. the ledger was written before the transition, so a failed stamp/label write left an
   in_progress card with a closed ledger — and a `br` timeout escaped the drive task;
7. an empty delivery still succeeded, and listed as delivered.

Plus `delivered` now describes the CURRENT round. Real `br` for every state assertion; the
only fakes are the model's turn and the injected `br` faults.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
import time

import pytest

import project_board as pb
from project_board import coder_seam, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop, _parse_requirements_reply
from project_board.store import AlreadyDelivered, BeadsBoard, BoardError, open_requirements_note

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — see tests/test_integration.py (CI sets PB_REQUIRE_BR=1)",
)

CRITERIA = "- the decision names the chosen option\n- the decision names a rejected option"


@pytest.fixture
def board(tmp_path):
    """A REAL ``BeadsBoard`` over a throwaway workspace (the test_integration pattern)."""
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _task(board, assignee="alice"):
    t = board.create_feature(
        "Decide", spec="Record the decision.", acceptance_criteria=CRITERIA, issue_type="task", assignee=assignee
    )
    board.mark_ready(t["id"])
    return t["id"]


def _in_progress(board, assignee="alice"):
    fid = _task(board, assignee)
    assert board.claim_task(fid, assignee=assignee)["board_state"] == "in_progress"
    return fid


def _ledger(board, fid):
    return {r["id"]: r["status"] for r in board.get_feature(fid)["requirements"]}


def _row(board, fid):
    return next(f for f in board.list_features() if f["id"] == fid)


def _tools(board, monkeypatch):
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    return {t.name: t for t in pb._board_tools({})}


def _inject_once(board, monkeypatch, pred, exc):
    """Fail the FIRST real `br` call matching ``pred`` with ``exc``; everything else is real."""
    real, fired = board._run, []

    def _run(*args, **kwargs):
        if not fired and pred(args):
            fired.append(args)
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(board, "_run", _run)
    return fired


# ── 1. a rejection reopens the round's items, and the next round is told why ────────


@requires_br
def test_a_rejection_reopens_the_items_and_the_next_round_leads_with_why(board, monkeypatch):
    tools = _tools(board, monkeypatch)
    fid = _in_progress(board)
    tools["board_deliver"].invoke(
        {"feature_id": fid, "text": "ROUND 1\n\n## Requirements\n- r1: done\n- r2: declined — out of scope"}
    )
    assert _ledger(board, fid) == {"r1": "done", "r2": "declined"}

    tools["board_verify"].invoke(
        {"feature_id": fid, "approved": False, "feedback": "r2 IS in scope — name the rejected option"}
    )

    reqs = {r["id"]: r for r in board.get_feature(fid)["requirements"]}
    assert {i: r["status"] for i, r in reqs.items()} == {"r1": "open", "r2": "open"}  # the refuted claims reopen…
    assert reqs["r1"]["reopened_from"] == "done"  # …each keeping what it claimed
    assert reqs["r2"]["reopened_from"] == "declined — out of scope" and "decline_reason" not in reqs["r2"]

    # The requeued card is not delivered in the round it is now in.
    row = _row(board, fid)
    assert row["board_state"] == "ready" and row["delivered"] is False
    assert (row["deliverable_chars"], row["deliverable_preview"]) == (0, "")
    assert row["last_deliverable_preview"].startswith("ROUND 1")

    # The next round's prompt LEADS with the rejection, and shows the reopened items.
    prompt = BoardLoop({})._build_task_prompt(board.claim_task(fid, assignee="alice"))
    rejected = prompt.index("Your previous delivery was REJECTED")
    assert rejected < prompt.index("## Task") and "r2 IS in scope — name the rejected option" in prompt
    assert "(reopened by the rejection — was declined — out of scope)" in prompt

    # A round-2 delivery that disposes of nothing leaves both open — and says so to the verifier.
    out = board.record_delivery(fid, text="ROUND 2, no section")
    assert open_requirements_note(out["requirements"]) == "2 requirement(s) still open: r1, r2"
    assert out["rejection_feedback"] == ""  # a newer delivery supersedes the old verdict


# ── 2. two deliveries racing on one card: exactly one lands ──────────────────────────


@requires_br
def test_two_racing_deliveries_cannot_both_land(board, monkeypatch):
    """Forced, not left to thread timing: the first delivery stalls INSIDE its writes (after
    it read in_progress), and the second starts reading meanwhile. Without the card lock both
    read in_progress and both land; with it, the second waits, sees in_review, is refused."""
    fid = _in_progress(board)
    real, stalled = board._run, threading.Event()

    def _run(*args, **kwargs):
        if args[:2] == ("comments", "add") and str(args[3]).startswith("deliverable: from self-drive"):
            stalled.set()
            time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(board, "_run", _run)
    outcome = {}

    def deliver(tag):
        try:
            board.record_delivery(fid, text=f"from {tag}")
            outcome[tag] = "ok"
        except AlreadyDelivered:
            outcome[tag] = "already"

    first = threading.Thread(target=deliver, args=("self-drive",))
    first.start()
    assert stalled.wait(10)
    deliver("pm-repair")  # starts while the first is mid-write
    first.join()

    assert outcome == {"self-drive": "ok", "pm-repair": "already"}
    comments = board._run("show", fid, want_json=True)
    bead = comments[0] if isinstance(comments, list) else comments
    records = [c for c in bead.get("comments") or [] if store_mod._comment_text(c).startswith("deliverable:")]
    assert len(records) == 1 and board.get_feature(fid)["deliverable"] == "from self-drive"


# ── 3. rows on a refused/no-op'd delivery still land while the card awaits its verdict ─


@requires_br
async def test_the_reply_carrying_the_section_closes_the_ledger_after_an_in_turn_delivery(board, monkeypatch):
    """The K case end to end: the agent board_delivers its document (no section) in-turn,
    then replies with the section the prompt asked for. The reply is not the deliverable of
    record — but its rows are about the round under review, so they land."""
    fid = _task(board, assignee="agent")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    deliver = _tools(board, monkeypatch)["board_deliver"]
    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())

    async def _turn(invoke, prompt, session_id, *, timeout=None):
        await asyncio.to_thread(deliver.invoke, {"feature_id": fid, "text": "# Decision\nA over B."})
        return "Delivered.\n\n## Requirements\n- r1: done\n- r2: done"

    monkeypatch.setattr(coder_seam, "dispatch_self", _turn)
    loop = BoardLoop({"coder": "proto"})
    assert await loop._spawn_ready() is True
    await asyncio.gather(*list(loop._drives))

    card = board.get_feature(fid)
    assert card["deliverable"] == "# Decision\nA over B."  # the explicit delivery stands…
    assert _ledger(board, fid) == {"r1": "done", "r2": "done"}  # …and the reply's rows landed


@requires_br
def test_rows_land_while_the_card_awaits_its_verdict_and_never_after(board):
    """The store-level rule: a refused delivery's rows still close items while the card is
    in review — the refusal says so — and nothing lands once the verdict is in."""
    fid = _in_progress(board)
    board.record_delivery(fid, text="Doc")
    with pytest.raises(AlreadyDelivered, match="rows were applied"):
        board.record_delivery(fid, text="Doc, again.\n\n## Requirements\n- r1: done")
    assert _ledger(board, fid) == {"r1": "done", "r2": "open"}
    assert board.get_feature(fid)["deliverable"] == "Doc"  # the record itself is untouched

    board.record_verification(fid, approved=True, by="reviewer")
    with pytest.raises(BoardError, match="expects in_progress, got 'done'"):
        board.record_delivery(fid, text="Late.\n\n## Requirements\n- r2: done")
    assert _ledger(board, fid) == {"r1": "done", "r2": "open"}  # after the verdict: nothing


# ── 4. the parser: quoted examples and hedged rows are not dispositions ──────────────


def test_the_parser_ignores_fenced_examples_and_hedged_rows():
    fenced = "How to report:\n\n```markdown\n## Requirements\n- r1: done\n```\n\nThe board closes them.\n"
    assert _parse_requirements_reply(fenced) == []
    # …but a real section after a fenced example still counts
    assert _parse_requirements_reply(fenced + "\n## Requirements\n- r2: done\n") == [{"id": "r2", "status": "done"}]
    # an unclosed fence runs to the end: everything below it is quoted
    assert _parse_requirements_reply("~~~\n## Requirements\n- r1: done\n") == []

    hedged = "## Requirements\n- r1: done? not yet, blocked on legal\n- r2: done-ish, partially\n- r3: declined\n- r4: declined-ish\n"
    assert _parse_requirements_reply(hedged) == []  # hedges, and a decline with no reason, close nothing
    exact = "## Requirements\n- r1: done\n- r2: Done.\n- r3: declined — out of scope\n- r4: declined: n/a\n- r5: declined - dup of r4\n- r6: open\n"
    assert _parse_requirements_reply(exact) == [
        {"id": "r1", "status": "done"},
        {"id": "r2", "status": "done"},
        {"id": "r3", "status": "declined", "decline_reason": "out of scope"},
        {"id": "r4", "status": "declined", "decline_reason": "n/a"},
        {"id": "r5", "status": "declined", "decline_reason": "dup of r4"},
    ]


def test_a_done_row_may_cite_its_evidence_but_not_hedge():
    """Coders cite evidence after the status (`- r1: done — <where>`). Reading that as
    silence left all seven of bd-9wh1's items open through two fix rounds and blocked a
    finished card; a note after a real separator is a disposition. A note that OPENS with
    a hedge is still not one — the rule the exact-token parser exists to keep."""
    evidence = (
        "## Requirements\n"
        "- r1: done — `ChatMessageView` (shared by main chat and `PaletteChat`) renders `<SentTimestamp>`\n"
        "- r2: done: covered by `test_tooltip_label`\n"
        "- r3: done - see the screenshot\n"
        "- r4: Done — Notably, both consumers\n"
    )
    assert _parse_requirements_reply(evidence) == [{"id": f"r{i}", "status": "done"} for i in range(1, 5)]
    hedged = (
        "## Requirements\n"
        "- r1: done — but only for user messages\n"
        "- r2: done — partially\n"
        "- r3: done: not yet wired\n"
        "- r4: done - pending review\n"
        "- r5: done — in progress\n"
        "- r6: done, mostly\n"
        "- r7: done —\n"
    )
    assert _parse_requirements_reply(hedged) == []


@requires_br
def test_a_document_that_quotes_the_format_closes_nothing(board):
    fid = _in_progress(board)
    doc = "# How a task reports\n\n```markdown\n## Requirements\n- r1: done\n- r2: declined — out of scope\n```\n"
    board.record_delivery(fid, text=doc)
    assert _ledger(board, fid) == {"r1": "open", "r2": "open"}


# ── 5. a failure AFTER an in-turn delivery never blocks the delivered card ────────────


@requires_br
async def test_a_turn_that_times_out_after_delivering_leaves_the_card_for_the_verifier(board, monkeypatch):
    fid = _task(board, assignee="agent")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    deliver = _tools(board, monkeypatch)["board_deliver"]
    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())

    async def _turn(invoke, prompt, session_id, *, timeout=None):
        await asyncio.to_thread(deliver.invoke, {"feature_id": fid, "text": "# Decision\nA over B."})
        raise worktree.CoderTimeout("self task exceeded coder_timeout_s")

    monkeypatch.setattr(coder_seam, "dispatch_self", _turn)
    loop = BoardLoop({"coder": "proto"})
    assert await loop._spawn_ready() is True
    await asyncio.gather(*list(loop._drives))

    card = board.get_feature(fid)
    assert card["board_state"] == "in_review" and not card["blocked"]
    assert board.record_verification(fid, approved=True, by="reviewer")["board_state"] == "done"


# ── 6. failure ordering: the ledger follows the transition; a timeout never escapes ──


@requires_br
@pytest.mark.parametrize("which", ["stamp", "label"])
def test_a_failed_stamp_or_label_write_leaves_no_closed_ledger(board, monkeypatch, which):
    fid = _in_progress(board)
    pred = {
        "stamp": lambda a: a[:2] == ("comments", "add") and str(a[3]).startswith("delivered-by:"),
        "label": lambda a: a[0] == "update" and "in-review" in a,
    }[which]
    fired = _inject_once(board, monkeypatch, pred, BoardError("`br` failed: database is locked"))
    with pytest.raises(BoardError):
        board.record_delivery(fid, text="Doc\n\n## Requirements\n- r1: done\n- r2: done")
    assert fired  # the fault really hit that write
    assert board.get_feature(fid)["board_state"] == "in_progress"
    assert _ledger(board, fid) == {"r1": "open", "r2": "open"}  # was: closed on an undelivered card
    assert _row(board, fid)["delivered"] is False


@requires_br
def test_a_ledger_write_that_times_out_costs_nothing(board, monkeypatch):
    fid = _in_progress(board)
    fired = _inject_once(
        board,
        monkeypatch,
        lambda a: a[0] == "update" and any(str(x).startswith("--notes=") for x in a),
        subprocess.TimeoutExpired(cmd="br update", timeout=30),
    )
    out = board.record_delivery(fid, text="Doc\n\n## Requirements\n- r1: done")  # does not raise
    assert fired and out["board_state"] == "in_review" and out["deliverable"].startswith("Doc")


@requires_br
async def test_a_br_timeout_on_the_delivery_never_escapes_the_drive(board, monkeypatch, caplog):
    fid = _in_progress(board, assignee="quinn")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    _inject_once(
        board,
        monkeypatch,
        lambda a: a[:2] == ("comments", "add") and str(a[3]).startswith("deliverable:"),
        subprocess.TimeoutExpired(cmd="br comments add", timeout=30),
    )

    async def _reply(delegate, prompt, *, timeout=None):
        return "The deliverable."

    monkeypatch.setattr(coder_seam, "dispatch_task", _reply)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await BoardLoop({"coder": "proto"})._drive_task(board.get_feature(fid), delegate=object())  # no raise
    assert board.get_feature(fid)["board_state"] == "in_progress"  # the sweep re-dispatches it
    assert any("not recorded" in r.getMessage() for r in caplog.records)


# ── 7. an empty delivery is refused; an empty reply is a failed dispatch ──────────────


@requires_br
def test_an_empty_delivery_is_refused(board, monkeypatch):
    fid = _in_progress(board)
    with pytest.raises(BoardError, match="nothing to deliver"):
        board.record_delivery(fid, text="   ")
    out = _tools(board, monkeypatch)["board_deliver"].invoke({"feature_id": fid, "text": ""})
    assert out.startswith("Error: ") and "nothing to deliver" in out
    assert board.get_feature(fid)["board_state"] == "in_progress" and _row(board, fid)["delivered"] is False


@requires_br
async def test_an_empty_reply_is_a_failed_dispatch_not_a_delivery(board, monkeypatch):
    fid = _in_progress(board, assignee="quinn")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)

    async def _empty(delegate, prompt, *, timeout=None):
        return "  \n"

    monkeypatch.setattr(coder_seam, "dispatch_task", _empty)
    await BoardLoop({"coder": "proto"})._drive_task(board.get_feature(fid), delegate=object())

    card = board.get_feature(fid)
    assert card["board_state"] == "blocked" and "empty reply" in card["blocked_reason"]
    assert card["delivered"] is False

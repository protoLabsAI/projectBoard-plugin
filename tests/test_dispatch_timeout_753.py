"""#753 (last slice): a coder timeout leaves the lead something to work from.

When a dispatch runs past its bound, the drive records the timed-out worktree in the
card's work-location ledger BEFORE it discards the tree or blocks/parks the card, and the
block reason names that tree and carries a bounded tail of the coder's last output (from
the progress ring buffer). The reason's leading ``coder timed out after …`` text is
unchanged, so failure classification is the same as before. Recording is best-effort: a
store that refuses it never changes what the drive does with the card.
"""

from __future__ import annotations

import logging
import subprocess
import shutil

import pytest

from project_board import coder_seam, failures, worktree, work_locations, work_location_sweep
from project_board.loop import drive as drive_mod

from test_timeout_decompose_378 import _CARD, _SplitStore, _TIMEOUT, _blocks, _timing_out_board


class _LedgerStore(_SplitStore):
    """A _SplitStore with a work-location ledger; `fail=True` makes recording raise."""

    def __init__(self, tiers=(), *, answer=None, fail=False):
        super().__init__(tiers, answer=answer)
        self.locations: dict[str, list[dict]] = {}
        self._fail = fail

    def record_work_location(self, fid, entry):
        self.calls.append(("record_work_location", fid, dict(entry)))
        if self._fail:
            raise RuntimeError("ledger is down")
        entries = self.locations.setdefault(fid, [])
        entries[:] = [e for e in entries if e["id"] != entry["id"]]
        entries.append(work_locations.validate(entry))
        return self.list_work_locations(fid)

    def list_work_locations(self, fid):
        return {"entries": list(self.locations.get(fid, [])), "errors": [], "history": []}


def _git_tree(tmp_path):
    """A real worktree on disk with one commit, so HEAD is readable."""
    wt = tmp_path / "feat-bd-1"
    wt.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=wt, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, check=True, capture_output=True, text=True)
    return str(wt), sha.stdout.strip()


def _board(monkeypatch, store, cfg, wt, *, thought="measuring", answer="partial result: 41 of 60 rows done"):
    """`_timing_out_board`, but the tree is `wt` and the coder leaves a thought/answer in
    the ring buffer before it is killed. Every `_discard_tree` is logged on `store.calls`."""
    loop, dispatched = _timing_out_board(monkeypatch, store, cfg)

    async def _create(repo, base, fid, root, title="", **_kw):
        return (wt, "feat/" + fid)

    async def _dispatch(coder, wt_, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(coder)
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Bash", "id": "t1", "input": {}})
        if thought:
            coder_seam.progress_thought("bd-1", 1, thought)
        if answer:
            coder_seam.progress_answer("bd-1", 1, answer)
        raise worktree.CoderTimeout(_TIMEOUT)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    real_discard = loop._discard_tree

    async def _discard(store_, fid, *a, **kw):
        store.calls.append(("discard_tree", fid))
        return await real_discard(store_, fid, *a, **kw)

    monkeypatch.setattr(loop, "_discard_tree", _discard)
    return loop, dispatched


# ── r1: the worktree is recorded, before it is discarded ─────────────────────────────


async def test_a_timeout_records_the_worktree_before_the_tree_is_discarded(monkeypatch, tmp_path):
    wt, sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)
    await loop._drive(_CARD)

    entries = store.list_work_locations("bd-1")["entries"]
    (entry,) = [e for e in entries if e["path"] == wt]
    assert entry["id"] == drive_mod.TIMEOUT_WORKTREE_LOCATION_ID
    assert "timed out" in entry["purpose"]
    assert entry["branch"] == "feat/bd-1" and entry["sha"] == sha
    names = store.names()
    assert names.index("record_work_location") < names.index("flag_blocked")
    assert names.index("record_work_location") < names.index("discard_tree")


async def test_a_timeout_that_climbs_a_tier_records_its_worktree_too(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore(tiers=["reasoning"])
    loop, dispatched = _board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}}, wt)
    await loop._drive(_CARD)

    assert store.escalated, "the first timeout still climbs"
    assert [e["path"] for e in store.list_work_locations("bd-1")["entries"]][:1] == [wt]


async def test_no_tree_on_disk_records_nothing(monkeypatch, tmp_path):
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, str(tmp_path / "gone"))
    await loop._drive(_CARD)
    assert "record_work_location" not in store.names()
    assert _blocks(store), "the card still blocks"


# ── r2: the block reason carries the tree and a bounded output tail ──────────────────


async def test_the_block_reason_names_the_tree_and_the_coders_last_output(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)
    await loop._drive(_CARD)

    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "transient"  # the ordinary first-timeout block, as before
    assert "coder timed out after 1800s" in reason
    assert wt in reason
    assert "partial result: 41 of 60 rows done" in reason and "measuring" in reason and "Bash" in reason
    first = reason.splitlines()[0]
    assert wt not in first and "partial result" not in first, "the reason's first line is unchanged"
    assert failures.classify(reason).category == failures.classify(first).category


async def test_a_parked_card_reason_names_the_tree_and_the_tail(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    store.budgets["bd-1:timeout"] = 1  # this timeout reaches the too-wide threshold
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)
    await loop._drive(_CARD)

    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "too-wide" and "timed out 2x" in reason and "bd-split" in reason
    assert "coder timed out after" in reason and wt in reason and "41 of 60 rows" in reason
    names = store.names()
    assert names.index("record_work_location") < names.index("flag_blocked")


async def test_the_tail_is_capped(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    long_answer = "x" * 5000 + "THE-END"
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt, thought="t" * 3000, answer=long_answer)
    await loop._drive(_CARD)

    (_, _, reason, _cat) = _blocks(store)[-1]
    tail = coder_seam.timeout_output_tail("bd-1")
    assert len(tail) <= coder_seam.TIMEOUT_TAIL_CAP
    assert tail.endswith("THE-END") and tail in reason
    assert reason.count("x") <= coder_seam.TIMEOUT_TAIL_CAP


def test_the_tail_comes_from_the_timed_out_gen():
    """A max-mode sibling that failed fast must not speak for the candidate that timed out."""
    fid = "bd-tail"
    coder_seam.progress_new_run(fid)
    coder_seam.progress_begin(fid, 1, "smart")
    coder_seam.progress_answer(fid, 1, "the slow candidate's answer")
    coder_seam.progress_stop_reason(fid, 1, f"{coder_seam.TIMED_OUT_REASON} after 10s")
    coder_seam.progress_end(fid, 1)
    coder_seam.progress_begin(fid, 2, "smart")
    coder_seam.progress_answer(fid, 2, "the fast sibling's answer")
    coder_seam.progress_end(fid, 2)
    tail = coder_seam.timeout_output_tail(fid)
    assert "slow candidate" in tail and "fast sibling" not in tail
    assert coder_seam.timeout_output_tail("bd-nothing-here") == ""


# ── r3: a ledger failure never changes what happens to the card ──────────────────────


async def test_a_failed_record_is_logged_and_the_card_still_blocks(monkeypatch, tmp_path, caplog):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore(fail=True)
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)
    with caplog.at_level(logging.WARNING):
        await loop._drive(_CARD)

    assert "record_work_location" in store.names()
    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "transient" and "coder timed out after" in reason and wt in reason
    assert any("could not record its timed-out worktree" in r.getMessage() for r in caplog.records)


async def test_a_failed_record_still_escalates(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore(tiers=["reasoning"], fail=True)
    loop, dispatched = _board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}}, wt)
    await loop._drive(_CARD)
    assert store.escalated and dispatched[:2] == ["a", "b"]


@pytest.mark.parametrize("discarded", [True, False])
async def test_timeout_location_is_retired_only_when_discarded(monkeypatch, tmp_path, discarded):
    wt, sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)

    async def discard(repo, path, branch, **kwargs):
        # The live entry must exist before cleanup starts.
        (entry,) = store.list_work_locations("bd-1")["entries"]
        assert entry["path"] == wt and not entry.get("retired")
        if discarded:
            shutil.rmtree(path)
        return discarded, None

    monkeypatch.setattr(worktree, "discard_worktree", discard)
    await loop._drive(_CARD)
    (entry,) = store.list_work_locations("bd-1")["entries"]
    assert bool(entry.get("retired")) is discarded
    assert entry["path"] == wt and entry["sha"] == sha
    assert entry["branch"] == "feat/bd-1"

    if discarded:
        monkeypatch.setattr(store, "list_features", lambda: [{"id": "bd-1", "board_state": "blocked"}])
        monkeypatch.setattr(work_location_sweep, "approved_roots", lambda: [tmp_path])
        monkeypatch.setattr(
            work_location_sweep, "probe", lambda *a, **kw: pytest.fail("discarded tree must not be probed")
        )
        notices = []
        work_location_sweep.Sweep().check(store, lambda *a, **kw: notices.append(a))
        assert notices == []


# ── every removal or rebuild of the timed-out tree retires its ledger entry ──────────
#
# The one helper `_retire_timeout_worktree` runs on the terminal `_discard_tree`, on the
# fresh-build removal a tier climb or provider-stall retry goes through, and on the
# rebuild that sets a kept tree aside (the retry after an unblock). After each, no LIVE
# `timeout-worktree` entry may still point at the removed (or about-to-be-reused) path,
# or the health sweep probes it and pages a false recovery alert.


def _live(store, wt):
    return [
        e
        for e in store.locations.get("bd-1", [])  # the raw ledger, not the (patchable) read
        if e["id"] == drive_mod.TIMEOUT_WORKTREE_LOCATION_ID and e["path"] == wt and not e.get("retired")
    ]


def _rebuilds_watched(monkeypatch, store, wt, tmp_path):
    """Make the tree really leave on removal, and snapshot the live entries each time the
    drive (re)creates it — i.e. after the previous attempt's tree was removed."""
    seen: list[list[dict]] = []
    created = [0]

    async def _remove(repo, path, branch="", **_kw):
        shutil.rmtree(path, ignore_errors=True)

    async def _create(repo, base, fid, root, title="", **_kw):
        if created[0]:
            seen.append(_live(store, wt))
            _git_tree(tmp_path)  # the rebuild reuses the same path
        created[0] += 1
        return (wt, "feat/" + fid)

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "create_worktree", _create)
    return seen


def _terminal_discard_removes(monkeypatch):
    async def discard(repo, path, branch, **kwargs):
        shutil.rmtree(path, ignore_errors=True)
        return True, None

    monkeypatch.setattr(worktree, "discard_worktree", discard)


async def test_a_tier_climb_rebuild_retires_the_timed_out_tree(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore(tiers=["reasoning"])
    loop, dispatched = _board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}}, wt)
    seen = _rebuilds_watched(monkeypatch, store, wt, tmp_path)
    _terminal_discard_removes(monkeypatch)
    await loop._drive(_CARD)

    assert store.escalated and dispatched[:2] == ["a", "b"]
    assert seen and seen[0] == [], "the climb's rebuild left a live entry on the removed tree"
    assert _live(store, wt) == []
    assert store.list_work_locations("bd-1")["entries"], "the evidence is kept, retired"


async def test_a_provider_stall_retry_retires_the_timed_out_tree(monkeypatch, tmp_path):
    from test_provider_stall_585 import _Clock

    clock = _Clock()
    monkeypatch.setattr(coder_seam, "_monotonic", clock)
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, dispatched = _board(monkeypatch, store, {"coder": "proto", "decompose_after_timeouts": 1}, wt)
    notes = []
    monkeypatch.setattr(store, "comment", lambda fid, text: notes.append(text), raising=False)

    async def _dispatch(coder, wt_, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(coder)
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        clock.advance(600)
        raise worktree.CoderTimeout(_TIMEOUT)

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    seen = _rebuilds_watched(monkeypatch, store, wt, tmp_path)
    _terminal_discard_removes(monkeypatch)
    await loop._drive(_CARD)

    assert any(n.startswith("provider_stall:") for n in notes), "the same-tier stall retry ran"
    assert len(dispatched) >= 2 and seen and all(s == [] for s in seen)
    assert _live(store, wt) == []


async def test_a_retry_rebuild_over_a_kept_tree_retires_it(monkeypatch, tmp_path):
    """The retry after a block whose tree was KEPT (its removal failed): the next drive's
    fresh build sets that tree aside, and its live timeout entry is retired then."""
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)

    async def keep(repo, path, branch, **kwargs):
        return False, None  # the terminal discard could not remove it

    monkeypatch.setattr(worktree, "discard_worktree", keep)
    await loop._drive(_CARD)
    assert len(_live(store, wt)) == 1, "a tree kept for recovery stays live"

    async def set_aside(repo, root, fid, *, base=""):
        shutil.rmtree(wt, ignore_errors=True)
        rec = worktree.PreservedTree(wt, "feat/bd-1", "stranded/x", "abc1234", "1 commit", "", removed=True)
        return [rec], []

    async def note_preserved(*a, **kw):
        return None

    monkeypatch.setattr(worktree, "set_aside_stranded_worktrees", set_aside)
    monkeypatch.setattr(loop, "_note_preserved", note_preserved)
    seen = []

    async def _create(repo, base, fid, root, title="", **_kw):
        seen.append(_live(store, wt))
        raise worktree.WorktreeError("stop after the rebuild's set-aside")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    await loop._drive(_CARD)
    assert seen == [[]], "the retry's rebuild left a live entry on the set-aside tree"


async def test_a_terminal_discard_retires_the_timed_out_tree(monkeypatch, tmp_path):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore()
    loop, _ = _board(monkeypatch, store, {"coder": "proto"}, wt)
    _terminal_discard_removes(monkeypatch)
    await loop._drive(_CARD)

    assert _blocks(store), "the card blocks"
    assert _live(store, wt) == []
    (entry,) = store.list_work_locations("bd-1")["entries"]
    assert entry["retired"] is True and entry["path"] == wt


async def test_a_ledger_that_cannot_retire_never_breaks_the_rebuild(monkeypatch, tmp_path, caplog):
    wt, _sha = _git_tree(tmp_path)
    store = _LedgerStore(tiers=["reasoning"])
    loop, dispatched = _board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}}, wt)
    _rebuilds_watched(monkeypatch, store, wt, tmp_path)
    monkeypatch.setattr(store, "list_work_locations", lambda fid: (_ for _ in ()).throw(RuntimeError("down")))
    with caplog.at_level(logging.WARNING):
        await loop._drive(_CARD)
    assert dispatched[:2] == ["a", "b"], "the climb still re-dispatched"
    assert any("could not retire its timed-out worktree" in r.getMessage() for r in caplog.records)

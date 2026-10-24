"""#585: a long provider silence has its own durable, same-tier retry budget.

The idle gap is mined from the PROGRESS RING BUFFER (``coder_seam``), not an "age since
last tool event" baked into the timeout message: the plugin's own seam raises a plain
``coder timed out after Ns`` string, so a test that wrote the age into the message by
hand would exercise a path production never takes. Every drive test here goes through
the REAL ``dispatch_coder_tapped`` → progress-buffer lifecycle and advances a controlled
monotonic clock, so the gap the drive reads back is exactly what a stalled backend — a
tool call, then a long silence, then the kill — would record.
"""

import pytest

from project_board import coder_seam, failures, worktree
from project_board.loop import BoardLoop
from project_board.loop.drive import _timeout_idle_s
from test_timeout_decompose_378 import _CARD, _SplitStore, _blocks, _timing_out_board


class _Clock:
    """A monkeypatched ``coder_seam._monotonic`` the tests advance by hand, so a gen's
    recorded idle gap (``last_event`` → ``ended``) is deterministic."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("age since last tool event: 600s", 600),
        ("last tool event 601.5 seconds ago", 601.5),
        ("last tool event age=0s", 0),
        ("coder timed out after 1800s", None),
        ("age since last tool event: -600s", None),
        ("age since last tool event: nans", None),
        ("age since last tool event: 600ms", None),
        ("age since last tool event: 1e3s", None),
        ("age since last tool event: " + "9" * 400 + "s", None),
    ],
)
def test_message_age_fallback_parser(text, expected):
    # Only the FALLBACK source — a host that pre-formatted the age into its message.
    assert _timeout_idle_s(text) == expected


def test_stall_is_transient():
    policy = failures.classify(failures.PROVIDER_STALL_CLASS)
    assert policy.category == "provider_stall"
    assert policy.retryable


# ── the ring buffer is the real source, and the drive reads it ───────────────────────


def _stall_gen(monkeypatch, fid, gap):
    """Drive one gen through the real progress lifecycle: begin, a tool call, `gap`
    seconds of silence, then the timeout stamp + close. Returns the clock."""
    clock = _Clock()
    monkeypatch.setattr(coder_seam, "_monotonic", clock)
    coder_seam.progress_new_run(fid)
    coder_seam.progress_begin(fid, 1, "reasoning")
    coder_seam.progress_tool(fid, 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
    clock.advance(gap)
    coder_seam.progress_stop_reason(fid, 1, f"{coder_seam.TIMED_OUT_REASON} after 1800s")
    coder_seam.progress_end(fid, 1)
    return clock


def test_ring_buffer_records_the_silence(monkeypatch):
    _stall_gen(monkeypatch, "bd-ring", 777)
    gen = coder_seam.progress_snapshot("bd-ring")["gens"][-1]
    # Silence since the last tool event, NOT total runtime — the two differ here.
    assert gen["idle_s"] == 777
    assert gen["elapsed_s"] == 777


def test_drive_reads_the_gap_from_the_buffer(monkeypatch):
    _stall_gen(monkeypatch, "bd-ring", 900)
    loop = BoardLoop({"coder": "proto"})
    exc = worktree.CoderTimeout("coder timed out after 1800s")  # plain — no age in the text
    assert loop._timeout_idle_gap("bd-ring", exc) == 900
    # An evicted / untapped buffer with a plain message is UNKNOWN, never a false 0.
    assert loop._timeout_idle_gap("bd-gone", exc) is None
    # …but a host that DID bake the age in is still honoured as a fallback.
    baked = worktree.CoderTimeout("timed out; last tool event 450s ago")
    assert loop._timeout_idle_gap("bd-gone", baked) == 450


# ── the drive branch, end to end through the real dispatch seam ──────────────────────


def _board(monkeypatch, *, gap=600.0, cfg=None, worked=True, blind=False):
    """A drive whose dispatch records a tool event then goes silent for ``gap`` seconds
    before timing out — the provider-stall shape, read back from the ring buffer.

    ``worked=False`` reaches no model at all (the pre-model infra timeout); ``blind``
    hides the ring buffer from the drive (an untapped host / evicted buffer) so the gap
    is genuinely unknown.
    """
    clock = _Clock()
    monkeypatch.setattr(coder_seam, "_monotonic", clock)
    store = _SplitStore(tiers=["reasoning"])
    loop, dispatched = _timing_out_board(
        monkeypatch,
        store,
        {"coders": {"smart": "a", "reasoning": "b"}, "decompose_after_timeouts": 1, **(cfg or {})},
    )
    notes = []
    monkeypatch.setattr(store, "comment", lambda fid, text: notes.append(text), raising=False)
    if blind:
        monkeypatch.setattr(coder_seam, "progress_snapshot", lambda fid: {"gens": []})

    async def dispatch(coder, wt, prompt, **kwargs):
        dispatched.append(coder)
        if worked:
            coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        clock.advance(gap)
        raise worktree.CoderTimeout("coder timed out after 1800s")

    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    return loop, store, dispatched, notes


async def test_stall_retries_same_tier_without_counting_or_parking(monkeypatch):
    loop, store, dispatched, notes = _board(monkeypatch)
    original = worktree.dispatch_coder

    async def dispatch(*args, **kwargs):
        if dispatched:
            assert args[0] == dispatched[0] == "a"  # same tier's coder, re-dispatched
            assert store.budgets == {"bd-1:provider-stall": 1}
            assert not store.asked and not _blocks(store) and not store.escalated
            return "built it"
        return await original(*args, **kwargs)

    async def open_pr(*args, **kwargs):
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    await loop._drive(_CARD)
    assert len(notes) == 1 and notes[0].startswith("provider_stall:")
    assert "last event 600s before timeout" in notes[0]
    assert not store.escalated and not store.asked and not _blocks(store)
    assert "bd-1:provider-stall" not in store.budgets  # reset once the build reached review


@pytest.mark.parametrize("spent, expected", [(0, 3), (1, 2), (2, 1)])
async def test_budget_bounds_retries_then_falls_through(monkeypatch, spent, expected):
    loop, store, dispatched, notes = _board(monkeypatch)
    store.budgets["bd-1:provider-stall"] = spent  # durable count survives a restart
    await loop._drive(_CARD)
    assert dispatched == ["a"] * expected
    assert store.budgets == {"bd-1:provider-stall": 2, "bd-1:timeout": 1}
    assert len(notes) == expected - 1
    assert store.asked == [("bd-1", 1)]  # once spent, the ordinary too-wide park runs
    assert _blocks(store)[-1][3] == "too-wide"
    assert "last event 600s before timeout" in _blocks(store)[-1][2]


async def test_short_gap_counts_toward_too_wide(monkeypatch):
    # A coder killed mid-work (short silence) is a SIZE signal, parked as before.
    loop, store, dispatched, notes = _board(monkeypatch, gap=120)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes
    assert store.budgets == {"bd-1:timeout": 1}
    assert store.asked == [("bd-1", 1)]
    assert _blocks(store)[-1][3] == "too-wide"
    assert "last event 120s before timeout" in _blocks(store)[-1][2]


async def test_unknown_gap_behaves_exactly_as_before(monkeypatch):
    # No readable idle signal at all → the ordinary too-wide path, no gap in the reason.
    loop, store, dispatched, notes = _board(monkeypatch, blind=True)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes
    assert store.budgets == {"bd-1:timeout": 1}
    assert store.asked == [("bd-1", 1)]
    assert _blocks(store)[-1][3] == "too-wide"
    assert "before timeout" not in _blocks(store)[-1][2]


@pytest.mark.parametrize("cfg", [{"provider_stall_idle_s": 601}, {"provider_stall_max": 0}])
async def test_config_controls_stall_retry(monkeypatch, cfg):
    # idle_s=601 makes a 600s gap too short; max=0 disables the retry outright.
    loop, store, dispatched, notes = _board(monkeypatch, cfg=cfg)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes
    assert store.asked == [("bd-1", 1)]


async def test_pre_model_guard_still_blocks_with_gap_reason(monkeypatch):
    # A timeout that never reached the model is infra triage, not a stall — but the
    # known gap still names itself in the block reason.
    loop, store, dispatched, notes = _board(monkeypatch, worked=False)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes and not store.budgets
    assert _blocks(store)[-1][3] == "dispatch-infra"
    assert "last event 600s before timeout" in _blocks(store)[-1][2]


async def test_ordinary_block_reason_includes_known_gap(monkeypatch):
    # No ladder, no decompose: a sub-threshold gap still rides the terminal block.
    loop, store, dispatched, notes = _board(
        monkeypatch, gap=42, cfg={"coder": "proto", "coders": {}, "decompose_after_timeouts": 0}
    )
    await loop._drive(_CARD)
    assert len(dispatched) == 1 and not notes and not store.asked
    assert store.budgets == {"bd-1:timeout": 1}
    assert _blocks(store)[-1][3] == "transient"
    assert "last event 42s before timeout" in _blocks(store)[-1][2]

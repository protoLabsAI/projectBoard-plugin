"""#422: an ACP adapter's own chatter is not evidence that the model ran.

#339's guard blocks a pre-model dispatch failure for triage instead of climbing the
ladder, and decides "pre-model" from what the dispatch left in the live-monitor ring
buffer. Streamed answer text used to count. But codex-acp streams its OWN warning on
the same `agent_message_chunk` channel before the model is ever called — probed
against codex-acp on 2026-09-10:

    gpt-5.6-sol:  agent_message_chunk "Model metadata for `gpt-5.6-sol` not found. …"
                  available_commands_update
                  → PROMPT ERROR 400 "… requires a newer version of Codex"
    gpt-5.5:      available_commands_update
                  agent_message_chunk "OK"
                  usage_update {used: 20835, size: 258400}     ← only after a real reply

So a failure seconds after session start read as model work and the card climbed a tier
(bd-ojsd, 2026-08-31: refused 3.5s after the adapter came up, then "escalating
smart→reasoning" on a build that carried the guard). #421 now classifies provider
REFUSALS from the message alone; every other pre-model failure still rides this
evidence, and those are what these tests drive.
"""

from __future__ import annotations

from project_board import coder_seam, worktree

from test_loop import FEATURE, _ladder_drive_env

# Verbatim from the probe: what codex-acp says before it calls a model it has no
# metadata for.
_CHATTER = (
    "Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback metadata; this can degrade "
    "performance and cause issues."
)
# A credential the provider rejects before any model runs — the shape codex-acp gives a
# provider error (JSON-RPC -32603 wrapping the HTTP status). Classifies as `auth`, which
# is NOT a provider-rotation class, so it reaches #339's evidence check.
_EXPIRED = (
    'coder dispatch failed: Internal error (JSON-RPC -32603): {"message": "unexpected status '
    '401 Unauthorized: Provided authentication token is expired. Please try signing in again."}'
)


def _blocks(store):
    return [c for c in store.calls if c[0] == "flag_blocked"]


async def test_adapter_chatter_before_a_pre_model_failure_blocks_instead_of_climbing(monkeypatch):
    """The #422 trace, on a failure #421's message rule does not cover: the adapter talks,
    then the provider refuses the call. Nothing a stronger model does can change that, so
    it must block for triage under `dispatch-infra` — ONE dispatch, ladder never consulted
    — not climb because the adapter's warning looked like a reply."""
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        coder_seam.progress_answer("bd-1", 1, _CHATTER)  # the on_text callback, as the tap wires it
        raise worktree.WorktreeError(_EXPIRED)

    loop, store = _ladder_drive_env(monkeypatch, _dispatch, tiers=["reasoning"])
    await loop._drive(FEATURE)

    assert len(dispatches) == 1, "a pre-model failure must not be re-dispatched at a stronger tier"
    assert store.escalated == [], "the adapter's own warning was read as model work"
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra", blocked
    assert "401 Unauthorized" in blocked[0][2]  # the operator gets the real evidence
    assert loop._inflight == {}


async def test_adapter_chatter_then_a_hang_is_a_pre_first_token_timeout(monkeypatch):
    """The same chatter ahead of a model call that never answers: the watchdog fires with
    no model activity at all — a wedged session, not an oversized card or a weak model.
    It blocks as infra; it must not climb on the strength of the warning."""

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        coder_seam.progress_answer("bd-1", 1, _CHATTER)
        raise worktree.CoderTimeout("coder timed out after 1800s")

    loop, store = _ladder_drive_env(monkeypatch, _dispatch, tiers=["reasoning"])
    await loop._drive(FEATURE)

    assert store.escalated == []
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra", blocked


async def test_a_turn_that_really_reached_the_model_still_climbs(monkeypatch):
    """The guard must not over-block. The same chatter and the same failure, but the model
    had started working — it thought before the call died. That is model-reachable, so
    the card stays on the ladder exactly as before."""
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        coder_seam.progress_answer("bd-1", 1, _CHATTER)
        coder_seam.progress_thought("bd-1", 1, "The failing test is in test_loop.py; start there.")
        raise worktree.WorktreeError(_EXPIRED)

    loop, store = _ladder_drive_env(monkeypatch, _dispatch, tiers=["reasoning"])
    await loop._drive(FEATURE)

    assert [e[0] for e in store.escalated][:1] == ["bd-1"], "a model-reachable failure must climb"
    assert len(dispatches) == 2  # the climbed tier got its dispatch
    assert all(c[3] != "dispatch-infra" for c in _blocks(store))


def test_only_model_produced_signals_count_as_reaching_the_model():
    """The evidence rule on its own: text is ambiguous (either party can send it); a tool
    call, a thought, and token usage are the model's alone."""
    coder_seam._progress.clear()
    coder_seam.progress_new_run("f")
    coder_seam.progress_begin("f", 1, "smart")
    coder_seam.progress_answer("f", 1, _CHATTER)
    assert coder_seam.dispatch_reached_model("f") is False

    coder_seam.progress_usage("f", 1, {"used": 20835, "size": 258400})
    assert coder_seam.dispatch_reached_model("f") is True

    for signal in (
        lambda: coder_seam.progress_thought("f", 1, "reading the spec"),
        lambda: coder_seam.progress_tool("f", 1, {"phase": "start", "id": "t1", "name": "Read"}),
    ):
        coder_seam.progress_new_run("f")
        coder_seam.progress_begin("f", 1, "smart")
        coder_seam.progress_answer("f", 1, _CHATTER)
        signal()
        assert coder_seam.dispatch_reached_model("f") is True

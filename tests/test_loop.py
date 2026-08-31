"""Loop tests — config parsing, the coder prompt, and the drive state machine.

``_drive`` is the only thing that moves a feature forward (``done`` is the merge
webhook's job). These tests stub the store (``loop.get_store``), the worktree
helpers (``worktree.create_worktree`` / ``dispatch_coder`` / ``open_pr`` /
``remove_worktree``), and the delegate lookup, then assert the transitions: a
clean build → ``open_review``; an empty diff with a single coder → ``flag_blocked``;
an unconfigured coder → ``flag_blocked`` before any worktree is created.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
import types

import pytest

from project_board import coder_seam
from project_board import store as store_mod
from project_board import worktree
import project_board.loop as loop_mod
from project_board.loop import (
    _MERGED_VERIFIED_SHA_LEN,
    _REVIEW_FINDINGS_TITLE,
    BoardLoop,
    _ci_failure_reason,
    _inject_source_issue_line,
    _issue_closed_by_board_sibling,
    _no_test_marker,
    _pr_body,
    _requirement_gate_diag_line,
    _requirement_gate_diagnostics,
    _resolve_gate_cmd,
    _source_issue,
    _source_issue_still_open,
)
from project_board.failures import classify
from project_board.retro import classify as retro_classify
from project_board.store import BeadsBoard, BoardError


class FakeLoopStore:
    def __init__(self):
        self.calls = []
        self.gens_spent = {}  # fid -> cumulative gens (record_gens_spent)
        self.features = []  # rows list_features() returns (the #253 sibling scan reads state="done")

    def current_tier(self, fid):
        return "fast"

    def list_features(self, state=None, include_archived=False):
        rows = self.features
        if state is not None:
            rows = [f for f in rows if f.get("board_state") == state]
        return list(rows)

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        return {"id": fid}

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", fid, reason, category))
        return {"id": fid}

    def record_attempt(self, fid, *, tier, outcome):
        self.calls.append(("record_attempt", fid, tier, outcome))
        return {"id": fid}

    def record_gens_spent(self, fid, n):
        self.gens_spent[fid] = self.gens_spent.get(fid, 0) + n
        return {"id": fid}

    def record_verified_candidate(self, fid, *, branch, sha, worktree):
        self.calls.append(("record_verified", fid, branch, sha, worktree))
        return {"id": fid}

    def clear_verified_candidate(self, fid):
        self.calls.append(("clear_verified", fid))
        return {"id": fid}

    def cancel_feature(self, fid, reason=""):
        self.calls.append(("cancel_feature", fid, reason))
        return {"id": fid}

    def set_requirements(self, fid, items):
        self.calls.append(("set_requirements", fid, items))
        return {"id": fid, "requirements": items}

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))
        return {"id": fid}

    def names(self):
        return [c[0] for c in self.calls]


FEATURE = {
    "id": "bd-1",
    "title": "Add a thing",
    "repo": "/repo",
    "base_branch": "main",
    "spec": "do the thing",
    "design": "",
    "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
    "files_to_modify": ["a.py", "b.py"],
}


async def _noop_coro():
    """Async no-op — used to patch BoardLoop._run in startup-log tests."""


# ── config parsing ──────────────────────────────────────────────────────────────


def test_config_defaults():
    loop = BoardLoop({})
    # v0.42.0: NO default coder — unset is "" (a setup-preflight gap), not "proto".
    assert loop.coder_name == "" and loop.reviewer_name == "quinn"
    assert loop.review_dispatch is False
    assert loop.interval == 30 and loop.enabled is False
    assert loop.escalation_on is False  # no coders map → single-coder mode
    assert loop.max_concurrent == 1  # serial by default
    assert loop.merge_poll is True and loop.merge_poll_interval == 60


def test_escalation_on_with_two_distinct_coders():
    loop = BoardLoop({"coders": {"fast": "proto", "smart": "proto-smart"}})
    assert loop.escalation_on is True


def test_max_concurrent_floors_at_one():
    assert BoardLoop({"max_concurrent": 0}).max_concurrent == 1
    assert BoardLoop({"max_concurrent": 4}).max_concurrent == 4


# ── reload(): the live concurrency knobs land on the RUNNING loop ────────────────


class _HostConfig:
    """Shape of what the host hands a surface's reload hook: a LangGraphConfig whose
    `.plugin_config[section]` is the resolved (manifest defaults ⊕ YAML) section."""

    def __init__(self, section):
        self.plugin_config = {"project_board": section}


def test_reload_applies_every_live_knob_and_returns_the_diff():
    loop = BoardLoop({"max_concurrent": 1, "max_pending_reviews": 5, "max_concurrent_sessions": 0})
    changed = loop.reload(_HostConfig({"max_concurrent": 6, "max_pending_reviews": 12, "max_concurrent_sessions": 2}))
    assert changed == {"max_concurrent": (1, 6), "max_pending_reviews": (5, 12), "max_concurrent_sessions": (0, 2)}
    assert (loop.max_concurrent, loop.max_pending_reviews, loop.max_concurrent_sessions) == (6, 12, 2)
    # Idempotent: the same payload again is a no-op diff.
    assert (
        loop.reload(_HostConfig({"max_concurrent": 6, "max_pending_reviews": 12, "max_concurrent_sessions": 2})) == {}
    )


def test_reload_accepts_a_bare_section_dict_or_a_wrapped_one():
    loop = BoardLoop({})
    assert loop.reload({"max_concurrent": 3}) == {"max_concurrent": (1, 3)}
    assert loop.reload({"project_board": {"max_concurrent": 4}}) == {"max_concurrent": (3, 4)}
    assert loop.reload(None) == {} and loop.reload("nonsense") == {}


def test_reload_floors_like_the_constructor_and_keeps_bad_values(caplog):
    """A Settings typo must not stall the loop: a non-integer keeps the current knob
    (with a warning); a below-floor value floors exactly as construction does; a
    payload missing the key leaves that knob alone."""
    loop = BoardLoop({"max_concurrent": 2, "max_pending_reviews": 5})
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert loop.reload({"max_concurrent": "lots"}) == {}
    assert loop.max_concurrent == 2
    assert "is malformed" in caplog.text
    assert loop.reload({"max_concurrent": 0, "max_pending_reviews": -3}) == {
        "max_concurrent": (2, 1),
        "max_pending_reviews": (5, 0),
    }
    assert loop.reload({"max_pending_reviews": 7}) == {"max_pending_reviews": (0, 7)}
    assert loop.max_concurrent == 1  # untouched by a payload without the key


def test_reload_flips_auto_merge_and_rejects_garbage():
    loop = BoardLoop({})
    assert loop.auto_merge is False
    assert loop.reload({"auto_merge": True}) == {"auto_merge": (False, True)}
    assert loop.reload({"auto_merge": "false"}) == {"auto_merge": (True, False)}  # YAML string spelling
    assert loop.reload({"auto_merge": "maybe"}) == {}  # malformed → keep
    assert loop.auto_merge is False


def test_reload_only_touches_live_knobs():
    loop = BoardLoop({"coder_timeout_s": 1800, "loop_interval_s": 30, "coders": {"smart": "a"}})
    assert loop.reload({"coder_timeout_s": 5, "loop_interval_s": 1, "coders": {"smart": "b"}}) == {}
    assert loop.coder_timeout == 1800 and loop.interval == 30 and loop.coders == {"smart": "a"}


def test_reload_applies_coder_live_and_keeps_cfg_in_step():
    """v0.42.0 (review on #212): `coder` is a live string knob — the drive resolves
    `self.coder_name` per attempt, and the setup preflight reads `self.cfg`, so a
    Settings save that names the coder clears the gap on the RUNNING loop."""
    loop = BoardLoop({"coder": ""})
    assert loop.reload({"coder": "  proto "}) == {"coder": ("", "proto")}
    assert loop.coder_name == "proto" and loop.cfg["coder"] == "proto"
    assert loop.reload({"coder": "proto"}) == {}  # steady
    assert loop.reload({"coder": ""}) == {"coder": ("proto", "")}  # can be unset live too
    assert loop.coder_name == "" and loop.cfg["coder"] == ""


def test_reload_applies_project_routing_to_loop_and_cached_store(monkeypatch):
    loop = BoardLoop({"repo": "/instance", "projects": {"old": {"repo": "/old"}}})
    loop._preflight_state["old"] = False
    loop._last_preflight["old"] = 123.0
    captured = []
    monkeypatch.setattr(loop_mod, "reconfigure_cached_store", lambda **kw: captured.append(kw) or True)

    changed = loop.reload(
        _HostConfig(
            {
                "projects": {
                    "old": {"repo": "/old"},
                    "new": {"repo": "/new", "base_branch": "develop"},
                },
                "default_project": "new",
            }
        )
    )

    assert changed == {
        "projects": (("old",), ("old", "new")),
        "default_project": ("old", "new"),
    }
    assert loop._repo_for({"project": "new"}) == "/new"
    assert loop._base_branch_for({"project": "new"}) == "develop"
    assert loop._store_kw["projects"] is loop._projects
    assert captured[-1]["projects"] is loop._projects and captured[-1]["default_project"] == "new"
    assert loop._preflight_state == {} and loop._last_preflight == {}


def test_reload_rejects_malformed_project_routing_as_one_unit(monkeypatch, caplog):
    loop = BoardLoop({"projects": {"old": {"repo": "/old"}}})
    monkeypatch.setattr(loop_mod, "reconfigure_cached_store", lambda **_kw: pytest.fail("must not mutate store"))
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert loop.reload({"projects": {"broken": {"base_branch": "main"}}}) == {}
    assert tuple(loop._projects) == ("old",)
    assert loop.cfg["projects"] == {"old": {"repo": "/old"}}
    assert "project routing is malformed" in caplog.text


def test_max_mode_n_parsing():
    assert BoardLoop({}).max_mode_n == 1  # off by default
    assert BoardLoop({"max_mode_n": 5}).max_mode_n == 5
    assert BoardLoop({"max_mode_n": 0}).max_mode_n == 1  # floors at 1 (never < 1)


# ── the coder prompt (ProtoMaker discipline: name the files, demand the diff) ────


def test_prompt_tells_the_coder_not_to_open_a_pr():
    """#207: the coder opened its own DRAFT PR before the loop's open_pr ran. The
    loop owns the PR lifecycle (title/body/ready/merge) — say so in the Rules."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "do NOT open a PR (draft or otherwise) — the loop opens it" in prompt


def test_build_prompt_is_imperative_and_lists_the_files():
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Add a thing" in prompt
    assert "do the thing" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt
    assert "WHEN x THE SYSTEM SHALL y" in prompt
    assert "make all the edits here, now" in prompt.lower()


def test_build_prompt_requires_tests():
    """The coder's definition of done includes writing tests — the #897 lesson:
    a feature merged testless because nothing in the prompt or gate mandated it."""
    prompt = BoardLoop({})._build_prompt(FEATURE).lower()
    assert "automated tests" in prompt
    assert "definition of done" in prompt
    assert "rejected before the pr opens" in prompt


def test_build_prompt_asks_for_a_clean_pr_summary_not_raw_reasoning():
    """The coder's reply feeds the PR body through `_pr_body`, which keeps only the
    `## Summary` section — so the prompt must ask for one explicitly, or every PR
    falls back to the bare no-summary template."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "final message becomes the pr description" in prompt.lower()
    assert "do not narrate your process" in prompt.lower()


def test_queue_review_feedback_reaches_the_next_prompt():
    """AC (bd-171): a /review bounce stashed via queue_review_feedback rides the SAME
    prompt path as an in-loop CI/review bounce — _build_prompt drains it into
    _ci_feedback, leads the prompt with it, and clears the one-shot pending entry."""
    from project_board.loop import _PENDING_FEEDBACK, queue_review_feedback

    _PENDING_FEEDBACK.clear()
    loop = BoardLoop({})
    queue_review_feedback("bd-1", "the auth check is missing a null guard")  # FEATURE id is bd-1
    prompt = loop._build_prompt(FEATURE)
    assert "REJECTED" in prompt  # the previous-attempt-rejected block fires
    assert "null guard" in prompt  # the findings text reached the dispatch prompt
    assert "bd-1" not in _PENDING_FEEDBACK  # drained one-shot
    assert loop._ci_feedback.get("bd-1")  # promoted into the per-run feedback lever


def test_queue_review_feedback_ignores_blank_findings():
    from project_board.loop import _PENDING_FEEDBACK, queue_review_feedback

    _PENDING_FEEDBACK.clear()
    queue_review_feedback("bd-9", "   ")
    assert "bd-9" not in _PENDING_FEEDBACK  # nothing to carry back


def test_queue_review_feedback_survives_a_plugin_reload():
    """#256: _PENDING_FEEDBACK lives on a process-stable sys.modules slot (the
    coder_seam #178 pattern), not a plain module global — a plugin reload
    re-imports loop.py as a FRESH module object while the running loop holds the
    old one, and a plain global forked into two dicts (the reloaded router wrote
    the new one, the loop drained the old: findings silently stranded). Two
    module objects, ONE dict: write through the reloaded instance, drain through
    the original's _build_prompt."""
    import importlib.util

    spec = importlib.util.find_spec("project_board.loop")
    assert spec is not None and spec.loader is not None
    reloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded)  # a second, distinct module object — the reload
    assert reloaded is not loop_mod
    assert reloaded._PENDING_FEEDBACK is loop_mod._PENDING_FEEDBACK  # shared via the slot, not copied

    loop_mod._PENDING_FEEDBACK.clear()
    reloaded.queue_review_feedback("bd-1", "the auth check is missing a null guard")
    prompt = BoardLoop({})._build_prompt(FEATURE)  # the ORIGINAL module instance drains it
    assert "REJECTED" in prompt  # the previous-attempt-rejected block fires
    assert "null guard" in prompt  # the findings crossed the module-object boundary
    assert "bd-1" not in loop_mod._PENDING_FEEDBACK  # drained one-shot…
    assert "bd-1" not in reloaded._PENDING_FEEDBACK  # …and both views agree


# ── repo standing gate files (#108) ──────────────────────────────────────────────


def test_gate_files_config_defaults_empty():
    """`project_board.gate_files` is per-repo, default empty (this repo has no
    CHANGELOG, so it declares none). Accepts a list or a comma/space string,
    de-duplicated with order preserved (mirrors env_passthrough)."""
    assert BoardLoop({}).gate_files == []
    assert BoardLoop({"gate_files": []}).gate_files == []
    assert BoardLoop({"gate_files": ["CHANGELOG.md", "docs/api.json"]}).gate_files == [
        "CHANGELOG.md",
        "docs/api.json",
    ]
    # string form: comma- or whitespace-separated, same result
    assert BoardLoop({"gate_files": "CHANGELOG.md, docs/api.json"}).gate_files == ["CHANGELOG.md", "docs/api.json"]
    assert BoardLoop({"gate_files": "CHANGELOG.md docs/api.json"}).gate_files == ["CHANGELOG.md", "docs/api.json"]
    # dedup, order preserved; blanks dropped
    assert BoardLoop({"gate_files": ["a.py", "a.py", "  ", "b.py"]}).gate_files == ["a.py", "b.py"]


def test_build_prompt_appends_repo_gate_files():
    """AC (#108): the prompt-assembly seam appends `project_board.gate_files` as a
    SEPARATE block — repo-wide obligations a tight `files_to_modify` would suppress,
    which a card author can't enumerate per repo."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md", "docs/openapi.json"]})
    prompt = loop._build_prompt(FEATURE)
    assert "Repo standing gate files" in prompt
    assert "- CHANGELOG.md" in prompt and "- docs/openapi.json" in prompt
    # the card's own files still appear — gate files are additive, not a replacement
    assert "- a.py" in prompt and "- b.py" in prompt


def test_build_prompt_omits_gate_files_block_when_none_configured():
    """Default (empty) → no block at all, so a repo that declares none (e.g. this one,
    no CHANGELOG) gets the unchanged prompt."""
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Repo standing gate files" not in prompt


def test_gate_files_are_not_ledger_items_nor_files_to_modify():
    """Composes with #113: gate files are a prompt-assembly ADDITION, not ledger items
    and not merged into the feature's `files_to_modify` — the block is distinct from
    the `## Files to create / modify` list and the `## Requirements ledger`."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md"]})
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    prompt = loop._build_prompt(feature)
    # gate file lands under its own heading, NOT under Files to create / modify
    # ([0] is the file list itself — everything up to the next `##` heading)
    files_section = prompt.split("## Files to create / modify")[1].split("##")[0]
    assert "CHANGELOG.md" not in files_section
    assert "- a.py" in files_section  # sanity: this IS the file-list section
    # the standing block does not leak into / become a ledger requirement line
    ledger_section = prompt.split("## Requirements ledger")[1]
    assert "CHANGELOG.md" not in ledger_section
    # …and the feature's own files_to_modify was never mutated by prompt assembly
    assert feature["files_to_modify"] == ["a.py", "b.py"]


# ── repo conventions (#108) ──────────────────────────────────────────────────────

_CONVENTIONS = (
    "- CI runs `ruff check . && ruff format --check .` — lint and format must pass.\n"
    "- Every PR must include a `changelog.d/<issue>.<kind>.md` fragment.\n"
    "- `plugins/docs/nav.json` is GENERATED — never edit it directly.\n"
    "- If a convention named here does not exist in the repo, STOP and say so."
)


def test_repo_conventions_config_defaults_empty():
    """`project_board.repo_conventions` is per-repo free-text markdown, default empty
    (backwards-compatible). Read verbatim — no parsing, unlike `gate_files`."""
    assert BoardLoop({}).repo_conventions == ""
    assert BoardLoop({"repo_conventions": ""}).repo_conventions == ""
    assert BoardLoop({"repo_conventions": _CONVENTIONS}).repo_conventions == _CONVENTIONS
    # a None in config coerces to "" rather than exploding on .strip() downstream
    assert BoardLoop({"repo_conventions": None}).repo_conventions == ""


def test_build_prompt_injects_repo_conventions():
    """AC (#108): when set, the conventions ride the dispatch prompt verbatim as a
    distinct `## Repo conventions` block — the standing RULES a card author can't
    restate per-card (what CI runs, required fragment formats, generated files)."""
    loop = BoardLoop({"repo_conventions": _CONVENTIONS, "gate_files": ["CHANGELOG.md"]})
    prompt = loop._build_prompt(FEATURE)
    assert "## Repo conventions" in prompt
    assert _CONVENTIONS in prompt  # injected verbatim, not reformatted
    # sits AFTER the gate-files block (its natural neighbour — both repo-wide, not per-card)
    assert prompt.index("## Repo conventions") > prompt.index("Repo standing gate files")
    # the card's own task/files are untouched — the block is additive
    assert "- a.py" in prompt and "do the thing" in prompt


def test_build_prompt_omits_repo_conventions_when_empty():
    """Default (empty/absent) → no block at all, so a repo that declares no conventions
    gets the unchanged prompt (backwards-compatible)."""
    assert "## Repo conventions" not in BoardLoop({})._build_prompt(FEATURE)
    # whitespace-only is treated as empty too
    assert "## Repo conventions" not in BoardLoop({"repo_conventions": "   \n  "})._build_prompt(FEATURE)


# ── standing scope-preservation block (#349 / bd-x01i) ───────────────────────────
#
# Removed-behavior is a recurring review-fix category (~21% of findings, one fix-round
# each). `_build_prompt` carries a UNIVERSAL, unconditional scope block: scope is
# additive by default, unrequested removal of guards/fallbacks/aliases/defaults is out,
# and a genuine removal stays legal only when named + explained in the final summary.

# The stable phrases that make up the block's contract — asserted verbatim so the block
# can't be gutted (turned into a blank line) while the heading survives.
_SCOPE_MARKERS = (
    "scope is ADDITIVE",  # additive-by-default framing (r2)
    "delete, narrow, or bypass",  # the three forbidden moves (r2)
    "guards, fallbacks, aliases, or defaults",  # the specific behaviors not to drop (r2)
    "still allowed",  # a necessary removal stays legal (r3)
    "name the removed behavior and the reason",  # …only if named… (r3)
    "`## Summary`",  # …in the final summary, where review can judge it (r3)
)


def _assert_scope_block(prompt: str):
    assert "## Scope" in prompt  # its own heading, distinct from Task / Rules
    for marker in _SCOPE_MARKERS:
        assert marker in prompt, marker


def test_build_prompt_always_carries_the_scope_preservation_block():
    """AC r1/r2/r3 (#349): every coding dispatch carries the standing preservation
    block — scope is additive, unrequested removal of guards/fallbacks/aliases/defaults
    is forbidden, and a genuine removal stays legal only when named with its reason in
    the final `## Summary`."""
    _assert_scope_block(BoardLoop({})._build_prompt(FEATURE))
    # a bare feature (no files, no design, no acceptance criteria, no requirements) too
    _assert_scope_block(BoardLoop({})._build_prompt({"id": "x", "title": "T", "spec": "s"}))


def test_scope_preservation_block_cannot_disappear_on_any_path():
    """AC r1/r5: the block is UNCONDITIONAL — present regardless of files_to_modify,
    repo gate files, repo conventions, distilled lessons, the requirement ledger, or the
    fix/retry path. Prove it survives with every optional lever toggled on, and again on
    the previous-attempt-rejected retry path."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md"], "repo_conventions": _CONVENTIONS})
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    # ordinary dispatch, every optional block populated
    _assert_scope_block(loop._build_prompt(feature, lessons="- always update the golden map"))
    # fix/retry path: a prior attempt was rejected → the block still rides along
    loop._ci_feedback["bd-1"] = "REQUESTED CHANGES: drops the null guard"
    loop._ci_prior_diff["bd-1"] = "diff --git a/x b/x\n+ bad"
    retry = loop._build_prompt(feature, lessons="- heed me")
    assert "REJECTED" in retry  # sanity: this really is the retry path
    _assert_scope_block(retry)


def test_scope_block_is_separated_from_and_does_not_disturb_existing_blocks():
    """AC r4: the new block is additive — the task, files, gate files, conventions,
    lessons, acceptance criteria and requirement ledger all remain under their own
    headings, correctly separated and in order."""
    loop = BoardLoop({"gate_files": ["CHANGELOG.md"], "repo_conventions": _CONVENTIONS})
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    prompt = loop._build_prompt(feature, lessons="- always update the golden map")
    for heading in (
        "## Task",
        "## Files to create / modify",
        "## Repo standing gate files",
        "## Repo conventions",
        "## Acceptance criteria",
        "## Requirements ledger",
        "## Rules",
    ):
        assert heading in prompt, heading
    # the scope block sits between the design/context blocks and the acceptance criteria,
    # and does not swallow the ledger that follows
    assert prompt.index("## Scope") < prompt.index("## Acceptance criteria")
    assert prompt.index("## Acceptance criteria") < prompt.index("## Requirements ledger")
    # the card's own content and the neighbouring blocks are untouched by the addition
    assert "do the thing" in prompt and "- a.py" in prompt
    assert _CONVENTIONS in prompt and "always update the golden map" in prompt


def test_is_test_path_classification():
    """The deterministic gate's path classifier — what counts as a test vs code."""
    from project_board.loop import _is_code_path, _is_test_path

    for p in ("tests/test_inbox.py", "test_x.py", "inbox/foo_test.py", "conftest.py", "web/x.test.tsx"):
        assert _is_test_path(p), p
    for p in ("inbox/store.py", "README.md", "config.yaml"):
        assert not _is_test_path(p), p
    assert _is_code_path("inbox/store.py") and _is_code_path("web/x.tsx")
    assert not _is_code_path("README.md") and not _is_code_path("config.yaml")


def test_format_cmd_parsed_from_config():
    assert BoardLoop({}).format_cmd == ""  # off by default
    assert BoardLoop({"format_cmd": "ruff check --fix ."}).format_cmd == "ruff check --fix ."


async def test_run_fixups_noop_when_unset(monkeypatch):
    """No format_cmd → _run_fixups must not shell out (it's the pre-PR auto-fix hook)."""
    loop = BoardLoop({})
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    await loop._run_fixups("/wt")
    assert not shelled


# ── pre-PR local gate (bd-xbh) ───────────────────────────────────────────────────


def test_local_gate_config_parsed():
    assert BoardLoop({}).local_gate_cmd == ""  # off by default
    assert BoardLoop({}).local_gate_max == 2
    loop = BoardLoop({"local_gate_cmd": "ruff check .", "local_gate_max": 1})
    assert loop.local_gate_cmd == "ruff check ." and loop.local_gate_max == 1


async def test_run_local_gate_noop_when_unset(monkeypatch):
    """No local_gate_cmd → never shells out."""
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    assert await BoardLoop({})._run_local_gate("/wt") is None
    assert not shelled


async def test_run_local_gate_passes_and_captures_failure(tmp_path):
    """Exit 0 → None (pass); non-zero → captured output for the coder."""
    assert await BoardLoop({"local_gate_cmd": "exit 0"})._run_local_gate(str(tmp_path)) is None
    out = await BoardLoop({"local_gate_cmd": "echo boom 1>&2; exit 1"})._run_local_gate(str(tmp_path))
    assert out is not None and "boom" in out


async def test_run_local_gate_signal_kill_is_no_verdict_not_a_failure(tmp_path, caplog):
    """A gate process killed by a signal (member shutdown delivering SIGTERM, an operator
    kill, the OOM killer) has no verdict — it must degrade to pass like a timeout does,
    not surface as "gate FAILED" with a half-finished pytest transcript. Before this a
    restart that landed mid merged-state gate flag_blocked a feature whose PR CI was
    fully green (2026-08-20, bd-k2j)."""
    # The shell kills ITSELF with SIGTERM after emitting partial output → returncode -15.
    cmd = "echo 'tests/x.py ....... [ 13%]'; kill -TERM $$"
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        out = await BoardLoop({"local_gate_cmd": cmd})._run_local_gate(str(tmp_path))
    assert out is None
    assert "killed by signal 15" in caplog.text


async def test_run_local_gate_degrades_to_pass_on_launch_error(monkeypatch):
    """A gate that can't even spawn must not block — it degrades to pass (CI gates)."""

    async def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _boom)
    assert await BoardLoop({"local_gate_cmd": "anything"})._run_local_gate("/wt") is None


def test_child_env_is_narrow_allowlist(monkeypatch):
    """_child_env (the env for gate/format/preflight children) is allowlist-based
    (F8a): the toolchain baseline plus env_passthrough, nothing else. An ordinary
    var outside the baseline does not leak; env_passthrough remains the only door.
    The full per-spawn-site proofs live in tests/test_env_sanitization.py."""
    monkeypatch.setenv("EDITOR", "vim")  # ordinary, outside the baseline
    monkeypatch.setenv("PATH", "/usr/bin")

    env = BoardLoop({})._child_env()
    assert "EDITOR" not in env and env["PATH"] == "/usr/bin"

    env = BoardLoop({"env_passthrough": ["EDITOR"]})._child_env()
    assert env["EDITOR"] == "vim"


# ── _drive: the state machine ───────────────────────────────────────────────────


async def _drive_with(
    monkeypatch,
    *,
    open_pr,
    coder=object(),
    dispatch=None,
    cfg=None,
    gate=None,
    judge=None,
    seed=None,
    feature=None,
    store_features=None,
):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions.

    ``judge`` stubs ``_judge_candidates`` (Max-Mode best-of-N); ``seed`` is a callable
    run on the loop before the drive (e.g. to pre-seed _ci_feedback for a CI-bounce test);
    ``store_features`` pre-seeds ``store.list_features`` rows (the #253 sibling scan)."""
    store = FakeLoopStore()
    store.features = store_features or []
    store.creates = []  # fids create_worktree was called for (a goal-fix retry reuses, so won't re-create)
    store.removes = []  # worktrees remove_worktree was called for
    store.reaps = []  # fids reap_feature_worktree was called for (Max-Mode loser teardown)
    store.promotes = []  # (src_wt, src_branch, fid) the Max-Mode winner was promoted with
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        store.creates.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _default_dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return None

    async def _reap(repo, root, fid):
        store.reaps.append(fid)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        store.promotes.append((src_wt, src_branch, fid))
        return ("/wt/feat-" + fid, "feat/" + fid)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch or _default_dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    loop = BoardLoop(cfg or {"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: coder)
    if gate is not None:
        monkeypatch.setattr(loop, "_run_local_gate", gate)
    if judge is not None:
        monkeypatch.setattr(loop, "_judge_candidates", judge)
    if seed is not None:
        seed(loop)
    await loop._drive(feature if feature is not None else FEATURE)
    return loop, store


async def test_drive_opens_review_on_a_clean_build(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._inflight == {}  # a completed drive leaves nothing to reap


async def test_drive_pr_body_is_the_summary_not_the_raw_stream(monkeypatch):
    """open_pr must receive `_pr_body`'s output, never the coder's raw reply (#56)."""
    bodies = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        bodies.append(body)
        return "https://example/pr/9"

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "I first looked at loop.py.\nLet me wire it up.\n## Summary\n\n- Wired the helper\n"

    await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    assert bodies == ["## Summary\n\n- Wired the helper"]


# ── Max-Mode: N parallel candidates → judge → promote winner → ship (#21) ────────


async def test_drive_max_mode_fans_out_and_ships_the_winner(monkeypatch):
    """max_mode_n=3 → 3 candidate worktrees built + dispatched in parallel, the judge
    picks one, the winner is promoted to the canonical name, the losers are reaped, and
    ONLY the winner's PR opens (on the canonical branch)."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append((wt, branch))
        return "https://example/pr/7"

    dispatched = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(wt)
        return f"reply from {wt}"

    async def _judge(feature, base, worktrees):
        assert len(worktrees) == 3  # the judge sees every candidate
        return 2  # candidate index 2 wins

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
    )
    # Three candidate worktrees, suffixed so none collides with the canonical name.
    assert store.creates == ["bd-1.c0", "bd-1.c1", "bd-1.c2"]
    assert len(dispatched) == 3  # all three coders ran
    # The winner (c2) is promoted to canonical; the two losers are reaped (winner is not).
    assert store.promotes == [("/wt/feat-bd-1.c2", "feat/bd-1.c2", "bd-1")]
    assert set(store.reaps) == {"bd-1.c0", "bd-1.c1"}
    # Only the winner's PR opens, on the canonical branch.
    assert opened == [("/wt/feat-bd-1", "feat/bd-1")]
    assert ("open_review", "bd-1", "https://example/pr/7") in store.calls
    assert loop._inflight == {}


async def test_drive_max_mode_all_empty_reaps_all_and_blocks(monkeypatch):
    """Every candidate empty → judge returns None → all candidates reaped, no PR, and
    the feature blocks (NoChangesError, single coder with no ladder)."""

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("no PR should open when every candidate is empty")

    async def _judge(feature, base, worktrees):
        return None  # nothing to ship

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
    )
    assert set(store.reaps) == {"bd-1.c0", "bd-1.c1", "bd-1.c2"}  # every candidate torn down
    assert store.promotes == []  # nothing promoted
    assert "flag_blocked" in store.names()


async def test_drive_max_mode_skips_fanout_on_a_carried_forward_fix(monkeypatch):
    """A re-dispatch carrying _ci_feedback (a CI bounce / goal-fix / gate-fix) FIXES the
    existing diff with ONE coder — Max-Mode must not re-fan-out N candidates."""

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/9"

    async def _judge(feature, base, worktrees):
        raise AssertionError("the judge must not run on a single-candidate carried-forward fix")

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3},
        seed=lambda lp: lp._ci_feedback.__setitem__("bd-1", "CI failed: lint"),
    )
    assert store.creates == ["bd-1"]  # one canonical worktree, NOT N suffixed candidates
    assert store.promotes == [] and store.reaps == []
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_drive_local_gate_failure_redispatches_then_opens(monkeypatch):
    """A pre-PR gate failure re-dispatches the SAME tier with the output injected,
    REUSING the worktree (one create), then opens the PR once the gate passes."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    gate_seq = iter(["FAILED tests/test_config.py::golden - boom", None])

    async def _gate(wt, feature=None):
        return next(gate_seq)

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 2},
    )
    assert len(prompts) == 2  # initial + 1 gate-fix re-dispatch
    assert store.creates == ["bd-1"]  # keep-worktree → only one worktree created
    assert "boom" in prompts[1]  # the gate output was carried into the retry prompt
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._gate_fix_attempts.get("bd-1", 0) == 0  # budget reset once the PR opened


async def test_drive_local_gate_exhausted_opens_pr_anyway(monkeypatch):
    """A persistent gate failure opens the PR after local_gate_max tries — never
    blocks (CI + the ci-fix budget are the backstop)."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/2"

    async def _gate(wt, feature=None):
        return "still red"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 1},
    )
    assert len(prompts) == 2  # initial + 1 (local_gate_max) then opens anyway
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)  # never blocked


async def test_drive_blocks_on_an_empty_diff_with_a_single_coder(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    # No diff + no tool activity ⇒ empty_result (#198): one same-tier retry, then
    # blocked for triage (single coder — there's no ladder to consult anyway).
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


# ── empty_result: a completed dispatch with no diff and no tool activity (#198) ──


def test_empty_result_max_config():
    assert BoardLoop({}).empty_result_max == 2
    assert BoardLoop({"empty_result_max": 3}).empty_result_max == 3
    assert BoardLoop({"empty_result_max": 0}).empty_result_max == 1  # floored — 0 could never block


async def test_drive_classifies_empty_result_and_records_the_stop_reason(monkeypatch):
    """No worktree diff + no tool-call activity ⇒ the attempt is `empty_result`, its
    own failure class (#198) — recorded on the attempt with the ACP adapter's
    stop-reason so the retro and the monitor drawer can show WHY the coder produced
    nothing. After empty_result_max (default 2) occurrences the feature blocks with
    a reason naming the class and the evidence."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        # The gen buffer exists (dispatch_coder_tapped ran progress_begin before
        # falling back to worktree.dispatch_coder in a host-free test env); simulate
        # the tap stashing the adapter's stop-reason. NO tool events — the coder
        # connected but never executed.
        coder_seam.progress_stop_reason("bd-1", 1, "refusal")
        return ""

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.NoChangesError("coder produced no commits vs base — nothing to PR")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    # Every empty occurrence is recorded on the attempt, class + stop-reason included.
    attempts = [c for c in store.calls if c[0] == "record_attempt"]
    assert len(attempts) == 2 and len(prompts) == 2  # blocked after N=2, not before
    assert all("empty_result" in a[3] for a in attempts)
    assert all("stop_reason=refusal" in a[3] for a in attempts)
    # The block reason names the failure class and the evidence.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    reason = blocked[0][2]
    assert reason.startswith("empty_result")
    assert "empty coder reply — no diff, no tool calls" in reason
    assert "stop_reason=refusal" in reason
    assert loop._inflight == {} and loop._empty_results.get("bd-1", 0) == 0  # count reset (pinned 0) with the climb


async def test_drive_empty_result_retries_same_tier_before_the_ladder(monkeypatch):
    """The empty-reply retry is PRE-escalation (#2991): the first empty at a tier
    retries once on the SAME tier without consulting the ladder (no escalation
    attempt spent); only when the retry ALSO returns empty is the failure recorded
    and the normal escalation ladder climbed. The escalated tier gets its own fresh
    retry window; the ladder top still empty → Blocked, reason naming the class."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        return ""

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.NoChangesError("coder produced no commits")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # 4 dispatches for 2 ladder consults: each tier's first empty burned a free
    # same-tier retry, never an escalation attempt.
    assert len(dispatches) == 4
    assert [e[0] for e in store.escalated] == ["bd-1", "bd-1"]  # fast→smart, then the top
    assert all("empty_result" in e[1] for e in store.escalated)  # the ladder saw the class
    # The retro marker separates the two shapes: retries carry same_tier_retry
    # (tier unchanged), recorded failures don't.
    attempts = [c for c in store.calls if c[0] == "record_attempt"]
    assert len(attempts) == 4
    assert "same_tier_retry" in attempts[0][3] and "same_tier_retry" in attempts[2][3]
    assert "same_tier_retry" not in attempts[1][3] and "same_tier_retry" not in attempts[3][3]
    assert attempts[0][2] == "fast" and attempts[2][2] == "smart"  # the retry stays on-tier
    # Ladder exhausted + still empty → blocked, reason naming the class.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and "empty coder reply — no diff, no tool calls" in blocked[0][2]
    assert loop._empty_results.get("bd-1", 0) == 0  # each climb reset the count (pinned 0)


async def test_drive_no_diff_with_tool_activity_still_escalates(monkeypatch):
    """The existing capability class is untouched (#198 narrows only the truly-empty
    case): a no-diff dispatch that DID run tools still climbs the ladder."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # Real tool activity in the gen buffer — the coder executed, just shipped nothing.
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "a.py"}})
        return "I read the files but made no edits"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.NoChangesError("coder produced no commits")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # fast → smart (one climb), smart is the top → blocked as a capability failure.
    assert [e[0] for e in store.escalated] == ["bd-1", "bd-1"]
    assert len(dispatches) == 2
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and "empty coder reply" not in blocked[0][2]


# ── pre-model dispatch failure: block for triage, NEVER climb the ladder (#339) ──


async def test_drive_pre_model_dispatch_failure_blocks_without_a_tier_climb(monkeypatch):
    """The C1 seam-style bug: a dispatch that dies BELOW the seam (a `dispatch_tapped`
    kwarg mismatch, normalised to `coder dispatch failed: …`) with NO model activity is
    a pre-model infra failure. It must block DIRECTLY for triage — no smart→reasoning→
    opus climb, no `tier:` label, ONE dispatch — under the `dispatch-infra` class, with
    the original infra evidence preserved on the block reason."""
    store = _EscalatingStore(tiers=["reasoning", "opus"])  # a real 2-rung ladder is available…
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    removes = []

    async def _remove(repo, wt, branch=""):
        removes.append(wt)

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # The gen buffer exists (dispatch_coder_tapped ran progress_begin) but the model
        # never produced a token — the seam threw first. Simulate the seam's normalised
        # WorktreeError with the C1 kwarg-mismatch evidence.
        raise worktree.WorktreeError(
            "coder dispatch failed: dispatch_tapped() got an unexpected keyword argument 'tool_callback'"
        )

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("no PR on a pre-model dispatch failure")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on  # …and yet it is NEVER consulted
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # No climb: the ladder was available but escalate() was never called, and only ONE
    # dispatch ran (no re-dispatch at a stronger tier).
    assert store.escalated == []
    assert len(dispatches) == 1
    # Blocked for infra triage under the dispatch-infra class, evidence intact.
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    fid, reason, category = blocked[0][1], blocked[0][2], blocked[0][3]
    assert fid == "bd-1" and category == "dispatch-infra"
    assert "dispatch_tapped" in reason and "unexpected keyword" in reason
    assert loop._inflight == {} and removes == ["/wt/feat-bd-1"]  # worktree reaped


async def test_drive_pre_model_timeout_before_first_token_blocks_not_escalates(monkeypatch):
    """A CoderTimeout with NO model activity is a pre-first-token infra timeout (a
    wedged adapter/session), not a model running out of time — a stronger model can't
    clear it, so it blocks for triage rather than climbing the ladder (#339)."""
    store = _EscalatingStore(tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # No progress_thought/progress_tool — the model never produced a first token.
        raise worktree.CoderTimeout("coder timed out after 1800s")

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("no PR on a pre-first-token timeout")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    assert store.escalated == []  # a pre-first-token timeout never climbs
    assert len(dispatches) == 1
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra"


async def test_drive_dispatch_failure_after_model_work_still_escalates(monkeypatch):
    """The genuine model-capability case is PRESERVED: a dispatch failure that lands
    AFTER the model reached first token (tools/thoughts recorded) is model-reachable —
    it still climbs the tier ladder exactly as before, NOT a pre-model infra block."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        # The model DID reach first token — a real tool ran — then the dispatch failed
        # below the seam. Lifecycle evidence ⇒ model-reachable ⇒ escalate, not block.
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {"path": "a.py"}})
        if len(dispatches) == 1:
            # A non-transient seam error that landed AFTER the model streamed — the
            # message even matches the pre-model signature, but the lifecycle evidence
            # (a real tool ran) makes it model-reachable, so it escalates regardless.
            raise worktree.WorktreeError("coder dispatch failed: seam raised after the model stream began")
        return "the escalated coder's reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # It climbed (fast→smart), re-dispatched, and shipped — no dispatch-infra block.
    assert [e[0] for e in store.escalated] == ["bd-1"]
    assert len(dispatches) == 2
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)


async def test_drive_pre_model_failure_on_a_keep_worktree_redispatch_still_blocks(monkeypatch):
    """The review finding: model-reached evidence must be scoped to the CURRENT dispatch.
    A first dispatch reaches the model and leaves an active sibling gen in the feature's
    ring buffer; a goal-verify gap keeps the worktree and re-dispatches (gen 1, NO
    progress_new_run — the stale sibling survives for the drawer). That re-dispatch dies
    BELOW the seam before a first token. The unscoped model-reached scan saw the PRIOR
    dispatch's activity and climbed the ladder; with run-scoping it correctly reads "no
    model work this dispatch" and blocks for infra triage — no tier climb (#339)."""
    coder_seam._progress.clear()
    store = _EscalatingStore(tiers=["reasoning", "opus"])  # a real ladder is available…
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    removes = []

    async def _remove(repo, wt, branch=""):
        removes.append(wt)

    async def _reap(repo, root, fid):
        return None

    # Goal-verify gaps once (dispatch 1) → keep-worktree re-dispatch; the re-dispatch
    # never reaches the gate (it dies below the seam first), so this fires exactly once.
    async def _verify(feature, wt, base, coder_reply=""):
        return "missing tests for the new behavior"

    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        if len(dispatches) == 1:
            # Dispatch 1 reached the model: record activity on gen 1 AND an active
            # sibling gen 2 (a solve/max-mode fan-out the earlier dispatch left behind).
            coder_seam.progress_begin("bd-1", 2, "fast")
            coder_seam.progress_tool("bd-1", 2, {"phase": "start", "id": "s1", "name": "edit_file"})
            coder_seam.progress_tool("bd-1", 1, {"phase": "start", "id": "s2", "name": "edit_file"})
            return "implementation is in the worktree"
        # Dispatch 2 (keep-worktree re-run of gen 1) dies below the seam before a token.
        raise worktree.WorktreeError(
            "coder dispatch failed: dispatch_tapped() got an unexpected keyword argument 'tool_callback'"
        )

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "pf", "smart": "ps"}, "goal_verify": True, "goal_fix_max": 2})
    assert loop.escalation_on  # …and yet the ladder is NEVER consulted
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    monkeypatch.setattr(loop, "_verify_goal", _verify)
    await loop._drive(FEATURE)

    # No climb: the stale sibling gen did NOT masquerade as this dispatch's model work.
    assert store.escalated == []
    assert len(dispatches) == 2  # initial build + the one keep-worktree re-dispatch
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    fid, reason, category = blocked[0][1], blocked[0][2], blocked[0][3]
    assert fid == "bd-1" and category == "dispatch-infra"
    assert "dispatch_tapped" in reason  # the original infra evidence is preserved
    assert removes == ["/wt/feat-bd-1"]  # the worktree is reaped


async def test_drive_empty_result_retry_recovers_and_resets_the_count(monkeypatch):
    """One empty occurrence then a real diff: the same-tier retry ships normally and
    the empty-result count resets (a later empty attempt starts a fresh window)."""
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.NoChangesError("coder produced no commits")
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls
    assert "flag_blocked" not in store.names()
    assert loop._empty_results.get("bd-1", 0) == 0  # reset (pinned 0) once the PR opened


async def test_drive_empty_result_retry_records_the_retro_marker(monkeypatch):
    """The retry attempt carries the same_tier_retry marker (#2991) so the board
    retro can tell a pre-escalation retry from an escalation — and a retry that
    recovers leaves NO recorded failure: only the marker attempt, nothing blocked.
    The marked outcome still mines as the empty-result class."""
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.NoChangesError("coder produced no commits")
        return "https://example/pr/4"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    attempts = [c for c in store.calls if c[0] == "record_attempt"]
    assert len(attempts) == 1  # the marker attempt only — the recovery recorded no failure
    assert "empty_result" in attempts[0][3] and "same_tier_retry" in attempts[0][3]
    assert "flag_blocked" not in store.names()
    assert ("open_review", "bd-1", "https://example/pr/4") in store.calls
    assert retro_classify(attempts[0][3]) == "empty result (no diff, no tool calls)"


async def test_drive_empty_result_retry_logs_same_tier_retry(monkeypatch, caplog):
    """The monitor log names the empty-reply retry distinctly (#2991): an operator
    tailing the coder monitor can tell "Retrying on same tier (empty reply)" from
    an escalation line."""
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.NoChangesError("coder produced no commits")
        return "https://example/pr/5"

    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "Retrying on same tier (empty reply)" in caplog.text
    assert ("open_review", "bd-1", "https://example/pr/5") in store.calls


# ── source-issue closed guard (#166) ────────────────────────────────────────────


async def test_source_issue_still_open_no_raw():
    """Empty source_issue → True (no gh call, proceed)."""
    assert await _source_issue_still_open("", "/wt") is True


async def test_source_issue_still_open_open_state(monkeypatch):
    """gh reports 'open' → True (proceed)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 0, "open\n", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_closed_state(monkeypatch):
    """gh reports 'closed' → False (skip PR)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 0, "closed\n", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is False


async def test_source_issue_still_open_gh_nonzero_fail_open(monkeypatch):
    """gh exits non-zero (e.g. 404) → True (fail-open)."""

    async def _fake_gh(*args, cwd, timeout=60):
        return 1, "", "not found"

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_gh_raises_fail_open(monkeypatch):
    """gh raises (e.g. WorktreeError timeout) → True (fail-open)."""

    async def _fake_gh(*args, cwd, timeout=60):
        raise worktree.WorktreeError("timed out")

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("owner/repo#42", "/wt")
    assert result is True


async def test_source_issue_still_open_full_url(monkeypatch):
    """Full GitHub issue URL is parsed correctly."""

    async def _fake_gh(*args, cwd, timeout=60):
        assert "repos/acme/lib/issues/7" in args
        return 0, "closed", ""

    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("https://github.com/acme/lib/issues/7", "/wt")
    assert result is False


async def test_source_issue_still_open_bare_number_resolves_slug(monkeypatch):
    """A bare issue number resolves the slug from the worktree remote."""
    called_with_slug = []

    async def _fake_slug(*, cwd):
        return "myorg/myrepo"

    async def _fake_gh(*args, cwd, timeout=60):
        called_with_slug.append(args)
        assert "repos/myorg/myrepo/issues/5" in args
        return 0, "open", ""

    monkeypatch.setattr(worktree, "repo_slug", _fake_slug)
    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("#5", "/wt")
    assert result is True
    assert called_with_slug  # gh was called


async def test_source_issue_still_open_bare_number_unresolvable_fail_open(monkeypatch):
    """Bare number with unresolvable slug → True (fail-open, no gh call)."""
    gh_called = []

    async def _fake_slug(*, cwd):
        return ""  # couldn't resolve

    async def _fake_gh(*args, cwd, timeout=60):
        gh_called.append(args)
        return 0, "closed", ""

    monkeypatch.setattr(worktree, "repo_slug", _fake_slug)
    monkeypatch.setattr(worktree, "_gh", _fake_gh)
    result = await _source_issue_still_open("#99", "/wt")
    assert result is True
    assert gh_called == []  # never called gh when slug unresolvable


async def test_drive_skips_pr_when_source_issue_closed(monkeypatch):
    """When source_issue is closed at PR-open time, no PR is created and the
    card is cancelled (cancel_feature is called with the supersede reason)."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append((wt, branch))
        return "https://example/pr/99"

    async def _closed(si_raw, cwd):
        return False

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _closed)
    feature = dict(FEATURE, source_issue="owner/repo#42")
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert opened == []  # no PR opened
    assert "cancel_feature" in store.names()
    assert "open_review" not in store.names()
    cancel_calls = [c for c in store.calls if c[0] == "cancel_feature"]
    assert any("superseded" in c[2] for c in cancel_calls)
    assert loop._inflight == {}


async def test_drive_opens_pr_when_source_issue_still_open(monkeypatch):
    """When source_issue is still open, the PR opens normally."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append("https://example/pr/1")
        return opened[-1]

    async def _still_open(si_raw, cwd):
        return True

    async def _slug(*, cwd):
        return "owner/repo"

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _still_open)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    feature = dict(FEATURE, source_issue="owner/repo#42")
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert len(opened) == 1
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert "cancel_feature" not in store.names()


# ── source-issue closed by a board SIBLING vs. an external PR (#253) ──────────────


def test_issue_closed_by_board_sibling_matches_done_sibling_with_pr():
    """A done sibling (different card) carrying the same source_issue WITH a
    pr_url ⇒ True: the board's own merged PR closed the issue."""
    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    store = FakeLoopStore()
    store.features = [
        {"id": "bd-1", "board_state": "done", "source_issue": "owner/repo#42", "pr_url": "https://x/pr/1"},
    ]
    assert _issue_closed_by_board_sibling(store, feature) is True


def test_issue_closed_by_board_sibling_normalizes_reference_forms():
    """The match is on the parsed issue, so a full URL sibling matches an
    owner/repo#N current (different textual form, same issue)."""
    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    store = FakeLoopStore()
    store.features = [
        {
            "id": "bd-1",
            "board_state": "done",
            "source_issue": "https://github.com/owner/repo/issues/42",
            "pr_url": "https://x/pr/1",
        },
    ]
    assert _issue_closed_by_board_sibling(store, feature) is True


def test_issue_closed_by_board_sibling_no_match_when_external():
    """No done sibling names this issue (a done card for a DIFFERENT issue, and a
    same-issue card that never opened a PR) ⇒ False: the closure was external."""
    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    store = FakeLoopStore()
    store.features = [
        {"id": "bd-1", "board_state": "done", "source_issue": "owner/repo#99", "pr_url": "https://x/pr/1"},
        {"id": "bd-3", "board_state": "done", "source_issue": "owner/repo#42", "pr_url": ""},
    ]
    assert _issue_closed_by_board_sibling(store, feature) is False


def test_issue_closed_by_board_sibling_excludes_self():
    """The current card must not match itself, even if it is (racily) projected
    as done with a pr_url — a self-match would defeat the external-close cancel."""
    feature = dict(FEATURE, id="bd-1", source_issue="owner/repo#42")
    store = FakeLoopStore()
    store.features = [
        {"id": "bd-1", "board_state": "done", "source_issue": "owner/repo#42", "pr_url": "https://x/pr/1"},
    ]
    assert _issue_closed_by_board_sibling(store, feature) is False


def test_issue_closed_by_board_sibling_fails_open_on_store_error():
    """A store read error fails OPEN (True ⇒ skip the cancel) — the same direction
    as the #166 gh guard, so a flaky read never discards completed work."""

    class _BoomStore:
        def list_features(self, state=None):
            raise RuntimeError("board unreachable")

    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    assert _issue_closed_by_board_sibling(_BoomStore(), feature) is True


async def test_drive_proceeds_when_source_issue_closed_by_board_sibling(monkeypatch):
    """#253: the source issue is closed, but a SIBLING slice on the same board
    (same source_issue, done, with a pr_url) is what closed it — so the current
    feature must NOT be cancelled and SHALL open its own PR."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append("https://example/pr/2")
        return opened[-1]

    async def _closed(si_raw, cwd):
        return False  # gh reports the shared issue as closed

    async def _slug(*, cwd):
        return "owner/repo"

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _closed)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    sibling = {"id": "bd-1", "board_state": "done", "source_issue": "owner/repo#42", "pr_url": "https://x/pr/1"}
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature, store_features=[sibling])
    assert len(opened) == 1  # PR opened despite the closed issue
    assert ("open_review", "bd-2", "https://example/pr/2") in store.calls
    assert "cancel_feature" not in store.names()
    assert loop._inflight == {}


async def test_drive_cancels_when_source_issue_closed_externally(monkeypatch):
    """#253 boundary: the source issue is closed and NO board sibling carries it
    (only a done card for an unrelated issue), so the closure is external and the
    #166 supersede-cancel proceeds unchanged."""
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append((wt, branch))
        return "https://example/pr/99"

    async def _closed(si_raw, cwd):
        return False

    monkeypatch.setattr(loop_mod, "_source_issue_still_open", _closed)
    feature = dict(FEATURE, id="bd-2", source_issue="owner/repo#42")
    unrelated = {"id": "bd-1", "board_state": "done", "source_issue": "owner/repo#99", "pr_url": "https://x/pr/1"}
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature, store_features=[unrelated])
    assert opened == []  # no PR opened
    assert "cancel_feature" in store.names()
    assert "open_review" not in store.names()
    cancel_calls = [c for c in store.calls if c[0] == "cancel_feature"]
    assert any("superseded" in c[2] for c in cancel_calls)
    assert loop._inflight == {}


# ── coder.solve() board seam (ADR 0064 P2) ───────────────────────────────────────


def test_coder_solve_config_defaults():
    loop = BoardLoop({})
    assert loop.coder_solve is True  # opt-OUT valve; the real gate is coder_seam
    assert loop.coder_solve_test_cmd == ""  # no local_gate_cmd to fall back to either
    assert loop.coder_solve_budget == 6
    assert loop.coder_solve_k == 3
    assert loop.coder_solve_tree_depth == 2
    assert loop.coder_solve_test_timeout == 300
    assert loop.max_concurrent_sessions == 0  # default: unlimited within the k budget


def test_max_concurrent_sessions_config():
    assert BoardLoop({"max_concurrent_sessions": 1}).max_concurrent_sessions == 1
    assert BoardLoop({"max_concurrent_sessions": 4}).max_concurrent_sessions == 4
    assert BoardLoop({"max_concurrent_sessions": -1}).max_concurrent_sessions == 0  # floors at 0


async def test_coder_solve_startup_log_notes_peak_sessions_when_k_gt_1(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 3, "max_concurrent": 2, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert peak_lines, "expected a startup INFO line noting peak concurrent ACP sessions"
    assert "2" in peak_lines[0] and "3" in peak_lines[0] and "6" in peak_lines[0]


async def test_coder_solve_startup_log_silent_when_k_is_one(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 1, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert not peak_lines, "no peak-sessions log expected when coder_solve_k=1"


async def test_coder_solve_startup_log_notes_cap_when_max_concurrent_sessions_set(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"coder_solve": True, "coder_solve_k": 3, "max_concurrent_sessions": 1, "loop_enabled": True})
        loop.start()

    if loop._task:
        loop._task.cancel()

    peak_lines = [r.message for r in caplog.records if "peak concurrent ACP sessions" in r.message]
    assert peak_lines
    assert "capped at 1" in peak_lines[0]


def test_coder_solve_test_cmd_falls_back_to_local_gate_cmd():
    assert BoardLoop({"local_gate_cmd": "pytest -q"}).coder_solve_test_cmd == "pytest -q"
    loop = BoardLoop({"local_gate_cmd": "pytest -q", "coder_solve_test_cmd": "pytest tests/unit -q"})
    assert loop.coder_solve_test_cmd == "pytest tests/unit -q"  # explicit wins over the fallback


def test_use_coder_solve_requires_the_opt_out_flag_plus_the_seam_gate(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())  # pretend `coder` is installed
    on = BoardLoop({"local_gate_cmd": "pytest -q"})
    assert on._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is True
    assert on._use_coder_solve({"acceptance_criteria": ""}) is False  # no oracle → degrade

    off = BoardLoop({"local_gate_cmd": "pytest -q", "coder_solve": False})
    assert off._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False  # opted out


def test_use_coder_solve_false_when_coder_plugin_unavailable(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: None)  # coder plugin absent/disabled
    loop = BoardLoop({"local_gate_cmd": "pytest -q"})
    assert loop._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False


def test_use_coder_solve_false_without_a_test_command(monkeypatch):
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    loop = BoardLoop({})  # no local_gate_cmd, no coder_solve_test_cmd
    assert loop._use_coder_solve({"acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}) is False


async def _pass_gate(wt, feature=None):
    """A stand-in for `_run_local_gate` — pass immediately. These tests set
    `local_gate_cmd` (needed as the coder_solve_test_cmd fallback) but the drive's
    fake worktree paths don't exist on disk, so the REAL gate would just shell out
    against a bogus cwd; stub it rather than rely on that degrading to a pass."""
    return None


async def test_drive_uses_coder_solve_when_available_and_records_gens(monkeypatch):
    """coder available + acceptance present + a test command → the solve path runs
    INSTEAD of the single delegate_to(acp) shot, and gens-spent lands on the feature
    via store.record_gens_spent (so portfolio_rollup can read it)."""
    from project_board import coder_seam

    seen = {}

    async def _fake_dispatch(
        *,
        task,
        coder,
        repo,
        base,
        root,
        fid,
        dispatch_timeout,
        test_cmd,
        test_timeout,
        budget,
        k,
        tree_depth,
        record_gens=None,
        fusion_delegate=None,
        fusion_k=2,
        files_to_modify=None,
        fusion_max_file_chars=None,
        env_passthrough=(),
        tier="",
        record_verified=None,
        commit_message="",
        title="",
        max_concurrent_sessions=0,
    ):
        seen["fid"] = fid
        seen["test_cmd"] = test_cmd
        seen["task"] = task
        seen["env_passthrough"] = env_passthrough
        seen["tier"] = tier
        seen["commit_message"] = commit_message
        seen["title"] = title
        record_gens(4)
        # dispatch() calls this at the verify boundary (#91) — the loop must have
        # threaded a recorder that lands the record on THIS feature's bead.
        record_verified(f"feat/{fid}", "abc123", f"/wt/feat-{fid}")
        return (f"/wt/feat-{fid}", f"feat/{fid}", "[coder.solve rung=best-of-k gens=4] solved")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _fake_dispatch)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/42"

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}, gate=_pass_gate
    )
    assert seen["fid"] == "bd-1" and seen["test_cmd"] == "pytest -q"
    assert "Add a thing" in seen["task"]  # the same built prompt, not a different one
    assert seen["commit_message"] == "feat: Add a thing"  # the verified commit keeps the PR title
    assert seen["title"] == "Add a thing"  # #227: the RAW title is threaded for the canonical slug
    assert store.gens_spent.get("bd-1") == 4
    # The verify-boundary salvage record (#91) landed on the bead via the store.
    assert ("record_verified", "bd-1", "feat/bd-1", "abc123", "/wt/feat-bd-1") in store.calls
    assert ("open_review", "bd-1", "https://example/pr/42") in store.calls
    assert store.creates == []  # solve()'s own per-candidate worktrees replaced the single create


async def test_drive_skips_fusion_for_a_dispatch_when_files_are_oversized(monkeypatch, tmp_path):
    """Fusion can't tool-call and returns whole-file replacements — an oversized
    file must gate BEFORE dispatch (fusion_delegate=None for that dispatch), not
    get attempted and risk a silently truncated rewrite. The ladder still runs
    (greedy/best-of-k/tree-search), it just skips the fusion rung."""
    from project_board import coder_seam

    (tmp_path / "big.py").write_text("x" * 1000)
    seen = {}

    async def _fake_dispatch(*, fusion_delegate=None, **kw):
        seen["fusion_delegate"] = fusion_delegate
        return (f"/wt/feat-{kw['fid']}", f"feat/{kw['fid']}", "[coder.solve rung=greedy gens=1] solved")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _fake_dispatch)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/42"

    feature = {**FEATURE, "repo": str(tmp_path), "files_to_modify": ["big.py"]}
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={
            "coder": "proto",
            "local_gate_cmd": "pytest -q",
            "coder_solve_fusion_delegate": "fusion-model",
            "coder_solve_fusion_max_file_chars": 10,
        },
        gate=_pass_gate,
        feature=feature,
    )
    assert seen["fusion_delegate"] is None  # gated out before dispatch, not attempted


async def test_drive_falls_back_to_single_shot_without_acceptance_criteria(monkeypatch):
    """Honest degrade: even with the coder plugin available and a test command
    configured, a feature with NO acceptance criteria takes today's single
    delegate_to(acp) shot — never a silent best-of-k."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run without acceptance criteria")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    feature = dict(FEATURE, acceptance_criteria="")
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={"coder": "proto", "local_gate_cmd": "pytest -q"},
        feature=feature,
        gate=_pass_gate,
    )
    assert store.creates == ["bd-1"]  # the plain single-worktree path ran
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls


async def test_drive_falls_back_to_single_shot_when_coder_plugin_unavailable(monkeypatch):
    """Honest degrade: acceptance criteria + a test command present, but `coder`
    itself isn't installed/enabled — still the single shot, never a fake ladder."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: None)

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run when the coder plugin is unavailable")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/2"

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}, gate=_pass_gate
    )
    assert store.creates == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls


async def test_drive_falls_back_to_single_shot_without_a_test_command(monkeypatch):
    """Honest degrade: `coder` available + acceptance present, but NO test command
    configured (no coder_solve_test_cmd, no local_gate_cmd) — no runnable oracle, so
    the single shot runs rather than fake grounding."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run with no runnable test command")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, cfg={"coder": "proto"})  # no test cmd anywhere
    assert store.creates == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


async def test_drive_coder_solve_exhausted_blocks_like_a_capability_failure(monkeypatch):
    """A SolveExhausted (no candidate passed the acceptance tests) is treated exactly
    like NoChangesError/CoderTimeout — blocked immediately with no ladder configured."""
    from project_board import coder_seam

    async def _exhausted(**kw):
        raise coder_seam.SolveExhausted("coder.solve exhausted after 6 generation(s) (rung=best-partial): 1/3 failing")

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _exhausted)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("open_pr should not run — no candidate passed")

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}
    )
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


async def test_drive_coder_solve_skipped_on_a_carried_forward_ci_bounce(monkeypatch):
    """A CI-bounce re-dispatch (signalled by _ci_feedback) fixes the EXISTING diff
    with the single coder — coder.solve must not re-fan-out on that retry, same rule
    as Max-Mode."""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run on a carried-forward re-dispatch")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/9"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        cfg={"coder": "proto", "local_gate_cmd": "pytest -q"},
        seed=lambda lp: lp._ci_feedback.__setitem__("bd-1", "CI failed: lint"),
        gate=_pass_gate,
    )
    assert store.creates == ["bd-1"]  # the plain single-worktree path ran, not solve()
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_drive_max_mode_wins_precedence_over_coder_solve_when_both_configured(monkeypatch):
    """A board already running Max-Mode (`max_mode_n>1`) must keep fanning out N
    candidates and judging, NOT silently switch to coder.solve's ladder, even once
    the `coder` plugin becomes importable and every one of
    coder_seam.should_use_solve's gates (acceptance criteria + a runnable test
    command) is satisfied. Pins the fix for the precedence bug: coder.solve only
    preempts Max-Mode when max_mode_n<=1. (Uses `coder_solve_test_cmd`, not
    `local_gate_cmd`, to satisfy the test-command gate without also flipping
    Max-Mode's OWN candidate-selection strategy from judge to execution-grounded —
    that's an orthogonal knob this test isn't about.)"""
    from project_board import coder_seam

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())  # coder plugin available

    async def _boom(**kw):
        raise AssertionError("coder.solve must not run — Max-Mode has precedence when max_mode_n>1")

    monkeypatch.setattr(coder_seam, "dispatch", _boom)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/11"

    async def _judge(feature, base, worktrees):
        assert len(worktrees) == 3  # Max-Mode's fan-out ran, not solve()
        return 0

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        judge=_judge,
        cfg={"coder": "proto", "max_mode_n": 3, "coder_solve_test_cmd": "pytest -q"},
    )
    assert store.creates == ["bd-1.c0", "bd-1.c1", "bd-1.c2"]  # Max-Mode's candidates, not solve()'s
    assert ("open_review", "bd-1", "https://example/pr/11") in store.calls


# ── goal-verification gate (MiMo-borrowed; opt-in `goal_verify`) ─────────────────


async def test_goal_verify_pass_opens_the_pr(monkeypatch):
    async def _ok(self, feature, wt, base, coder_reply=""):
        return None  # PASS — no gap

    monkeypatch.setattr(BoardLoop, "_verify_goal", _ok)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/9"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "goal_verify": True})
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_goal_verify_gap_retries_same_tier_then_opens(monkeypatch):
    """A goal-verify gap (e.g. missing tests) re-dispatches the SAME coder with the
    gap carried into the prompt — and opens the PR once the coder fixes it."""
    calls = {"n": 0}

    async def _verify(self, feature, wt, base, coder_reply=""):
        calls["n"] += 1
        return "missing tests for the new behavior" if calls["n"] == 1 else None  # gap once, then PASS

    monkeypatch.setattr(BoardLoop, "_verify_goal", _verify)
    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/77"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert ("open_review", "bd-1", "https://example/pr/77") in store.calls  # opened after the retry
    assert len(dispatched) == 2  # initial + 1 same-tier re-dispatch
    # keep-worktree: the retry REUSES the worktree (impl intact) — created once, never removed
    assert store.creates == ["bd-1"]  # NOT re-created for the retry
    assert store.removes == []  # not wiped between attempts
    assert "ALREADY in this worktree" in dispatched[1] and "missing tests" in dispatched[1]  # add-to-existing feedback
    assert loop._goal_fix_attempts.get("bd-1", 0) == 0  # reset (pinned 0) once the gate passes


async def test_goal_verify_gap_exhausts_retries_then_blocks(monkeypatch):
    """A persistent gap exhausts goal_fix_max same-tier retries, then blocks — no PR."""

    async def _gap(self, feature, wt, base, coder_reply=""):
        return "AC #1 unmet: multiply() missing"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _gap)
    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append(True)
        return "https://example/pr/x"

    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(prompt)
        return "reply"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert not opened  # the gate stopped the PR from being opened
    assert len(dispatched) == 3  # initial + goal_fix_max (2) same-tier retries
    assert store.creates == ["bd-1"]  # keep-worktree: created ONCE, reused across both retries
    assert "flag_blocked" in store.names()  # then blocked for triage
    assert "open_review" not in store.names()


# ── requirement-gate diagnostics (#284) ──────────────────────────────────────────


def test_requirement_gate_diagnostics_pure_fields():
    """The payload the gate logs and persists: parsed dispositions, still-open ids,
    len(result), whether a `## Requirements` heading is present, and the first 200
    chars after the LAST such heading."""
    open_items = [{"id": "r1", "text": "do x", "status": "open"}]

    # No `## Requirements` section at all — silence, not a parse miss.
    silent = _requirement_gate_diagnostics("just some prose reply", open_items)
    assert silent["dispositions"] == []
    assert silent["open_ids"] == ["r1"]
    assert silent["result_len"] == len("just some prose reply")
    assert silent["has_requirements_heading"] is False
    assert silent["after_heading"] == ""

    # A heading whose disposition line is MALFORMED — heading present, nothing parses,
    # r1 stays open. This is the exact "wrote a section but the loop didn't read it" case.
    body = "## Requirements\nr1 is basically handled I think\n\n## Summary\ndone"
    parsed = _requirement_gate_diagnostics(body, open_items)
    assert parsed["dispositions"] == []  # the malformed row is silence
    assert parsed["has_requirements_heading"] is True
    assert parsed["after_heading"].startswith("\nr1 is basically handled")
    assert len(parsed["after_heading"]) <= 200


def test_requirement_gate_diagnostics_after_heading_caps_at_200_and_uses_last():
    """The window is capped at 200 chars and taken from the LAST heading (a mid-narration
    mention must not shadow the real section — the #56 discipline)."""
    tail = "x" * 500
    body = f"## Requirements (early mention)\nignored\n## Requirements\n{tail}"
    diag = _requirement_gate_diagnostics(body, [])
    assert len(diag["after_heading"]) == 200
    assert diag["after_heading"].strip("\n").startswith("x")


def test_requirement_gate_diag_line_carries_every_field():
    diag = {
        "dispositions": [{"id": "r1", "status": "done"}],
        "open_ids": ["r2"],
        "result_len": 42,
        "has_requirements_heading": True,
        "after_heading": "- r1: done",
    }
    line = _requirement_gate_diag_line(diag)
    assert "dispositions=" in line and "r1" in line
    assert "open_ids=['r2']" in line
    assert "len(result)=42" in line
    assert "has_requirements_heading=True" in line
    assert "after_heading=" in line


async def test_requirement_gate_logs_diagnostics_and_persists_bounce_comment(monkeypatch, caplog):
    """An open requirement item bounces the build: the gate logs the diagnostic payload
    at INFO AND persists the same fields as a bead comment, then the coder disposes the
    item on the retry and the PR opens (#284)."""
    replies = iter(
        [
            # First reply: a `## Requirements` heading present but the disposition line
            # is malformed, so r1 never parses and stays OPEN → the gate bounces.
            "## Requirements\nr1 — mostly done?\n\n## Summary\nwip",
            # Second reply: a clean disposition → r1 done → the gate passes.
            "## Requirements\n- r1: done\n\n## Summary\nshipped",
        ]
    )

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return next(replies)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/284"

    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        loop, store = await _drive_with(
            monkeypatch,
            open_pr=_open_pr,
            dispatch=_disp,
            cfg={"coder": "proto", "goal_fix_max": 2},
            feature=feature,
        )

    # The gate opened the PR once the retry disposed r1.
    assert ("open_review", "bd-1", "https://example/pr/284") in store.calls
    assert store.creates == ["bd-1"]  # keep-worktree: one worktree, reused for the fix

    # r1: INFO log records dispositions, still-open ids, len(result), heading presence,
    # and the first 200 chars after the heading.
    diag_lines = [m for m in caplog.messages if "requirement gate diagnostics" in m]
    assert diag_lines, "the gate must log its diagnostics at INFO"
    line = diag_lines[0]
    assert "dispositions=[]" in line  # the malformed row parsed to nothing
    assert "open_ids=['r1']" in line
    assert "len(result)=" in line
    assert "has_requirements_heading=True" in line
    assert "after_heading=" in line and "mostly done" in line

    # r2: the SAME diagnostic fields are persisted on the bead attempt comment.
    comments = [c for c in store.calls if c[0] == "comment"]
    assert len(comments) == 1, "one bounce → one diagnostic comment"
    _, cid, text = comments[0]
    assert cid == "bd-1"
    assert "requirement gate bounce" in text and "re-dispatch 1/2" in text
    assert "dispositions=[]" in text
    assert "open_ids=['r1']" in text
    assert "has_requirements_heading=True" in text
    assert "mostly done" in text  # the first-200-chars window rode along


async def test_requirement_gate_diagnostic_comment_failure_never_breaks_the_build(monkeypatch, caplog):
    """The bounce comment is bookkeeping: a store.comment failure is logged and swallowed,
    and the build still re-dispatches and opens the PR (#284)."""
    replies = iter(
        [
            "no requirements section here — r1 stays open",
            "## Requirements\n- r1: done\n\n## Summary\nshipped",
        ]
    )

    async def _disp(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return next(replies)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/285"

    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}

    def _boom(loop):
        store = loop_mod.get_store()

        def _raise(fid, text):
            store.calls.append(("comment", fid, text))
            raise RuntimeError("br is down")

        store.comment = _raise

    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        loop, store = await _drive_with(
            monkeypatch,
            open_pr=_open_pr,
            dispatch=_disp,
            cfg={"coder": "proto", "goal_fix_max": 2},
            feature=feature,
            seed=_boom,
        )

    assert ("open_review", "bd-1", "https://example/pr/285") in store.calls  # build survived
    assert "requirement gate diagnostic comment failed" in caplog.text


async def test_goal_verify_off_by_default_skips_the_gate(monkeypatch):
    called = []

    async def _spy(self, feature, wt, base):
        called.append(True)
        return "would fail if invoked"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _spy)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)  # default cfg → off
    assert not called  # the gate is never invoked when goal_verify is off
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


async def test_verify_goal_requires_a_test_deterministically(monkeypatch):
    """The gate is path-based — no LLM, no diff. A code change with no test file → gap;
    with a test → pass; docs/config-only → pass. Immune to diff truncation (the bug that
    made the old LLM verifier false-reject tests that sorted past the cap)."""
    loop = BoardLoop({"goal_verify": True})

    def _git_listing(names):
        async def _git(wt, *args, timeout=60):
            # `add -A` → empty; `diff --cached --name-only` → the changed-file list
            return (0, names if "--name-only" in args else "", "")

        return _git

    # code changed, NO test → gap
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ngraph/config.py"))
    gap = await loop._verify_goal(FEATURE, "/wt", "main")
    assert gap and "no test" in gap.lower()

    # code changed WITH a test → pass (this is the case the old verifier wrongly blocked)
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ntests/test_inbox.py"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # code changed, no test, but the coder declared NO_TEST_NEEDED in its summary → pass
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py"))
    reply = "## Summary\n\nPure rename refactor.\nNO_TEST_NEEDED: behavior unchanged, covered by existing tests"
    assert await loop._verify_goal(FEATURE, "/wt", "main", reply) is None
    # ...but without the declaration, the same change is still a gap
    assert await loop._verify_goal(FEATURE, "/wt", "main", "I changed inbox/store.py") is not None

    # docs/config only → pass (no code change → no test required)
    monkeypatch.setattr(worktree, "_git", _git_listing("README.md\ndocs/x.md\nconfig.yaml"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # empty diff → None (open_pr's NoChangesError job, not the gate's)
    monkeypatch.setattr(worktree, "_git", _git_listing(""))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None


async def test_verify_goal_no_test_marker_is_structural_and_summary_scoped(monkeypatch):
    """The NO_TEST_NEEDED escape hatch (#264) is not a substring scan: only a
    line-start `NO_TEST_NEEDED: <reason>` inside the LAST ## Summary section counts.
    Mentioning the marker mid-narration, placing it before the summary, or omitting
    the reason leaves the gap in place."""
    loop = BoardLoop({"goal_verify": True})

    async def _git(wt, *args, timeout=60):
        return (0, "inbox/store.py" if "--name-only" in args else "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def gap(reply):
        return await loop._verify_goal(FEATURE, "/wt", "main", reply)

    # mid-narration mention, no summary section at all → still a gap
    assert await gap("If a test doesn't apply I could write NO_TEST_NEEDED: skip it.") is not None
    # marker BEFORE the summary section → still a gap (summary-scoped)
    assert await gap("NO_TEST_NEEDED: config only\n## Summary\n\nTweaked a default.") is not None
    # mid-sentence inside the summary → still a gap (line start required)
    assert await gap("## Summary\n\nI decided NO_TEST_NEEDED: because it is a refactor.") is not None
    # bare marker with no reason → still a gap (the reason is the evidence)
    assert await gap("## Summary\n\nRenamed a variable.\nNO_TEST_NEEDED:") is not None
    # marker under an EARLIER summary only → still a gap (last-occurrence, the _pr_body rule)
    assert (
        await gap("## Summary\n\ndraft\nNO_TEST_NEEDED: draft reason\n## Summary\n\nFinal: renamed a variable.")
        is not None
    )
    # marker in a section AFTER the final summary → still a gap (the summary ends
    # at the next heading — a later section is not the summary)
    assert await gap("## Summary\n\nRenamed a variable.\n## Notes\n\nNO_TEST_NEEDED: covered elsewhere") is not None
    # ...including a LEVEL-ONE heading — any heading level ends the summary
    assert await gap("## Summary\n\nRenamed a variable.\n# Appendix\n\nNO_TEST_NEEDED: covered elsewhere") is not None
    # ...and the gap tells the coder where the marker belongs
    assert "## Summary" in (await gap("no marker") or "")

    # a well-formed line in the (last) summary → pass, indentation tolerated
    assert await gap("narration\n## Summary\n\nPure rename.\n  NO_TEST_NEEDED: existing tests cover it\n") is None


def test_no_test_marker_last_summary_and_reason_extraction():
    """_no_test_marker keeps the LAST ## Summary and returns the declared reason."""
    reply = (
        "## Summary\n\ndraft\nNO_TEST_NEEDED: stale draft reason\n"
        "## Requirements\n- r1: done\n"
        "## Summary\n\nFinal.\nNO_TEST_NEEDED: pure refactor, no behavior change\n"
    )
    assert _no_test_marker(reply) == "pure refactor, no behavior change"
    assert _no_test_marker("NO_TEST_NEEDED: no summary section") is None
    assert _no_test_marker("") is None
    # the summary ends at the next heading of ANY level — a marker in a later
    # section is prose, whether that section is #, ## or ### deep
    assert _no_test_marker("## Summary\n\nFinal.\n## Appendix\nNO_TEST_NEEDED: outside the summary") is None
    assert _no_test_marker("## Summary\n\nFinal.\n# Appendix\nNO_TEST_NEEDED: under a level-one heading") is None
    assert _no_test_marker("## Summary\n\nFinal.\n### Details\nNO_TEST_NEEDED: in a subsection") is None


async def test_verify_goal_fails_open_when_no_criteria(monkeypatch):
    loop = BoardLoop({"goal_verify": True})
    # No acceptance_criteria → gate must not even shell out / call the model.
    assert await loop._verify_goal({"id": "x", "acceptance_criteria": ""}, "/wt", "main") is None


async def test_drive_blocks_when_the_coder_is_not_configured(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("open_pr should not be reached")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, coder=None)
    assert store.names() == ["flag_blocked"]  # blocked before any worktree work


# ── _drive: failure classification + backoff (no real sleeps) ───────────────────


async def _no_sleep(_delay):
    return None


async def test_drive_retries_a_transient_failure_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.WorktreeError("git push failed: connection reset by peer")
        return "https://example/pr/1"

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert calls["n"] == 2  # one transient retry, then success
    assert "flag_blocked" not in store.names()
    assert loop._inflight == {}


async def test_drive_blocks_after_exhausting_transient_retries(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 503 service unavailable")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 3  # transient policy = 3 attempts, then Blocked
    assert loop._inflight == {}


async def test_drive_blocks_immediately_on_a_terminal_failure(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 403 forbidden — bad credential")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 1  # auth is terminal → no retry


# ── _drive: the stuck-coder watchdog (CoderTimeout) ─────────────────────────────


async def test_drive_blocks_on_a_coder_timeout_not_transient_retried(monkeypatch):
    calls = {"n": 0}

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        raise worktree.CoderTimeout("coder timed out after 1800s")

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("open_pr should not run after a coder timeout")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    # A timeout matches "timed out" in classify (transient), but it's a CAPABILITY
    # failure → it must NOT be transient-retried: blocked after a single attempt.
    assert calls["n"] == 1
    assert "flag_blocked" in store.names()
    assert loop._inflight == {}


class _EscalatingStore(FakeLoopStore):
    """A store that hands out one climb (fast→smart) then None (ladder top), so a
    _drive test can exercise the timeout-escalation path end to end."""

    def __init__(self, tiers):
        super().__init__()
        self._tiers = list(tiers)
        self.escalated = []

    def escalate(self, fid, reason):
        self.escalated.append((fid, reason))
        return self._tiers.pop(0) if self._tiers else None


async def test_drive_carries_timeout_context_into_the_escalated_prompt(monkeypatch):
    """A CoderTimeout that climbs the tier ladder must NOT hand the stronger model a
    byte-identical prompt (#146): the escalated dispatch leads with the ring buffer's
    timeout context — elapsed time, that no diff was produced, and the last tool/thought
    tail — injected via `_ci_feedback` so it rides the normal prompt-building path."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    # Deterministic gen clock: the timed-out gen runs 0.0s→1800.0s (elapsed 1800.0),
    # every later _monotonic() reads freeze at the tail value.
    ticks = iter([0.0, 1800.0])
    monkeypatch.setattr(coder_seam, "_monotonic", lambda: next(ticks, 1800.0))

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        if len(prompts) == 1:
            # Simulate live activity the tap would have recorded, then time out. The
            # gen buffer already exists (dispatch_coder_tapped called progress_begin
            # before falling back to worktree.dispatch_coder in a host-free test env).
            coder_seam.progress_thought("bd-1", 1, "still mapping the dispatch flow in loop.py")
            coder_seam.progress_tool(
                "bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "loop.py"}}
            )
            raise worktree.CoderTimeout("coder timed out after 1800s")
        return "the escalated coder's reply"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)

    # It escalated once (fast→smart), redispatched, and shipped a clean PR.
    assert [e[0] for e in store.escalated] == ["bd-1"]
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert len(prompts) == 2

    original, escalated = prompts
    # r1: the escalated prompt is NOT byte-identical to the one that timed out.
    assert escalated != original
    # The original carried no timeout context (nothing had failed yet).
    assert "TIMED OUT" not in original
    # r2: the escalated prompt names the elapsed time, the no-diff outcome, and the
    # last tool + thought tail mined from the progress ring buffer.
    assert "TIMED OUT" in escalated
    assert "1800.0s" in escalated
    assert "produced NO diff" in escalated
    assert "Read" in escalated and "loop.py" in escalated
    assert "still mapping the dispatch flow" in escalated
    # r3: it arrived via `_ci_feedback`, so it rides the standard rejected-attempt block.
    assert "previous attempt was REJECTED" in escalated
    assert "still mapping the dispatch flow" in loop._ci_feedback.get("bd-1", "")


async def test_drive_tier_climb_grants_a_fresh_window_despite_stale_budget_labels(monkeypatch):
    """A restarted feature whose bead carries an exhausted `budget:goal-fix` climbs
    the ladder — and the CLIMBED tier must get the fresh retry window the reset
    granted. ``_drive`` keeps its original ``feature`` projection across the climb,
    so with pop-the-key reset semantics the stronger tier's first goal-verify gap
    re-derived the exhausted count from the projection's unchanged labels and
    blocked at once: two dispatches, no goal-fix bounce, flag_blocked — instead of
    the three dispatches and clean PR asserted here."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    gaps = {"n": 0}

    async def _verify(self, feature, wt, base, coder_reply=""):
        gaps["n"] += 1
        # Gap 1 (tier fast): the persisted budget is already spent → escalate.
        # Gap 2 (tier smart, first build): must be a same-tier bounce off the
        # reset budget, NOT a block off the stale labels. Then PASS → PR.
        return None if gaps["n"] >= 3 else "missing tests for the new behavior"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _verify)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/2"

    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "the coder's reply"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "pf", "smart": "ps"}, "goal_verify": True, "goal_fix_max": 2})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    # The restart scenario: a fresh process (empty dicts), the spent budget on the bead.
    await loop._drive({**FEATURE, "labels": ["budget:goal-fix:2"]})

    # fast's budget resumed exhausted off the labels → ONE climb, straight away.
    assert len(store.escalated) == 1
    assert "goal verification failed" in store.escalated[0][1]
    # The climbed tier got its window: fast build + smart build + smart's goal-fix
    # bounce (carrying the gap), then the PR opened — never a block.
    assert len(prompts) == 3
    assert "missing tests" in prompts[2] and "ALREADY in this worktree" in prompts[2]
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls


# ── #282: keep-worktree state + feedback survive an escalation ───────────────────


async def test_drive_ledger_gate_exhaustion_escalates_into_the_same_worktree(monkeypatch):
    """r1 (#282): a requirement-ledger gate that exhausts its keep-worktree fix budget
    and climbs a tier re-dispatches into the SAME worktree — the verified impl is on
    disk — so NO fresh create_worktree runs, and the escalated tier still leads with the
    truthful gate feedback that the work is already in the worktree and the item is open."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    creates = []

    async def _create(repo, base, fid, root, title="", **_kw):
        creates.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        # fast build + fast keep-wt fix leave r1 OPEN (no disposition); the escalated
        # smart build finally disposes it so the gate passes and the PR opens.
        if len(prompts) >= 3:
            return "## Requirements\n- r1: done\n\n## Summary\nshipped"
        return "## Summary\nwip"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/282"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "pf", "smart": "ps"}, "goal_fix_max": 1})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    await loop._drive(feature)

    # ONE climb (fast→smart), triggered by the requirement-ledger exhaustion.
    assert len(store.escalated) == 1
    assert "requirements unresolved" in store.escalated[0][1]
    # The escalated dispatch REUSED the worktree — created exactly once, never again.
    assert creates == ["bd-1"]
    # fast build, fast keep-wt fix, smart escalated build — all in the one worktree.
    assert len(prompts) == 3
    # The escalated (3rd) prompt still carries the truthful keep-worktree gate feedback:
    # the impl is already in the worktree and the item is still open (feedback intact).
    assert "ALREADY in this worktree" in prompts[2]
    assert "still OPEN" in prompts[2]
    # Shipped on the escalated tier once r1 was disposed.
    assert ("open_review", "bd-1", "https://example/pr/282") in store.calls


async def test_drive_non_keep_worktree_failure_rebuilds_and_drops_stale_feedback(monkeypatch):
    """r2 (#282): a capability failure that is NOT a keep-worktree class (an empty diff
    with tool activity) keeps the existing fresh-worktree escalation — a NEW worktree for
    the climbed tier — and does NOT carry the prior round's keep-worktree feedback/diff
    (which described a worktree that no longer exists) into the escalated prompt."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    creates = []

    async def _create(repo, base, fid, root, title="", **_kw):
        creates.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        if len(prompts) == 1:
            # Fast attempt: real tool activity but no diff → escalates immediately (a
            # capability failure, not the empty-result same-tier retry).
            coder_seam.progress_tool(
                "bd-1", 1, {"phase": "start", "name": "Read", "id": "t1", "input": {"path": "a.py"}}
            )
        return "reply"

    opens = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opens["n"] += 1
        if opens["n"] == 1:
            raise worktree.NoChangesError("coder produced no commits")
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "pf", "smart": "ps"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    # Stand in for a prior keep-worktree gate-fix round: feedback + prior diff seeded,
    # both describing the (now-gone) worktree.
    loop._ci_feedback["bd-1"] = "Your implementation is ALREADY in this worktree's files — add tests."
    loop._ci_prior_diff["bd-1"] = "--- a/a.py\n+++ b/a.py\n@@ stale @@"
    await loop._drive(FEATURE)

    # A non-keep-worktree capability class → the existing fresh-worktree climb: a NEW
    # worktree for the escalated tier (two creates, not one reused).
    assert len(store.escalated) == 1
    assert creates == ["bd-1", "bd-1"]
    assert len(prompts) == 2
    # The escalated prompt does NOT carry the stale keep-worktree feedback or diff.
    assert "ALREADY in this worktree" not in prompts[1]
    assert "previous attempt was REJECTED" not in prompts[1]
    assert "@@ stale @@" not in prompts[1]
    assert loop._ci_feedback.get("bd-1") is None
    assert loop._ci_prior_diff.get("bd-1") is None
    # Shipped from the fresh worktree on the climbed tier.
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls


async def test_drive_keep_worktree_exhaustion_clears_feedback_when_no_worktree(monkeypatch):
    """r3 (#282): when a keep-worktree gate class exhausts but there is no worktree to
    carry forward (wt unset), the escalated dispatch is a from-scratch build — so both
    _ci_feedback and _ci_prior_diff are cleared before it, never leaving the fresh build
    told its work is already on disk."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    # create_worktree hands back NO worktree path — the keep-wt reuse can never fire, so
    # the ledger exhaustion escalates with wt unset (the "cannot be kept" branch).
    async def _create(repo, base, fid, root, title="", **_kw):
        return (None, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    prompts = []
    feedback_seen = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        feedback_seen.append(loop._ci_feedback.get("bd-1"))
        if len(prompts) >= 3:
            return "## Requirements\n- r1: done\n\n## Summary\nshipped"
        return "## Summary\nwip"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/3"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({"coders": {"fast": "pf", "smart": "ps"}, "goal_fix_max": 1})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "do x", "status": "open"}]}
    await loop._drive(feature)

    # The ledger exhaustion still climbed once.
    assert len(store.escalated) == 1
    assert "requirements unresolved" in store.escalated[0][1]
    # fast build + fast fix seeded the keep-worktree feedback...
    assert feedback_seen[1] and "ALREADY in this worktree" in feedback_seen[1]
    # ...but with no worktree to keep, the escalated (3rd) build ran from scratch with the
    # stale feedback and prior diff cleared.
    assert feedback_seen[2] is None
    assert loop._ci_prior_diff.get("bd-1") is None
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


# ── concurrency: _spawn_ready claims up to max_concurrent ────────────────────────


class _ClaimStore:
    """A peekable ready queue + atomic claim(fid), mirroring the store API _spawn_ready
    now uses. Records claims so a test can prove the caps/gates stop the puller."""

    def __init__(self, features, in_review=0):
        self._features = [dict(f) for f in features]
        self._in_review = in_review
        self.claimed = []
        self.last_relaxed = None

    def ready_queue(self, relaxed=False):
        self.last_relaxed = relaxed
        return [f for f in self._features if f["id"] not in self.claimed]

    def claim(self, fid, assignee=""):
        if fid in self.claimed:
            return None
        self.claimed.append(fid)
        return next((f for f in self._features if f["id"] == fid), None)

    def list_features(self, state=None):
        return [{"id": f"rev-{i}"} for i in range(self._in_review)] if state == "in_review" else []


def _ready(fid, files, project=""):
    return {"id": fid, "board_state": "ready", "files_to_modify": files, "project": project}


async def _hold_drives(loop, monkeypatch):
    """Replace _drive with a coroutine that blocks, so spawned tasks stay 'running'.
    Returns a finalizer the test calls to release + await them."""
    release = asyncio.Event()

    async def _hold(feature):
        await release.wait()

    monkeypatch.setattr(loop, "_drive", _hold)

    async def _finish():
        release.set()
        await asyncio.gather(*loop._drives, return_exceptions=True)

    return _finish


async def test_spawn_ready_claims_up_to_max_concurrent(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is True
        assert len(loop._drives) == 2  # capped at max_concurrent
        assert store.claimed == ["bd-1", "bd-2"]  # stopped claiming once full
    finally:
        await finish()


async def test_reload_raises_the_cap_for_the_next_claim_scan_without_a_restart(monkeypatch):
    """The bug this fixes: six projects' worth of ready cards behind a one-slot loop,
    and a max_concurrent edit that only landed after a restart. After reload() the
    very next _spawn_ready claims up to the NEW cap on the SAME running loop."""
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        await loop._spawn_ready()
        assert store.claimed == ["bd-1"]  # serial: one slot
        assert loop.reload(_HostConfig({"max_concurrent": 3})) == {"max_concurrent": (1, 3)}
        await loop._spawn_ready()
        assert store.claimed == ["bd-1", "bd-2", "bd-3"]  # both extra slots filled this tick
        assert len(loop._drives) == 3
    finally:
        await finish()


async def test_reload_lowering_the_cap_never_kills_a_drive(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        await loop._spawn_ready()
        assert len(loop._drives) == 2
        loop.reload({"max_concurrent": 1})
        assert len(loop._drives) == 2  # in-flight builds keep running…
        assert await loop._spawn_ready() is False  # …the loop just stops claiming until under the cap
        assert store.claimed == ["bd-1", "bd-2"]
    finally:
        await finish()


async def test_spawn_ready_is_false_when_nothing_ready(monkeypatch):
    store = _ClaimStore([])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    assert await loop._spawn_ready() is False
    assert loop._drives == set()


async def test_spawn_ready_skips_a_file_conflicting_candidate(monkeypatch):
    # bd-1 + bd-2 both touch shared.py; bd-3 touches other.py.
    store = _ClaimStore([_ready("bd-1", ["shared.py"]), _ready("bd-2", ["shared.py"]), _ready("bd-3", ["other.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 3})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        await loop._spawn_ready()
        # bd-1 claimed; bd-2 deferred (overlaps bd-1's file); bd-3 claimed (disjoint).
        assert store.claimed == ["bd-1", "bd-3"]
        # #197: guard keys are (project, path); unstamped cards resolve to "default".
        assert loop._inflight_files == {"bd-1": {("default", "shared.py")}, "bd-3": {("default", "other.py")}}
    finally:
        await finish()


async def test_spawn_ready_respects_the_review_wip_limit(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])], in_review=5)  # already at the cap
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2, "max_pending_reviews": 5})
    assert await loop._spawn_ready() is False
    assert store.claimed == []  # paused: too many PRs await review


async def test_drive_done_releases_its_files(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1})

    async def _quick(feature):
        return None

    monkeypatch.setattr(loop, "_drive", _quick)
    await loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callbacks run
    assert loop._inflight_files == {}  # files released when the drive finished
    assert loop._drives == set()


# ── #217: task-type dispatch (delegate_to for agents, skip-and-wait for humans) ───


class _TaskStore:
    """A ready queue + the store verbs the task dispatch path uses (claim/claim_task,
    get_feature, record_delivery, flag_blocked, requeue). Records calls so a test can
    prove which edge a task took."""

    def __init__(self, features):
        self._features = [dict(f) for f in features]
        self.claimed = []
        self.claim_task_calls = []  # (#356) (fid, assignee) the task path claimed with
        self.calls = []  # (verb, fid, extra…)

    def ready_queue(self, relaxed=False):
        return [f for f in self._features if f["id"] not in self.claimed]

    def claim(self, fid, assignee=""):
        # The CODING-feature primitive (atomic `--claim`, actor-assignment) — still used
        # by the feature path (a coding card in a _TaskStore); the task path uses
        # claim_task below so the dispatch-target assignee survives (#356).
        if fid in self.claimed:
            return None
        self.claimed.append(fid)
        f = next((x for x in self._features if x["id"] == fid), None)
        if f is None:
            return None
        return dict(f, board_state="in_progress", assignee=assignee or f.get("assignee", ""))

    def claim_task(self, fid, assignee=""):
        # #356: the TASK primitive — PRESERVES the dispatch target rather than reassigning
        # to the actor. State-race safe: a second claim (already in_progress) returns None.
        if fid in self.claimed:
            return None
        self.claimed.append(fid)
        self.claim_task_calls.append((fid, assignee))
        f = next((x for x in self._features if x["id"] == fid), None)
        if f is None:
            return None
        return dict(f, board_state="in_progress", assignee=(assignee or f.get("assignee", "")))

    def list_features(self, state=None):
        return []

    def get_feature(self, fid):
        return next((dict(x) for x in self._features if x["id"] == fid), None)

    def record_delivery(self, fid, text=""):
        self.calls.append(("record_delivery", fid, text))
        return {"id": fid}

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", fid, reason))
        return {"id": fid}

    def requeue(self, fid):
        self.calls.append(("requeue", fid))
        return {"id": fid}

    def names(self):
        return [c[0] for c in self.calls]


def _task(fid, *, assignee="", spec="do the task", criteria="WHEN done THE SYSTEM SHALL deliver"):
    return {
        "id": fid,
        "board_state": "ready",
        "issue_type": "task",
        "assignee": assignee,
        "title": f"Task {fid}",
        "spec": spec,
        "acceptance_criteria": criteria,
        "files_to_modify": [],
    }


def test_build_task_prompt_carries_spec_and_criteria_but_no_coder_rules():
    """The task prompt leads with the spec + acceptance criteria and frames a
    deliverable (no worktree/PR), unlike the coder prompt's 'write tests / open no PR'."""
    loop = BoardLoop({"coder": "proto"})
    prompt = loop._build_task_prompt(
        _task("bd-1", assignee="agent-bot", spec="Draft the RFC", criteria="WHEN approved THE SYSTEM SHALL publish")
    )
    assert "Draft the RFC" in prompt  # the spec…
    assert "WHEN approved THE SYSTEM SHALL publish" in prompt  # …and the acceptance criteria
    assert "## Acceptance criteria" in prompt
    assert "deliverable" in prompt.lower()
    assert "worktree" in prompt.lower()  # tells the delegate there is no worktree/PR
    assert "Write automated tests" not in prompt  # NOT the coder prompt's rules


async def test_spawn_ready_dispatches_an_acp_task_via_delegate_to(monkeypatch):
    """r1/r2/r3/r6: a task with an ACP agent assignee is dispatched via delegate_to with
    the spec + acceptance criteria as the prompt — NO worktree — and the reply is
    recorded as the deliverable (→ in_review)."""
    store = _TaskStore(
        [_task("bd-task", assignee="agent-bot", spec="Write the ADR", criteria="WHEN merged THE SYSTEM SHALL link it")]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_worktree(*a, **k):
        raise AssertionError("a task must NOT create a git worktree")

    monkeypatch.setattr(worktree, "create_worktree", _no_worktree)

    prompts = []

    async def _dispatch_task(delegate, prompt, *, timeout=None):
        prompts.append(prompt)
        return "Here is the ADR deliverable."

    monkeypatch.setattr(coder_seam, "dispatch_task", _dispatch_task)

    delegate = object()
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: delegate if name == "agent-bot" else None)

    assert await loop._spawn_ready() is True  # an ACP task IS a drive → counts as started
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callback run

    assert store.claimed == ["bd-task"]
    assert len(prompts) == 1
    assert "Write the ADR" in prompts[0]  # the spec…
    assert "WHEN merged THE SYSTEM SHALL link it" in prompts[0]  # …and the acceptance criteria
    assert ("record_delivery", "bd-task", "Here is the ADR deliverable.") in store.calls
    assert loop._drives == set() and loop._inflight_files == {}  # the slot released on completion


async def test_spawn_ready_parks_a_human_task_without_dispatching(monkeypatch):
    """r1/r4: a task with a human (non-ACP) assignee is claimed to in_progress and left
    there — no worktree, no delegate dispatch — so its delivery can arrive out-of-band."""
    store = _TaskStore([_task("bd-human", assignee="alice")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_worktree(*a, **k):
        raise AssertionError("a parked human task must NOT create a worktree")

    async def _no_dispatch(*a, **k):
        raise AssertionError("a human task must NOT dispatch a delegate")

    monkeypatch.setattr(worktree, "create_worktree", _no_worktree)
    monkeypatch.setattr(coder_seam, "dispatch_task", _no_dispatch)

    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)  # alice is not an ACP agent

    assert await loop._spawn_ready() is False  # parked → nothing started this tick
    assert store.claimed == ["bd-human"]  # …but it WAS claimed to in_progress
    assert loop._drives == set()  # holds no concurrency slot
    assert "bd-human" not in loop._inflight_files
    assert store.calls == []  # no delivery, no block — just parked


async def test_a_parked_human_task_does_not_consume_a_concurrency_slot(monkeypatch):
    """r5: a human-wait task parked in_progress must NOT count toward max_concurrent —
    a coding feature behind it still gets the single slot."""
    store = _TaskStore([_task("bd-human", assignee="alice"), _ready("bd-feat", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1, "coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)  # human, no ACP agent
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is True  # the coding feature started
        # Both claimed: the human task parked (no slot), the coding feature took the slot.
        assert store.claimed == ["bd-human", "bd-feat"]
        assert len(loop._drives) == 1  # ONLY the coding feature's drive — the park held no slot
    finally:
        await finish()


async def test_acp_task_dispatch_failure_is_classified_and_blocks(monkeypatch):
    """r7: a delegate error on a task dispatch is classified like a coder failure and
    blocks the card — no deliverable is recorded."""
    store = _TaskStore([_task("bd-task", assignee="agent-bot")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _boom(delegate, prompt, *, timeout=None):
        raise worktree.WorktreeError("coder dispatch failed: delegate exploded")

    monkeypatch.setattr(coder_seam, "dispatch_task", _boom)

    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    await loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)

    assert "flag_blocked" in store.names()
    assert "record_delivery" not in store.names()
    reason = next(c[2] for c in store.calls if c[0] == "flag_blocked")
    assert reason.startswith("terminal:")  # classify() → TERMINAL for an unknown error


async def test_reconcile_orphan_leaves_a_human_task_parked(monkeypatch):
    """r4 (durability): a human/unassigned task parked in_progress is NOT orphaned — the
    sweep/boot reconcile leaves it be (its delivery arrives async) rather than requeueing
    it to ready and re-parking it every sweep."""
    store = _TaskStore([_task("bd-human", assignee="alice")])
    store.claimed.append("bd-human")  # already in_progress
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)
    await loop._reconcile_orphan("bd-human")
    assert store.names() == []  # not requeued, not blocked — left parked


async def test_reconcile_orphan_requeues_a_dead_acp_task(monkeypatch):
    """#217: an ACP-agent task whose drive died mid-flight IS orphaned — reconcile
    requeues it to ready for a clean re-dispatch."""
    store = _TaskStore([_task("bd-task", assignee="agent-bot")])
    store.claimed.append("bd-task")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._reconcile_orphan("bd-task")
    assert store.names() == ["requeue"]


async def test_sweep_reclaims_a_dead_acp_task_end_to_end(monkeypatch):
    """#303 (r3): a task stuck in_progress with a dead ACP drive is reclaimed by the
    health sweep END TO END. The sweep enumerates ``list_features(state="in_progress")``
    — which now surfaces task beads (the old `feature`-only projection hid them, so this
    branch of ``_reconcile_orphan`` was structurally UNREACHABLE) — reaches the task
    branch, and requeues the task for a clean re-dispatch. The task branch returns before
    any PR/worktree probe (that path is for coding features), so ``pr_url_for_branch``
    must never be reached — proving the task, not the feature, path handled it."""
    task = {"id": "bd-task", "issue_type": "task", "assignee": "agent-bot", "board_state": "in_progress"}

    class _Store:
        def __init__(self):
            self.requeued = []

        def list_features(self, state=None):
            # the fix: the in_progress projection now includes task beads
            return [dict(task)] if state == "in_progress" else []

        def get_feature(self, fid):
            return dict(task) if fid == task["id"] else None

        def requeue(self, fid):
            self.requeued.append(fid)

        def archive_stale(self, archive_after_days=7):
            return []

    store = _Store()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _boom_pr(*_a, **_k):
        raise AssertionError("a task must not probe for a PR branch — that path is for coding features")

    monkeypatch.setattr(worktree, "pr_url_for_branch", _boom_pr)
    loop = BoardLoop({})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())  # agent-bot is an ACP agent

    await loop._sweep()
    assert store.requeued == ["bd-task"]  # the dead-drive task was reclaimed to ready


async def test_spawn_ready_task_branch_leaves_coding_features_unchanged(monkeypatch):
    """r6: an issue_type=feature card is NOT diverted by the task branch — it takes the
    normal claim → _drive (worktree) path, stamping its files in _inflight_files."""
    store = _TaskStore([_ready("bd-feat", ["a.py"])])  # no issue_type → a coding feature
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_task(*a, **k):
        raise AssertionError("a coding feature must NOT take the task dispatch path")

    monkeypatch.setattr(coder_seam, "dispatch_task", _no_task)
    loop = BoardLoop({"max_concurrent": 1, "coder": "proto"})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is True
        assert store.claimed == ["bd-feat"]
        assert loop._inflight_files == {"bd-feat": {("default", "a.py")}}  # went through the coder path
    finally:
        await finish()


# ── #304: dispatch a named A2A sister-agent task assignee over A2A ────────────────


def _sister(kind: str):
    """A resolved sister-agent Delegate stand-in carrying just the ``type`` the task
    dispatch path branches on (``acp`` | ``a2a``)."""
    import types

    return types.SimpleNamespace(type=kind, name=f"{kind}-agent")


async def test_spawn_ready_dispatches_an_a2a_sister_agent_task(monkeypatch):
    """#304 r1: a task whose assignee names an A2A sister agent (not an ACP coder) is
    claimed, driven, and its reply recorded via record_delivery → in_review. Today it
    parks: resolution was ACP-only, so a named A2A agent fell through to the
    human/unassigned path. Resolution is now ACP-first, then A2A — the acp lookup
    misses, the a2a lookup resolves the sister agent."""
    store = _TaskStore(
        [_task("bd-a2a", assignee="quinn", spec="Audit the API", criteria="WHEN done THE SYSTEM SHALL report")]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_worktree(*a, **k):
        raise AssertionError("a task must NOT create a git worktree")

    monkeypatch.setattr(worktree, "create_worktree", _no_worktree)

    seen = {}

    async def _dispatch_task(delegate, prompt, *, timeout=None):
        seen["delegate"] = delegate
        seen["prompt"] = prompt
        seen["timeout"] = timeout
        return "The audit deliverable."

    monkeypatch.setattr(coder_seam, "dispatch_task", _dispatch_task)

    a2a = _sister("a2a")

    def _resolve(name, expect):
        # ACP lookup misses; the A2A lookup is what resolves the sister agent — proving
        # the ACP-first-then-A2A order (r2's regression pin lives in the acp test above).
        return a2a if (name == "quinn" and expect == "a2a") else None

    loop = BoardLoop({"coder": "proto", "coder_timeout_s": 1800})
    monkeypatch.setattr(loop, "_resolve_delegate", _resolve)

    assert await loop._spawn_ready() is True  # an A2A task IS a drive → counts as started
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callback run

    assert store.claimed == ["bd-a2a"]
    assert seen["delegate"] is a2a  # the A2A sister agent, not the (missing) ACP coder
    assert seen["timeout"] == 1800  # bounded by coder_timeout_s (30 min default)
    assert "Audit the API" in seen["prompt"]  # the spec…
    assert "WHEN done THE SYSTEM SHALL report" in seen["prompt"]  # …and the acceptance criteria
    assert ("record_delivery", "bd-a2a", "The audit deliverable.") in store.calls
    assert loop._drives == set() and loop._inflight_files == {}  # slot released on completion


async def test_spawn_ready_parks_a_task_that_resolves_to_neither_agent_type(monkeypatch):
    """#304 r3: an assignee that resolves to NEITHER an ACP coder nor an A2A sister
    agent still parks in_progress with the existing log line — no dispatch, no block."""
    store = _TaskStore([_task("bd-ghost", assignee="ghost")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_dispatch(*a, **k):
        raise AssertionError("an unresolvable assignee must NOT dispatch a delegate")

    monkeypatch.setattr(coder_seam, "dispatch_task", _no_dispatch)

    loop = BoardLoop({"coder": "proto"})
    # every lookup misses — 'ghost' is neither an acp coder nor an a2a agent
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)

    assert await loop._spawn_ready() is False  # parked → nothing started this tick
    assert store.claimed == ["bd-ghost"]  # …but it WAS claimed to in_progress
    assert loop._drives == set()  # holds no concurrency slot
    assert "bd-ghost" not in loop._inflight_files
    assert store.calls == []  # no delivery, no block — just parked


async def test_a2a_task_dispatch_failure_is_classified_and_blocks(monkeypatch):
    """#304 r4: an A2A dispatch failure (surfaced as WorktreeError by coder_seam) is
    classified like a coder failure and blocks the card with the CLASSIFIED reason — no
    deliverable recorded. Same terminal block-for-triage edge as the ACP path, whose
    classification is unchanged (test_acp_task_dispatch_failure_is_classified_and_blocks)."""
    store = _TaskStore([_task("bd-a2a-fail", assignee="quinn")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _boom(delegate, prompt, *, timeout=None):
        raise worktree.WorktreeError("coder dispatch failed: a2a agent refused the task")

    monkeypatch.setattr(coder_seam, "dispatch_task", _boom)

    a2a = _sister("a2a")
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: a2a if expect == "a2a" else None)

    await loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)

    assert "flag_blocked" in store.names()
    assert "record_delivery" not in store.names()
    reason = next(c[2] for c in store.calls if c[0] == "flag_blocked")
    assert reason.startswith("terminal:")  # classify() → TERMINAL — the classified reason


async def test_reconcile_orphan_requeues_a_dead_a2a_task(monkeypatch):
    """#304: an A2A sister-agent task whose drive died mid-flight IS orphaned — the
    ACP-first-then-A2A resolution recognizes it as a dispatchable agent (not a parked
    human), so reconcile requeues it for a clean re-dispatch."""
    store = _TaskStore([_task("bd-a2a", assignee="quinn")])
    store.claimed.append("bd-a2a")  # already in_progress
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: _sister("a2a") if expect == "a2a" else None)
    await loop._reconcile_orphan("bd-a2a")
    assert store.names() == ["requeue"]


# ── #311: dispatch a task assigned to the board's OWN agent through HOST.invoke ────


def test_is_self_assignee_matches_only_the_own_name_and_reserved_aliases():
    """#311: the self-dispatch trigger is NARROW — the board's configured coder name
    (case-insensitively) or the reserved ``self``/``agent`` aliases. A sister-agent name,
    a human, or an empty assignee is never self, so ACP/A2A dispatch is untouched (r4)."""
    loop = BoardLoop({"coder": "proto"})
    assert loop._is_self_assignee("proto")  # the board's own configured name…
    assert loop._is_self_assignee("PROTO")  # …case-insensitively
    assert loop._is_self_assignee("self")  # reserved alias
    assert loop._is_self_assignee("agent")  # reserved alias
    assert not loop._is_self_assignee("agent-bot")  # a sister ACP agent — NOT the "agent" alias
    assert not loop._is_self_assignee("quinn")  # an A2A sister agent
    assert not loop._is_self_assignee("alice")  # a human
    assert not loop._is_self_assignee("")  # unassigned
    # With no coder configured, ONLY the aliases are self — no accidental match on "".
    bare = BoardLoop({})
    assert bare._is_self_assignee("self") and bare._is_self_assignee("agent")
    assert not bare._is_self_assignee("proto")


async def test_spawn_ready_dispatches_a_self_task_via_host_invoke(monkeypatch):
    """#311 r1: a task assigned to the board's OWN agent (here its coder name) is dispatched
    first-party through HOST.invoke with the spec + acceptance criteria as the prompt and a
    stable per-card session id — NO worktree — and the reply is recorded as the deliverable
    (→ in_review). Red-is-reachable: today such a task parks (it resolves to no delegate)."""
    store = _TaskStore(
        [_task("bd-self", assignee="proto", spec="Draft the ADR", criteria="WHEN merged THE SYSTEM SHALL link it")]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_worktree(*a, **k):
        raise AssertionError("a self task must NOT create a git worktree")

    monkeypatch.setattr(worktree, "create_worktree", _no_worktree)

    invoke = object()  # the resolved HOST.invoke callable (feature-detected)
    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: invoke)

    seen = {}

    async def _dispatch_self(inv, prompt, session_id, *, timeout=None):
        seen["invoke"] = inv
        seen["prompt"] = prompt
        seen["session_id"] = session_id
        seen["timeout"] = timeout
        return "The ADR deliverable."

    monkeypatch.setattr(coder_seam, "dispatch_self", _dispatch_self)

    loop = BoardLoop({"coder": "proto", "coder_timeout_s": 1800})

    assert await loop._spawn_ready() is True  # a self task IS a drive → counts as started
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callback run

    assert store.claimed == ["bd-self"]
    assert seen["invoke"] is invoke  # the resolved HOST.invoke, driven first-party
    assert seen["session_id"] == "board-self-bd-self"  # stable per-card session id
    assert seen["timeout"] == 1800  # bounded by coder_timeout_s
    assert "Draft the ADR" in seen["prompt"]  # the spec…
    assert "WHEN merged THE SYSTEM SHALL link it" in seen["prompt"]  # …and the acceptance criteria
    assert ("record_delivery", "bd-self", "The ADR deliverable.") in store.calls
    assert loop._drives == set() and loop._inflight_files == {}  # slot released on completion
    assert loop._self_inflight is False  # the one-in-flight guard cleared


async def test_spawn_ready_dispatches_the_self_alias_through_host_invoke(monkeypatch):
    """#311 r1: the reserved ``self`` alias resolves to the host even when NO coder name is
    configured — the aliases are always self, so an operator can hand a task to the board
    itself without naming it."""
    store = _TaskStore([_task("bd-alias", assignee="self")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())

    async def _dispatch_self(inv, prompt, session_id, *, timeout=None):
        return "alias deliverable"

    monkeypatch.setattr(coder_seam, "dispatch_self", _dispatch_self)

    loop = BoardLoop({})  # no coder configured — only the aliases are self
    assert await loop._spawn_ready() is True
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)
    assert ("record_delivery", "bd-alias", "alias deliverable") in store.calls


async def test_spawn_ready_parks_a_self_task_when_the_host_has_no_invoke(monkeypatch):
    """#311 r2: a host predating the HOST.invoke seam (or the host-free env) → the self task
    parks in_progress with the existing park log line, exactly like a human/unassigned task.
    No dispatch, no delivery, no block, and the one-in-flight guard is never raised."""
    store = _TaskStore([_task("bd-self", assignee="agent")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: None)  # no seam

    async def _no_dispatch(*a, **k):
        raise AssertionError("a self task must NOT dispatch when the host has no invoke seam")

    monkeypatch.setattr(coder_seam, "dispatch_self", _no_dispatch)

    loop = BoardLoop({"coder": "proto"})
    assert await loop._spawn_ready() is False  # parked → nothing started this tick
    assert store.claimed == ["bd-self"]  # …but it WAS claimed to in_progress
    assert loop._drives == set()  # holds no concurrency slot
    assert "bd-self" not in loop._inflight_files
    assert store.calls == []  # no delivery, no block — just parked
    assert loop._self_inflight is False  # never raised — the seam was absent


async def test_a_second_self_task_parks_while_one_self_dispatch_is_in_flight(monkeypatch):
    """#311 r3: only ONE self-dispatch runs per board at a time — a second self-assigned
    task parks in_progress rather than invoking the host recursively/concurrently. The
    first stays in flight (held), the second is claimed-and-parked, and only the first
    holds a concurrency slot."""
    store = _TaskStore([_task("bd-self-1", assignee="self"), _task("bd-self-2", assignee="self")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())

    release = asyncio.Event()
    dispatched = []

    async def _hold_dispatch(inv, prompt, session_id, *, timeout=None):
        dispatched.append(session_id)
        await release.wait()  # keep the first self-dispatch in flight
        return "done"

    monkeypatch.setattr(coder_seam, "dispatch_self", _hold_dispatch)

    loop = BoardLoop({"max_concurrent": 5, "coder": "proto"})
    try:
        assert await loop._spawn_ready() is True  # the first self task dispatched
        await asyncio.sleep(0)  # let the held drive reach its first await
        assert store.claimed == ["bd-self-1", "bd-self-2"]  # both claimed: 1st drives, 2nd parks
        assert len(loop._drives) == 1  # ONLY the first is a drive — the second parked (no slot)
        assert loop._self_inflight is True  # one self-dispatch in flight
        assert dispatched == ["board-self-bd-self-1"]  # the host was invoked ONCE, not recursively
    finally:
        release.set()
        await asyncio.gather(*list(loop._drives), return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run
    assert loop._self_inflight is False  # guard cleared once the first completed
    assert ("record_delivery", "bd-self-1", "done") in store.calls
    assert ("record_delivery", "bd-self-2", "done") not in store.calls  # the parked one was NOT delivered


async def test_a_self_task_parks_while_a_cancelled_dispatchs_worker_still_runs(monkeypatch):
    """`_self_inflight` is cleared by the drive's done-callback, which fires as soon as the
    drive is CANCELLED — while the uncancellable worker may still be on the host. The
    admission gate therefore also consults `host_invoke_busy()`, so the next self task
    parks instead of racing a call that never stopped."""
    store = _TaskStore([_task("bd-after-cancel", assignee="self")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda *a, **k: lambda *_a, **_k: "x")
    monkeypatch.setattr(coder_seam, "host_invoke_busy", lambda: True)  # abandoned worker STILL running

    async def _no_self(*a, **k):
        raise AssertionError("must not invoke the host while an abandoned worker is live")

    monkeypatch.setattr(coder_seam, "dispatch_self", _no_self)

    loop = BoardLoop({"coder": "proto"})
    assert loop._self_inflight is False  # the guard is already free — only the flag holds it
    assert await loop._dispatch_task(store, _task("bd-after-cancel", assignee="self")) == "parked"
    assert not loop._drives


async def test_self_task_dispatch_failure_is_classified_and_blocks(monkeypatch):
    """#311 r5: a self-dispatch failure (surfaced as WorktreeError by coder_seam) is
    classified like a coder failure and blocks the card with the CLASSIFIED reason — no
    deliverable recorded — the same terminal block-for-triage edge as the ACP/A2A paths.
    The one-in-flight guard is cleared even on the failure exit."""
    store = _TaskStore([_task("bd-self-fail", assignee="agent")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())

    async def _boom(inv, prompt, session_id, *, timeout=None):
        raise worktree.WorktreeError("coder dispatch failed: the host agent errored")

    monkeypatch.setattr(coder_seam, "dispatch_self", _boom)

    loop = BoardLoop({"coder": "proto"})
    await loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)

    assert "flag_blocked" in store.names()
    assert "record_delivery" not in store.names()
    reason = next(c[2] for c in store.calls if c[0] == "flag_blocked")
    assert reason.startswith("terminal:")  # classify() → TERMINAL — the classified reason
    assert loop._self_inflight is False  # guard released on the failure exit — the next self task can run


async def test_a_timed_out_sync_self_invoke_holds_the_guard_until_its_thread_settles(monkeypatch):
    """#311 review finding: a SYNCHRONOUS HOST.invoke that outlives ``coder_timeout_s`` runs on
    an uncancellable worker thread. The self-drive must NOT be marked done — which would clear
    the one-in-flight guard and let a second self task invoke the host CONCURRENTLY — until that
    thread genuinely settles. Driving the REAL ``coder_seam.dispatch_self`` (the code under
    test): with the tiny timeout long since fired, the guard is STILL held and the drive is
    STILL live while the thread runs; only once the thread finishes does the drive complete and
    the guard clear — and the card blocks on the timeout, never delivers. Red-is-reachable: the
    abandon-on-timeout code returns from the drive at the timeout and clears the guard while the
    thread is still executing."""
    import threading

    store = _TaskStore([_task("bd-self", assignee="self")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    started = threading.Event()
    release = threading.Event()

    def invoke(prompt, session_id, *, tool_fence=None):
        started.set()
        release.wait()  # the thread cannot be cancelled — it outlives the timeout
        return "late deliverable"

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: invoke)
    # NB: coder_seam.dispatch_self is NOT stubbed here — its real drain is what we exercise.

    loop = BoardLoop({"coder": "proto", "coder_timeout_s": 0.05})
    try:
        assert await loop._spawn_ready() is True  # the self task dispatched → a real drive
        for _ in range(200):  # let the worker thread actually start
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.15)  # well past coder_timeout_s — the timeout has fired
        assert loop._self_inflight is True  # guard STILL held: the drive is not done while the thread runs
        assert len(loop._drives) == 1  # …and it still holds its slot
    finally:
        release.set()  # let the thread finish so the drain completes
        await asyncio.gather(*list(loop._drives), return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run
    assert loop._self_inflight is False  # cleared only once the drained thread settled
    assert "flag_blocked" in store.names()  # blocked on the timeout…
    assert "record_delivery" not in store.names()  # …never delivered


async def test_spawn_ready_does_not_divert_an_acp_assignee_to_self_dispatch(monkeypatch):
    """#311 r4 (regression pin): even with a coder name configured, a task whose assignee
    names a SISTER ACP agent (not the board's own name, not an alias) still takes the
    delegate path — dispatch_task, never dispatch_self, and resolve_self_invoke is not
    consulted."""
    store = _TaskStore([_task("bd-acp", assignee="agent-bot", spec="Write the ADR")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_self(*a, **k):
        raise AssertionError("a sister-agent assignee must NOT reach the self-dispatch seam")

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: pytest.fail("must not consult the host seam"))
    monkeypatch.setattr(coder_seam, "dispatch_self", _no_self)

    dispatched = []

    async def _dispatch_task(delegate, prompt, *, timeout=None):
        dispatched.append(prompt)
        return "ADR via the ACP delegate"

    monkeypatch.setattr(coder_seam, "dispatch_task", _dispatch_task)

    delegate = object()
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: delegate if name == "agent-bot" else None)

    assert await loop._spawn_ready() is True
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)
    assert len(dispatched) == 1 and "Write the ADR" in dispatched[0]  # the ACP delegate path ran once…
    assert ("record_delivery", "bd-acp", "ADR via the ACP delegate") in store.calls
    assert loop._self_inflight is False  # the self guard was never touched


async def test_spawn_ready_logs_the_claim_decision_with_skip_reason(monkeypatch, caplog):
    """#124: when a lower-priority card claims ahead of a higher one, the single
    per-tick claim_decision line must name the selected fid AND why the higher card
    was passed over — the evidence to tell the hot-file guard from a lost claim race.
    Mirrors the caplog pattern in test_store.test_run_logs_the_retry_count_on_final_success."""
    # bd-hi is FIRST in the ready queue (higher priority) but collides on shared.py with
    # an in-flight build; bd-lo is disjoint and gets claimed ahead of it.
    store = _ClaimStore([_ready("bd-hi", ["shared.py"]), _ready("bd-lo", ["other.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    # #197: guard keys are (project, path); unstamped cards resolve to "default".
    loop._inflight_files = {"bd-live": {("default", "shared.py")}}  # an in-flight build owns shared.py
    finish = await _hold_drives(loop, monkeypatch)
    try:
        with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
            await loop._spawn_ready()
    finally:
        await finish()
    assert store.claimed == ["bd-lo"]  # the lower-priority card claimed ahead of bd-hi
    lines = [m for m in caplog.messages if "claim_decision" in m]
    assert len(lines) == 1  # exactly one structured line per tick
    payload = json.loads(lines[0].split("claim_decision", 1)[1])  # parseable without log grepping
    assert payload["selected"] == ["bd-lo"]  # the selected fid is recorded
    skip = {s["fid"]: s for s in payload["skipped"]}
    assert skip["bd-hi"]["reason"] == "hot-file"  # the passed-over card's reason…
    assert skip["bd-hi"]["overlaps"] == ["bd-live"]  # …names the in-flight build it collides with
    assert skip["bd-hi"]["files"] == ["shared.py"]


async def test_spawn_ready_logs_a_claim_race_skip(monkeypatch, caplog):
    """#124: a candidate whose claim() returns None (lost the atomic-claim race) is
    recorded with a distinct, parseable reason — not conflated with the hot-file guard."""

    class _RacingStore(_ClaimStore):
        def claim(self, fid, assignee=""):
            if fid == "bd-hi":
                return None  # someone else won the claim race for the higher card
            return super().claim(fid, assignee=assignee)

    store = _RacingStore([_ready("bd-hi", ["a.py"]), _ready("bd-lo", ["b.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
            await loop._spawn_ready()
    finally:
        await finish()
    lines = [m for m in caplog.messages if "claim_decision" in m]
    assert len(lines) == 1
    payload = json.loads(lines[0].split("claim_decision", 1)[1])
    assert payload["selected"] == ["bd-lo"]
    skip = {s["fid"]: s for s in payload["skipped"]}
    assert skip["bd-hi"]["reason"] == "claim-race"


# ── #356: task-claim target preservation + ready-queue livelock bound ─────────────


class _BlockRecorder:
    """Records flag_blocked calls — the minimal store `_bound_ready_skips` touches."""

    def __init__(self):
        self.blocked = []  # (fid, reason, category)

    def flag_blocked(self, fid, reason, category=""):
        self.blocked.append((fid, reason, category))
        return {"id": fid}


async def test_task_claim_preserves_the_dispatch_target(monkeypatch):
    """#356 r1: a ready task assigned to a sister agent transitions to in_progress via
    claim_task with its assignee/dispatch target PRESERVED — never the coding-feature
    `claim` (which would reassign to the actor and refuse the already-assigned bead).
    Its dispatch then follows the existing target-resolution path."""
    store = _TaskStore([_task("bd-a2a", assignee="quinn")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_worktree(*a, **k):
        raise AssertionError("a task must NOT create a git worktree")

    monkeypatch.setattr(worktree, "create_worktree", _no_worktree)

    async def _dispatch_task(delegate, prompt, *, timeout=None):
        return "the deliverable"

    monkeypatch.setattr(coder_seam, "dispatch_task", _dispatch_task)

    a2a = _sister("a2a")
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(
        loop, "_resolve_delegate", lambda name, expect: a2a if (name == "quinn" and expect == "a2a") else None
    )

    assert await loop._spawn_ready() is True
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)

    # the transition went through claim_task with the target PRESERVED (not the actor)…
    assert store.claim_task_calls == [("bd-a2a", "quinn")]
    # …and the reply was recorded, i.e. the dispatch followed the resolved target path.
    assert ("record_delivery", "bd-a2a", "the deliverable") in store.calls


async def test_coding_feature_still_uses_the_atomic_claim_primitive(monkeypatch):
    """#356 r2: an ordinary coding feature is unaffected — it still claims through the
    atomic `claim` (`br --claim`, actor-assignment), NEVER the task-only claim_task."""
    store = _TaskStore([_ready("bd-feat", ["a.py"])])  # no issue_type → a coding feature
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1, "coder": "proto"})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is True
        assert store.claimed == ["bd-feat"]  # it WAS claimed…
        assert store.claim_task_calls == []  # …via the atomic `claim`, not claim_task (r2)
    finally:
        await finish()


async def test_task_claim_race_starts_no_drive_and_is_retried(monkeypatch):
    """#356 r3: when claim_task loses the race (the ready card changed state under us),
    no drive starts and the outcome is a retriable claim-race skip — no duplicate drive
    start, no stale ready card claimed."""

    class _RaceTaskStore(_TaskStore):
        def claim_task(self, fid, assignee=""):
            return None  # the atomic transition lost the race / the card is no longer ready

    store = _RaceTaskStore([_task("bd-task", assignee="agent-bot")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_dispatch(*a, **k):
        raise AssertionError("a lost claim race must NOT dispatch")

    monkeypatch.setattr(coder_seam, "dispatch_task", _no_dispatch)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    assert await loop._spawn_ready() is False  # nothing started…
    assert loop._drives == set()  # …no drive
    assert loop._ready_skips["bd-task"] == ("claim-race", 1)  # recorded as a retriable claim-race


class _AlwaysRaceStore(_ClaimStore):
    """A ready card whose atomic claim ALWAYS loses the race — it stays ready and is
    re-skipped as claim-race every tick, the #356 livelock shape. flag_blocked marks the
    card blocked (as real `br` does), so a blocked card is not endlessly re-blocked."""

    def __init__(self, features):
        super().__init__(features)
        self.blocked = []  # (fid, reason, category)

    def claim(self, fid, assignee=""):
        return None  # never wins the race

    def flag_blocked(self, fid, reason, category=""):
        self.blocked.append((fid, reason, category))
        for f in self._features:
            if f["id"] == fid:
                f["blocked"] = True  # the card now carries the blocked flag (still ready-labelled)
        return {"id": fid}


async def test_ready_queue_claim_race_livelock_blocks_after_threshold(monkeypatch):
    """#356 r4/r6: a ready card whose claim loses the race EVERY tick is not retried
    forever — after `ready_skip_max` consecutive `claim-race` skips it is flagged blocked
    with an actionable livelock reason and a non-self-healing class, so the existing
    blocked sweep / operator escalation surfaces the formerly-invisible
    ready-but-unclaimable state. It is blocked exactly once."""
    store = _AlwaysRaceStore([_ready("bd-stuck", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1, "ready_skip_max": 3})

    for _ in range(2):  # two transient races — must NOT block yet
        assert await loop._spawn_ready() is False
        assert store.blocked == []
    assert await loop._spawn_ready() is False  # the 3rd consecutive claim-race trips the bound
    assert await loop._spawn_ready() is False  # …and further ticks do not re-block the now-blocked card
    assert await loop._spawn_ready() is False

    assert len(store.blocked) == 1
    fid, reason, category = store.blocked[0]
    assert fid == "bd-stuck"
    assert "livelock" in reason.lower() and "claim-race" in reason  # actionable livelock/claim reason
    assert category == "terminal"  # not a self-healing class → the sweep escalates to the operator


async def test_ready_queue_transient_claim_race_resets_on_success(monkeypatch):
    """#356 r5: a single/transient claim race must NOT block. A subsequent successful
    claim resets the skip counter, so the streak never reaches the threshold — even a
    threshold of 2, which the un-reset streak would have tripped on the second tick."""

    class _FlakyStore(_ClaimStore):
        def __init__(self, features):
            super().__init__(features)
            self.blocked = []
            self.attempt = 0

        def claim(self, fid, assignee=""):
            self.attempt += 1
            if self.attempt == 1:
                return None  # a one-off race on the first tick
            return super().claim(fid, assignee=assignee)

        def flag_blocked(self, fid, reason, category=""):
            self.blocked.append((fid, reason))
            return {"id": fid}

    store = _FlakyStore([_ready("bd-flaky", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1, "ready_skip_max": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is False  # tick 1: raced (streak = 1)
        assert await loop._spawn_ready() is True  # tick 2: claimed → drive; streak reset
    finally:
        await finish()
    assert store.blocked == []  # a transient race never blocked
    assert loop._ready_skips == {}  # the counter cleared on the successful claim


async def test_bound_ready_skips_counts_and_blocks_by_fid_and_reason(monkeypatch):
    """#356 r4: the counter is keyed by (fid, reason) and only the SAME reason repeated
    to the threshold blocks — driven directly against `_bound_ready_skips`."""
    store = _BlockRecorder()
    loop = BoardLoop({"ready_skip_max": 3})
    skip = [{"fid": "bd-x", "reason": "claim-race"}]
    await loop._bound_ready_skips(store, [], [], skip)
    await loop._bound_ready_skips(store, [], [], skip)
    assert loop._ready_skips["bd-x"] == ("claim-race", 2)
    assert store.blocked == []  # below threshold — not yet
    await loop._bound_ready_skips(store, [], [], skip)  # the 3rd consecutive identical skip
    assert [b[0] for b in store.blocked] == ["bd-x"]
    assert store.blocked[0][2] == "terminal"
    assert "bd-x" not in loop._ready_skips  # counter dropped once blocked


async def test_bound_ready_skips_resets_on_reason_change_and_when_card_leaves(monkeypatch):
    """#356 r5: a CHANGED skip reason opens a fresh window, and a card that stops being
    skipped (claimed elsewhere, dependency-blocked, or closed) drops its counter — so a
    streak of DIFFERENT reasons, or an interrupted streak, never blocks prematurely."""
    store = _BlockRecorder()
    loop = BoardLoop({"ready_skip_max": 2})
    await loop._bound_ready_skips(store, [], [], [{"fid": "bd-y", "reason": "claim-race"}])
    # the reason changes → the window resets (count back to 1 for the new reason)…
    await loop._bound_ready_skips(store, [], [], [{"fid": "bd-y", "reason": "preflight-hold"}])
    assert loop._ready_skips["bd-y"] == ("preflight-hold", 1)
    await loop._bound_ready_skips(store, [], [], [{"fid": "bd-y", "reason": "claim-race"}])
    assert loop._ready_skips["bd-y"] == ("claim-race", 1)
    assert store.blocked == []  # alternating reasons never reached the threshold

    # a card that leaves the ready queue (no skip this tick) drops its counter → progress
    await loop._bound_ready_skips(store, [], [], [{"fid": "bd-z", "reason": "claim-race"}])
    assert loop._ready_skips["bd-z"] == ("claim-race", 1)
    await loop._bound_ready_skips(store, [], [], [])  # bd-z became dependency-blocked / claimed elsewhere
    assert "bd-z" not in loop._ready_skips
    # a successful claim (selected) also resets it
    await loop._bound_ready_skips(store, [], [], [{"fid": "bd-z", "reason": "claim-race"}])
    await loop._bound_ready_skips(store, ["bd-z"], [], [])
    assert "bd-z" not in loop._ready_skips
    assert store.blocked == []


async def test_bound_ready_skips_never_blocks_a_resolving_hot_file_wait(monkeypatch):
    """#356: a hot-file skip is RESOLVING (an in-flight build is progressing and, bounded
    by coder_timeout, will free the file) — transient by construction, not a livelock. It
    is counted (so a later reason change resets cleanly) but never trips the block, even
    far past the threshold."""
    store = _BlockRecorder()
    loop = BoardLoop({"ready_skip_max": 2})
    for _ in range(6):
        await loop._bound_ready_skips(store, [], [], [{"fid": "bd-hot", "reason": "hot-file"}])
    assert store.blocked == []
    assert loop._ready_skips["bd-hot"][0] == "hot-file"  # still tracked, never blocked


def test_ready_skip_max_floors_so_a_single_race_is_never_terminal():
    """#356 r5: the threshold floors at 2 — a misconfigured `ready_skip_max` of 0/1 can
    never make a single/transient claim race terminal. The default is a sane bound."""
    assert BoardLoop({})._ready_skip_max() >= 2  # a real default bound
    assert BoardLoop({"ready_skip_max": 1})._ready_skip_max() == 2  # floored
    assert BoardLoop({"ready_skip_max": 0})._ready_skip_max() == 2
    assert BoardLoop({"ready_skip_max": 7})._ready_skip_max() == 7  # honored above the floor
    assert (
        BoardLoop({"ready_skip_max": "oops"})._ready_skip_max() == BoardLoop({})._ready_skip_max()
    )  # malformed → default


# ── the PR reconcile (terminal-edge fallback) ───────────────────────────────────


class _ReconcileStore:
    def __init__(self, in_review):
        self._in_review = in_review
        self.merged = []
        self.blocked = []
        self.cleared = []  # (fid, kinds-tuple | None) — clear_budgets calls (#259)

    def list_features(self, state=None):
        return self._in_review if state == "in_review" else []

    def record_merge(self, *, pr_url):
        self.merged.append(pr_url)
        return {"id": "x", "board_state": "done"}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append((fid, tuple(kinds) if kinds is not None else None))
        return {"id": fid}


async def test_reconcile_drives_merged_to_done_and_closed_to_blocked(monkeypatch):
    store = _ReconcileStore(
        [
            {"id": "bd-merged", "pr_url": "https://example/pr/1"},
            {"id": "bd-closed", "pr_url": "https://example/pr/2"},
            {"id": "bd-open", "pr_url": "https://example/pr/3"},
            {"id": "bd-nopr", "pr_url": ""},  # no PR → skipped entirely
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    states = {
        "https://example/pr/1": "MERGED",
        "https://example/pr/2": "CLOSED",
        "https://example/pr/3": "OPEN",
    }

    async def _pr_state(url, *, cwd="."):
        return states[url]

    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ("passing", "")  # the OPEN PR's CI is green → left in review

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    await BoardLoop({})._reconcile_prs()
    assert store.merged == ["https://example/pr/1"]  # merged → done
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed-unmerged → blocked
    assert set(reaped) == {"bd-merged", "bd-closed"}  # both terminal states reap; open kept


async def test_reconcile_terminal_edges_reset_persisted_budgets(monkeypatch):
    """r2 (#259): the merge edge (and the closed-unmerged sibling) clears EVERY
    persisted `budget:` label (kinds=None) along with the in-memory caches — a
    reopened/requeued card starts with full budgets, exactly as pre-persistence."""
    store = _ReconcileStore(
        [
            {"id": "bd-m", "pr_url": "https://example/pr/1"},
            {"id": "bd-c", "pr_url": "https://example/pr/2"},
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    states = {"https://example/pr/1": "MERGED", "https://example/pr/2": "CLOSED"}

    async def _pr_state(url, *, cwd="."):
        return states[url]

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))

    loop = BoardLoop({})
    loop._ci_fix_attempts["bd-m"] = 2  # a spent budget the old process was carrying
    loop._review_fix_attempts["bd-m"] = 1
    await loop._reconcile_prs()
    assert ("bd-m", None) in store.cleared and ("bd-c", None) in store.cleared  # None = ALL kinds
    assert "bd-m" not in loop._ci_fix_attempts and "bd-m" not in loop._review_fix_attempts


# ── the CI-feedback edge (closed-loop verify) ────────────────────────────────────


class _CiStore:
    def __init__(self, feature, escalate_tiers=None):
        self._feature = feature
        self.requeued = []
        self.blocked = []
        self.escalated = []
        self.budgets = []  # (fid, kind, n) — record_budget writes (#259)
        self.cleared = []  # (fid, kinds-tuple | None) — clear_budgets calls (#259)
        self._escalate_tiers = list(escalate_tiers or [])

    def list_features(self, state=None):
        return [self._feature] if state == "in_review" else []

    def record_merge(self, *, pr_url):
        return None

    def requeue(self, fid):
        self.requeued.append(fid)
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))

    def escalate(self, fid, reason):
        self.escalated.append((fid, reason))
        return self._escalate_tiers.pop(0) if self._escalate_tiers else None

    def record_budget(self, fid, kind, n):
        self.budgets.append((fid, kind, n))
        return {"id": fid}

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append((fid, tuple(kinds) if kinds is not None else None))
        return {"id": fid}


async def _stub_ci_worktree(monkeypatch, *, ci, diff="- a\n+ b"):
    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ci() if callable(ci) else ci

    async def _pr_diff(url, *, cwd=".", max_chars=4000):
        return diff

    async def _reap(repo, root, fid):
        return None

    async def _merge_state(url, *, cwd="."):
        return "CLEAN"  # not BEHIND/DIRTY → auto-rebase no-ops, leaving the CI path under test

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "pr_diff", _pr_diff)
    monkeypatch.setattr(worktree, "pr_merge_state", _merge_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)


async def test_reconcile_ci_bounces_failing_pr_then_blocks(monkeypatch):
    """No coder ladder (single coder) → bounded same-tier retry capped by ci_fix_max."""
    store = _CiStore({"id": "bd-ci", "pr_url": "https://example/pr/9"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(
        monkeypatch, ci=("failing", "Failing checks:\n- Web E2E: FAILURE\n\nFailing log:\nelement not found")
    )

    loop = BoardLoop({"ci_fix_max": 2})  # no `coders` → escalation_on is False
    assert not loop.escalation_on
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert store.blocked == [] and store.escalated == []
    assert "element not found" in loop._ci_feedback["bd-ci"]
    assert loop._ci_fix_attempts["bd-ci"] == 2
    # cap=2 exhausted → blocked, no further requeue.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert [b[0] for b in store.blocked] == ["bd-ci"]


async def test_reconcile_ci_escalates_through_tiers_then_blocks(monkeypatch):
    """With a coder ladder AND no same-tier budget (ci_fix_max=0), each CI failure
    climbs a tier (stronger model) carrying the prior diff; the top tier failing →
    Blocked (the ladder is the bound)."""
    store = _CiStore({"id": "bd-esc", "pr_url": "https://example/pr/7"}, escalate_tiers=["smart", "reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Tests: FAILURE"), diff="- old\n+ new")

    loop = BoardLoop({"coders": {"fast": "a", "smart": "b", "reasoning": "c"}, "ci_fix_max": 0})
    assert loop.escalation_on
    # CI failures climb tiers (escalate), requeue, NOT blocked, carrying the prior diff.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [e[0] for e in store.escalated] == ["bd-esc", "bd-esc"]
    assert store.blocked == []
    assert "- old" in loop._ci_prior_diff["bd-esc"]
    # top tier exhausted (escalate → None) → blocked.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [b[0] for b in store.blocked] == ["bd-esc"]


async def test_reconcile_ci_spends_same_tier_budget_before_escalating(monkeypatch):
    """With a ladder AND ci_fix_max>0, a CI failure first spends same-tier fix
    attempts (cheap nits — lint, a golden-map update) before climbing a model tier,
    and the per-tier budget RESETS at the new rung. Without this, a one-line F841
    burned reasoning→opus and then blocked."""
    store = _CiStore({"id": "bd-b", "pr_url": "https://example/pr/5"}, escalate_tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Lint: F841 unused variable"))

    loop = BoardLoop({"coders": {"smart": "a", "reasoning": "b"}, "ci_fix_max": 2})
    assert loop.escalation_on

    # First two failures: same-tier CI-fix (requeue), no escalation.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b"]
    assert store.escalated == []
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted → escalate ONE tier and reset the per-tier budget.
    await loop._reconcile_prs()
    assert [e[0] for e in store.escalated] == ["bd-b"]
    assert store.requeued == ["bd-b", "bd-b", "bd-b"]
    assert loop._ci_fix_attempts.get("bd-b", 0) == 0  # fresh budget at the new rung

    # The new rung gets its own same-tier attempts before the ladder is exhausted.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b", "bd-b", "bd-b", "bd-b"]
    assert [e[0] for e in store.escalated] == ["bd-b"]  # still just the one climb
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted again → escalate returns None (ladder top) → blocked.
    await loop._reconcile_prs()
    assert [b[0] for b in store.blocked] == ["bd-b"]


# ── persisted fix budgets (#259): the bead is truth, the dicts are caches ────────


async def test_reconcile_ci_exhausted_budget_on_the_bead_blocks_after_restart(monkeypatch):
    """r1 (#259): the CI-fix budget rides the bead (`budget:ci-fix:<n>`), so a FRESHLY
    constructed loop — a restart, every in-memory counter dict empty — against a bead
    whose budget is already exhausted blocks instead of re-dispatching. Red-is-reachable:
    the pre-#259 loop derived attempts=0 from its empty dict and REQUEUED this exact
    card (test_reconcile_ci_bounces_failing_pr_then_blocks shows the requeue path with
    an unexhausted budget)."""
    store = _CiStore({"id": "bd-ci", "pr_url": "https://example/pr/9", "labels": ["budget:ci-fix:2"]})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Tests: FAILURE"))

    loop = BoardLoop({"ci_fix_max": 2})  # fresh process: _ci_fix_attempts is empty
    await loop._reconcile_prs()
    assert store.requeued == []  # NO re-dispatch — the bead's budget is spent
    assert [b[0] for b in store.blocked] == ["bd-ci"]
    assert ("bd-ci", ("ci-fix",)) in store.cleared  # the block clears the spent label too


async def test_reconcile_ci_resumes_a_half_spent_budget_from_the_bead(monkeypatch):
    """#259: a half-spent persisted budget resumes mid-count after a restart — the
    fresh loop derives 1 from the bead, spends the one remaining attempt (persisting
    the new count), then blocks."""
    store = _CiStore({"id": "bd-ci", "pr_url": "https://example/pr/9", "labels": ["budget:ci-fix:1"]})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Tests: FAILURE"))

    loop = BoardLoop({"ci_fix_max": 2})
    await loop._reconcile_prs()  # bead carries 1 → one more same-tier fix (2/2), persisted
    assert store.requeued == ["bd-ci"]
    assert store.budgets == [("bd-ci", "ci-fix", 2)]
    await loop._reconcile_prs()  # now exhausted → blocked, no further requeue
    assert store.requeued == ["bd-ci"]
    assert [b[0] for b in store.blocked] == ["bd-ci"]


async def test_reconcile_ci_persists_each_spend_and_the_climb_reset(monkeypatch):
    """r2 (#259): every same-tier CI-fix spend is written to the bead as it happens,
    and the tier-climb edge still resets the budget — on the bead as well as in the
    cache — so the new rung starts its own count."""
    store = _CiStore({"id": "bd-p", "pr_url": "https://example/pr/5"}, escalate_tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Lint: F841 unused variable"))

    loop = BoardLoop({"coders": {"smart": "a", "reasoning": "b"}, "ci_fix_max": 1})
    await loop._reconcile_prs()  # spend 1/1 — written through to the bead
    assert store.budgets == [("bd-p", "ci-fix", 1)]
    await loop._reconcile_prs()  # exhausted → climb; the persisted budget resets with the cache
    assert [e[0] for e in store.escalated] == ["bd-p"]
    assert ("bd-p", ("ci-fix",)) in store.cleared
    assert loop._ci_fix_attempts.get("bd-p", 0) == 0


async def test_budget_reset_pins_zero_against_a_stale_feature_projection():
    """The tier-climb regression: a targeted (mid-flow) reset must PIN 0 in the
    cache, not merely forget the fid — ``_drive`` keeps its ORIGINAL ``feature``
    projection across a climb, so with pop-the-key semantics the very next
    ``_budget_get(..., feature)`` re-derived the exhausted pre-climb count from
    the projection's unchanged labels and the stronger tier started blocked
    instead of getting the fresh window the climb granted (the final assert here
    read 2, not 0). The FULL reset (no kinds — merge/PR-closed) still drops the
    keys outright: the fid leaves the flow, and the dicts must not grow forever."""
    store = _ReconcileStore([])
    loop = BoardLoop({})
    stale = {"id": "bd-s", "labels": ["budget:goal-fix:2", "budget:ci-fix:1"]}  # _drive's in-hand projection
    assert await loop._budget_get(store, "bd-s", "goal-fix", stale) == 2  # resumed off the bead
    await loop._budget_reset(store, "bd-s", "goal-fix", "gate-fix", "req-fix")  # the climb edge
    assert ("bd-s", ("goal-fix", "gate-fix", "req-fix")) in store.cleared  # labels cleared with it
    # The SAME stale projection can no longer resurrect the spent count.
    assert await loop._budget_get(store, "bd-s", "goal-fix", stale) == 0
    assert loop._goal_fix_attempts.get("bd-s") == 0  # pinned, authoritative for this process
    # An untouched kind is unaffected by the targeted reset.
    assert await loop._budget_get(store, "bd-s", "ci-fix", stale) == 1
    # Terminal edge: the full reset pops every cache key (memory hygiene).
    await loop._budget_reset(store, "bd-s")
    assert "bd-s" not in loop._goal_fix_attempts and "bd-s" not in loop._ci_fix_attempts


async def test_reconcile_ci_leaves_passing_and_pending_in_review(monkeypatch):
    store = _CiStore({"id": "bd-ok", "pr_url": "https://example/pr/8"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    statuses = iter([("pending", ""), ("passing", "")])
    await _stub_ci_worktree(monkeypatch, ci=lambda: next(statuses))

    await BoardLoop({})._reconcile_prs()  # pending → leave
    await BoardLoop({})._reconcile_prs()  # passing → leave
    assert store.requeued == [] and store.blocked == []


# ── the merged/closed guard: never bounce a PR that already left review (bd-1zp) ──


async def test_reconcile_ci_skips_pr_that_left_review_since_the_poll(monkeypatch):
    """Lagged-poll guard (Test C): a PR that merged/closed between the top-level state
    read and the CI reconcile is bailed on BEFORE the CI rollup is even read — a CI fix
    must never dispatch against a PR that is no longer OPEN."""
    store = _CiStore({"id": "bd-gone", "pr_url": "https://example/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    ci_reads = []

    async def _pr_state(url, *, cwd="."):
        return "MERGED"  # merged out from under the poll

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        ci_reads.append(url)
        return ("failing", "Failing checks:\n- Tests: FAILURE")

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)

    loop = BoardLoop({"ci_fix_max": 2})
    await loop._reconcile_ci(store, "bd-gone", "https://example/pr/1", ".")
    assert ci_reads == []  # bailed on state before ever reading CI
    assert store.requeued == [] and store.blocked == [] and store.escalated == []


async def test_reconcile_prs_closed_pr_blocks_and_never_runs_ci(monkeypatch):
    """Test D: a CLOSED PR is driven to blocked by the merge poll, and the CI reconcile
    is never invoked against it — the closed edge, not a CI-fix re-dispatch, handles it."""
    store = _ReconcileStore([{"id": "bd-closed", "pr_url": "https://example/pr/1"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_state(url, *, cwd="."):
        return "CLOSED"

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({})
    ci_called = []

    async def _ci_spy(store, fid, pr_url, repo):
        ci_called.append(fid)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)

    await loop._reconcile_prs()
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed → blocked for triage
    assert ci_called == []  # the CI reconcile never ran against the closed PR


def test_build_prompt_injects_ci_feedback_and_prior_diff():
    loop = BoardLoop({})
    feature = {"id": "bd-ci", "title": "T", "spec": "do it", "acceptance_criteria": "AC", "files_to_modify": ["a.py"]}
    assert "previous attempt was REJECTED" not in loop._build_prompt(feature)  # none stored → no block
    loop._ci_feedback["bd-ci"] = "Failing checks:\n- Web E2E: FAILURE\nelement not found"
    loop._ci_prior_diff["bd-ci"] = "--- a/x.tsx\n+++ b/x.tsx\n+ bad code"
    prompt = loop._build_prompt(feature)
    assert "previous attempt was REJECTED" in prompt
    assert "element not found" in prompt
    assert "bad code" in prompt  # the prior diff is carried forward


# ── CI failure reason (sharpen the retro signal) ─────────────────────────────────


def test_ci_failure_reason_keeps_the_classifiable_error_not_the_header():
    assert _ci_failure_reason("") == "checks red"
    # checks-only (no log excerpt) → the failing check names, not "Failing checks:"
    r = _ci_failure_reason("Failing checks:\n- Python tests: FAILURE\n- Lint: FAILURE")
    assert "Python tests" in r and "Lint" in r and "Failing checks:" not in r
    # with a log excerpt → the SPECIFIC error survives so the retro can bucket it
    summary = (
        "Failing checks:\n- Python tests: FAILURE\n\n"
        "Failing log (truncated):\n"
        "    def test_golden(): ...\n"
        "E   AssertionError: golden field map is out of sync\n"
        "FAILED tests/test_config_roundtrip.py::test_golden - golden field map is out of sync\n"
    )
    r = _ci_failure_reason(summary)
    assert "golden field map" in r  # the classifiable signal is preserved
    assert "Failing checks:" not in r and len(r) <= 500


# ── PR body: the coder's summary, never the raw stream (#56) ─────────────────────


def test_pr_body_keeps_from_the_last_summary_heading():
    """Narration before the summary is dropped, and an EARLY `## Summary` line in the
    narration doesn't truncate the real section — the LAST heading wins."""
    raw = (
        "I first looked at loop.py.\n"
        "## Summary\n(placeholder — I'll finish this at the end)\n"
        "Let me edit the tests.\n"
        "## Summary\n\n- Added the helper\n- Wired the call site\n"
    )
    body = _pr_body(raw, FEATURE)
    assert body.startswith("## Summary")
    assert "Added the helper" in body and "Wired the call site" in body
    assert "I first looked" not in body and "Let me edit" not in body and "placeholder" not in body


def test_pr_body_without_a_summary_uses_the_template_not_the_raw_stream():
    raw = "step one: read the file\nstep two: edit it\nall done"
    body = _pr_body(raw, FEATURE)
    assert FEATURE["title"] in body and FEATURE["id"] in body
    assert "See the diff for details." in body
    assert "step one" not in body and "all done" not in body
    assert FEATURE["title"] in _pr_body("", FEATURE)  # empty reply → same template


def test_pr_body_drops_control_marker_lines():
    raw = "## Summary\n\nDid the thing.\n  NO_TEST_NEEDED: covered by the existing golden\nMore detail.\n"
    body = _pr_body(raw, FEATURE)
    assert "NO_TEST_NEEDED" not in body
    assert "Did the thing." in body and "More detail." in body


def test_pr_body_caps_at_4000_chars():
    raw = "narration first\n## Summary\n\n" + "x" * 9000
    body = _pr_body(raw, FEATURE)
    assert len(body) <= 4000 and body.startswith("## Summary")


# ── KG lessons injected into the coder prompt (flywheel read half) ───────────────


def test_kg_lessons_config_defaults():
    loop = BoardLoop({})
    assert loop.kg_lessons is True and loop.kg_lessons_k == 3 and loop.kg_lessons_domain == "loop-lessons"
    assert BoardLoop({"kg_lessons": False}).kg_lessons is False


async def test_fetch_kg_lessons_disabled_never_queries(monkeypatch):
    called = []

    async def _search(*a, **k):
        called.append(1)
        return [{"preview": "x"}]

    monkeypatch.setattr("graph.sdk.knowledge_search", _search)
    assert await BoardLoop({"kg_lessons": False})._fetch_kg_lessons(FEATURE) == ""
    assert not called


async def test_fetch_kg_lessons_formats_hits_and_scopes_query(monkeypatch):
    captured = {}

    async def _search(query, *, k=5, domain=None):
        captured.update(query=query, k=k, domain=domain)
        return [{"preview": "golden-map: also update settings_schema.FIELDS"}, {"content": "F841: no unused vars"}]

    monkeypatch.setattr("graph.sdk.knowledge_search", _search)
    out = await BoardLoop({"kg_lessons_k": 2})._fetch_kg_lessons(FEATURE)
    assert "- golden-map: also update settings_schema.FIELDS" in out
    assert "- F841: no unused vars" in out
    assert captured["domain"] == "loop-lessons" and captured["k"] == 2
    assert "Add a thing" in captured["query"] and "a.py" in captured["query"]  # title + files


async def test_fetch_kg_lessons_empty_or_error_returns_empty(monkeypatch):
    async def _empty(*a, **k):
        return []

    monkeypatch.setattr("graph.sdk.knowledge_search", _empty)
    assert await BoardLoop({})._fetch_kg_lessons(FEATURE) == ""

    async def _boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr("graph.sdk.knowledge_search", _boom)
    assert await BoardLoop({})._fetch_kg_lessons(FEATURE) == ""  # error → best-effort ""


def test_build_prompt_injects_lessons_block_only_when_present():
    loop = BoardLoop({})
    assert "Known gotchas for this area" not in loop._build_prompt(FEATURE)
    p = loop._build_prompt(FEATURE, lessons="- always update the golden map")
    assert "Known gotchas for this area" in p and "always update the golden map" in p


# ── auto-rebase on conflict (bd-2gu) ─────────────────────────────────────────────


def _aret(val):
    async def _f(*a, **k):
        return val

    return _f


def test_auto_rebase_config_defaults():
    assert BoardLoop({}).auto_rebase is True  # defaults to merge_poll (True)
    assert BoardLoop({"merge_poll": False}).auto_rebase is False
    assert BoardLoop({"auto_rebase": False}).auto_rebase is False
    assert BoardLoop({}).rebase_fix_max == 1


async def test_maybe_rebase_skips_when_not_behind_or_dirty(monkeypatch):
    """CLEAN / BLOCKED(checks) / UNKNOWN → not the rebase's job; never touches git."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("CLEAN"))
    rebased = []
    monkeypatch.setattr(worktree, "rebase_onto_base", lambda *a, **k: rebased.append(1))
    store = _CiStore({"id": "bd-1"})
    assert await BoardLoop({"coder": "proto"})._maybe_rebase(store, FEATURE, "pr", "/repo") is False
    assert not rebased


async def test_maybe_rebase_behind_does_clean_rebase_no_coder(monkeypatch):
    """BEHIND → a clean rebase + force-push; no requeue, no block, no coder."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("BEHIND"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("clean", "")))
    store = _CiStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto"})
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == [] and store.blocked == []
    assert loop._rebase_attempts.get("bd-1", 0) == 0


async def test_maybe_rebase_conflict_redispatches_then_blocks(monkeypatch):
    """DIRTY + a real conflict → re-dispatch the coder (requeue, conflicting file in
    the feedback) up to rebase_fix_max, then Block for a manual rebase."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("DIRTY"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("conflict", "graph/x.py")))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    store = _CiStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto", "rebase_fix_max": 1})
    # 1st conflict → re-dispatch, carrying the conflicting file into the feedback.
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == ["bd-1"]
    assert "graph/x.py" in loop._ci_feedback["bd-1"]
    assert loop._rebase_attempts["bd-1"] == 1
    # budget (1) exhausted → block, no second requeue.
    assert await loop._maybe_rebase(store, FEATURE, "pr", "/repo") is True
    assert store.requeued == ["bd-1"]
    assert [b[0] for b in store.blocked] == ["bd-1"]


async def test_maybe_rebase_exhausted_budget_on_the_bead_blocks_after_restart(monkeypatch):
    """#259: the rebase budget rides the bead too — a fresh loop (empty dicts) against
    a card carrying `budget:rebase:1` with rebase_fix_max=1 blocks for a manual rebase
    instead of burning another coder re-dispatch (the pre-#259 loop requeued here)."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("DIRTY"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("conflict", "graph/x.py")))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    store = _CiStore({"id": "bd-1"})
    feature = {**FEATURE, "labels": ["budget:rebase:1"]}
    loop = BoardLoop({"coder": "proto", "rebase_fix_max": 1})
    assert await loop._maybe_rebase(store, feature, "pr", "/repo") is True
    assert store.requeued == []  # no re-dispatch — the persisted budget is spent
    assert [b[0] for b in store.blocked] == ["bd-1"]


async def test_maybe_rebase_infra_error_is_noop(monkeypatch):
    """A fetch/push/worktree error degrades to no-op (next poll retries) — no block."""
    monkeypatch.setattr(worktree, "pr_merge_state", _aret("BEHIND"))
    monkeypatch.setattr(worktree, "rebase_onto_base", _aret(("error", "fetch failed")))
    store = _CiStore({"id": "bd-1"})
    assert await BoardLoop({"coder": "proto"})._maybe_rebase(store, FEATURE, "pr", "/repo") is False
    assert store.requeued == [] and store.blocked == []


async def test_reconcile_prs_rebase_acts_skips_ci(monkeypatch):
    """An OPEN PR the rebase handled skips the CI reconcile this pass (a rebase
    force-pushes + re-runs CI, so the stale head's CI would be thrown away)."""
    store = _CiStore({"id": "bd-1", "pr_url": "https://e/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = BoardLoop({"merge_poll": True})
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(True))
    await loop._reconcile_prs()
    assert ci == []  # rebase acted → CI reconcile skipped


async def test_reconcile_prs_no_rebase_runs_ci(monkeypatch):
    """An OPEN PR the rebase left alone still gets the CI reconcile."""
    store = _CiStore({"id": "bd-1", "pr_url": "https://e/pr/1"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = BoardLoop({"merge_poll": True})
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    await loop._reconcile_prs()
    assert ci == [1]  # nothing to rebase → CI reconcile runs


# ── merged-state verify: re-verify the verdict when base moves (#131) ────────────


class _VerifyStore(_CiStore):
    def __init__(self, feature):
        super().__init__(feature)
        self.verified = []

    def record_merged_verified(self, fid, sha):
        self.verified.append((fid, sha))
        return {"id": fid}


def _vloop(**cfg):
    base = {"coder": "proto", "local_gate_cmd": "pytest -q"}
    base.update(cfg)
    return BoardLoop(base)


async def test_verify_merged_state_noop_without_a_gate(monkeypatch):
    """No local_gate_cmd → nothing to verify the merged state WITH; never touches git."""
    calls = []
    monkeypatch.setattr(worktree, "origin_head_sha", lambda *a, **k: calls.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = BoardLoop({"coder": "proto"})  # no gate configured
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert not calls and store.verified == [] and store.blocked == []


async def test_verify_merged_state_current_stamp_is_noop(monkeypatch):
    """Stamp == current origin/<base> → the verdict is current; no worktree, no gate run."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc123"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    feature = {"id": "bd-1", "labels": ["merged-verified:abc123"]}
    assert await _vloop()._verify_merged_state(store, feature, "pr", "/repo") is False
    assert not built and store.verified == [] and store.blocked == []


async def test_verify_merged_state_sha_read_failure_is_noop(monkeypatch):
    """A git hiccup reading origin/<base> degrades to no-op — next poll retries."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(""))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    assert await _vloop()._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert not built and store.blocked == []


async def test_verify_merged_state_base_moved_green_gate_stamps_and_stays(monkeypatch):
    """Base moved + green gate on the merged state → stamp the sha, card STAYS in
    review (non-blocking default), throwaway worktree removed."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/repo/.worktrees/.verify-feat-bd-1")))
    removed = []

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    gates = []

    async def _gate(wt, feature=None):
        gates.append(wt)
        return None  # green

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    feature = {"id": "bd-1", "labels": ["merged-verified:oldsha"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert gates == ["/repo/.worktrees/.verify-feat-bd-1"]
    assert removed == gates  # throwaway removed even on success
    assert store.verified == [("bd-1", "def456")]
    assert store.requeued == [] and store.blocked == []


async def test_verify_merged_state_red_gate_blocks(monkeypatch):
    """A CLEAN gate FAILURE on the merged state is the ONLY blocking outcome — the
    PR merges clean but the result is broken; the reason carries the gate output."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret("1 failed: test_x"))
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is True
    assert [b[0] for b in store.blocked] == ["bd-1"]
    assert "merged state" in store.blocked[0][1] and "1 failed: test_x" in store.blocked[0][1]
    assert store.verified == []  # a red verdict is never stamped current
    assert reaped == ["bd-1"]


async def test_verify_merged_state_budget_bounds_reverification(monkeypatch):
    """merged_verify_max bounds re-verification: a base that moves repeatedly stops
    burning gate runs — the stale stamp stays visible, the card stays in review."""
    shas = iter(["aaa", "bbb", "ccc"])

    async def _sha(repo, ref):
        return next(shas)

    monkeypatch.setattr(worktree, "origin_head_sha", _sha)
    built = []

    async def _build(repo, branch, sha, root=".worktrees"):
        built.append(sha)
        return ("merged", "/wt")

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))
    # 1st move (no stamp yet) → verify + stamp against aaa.
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.verified == [("bd-1", "aaa")]
    # base moves again (bbb, then ccc) but the budget (1) is spent → no more gate
    # runs, no block — the stale stamp is the adjudicator's signal.
    feature = {"id": "bd-1", "labels": ["merged-verified:aaa"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == ["aaa"]
    assert store.verified == [("bd-1", "aaa")] and store.blocked == []


async def test_verify_merged_state_conflict_and_error_are_noops(monkeypatch):
    """An infra error retries next poll; a merge conflict is the DIRTY/rebase edge's
    job — neither blocks, stamps, nor burns the verify budget."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc"))
    outcomes = iter([("error", "fetch failed"), ("conflict", "x.py"), ("merged", "/wt")])

    async def _build(repo, branch, sha, root=".worktrees"):
        return next(outcomes)

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))
    feature = {"id": "bd-1", "labels": []}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # error
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # conflict
    assert store.verified == [] and store.blocked == []
    # budget untouched → the third call actually verifies and stamps.
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert store.verified == [("bd-1", "abc")]


async def test_verify_merged_state_stamps_the_short_sha(monkeypatch):
    """The stamp is the SHORT sha (12 chars): `merged-verified:` + 40 = 56 blew beads'
    50-char label cap, so #132's write always died VALIDATION_FAILED — 12 chars (28
    total) fits (#135)."""
    full_sha = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha
    assert len(full_sha) == 40
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(full_sha))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.verified == [("bd-1", full_sha[:_MERGED_VERIFIED_SHA_LEN])]
    # the stamp read back the next poll matches the same-width truncation → current,
    # so nothing rebuilds (the read-back comparison and the write use the SAME width).
    feature = {"id": "bd-1", "labels": [f"merged-verified:{full_sha[:_MERGED_VERIFIED_SHA_LEN]}"]}
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert not built  # current → no rebuild


async def test_verify_merged_state_stamp_write_failure_never_aborts_or_burns_budget(monkeypatch):
    """A BoardError stamping the verified sha is optional bookkeeping (#135): it must
    NOT propagate — the caller's CI/merge reconciliation still has to run — and must NOT
    spend the re-verify budget on a write that never landed; next poll re-verifies."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456abc789"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))

    class _BoomStore(_VerifyStore):
        def record_merged_verified(self, fid, sha):
            raise BoardError("`br update` failed: VALIDATION_FAILED")

    store = _BoomStore({"id": "bd-1"})
    loop = _vloop(rebase_fix_max=3)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # does not raise, does not block…
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert store.blocked == []
    # …and the budget is untouched — a write that can't land isn't a re-verify attempt.
    assert loop._merged_verify_attempts.get("bd-1", 0) == 0
    assert store.budgets == []  # AC5: no record_budget for a stamp that never landed


async def test_verify_merged_state_persists_the_exhaustion_sentinel_once(monkeypatch):
    """AC4/ADR 0326: at the cap, the first base move past it persists the ONE-TIME
    sentinel `budget:merged-verify:<max+1>` — the fact store.merge_posture reads to hold
    the card — and never writes again. AC5: that sentinel is NOT a gate-run spend (the
    gate never runs while the budget is at/over the cap)."""
    shas = iter(["s1", "s2", "s3", "s4"])

    async def _sha(repo, ref):
        return next(shas)

    monkeypatch.setattr(worktree, "origin_head_sha", _sha)
    built = []

    async def _build(repo, branch, sha, root=".worktrees"):
        built.append(sha)
        return ("merged", "/wt")

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # 1st move: budget 0 < cap 1 → the gate RUNS, stamps, and spends 0→1.
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    assert built == ["s1"] and store.budgets == [("bd-1", "merged-verify", 1)]
    # 2nd move AT the cap: the one-time sentinel (1→2) is persisted; the gate does NOT run.
    feature = {"id": "bd-1", "labels": ["merged-verified:s1"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == ["s1"]  # no new gate run
    assert store.budgets[-1] == ("bd-1", "merged-verify", 2)  # sentinel = max+1, the projection's signal
    # 3rd move PAST the cap: quiet — no further budget write, no gate run.
    writes = len(store.budgets)
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == ["s1"] and len(store.budgets) == writes


async def test_verify_merged_state_spends_budget_only_on_a_terminal_gate_result(monkeypatch):
    """AC5: a re-verify unit is spent ONLY after the merged-state gate actually runs and
    yields a terminal verdict. An infra error and a merge conflict run no terminal gate,
    so they spend nothing and the NEXT real verdict still gets its budget."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc"))
    outcomes = iter([("error", "fetch"), ("conflict", "x.py"), ("merged", "/wt")])

    async def _build(repo, branch, sha, root=".worktrees"):
        return next(outcomes)

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=5)
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green when the gate DOES run
    feature = {"id": "bd-1", "labels": []}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # infra error
    assert store.budgets == []
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # merge conflict
    assert store.budgets == []
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False  # terminal green
    assert store.verified == [("bd-1", "abc")] and store.budgets == [("bd-1", "merged-verify", 1)]


async def test_verify_merged_state_red_gate_spends_a_unit(monkeypatch):
    """AC5: a CLEAN gate FAILURE on the merged state is a terminal verdict too — it blocks
    AND spends exactly one re-verify unit (the gate ran), and never stamps a red verdict."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=5)
    monkeypatch.setattr(loop, "_run_local_gate", _aret("1 failed: test_x"))
    assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is True
    assert store.budgets == [("bd-1", "merged-verify", 1)] and store.verified == []


# ── merged-verify budget reset: the live-loop cache invalidation (ADR 0326, #326) ─


class _ResetStore:
    """A store double for the reset path: records the merged-verify label clears the loop
    re-issues under its reset lock (`clear_budgets`) and the sentinel writes a racing
    reconcile persists (`record_budget`)."""

    def __init__(self):
        self.cleared = []  # (fid, kinds-tuple | None)
        self.budgets = []  # (fid, kind, n)

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append((fid, tuple(kinds) if kinds is not None else None))
        return {"id": fid}

    def record_budget(self, fid, kind, n):
        self.budgets.append((fid, kind, n))
        return {"id": fid}


def test_reset_merged_verify_budget_pins_the_live_loops_count_to_zero():
    """AC6/ADR 0326: the reset verb resets the RUNNING loop's in-process merged-verify
    budget (clearing the label alone can't — `_budget_get` lets the cache win, #259). It
    PINS the count to 0 (not a pop — the #259 mid-flow rule, so a stale label snapshot
    can't rehydrate the exhausted count), re-clears the label under the reset lock, touches
    only the named fid, is idempotent, and returns False when no loop is live."""
    slot = loop_mod._loop_slot()
    prior = slot.loop
    slot.loop = None
    store = _ResetStore()
    try:
        assert loop_mod.reset_merged_verify_budget("bd-1", store) is False  # no live loop → nothing to do
        loop = BoardLoop({})
        loop._merged_verify_attempts["bd-1"] = 6
        loop._merged_verify_attempts["bd-2"] = 3
        loop_mod._register_loop(loop)
        assert loop_mod.reset_merged_verify_budget("bd-1", store) is True
        assert loop._merged_verify_attempts["bd-1"] == 0  # PINNED to 0, not popped
        assert loop._merged_verify_attempts["bd-2"] == 3  # a sibling's budget is untouched
        assert store.cleared == [("bd-1", ("merged-verify",))]  # only that kind's label re-cleared
        assert loop_mod.reset_merged_verify_budget("bd-1", store) is True  # idempotent
    finally:
        slot.loop = prior


async def test_reset_wins_a_race_with_the_in_flight_exhaustion_sentinel(monkeypatch):
    """The review finding: a reset that lands AFTER the reconcile read the at-cap count but
    BEFORE it writes the `max+1` sentinel must not be clobbered. The sentinel write is a
    compare-and-set under the reset lock; a reset that pins 0 first makes the CAS read 0
    and skip, so the card is not silently re-held."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("newbase"))
    built = []

    async def _build(repo, branch, sha, root=".worktrees"):
        built.append(sha)
        return ("merged", "/wt")

    monkeypatch.setattr(worktree, "merged_state_worktree", _build)
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    loop._merged_verify_attempts["bd-1"] = 1  # already AT the cap

    # Simulate the operator reset firing exactly while the reconcile is inside the CAS:
    # the to_thread arm runs _invalidate first (pin 0 + label clear), then the real CAS.
    real_arm = loop._arm_merged_verify_exhaustion

    def _arm_after_reset(st, fid):
        loop._invalidate_merged_verify_budget(fid, st)  # the reset lands first
        return real_arm(st, fid)

    monkeypatch.setattr(loop, "_arm_merged_verify_exhaustion", _arm_after_reset)
    feature = {"id": "bd-1", "labels": ["merged-verified:oldsha"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == []  # AT the cap → the gate never ran
    assert loop._merged_verify_attempts["bd-1"] == 0  # the reset's pinned 0 stands
    # No max+1 sentinel persisted — only the reset's label clear ran.
    assert ("bd-1", "merged-verify", 2) not in store.budgets
    assert store.cleared == [("bd-1", ("merged-verify",))]


async def test_pinned_zero_defeats_a_stale_exhaustion_label_snapshot(monkeypatch):
    """After a reset, a poll whose feature snapshot still carries the pre-reset
    `budget:merged-verify:<max+1>` label must NOT re-hold the card: the pinned 0 wins over
    the stale snapshot in `_budget_get`, so the loop re-verifies and re-stamps."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("freshbase"))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", "/wt")))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    loop._invalidate_merged_verify_budget("bd-1", store)  # operator reset → pins 0
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # The poll's snapshot is stale — it still shows the exhaustion sentinel label.
    feature = {"id": "bd-1", "labels": ["merged-verified:old", "budget:merged-verify:2"]}
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert store.verified == [("bd-1", "freshbase")]  # re-verified, not held on the stale label
    assert loop._merged_verify_attempts["bd-1"] == 1  # a real re-verify spent 0→1


def test_start_publishes_the_loop_to_the_process_stable_slot():
    """The live loop registers itself so the reset verb can reach its cache — even a
    DISABLED loop (start returns None, spawns no task) publishes the handle (ADR 0326)."""
    slot = loop_mod._loop_slot()
    prior = slot.loop
    slot.loop = None
    try:
        loop = BoardLoop({})  # loop_enabled defaults False
        assert loop.start() is None  # disabled → no task
        assert loop_mod.live_loop() is loop
    finally:
        slot.loop = prior


async def test_verify_merged_state_budget_zero_is_unlimited(monkeypatch):
    """merged_verify_max=0 ⇒ every base move re-verifies (the auto_merge posture for a
    busy base: a held card must never park on a stale stamp)."""
    loop = _vloop(merged_verify_max=0)
    assert loop.merged_verify_max == 0
    # Exhaustion branch unreachable: attempts far past any budget still verify.
    loop._merged_verify_attempts["bd-1"] = 50
    ran = []

    async def _head(repo, ref):
        return "f" * 40

    async def _mst(repo, branch, base_sha, root="."):
        ran.append(base_sha)
        return ("ok", "/wt")

    async def _gate(wt, feature=None):
        return None

    async def _rm(*a, **k):
        return None

    monkeypatch.setattr(worktree, "origin_head_sha", _head)
    monkeypatch.setattr(worktree, "merged_state_worktree", _mst)
    monkeypatch.setattr(worktree, "remove_worktree", _rm)
    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    store = _VerifyStore({"id": "bd-1"})
    feature = {"id": "bd-1", "labels": ["merged-verified:000000000000"]}
    await loop._verify_merged_state(store, feature, "https://x/pull/1", "/repo")
    assert ran == ["f" * 40]
    assert store.verified == [("bd-1", "f" * 12)]


async def test_verify_merged_state_absent_stamp_is_never_reported_stale(monkeypatch, caplog):
    """With the budget spent and NO stamp ever written (every write failed), the exhaustion
    log must not claim a 'stale stamp' that doesn't exist — that false report is the very
    class of bug #132 was built to prevent (#135)."""
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("abc123def456"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop(merged_verify_max=1)
    loop._merged_verify_attempts["bd-1"] = 1  # budget already spent, stamp write never landed
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        assert await loop._verify_merged_state(store, {"id": "bd-1", "labels": []}, "pr", "/repo") is False
    joined = "\n".join(caplog.messages)
    assert "no merged-verified stamp was ever written" in joined
    assert "leaving the stale merged-verified stamp" not in joined
    assert not built  # budget spent → never even builds the merged worktree
    assert store.verified == [] and store.blocked == []


@pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — integration label round-trip needs it",
)
async def test_verify_merged_state_stamp_lands_through_real_br(monkeypatch, tmp_path):
    """Integration (#135): the merged-verified stamp must LAND through the ACTUAL `br`
    label path with a real 40-char sha. `merged-verified:` + 40 = 56 > beads' 50-char
    cap, so the pre-#135 write died VALIDATION_FAILED and #132's promised stamp never
    existed — a gap the fake-`record_merged_verified` unit tests could never catch. A
    REAL BeadsBoard writes the (short) stamp here; we assert the label is on the bead."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    f = board.create_feature("Verify stamp", spec="s", acceptance_criteria="WHEN x THE SYSTEM SHALL y")
    fid = f["id"]
    full_sha = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha
    assert len(full_sha) == 40
    monkeypatch.setattr(worktree, "origin_head_sha", _aret(full_sha))
    monkeypatch.setattr(worktree, "merged_state_worktree", _aret(("merged", str(tmp_path / "wt"))))
    monkeypatch.setattr(worktree, "remove_worktree", _aret(None))
    loop = _vloop()
    monkeypatch.setattr(loop, "_run_local_gate", _aret(None))  # green
    # base moved (no stamp yet) + green gate → stamp the short sha through real `br`.
    assert await loop._verify_merged_state(board, board.get_feature(fid), "pr", str(tmp_path)) is False
    labels = board.get_feature(fid).get("labels") or []
    stamps = [l for l in labels if l.startswith("merged-verified:")]
    assert stamps == [f"merged-verified:{full_sha[:_MERGED_VERIFIED_SHA_LEN]}"]  # it LANDED
    assert all(len(l) <= 50 for l in labels)  # and every label fits beads' cap


async def test_reconcile_prs_verify_block_skips_ci(monkeypatch):
    """The merged-state verify rides the OPEN branch after the rebase; when it
    blocks (red gate on the merged state) the CI reconcile is skipped."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop()
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    monkeypatch.setattr(loop, "_verify_merged_state", _aret(True))
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    await loop._reconcile_prs()
    assert ci == []  # blocked on the merged state → CI reconcile skipped


async def test_reconcile_prs_verify_green_still_runs_ci(monkeypatch):
    """A verify that did NOT block (green gate, or nothing to do) leaves the rest
    of the pass — the CI reconcile — untouched."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop()
    monkeypatch.setattr(loop, "_maybe_rebase", _aret(False))
    monkeypatch.setattr(loop, "_verify_merged_state", _aret(False))
    ci = []

    async def _ci_spy(*a, **k):
        ci.append(1)

    monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
    await loop._reconcile_prs()
    assert ci == [1]


async def test_reconcile_prs_auto_rebase_off_skips_verify(monkeypatch):
    """The verify extends the auto_rebase mechanism — the same flag gates it."""
    store = _VerifyStore({"id": "bd-1", "pr_url": "https://e/pr/1", "labels": []})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("OPEN"))
    loop = _vloop(auto_rebase=False, ci_poll=False)
    called = []
    monkeypatch.setattr(loop, "_verify_merged_state", lambda *a, **k: called.append(1))
    await loop._reconcile_prs()
    assert called == []


async def test_reconcile_merged_clears_merged_verify_attempts(monkeypatch):
    """The merged terminal edge drops the per-run verify counter with the rest."""
    store = _ReconcileStore([{"id": "bd-m", "pr_url": "https://e/pr/1"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "pr_state", _aret("MERGED"))
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    loop = BoardLoop({})
    loop._merged_verify_attempts["bd-m"] = 2
    await loop._reconcile_prs()
    assert "bd-m" not in loop._merged_verify_attempts


async def test_maybe_reconcile_is_rate_limited(monkeypatch):
    loop = BoardLoop({"merge_poll": True, "merge_poll_interval_s": 60})
    calls = []

    async def _reconcile():
        calls.append(1)

    monkeypatch.setattr(loop, "_reconcile_prs", _reconcile)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])

    await loop._maybe_reconcile()  # first → reconciles
    await loop._maybe_reconcile()  # immediately → rate-limited
    clock["t"] += 61
    await loop._maybe_reconcile()  # interval elapsed → reconciles again
    assert len(calls) == 2


async def test_merge_poll_off_never_reconciles(monkeypatch):
    loop = BoardLoop({"merge_poll": False})
    called = []
    monkeypatch.setattr(loop, "_reconcile_prs", lambda: called.append(1))
    await loop._maybe_reconcile()
    assert called == []  # disabled → never reconciles


# ── crash recovery on boot ──────────────────────────────────────────────────────


class _RecoverStore:
    def __init__(self, in_progress):
        self._in_progress = in_progress
        self.calls = []

    def list_features(self, state=None):
        return self._in_progress if state == "in_progress" else []

    def raw_features_with_comments(self, states=("done", "blocked")):
        return []  # no blocked cards → the boot preflight release is a no-op

    def get_feature(self, fid):
        return {"id": fid}  # no verified_sha → the salvage check declines cleanly

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def requeue(self, fid):
        self.calls.append(("requeue", fid))


async def test_recover_adopts_an_open_pr_else_resets_to_ready(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        return "https://example/pr/1" if branch == "feat/bd-1" else ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()
    # bd-1 already had a PR (crash between open_pr and open_review) → adopt → in_review.
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    # bd-2 has no PR → reset to ready for a clean rebuild.
    assert ("requeue", "bd-2") in store.calls


async def test_recover_is_resilient_to_a_per_feature_error(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        if branch == "feat/bd-1":
            raise RuntimeError("gh exploded")
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()  # must not raise
    # bd-1 errored and was skipped; bd-2 still recovered.
    assert ("requeue", "bd-2") in store.calls
    assert all(c[1] != "bd-1" for c in store.calls)


# ── crash salvage: resume a verified candidate instead of rebuilding (#91) ──────


class _SalvageStore:
    """A recovery store whose one in_progress feature carries a verified-candidate
    record (the `verified:<sha>` label projected as ``verified_sha``)."""

    def __init__(self, feature):
        self.feature = feature
        self.calls = []

    def list_features(self, state=None):
        return [{"id": self.feature["id"]}] if state == "in_progress" else []

    def raw_features_with_comments(self, states=("done", "blocked")):
        return []  # no blocked cards → the boot preflight release is a no-op

    def get_feature(self, fid):
        return self.feature

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def requeue(self, fid):
        self.calls.append(("requeue", fid))

    def clear_verified_candidate(self, fid):
        self.calls.append(("clear_verified", fid))


def _salvage_git(*, head="abc123", branch="feat/bd-1-add-a-thing"):
    """A ``worktree._git`` fake answering the salvage probes (HEAD sha + branch). The
    default branch carries the #227 slug tail of the salvage feature's title."""

    async def _git(wt, *args, timeout=60):
        if args == ("rev-parse", "HEAD"):
            return (0, head + "\n", "")
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return (0, branch + "\n", "")
        return (0, "", "")

    return _git


async def _recover_salvage(monkeypatch, tmp_path, *, make_wt=True, head="abc123", gate_out=None):
    """Run boot recovery over ONE in_progress feature with a recorded verified sha
    of ``abc123``. Returns (store, promoted, opened, gates) for the assertions."""
    feature = {
        "id": "bd-1",
        "title": "Add a thing",
        "repo": str(tmp_path),
        "base_branch": "main",
        "verified_sha": "abc123",
    }
    store = _SalvageStore(feature)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "_git", _salvage_git(head=head))
    if make_wt:
        # #227: the verified candidate's canonical worktree carries the slugged tail.
        (tmp_path / ".worktrees" / "feat-bd-1-add-a-thing").mkdir(parents=True)

    promoted = []

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        promoted.append((src_wt, src_branch, fid))
        return (src_wt, src_branch)  # already canonical → the real one no-ops too

    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    opened = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append((wt, branch, base, title))
        return "https://example/pr/91"

    monkeypatch.setattr(worktree, "open_pr", _open_pr)

    loop = BoardLoop({"repo": str(tmp_path)})
    gates = []

    async def _gate(wt, feature=None):
        gates.append(wt)
        return gate_out

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    await loop._recover()
    return store, promoted, opened, gates


async def test_recover_salvages_a_verified_candidate(monkeypatch, tmp_path):
    """A crash between verify and open_pr: the recorded candidate's worktree exists,
    its branch+sha match, and the gate passes NOW → resume at promote → fixups →
    gate → open_pr → in_review. No re-solve, no rebuild-fresh requeue."""
    store, promoted, opened, gates = await _recover_salvage(monkeypatch, tmp_path)
    assert promoted and promoted[0][1:] == ("feat/bd-1-add-a-thing", "bd-1")  # resumed at promote (#227 slug)
    assert gates  # the gate re-ran on the candidate now
    assert opened and opened[0][1] == "feat/bd-1-add-a-thing" and opened[0][3] == "feat: Add a thing"
    assert ("open_review", "bd-1", "https://example/pr/91") in store.calls
    assert ("clear_verified", "bd-1") in store.calls  # the record's window closed
    assert ("requeue", "bd-1") not in store.calls  # never fell through to rebuild


async def test_recover_salvage_worktree_gone_rebuilds_fresh(monkeypatch, tmp_path):
    """The record exists but the worktree dir is gone → doubt → today's rebuild-fresh
    path (requeue), with no PR opened off a missing tree."""
    store, promoted, opened, _gates = await _recover_salvage(monkeypatch, tmp_path, make_wt=False)
    assert ("requeue", "bd-1") in store.calls
    assert not promoted and not opened
    assert ("clear_verified", "bd-1") in store.calls  # stale record dropped


async def test_recover_salvage_sha_mismatch_rebuilds_fresh(monkeypatch, tmp_path):
    """The worktree exists but its HEAD is not the recorded sha (someone/something
    moved it since verify) → doubt → rebuild fresh, never ship the drifted tree."""
    store, promoted, opened, _gates = await _recover_salvage(monkeypatch, tmp_path, head="0ther5ha")
    assert ("requeue", "bd-1") in store.calls
    assert not promoted and not opened


async def test_recover_salvage_gate_failing_now_rebuilds_fresh(monkeypatch, tmp_path):
    """Worktree+branch+sha all check out, but the pre-PR gate FAILS on the candidate
    now (base moved, env changed) → doubt → rebuild fresh, no PR."""
    store, _promoted, opened, gates = await _recover_salvage(monkeypatch, tmp_path, gate_out="FAILED tests: boom")
    assert gates  # the gate did run against the candidate
    assert not opened  # ...and its failure stopped the salvage before open_pr
    assert ("requeue", "bd-1") in store.calls
    assert ("clear_verified", "bd-1") in store.calls


async def test_recover_salvage_open_pr_error_falls_back_to_rebuild(monkeypatch, tmp_path):
    """ANY error inside the salvage (here: open_pr blowing up) must degrade to the
    rebuild-fresh path, never crash recovery or strand the feature in_progress."""
    feature = {"id": "bd-1", "title": "T", "repo": str(tmp_path), "base_branch": "main", "verified_sha": "abc123"}
    store = _SalvageStore(feature)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "_git", _salvage_git(branch="feat/bd-1-t"))  # title "T" → #227 slug "t"
    (tmp_path / ".worktrees" / "feat-bd-1-t").mkdir(parents=True)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        return (src_wt, src_branch)

    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    async def _boom_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise worktree.WorktreeError("gh exploded")

    monkeypatch.setattr(worktree, "open_pr", _boom_pr)
    loop = BoardLoop({"repo": str(tmp_path)})

    async def _gate(wt, feature=None):
        return None

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    await loop._recover()  # must not raise
    assert ("requeue", "bd-1") in store.calls
    assert all(c[0] != "open_review" for c in store.calls)  # nothing pretended a PR opened


# ── periodic health sweep ───────────────────────────────────────────────────────


class _SweepStore:
    def __init__(self, in_progress=(), features=None):
        self._in_progress = list(in_progress)
        self._features = features or {}  # fid -> board_state
        self.requeued = []
        self.archive_windows = []  # archive_after_days of each archive pass (#115)

    def list_features(self, state=None):
        return [{"id": f} for f in self._in_progress] if state == "in_progress" else []

    def requeue(self, fid):
        self.requeued.append(fid)

    def open_review(self, fid, *, pr_url):
        pass

    def get_feature(self, fid):
        st = self._features.get(fid)
        return {"id": fid, "board_state": st} if st else None

    def archive_stale(self, archive_after_days=7):
        self.archive_windows.append(archive_after_days)
        return []


async def test_sweep_reconciles_in_progress_with_no_live_drive(monkeypatch):
    store = _SweepStore(in_progress=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    loop = BoardLoop({})
    loop._inflight_files = {"bd-2": {"a.py"}}  # bd-2 has a live drive → skip
    await loop._sweep()
    assert store.requeued == ["bd-1"]  # bd-1 (no PR, no drive) reset; bd-2 left alone


async def test_sweep_reaps_orphaned_worktrees(monkeypatch):
    store = _SweepStore(features={"bd-done": "done", "bd-cancelled": "cancelled", "bd-rev": "in_review"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(
        worktree, "list_feature_worktrees", lambda repo, root: ["bd-done", "bd-cancelled", "bd-rev", "bd-gone"]
    )
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    await BoardLoop({})._sweep()
    # Terminal (done/cancelled) + missing feature → reaped; in_review keeps its worktree
    # (a CI-fail re-dispatch still pushes to the same PR). Cancelled is the crash backstop
    # for the terminal-edge reap in api._cancel (#109).
    assert set(reaped) == {"bd-done", "bd-cancelled", "bd-gone"}


async def test_sweep_treats_candidate_worktrees_by_parent_feature(monkeypatch):
    """A leftover `.gN`/`.cN` candidate worktree is NOT a feature id (bd-1cp.g1) — the
    sweep must resolve its PARENT feature's state (#91): parent done/gone → the
    candidate is reaped (by its FULL worktree id, so the right dir+branch go); parent
    with a live drive → left alone. And the store is never asked for the raw candidate
    id — that lookup was the old warning-spam-every-sweep path."""
    store = _SweepStore(features={"bd-done": "done", "bd-live": "in_progress"})
    looked_up = []
    orig_get = store.get_feature

    def _spy(fid):
        looked_up.append(fid)
        return orig_get(fid)

    store.get_feature = _spy
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(
        worktree, "list_feature_worktrees", lambda repo, root: ["bd-done.g1", "bd-done.c2", "bd-gone.g3", "bd-live.g1"]
    )
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    loop = BoardLoop({})
    loop._inflight_files = {"bd-live": {"a.py"}}  # bd-live's drive is live → its candidates stay
    await loop._sweep()
    assert set(reaped) == {"bd-done.g1", "bd-done.c2", "bd-gone.g3"}  # full worktree ids
    assert looked_up and all("." not in fid for fid in looked_up)  # never the raw candidate id


def test_archive_window_config_default_and_override():
    assert BoardLoop({}).archive_after_days == 7  # the #115 default window
    assert BoardLoop({"archive_after_days": 30}).archive_after_days == 30


async def test_sweep_runs_the_archive_pass_with_the_configured_window(monkeypatch):
    """The archive pass (#115) rides the existing health sweep — no scheduler of its
    own — and hands the store the configured window."""
    store = _SweepStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    await BoardLoop({"archive_after_days": 3})._sweep()
    assert store.archive_windows == [3.0]


async def test_sweep_survives_an_archive_pass_failure(monkeypatch):
    """The archive pass is best-effort: a store error there must not break the sweep
    (the self-heal halves already ran) or escape into the loop."""
    store = _SweepStore(in_progress=["bd-1"])

    def _boom(archive_after_days=7):
        raise RuntimeError("br unavailable")

    store.archive_stale = _boom
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    await BoardLoop({})._sweep()  # must not raise
    assert store.requeued == ["bd-1"]  # the reconcile half still did its job


async def test_maybe_sweep_is_rate_limited(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 300})
    calls = []

    async def _sweep():
        calls.append(1)

    monkeypatch.setattr(loop, "_sweep", _sweep)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])
    await loop._maybe_sweep()  # first → sweeps
    await loop._maybe_sweep()  # immediately → rate-limited
    clock["t"] += 301
    await loop._maybe_sweep()  # interval elapsed → sweeps again
    assert len(calls) == 2


async def test_sweep_off_when_interval_zero(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 0})
    called = []
    monkeypatch.setattr(loop, "_sweep", lambda: called.append(1))
    await loop._maybe_sweep()
    assert called == []  # disabled → never sweeps


# ── dependency gate (merge vs review) ───────────────────────────────────────────


def test_dep_gate_config_defaults_to_merge():
    assert BoardLoop({}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "merge"}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "review"}).relaxed_gate is True


async def test_spawn_ready_passes_the_dep_gate_to_ready_queue(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"dep_gate": "review", "max_concurrent": 1})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        await loop._spawn_ready()
        assert store.last_relaxed is True  # the relaxed gate reaches ready_queue
    finally:
        await finish()


# ── max-mode best-of-N judge (#21) ───────────────────────────────────────────────


async def test_judge_candidates_returns_the_model_pick(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        # distinct non-empty diff per worktree so every candidate competes
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _judge(prompt, *, system=None, model_name=None):
        assert "WHEN x THE SYSTEM SHALL y" in prompt  # acceptance criteria reach the judge
        return "Candidate 1 is the most complete."

    monkeypatch.setattr("graph.sdk.complete", _judge)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_none_when_all_empty(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run when there is nothing to judge")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) is None


async def test_judge_candidates_single_nonempty_skips_the_model(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        if args[0] == "diff" and wt == "/wt/b":
            return (0, "real diff", "")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run for a single candidate")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_fails_open_to_first_when_judge_errors(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _err(prompt, *, system=None, model_name=None):
        raise RuntimeError("model offline")

    monkeypatch.setattr("graph.sdk.complete", _err)
    # both candidates non-empty → first non-empty index wins when the judge dies
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 0


# ── execution-grounded candidate selection (ADR 0064) ────────────────────────────


def _git_nonempty_for(nonempty_wts):
    """A worktree._git stub: name-only diff is non-empty only for the given worktrees."""

    async def _git(wt, *args, timeout=60):
        if args and args[0] == "diff":
            return (0, ("solution.py" if wt in nonempty_wts else ""), "")
        return (0, "", "")

    return _git


async def test_select_candidate_prefers_passing_gate(monkeypatch):
    """With a gate, the candidate whose gate PASSES wins even if the judge would pick another."""
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 3})
    wts = ["/c0", "/c1", "/c2"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))  # all have a diff

    async def gate(wt, feature=None):
        return None if wt == "/c2" else "boom"  # only c2 passes

    async def judge(*a, **k):
        return 0  # the judge would (wrongly) pick c0 — must be overridden

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 2


async def test_select_candidate_judges_only_among_passing(monkeypatch):
    """Multiple candidates pass → the judge breaks the tie among the PASSING set only."""
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 3})
    wts = ["/c0", "/c1", "/c2"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        return None if wt in ("/c0", "/c2") else "boom"  # c0 + c2 pass, c1 fails

    async def judge(feature, base, sub):
        assert sub == ["/c0", "/c2"]  # judge sees only the passing candidates
        return 1  # picks the 2nd of the sublist → original index 2

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 2


async def test_select_candidate_falls_back_to_judge_when_none_pass(monkeypatch):
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 2})
    wts = ["/c0", "/c1"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        return "boom"  # none pass

    async def judge(feature, base, sub):
        assert sub == wts  # judges over ALL candidates
        return 1

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 1


async def test_select_candidate_no_gate_uses_judge_and_never_runs_gate(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})  # no local_gate_cmd
    wts = ["/c0", "/c1"]
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set(wts)))

    async def gate(wt, feature=None):
        raise AssertionError("the gate must not run when local_gate_cmd is unset")

    async def judge(*a, **k):
        return 0

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    monkeypatch.setattr(loop, "_judge_candidates", judge)
    assert await loop._select_candidate(FEATURE, "main", wts) == 0


async def test_select_candidate_none_when_no_diff(monkeypatch):
    loop = BoardLoop({"local_gate_cmd": "pytest", "max_mode_n": 2})
    monkeypatch.setattr(worktree, "_git", _git_nonempty_for(set()))  # all empty

    async def gate(wt, feature=None):
        raise AssertionError("no diffs → nothing to gate")

    monkeypatch.setattr(loop, "_run_local_gate", gate)
    assert await loop._select_candidate(FEATURE, "main", ["/c0", "/c1"]) is None


# ── the blocking review gate (plan M5): bounce / budget / exhaustion ─────────────


class _GateStore(FakeLoopStore):
    """FakeLoopStore + the review-gate surface (sub-state labels, requeue, lookup)."""

    def __init__(self):
        super().__init__()
        self.review_states = []  # (label, note) history
        self.state = "in_review"
        self.labels = []  # bead labels — carries persisted `budget:` counters (#259)

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.calls.append(("set_review_substate", fid, label))
        self.review_states.append((label, note))
        return {"id": fid}

    def record_reviewed_head(self, fid, sha):
        # #328: the head the verdict was rendered against (or "" to clear the stamp).
        self.calls.append(("record_reviewed_head", fid, sha))
        return {"id": fid}

    def requeue(self, fid):
        self.calls.append(("requeue", fid))
        self.state = "ready"
        return {"id": fid}

    def get_feature(self, fid):
        return {"id": fid, "board_state": self.state, "labels": list(self.labels)}


def _inject_fake_findings(monkeypatch):
    """Stand in for the HOST's graph.review.findings (absent in this suite) — the
    ADR 0077 contract _review_gate imports lazily. Parses any fenced/bare JSON
    array into finding-shaped objects."""
    import json as _json
    import types as _types
    from dataclasses import dataclass, field

    @dataclass
    class _Finding:
        file: str = ""
        line: int = 0
        severity: str = "minor"
        category: str = ""
        claim: str = ""
        evidence: str = ""
        verdict: str = ""
        note: str = field(default="")

        def to_dict(self):
            from dataclasses import asdict

            return asdict(self)

    def parse_findings(text):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            items = _json.loads(text[start : end + 1])
        except _json.JSONDecodeError:
            return []
        return [
            _Finding(**{k: v for k, v in it.items() if k in _Finding.__dataclass_fields__})
            for it in items
            if isinstance(it, dict) and it.get("claim")
        ]

    def render_findings_markdown(findings, title="Review findings"):
        return f"## {title}\n" + "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.claim}" for f in findings)

    mod = _types.ModuleType("graph.review.findings")
    mod.parse_findings = parse_findings
    mod.render_findings_markdown = render_findings_markdown
    pkg = _types.ModuleType("graph")
    sub = _types.ModuleType("graph.review")
    pkg.review = sub
    sub.findings = mod
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "graph", pkg)
    monkeypatch.setitem(_sys.modules, "graph.review", sub)
    monkeypatch.setitem(_sys.modules, "graph.review.findings", mod)


@pytest.fixture(autouse=True)
def _no_real_pr_head_sha(monkeypatch):
    """#328: the review gate now reads ``worktree.pr_head_sha`` to stamp the head a
    verdict is for, and the reconcile reads it to spot an external push. Pin it so no
    review-gate/reconcile test in this module shells a real ``gh`` — the default is ""
    (unreadable, the fail-closed edge); a test that exercises the re-arm injects its own
    head. Mirrors the ``_no_real_br_version`` / ``_clean_base_checkout`` guards."""

    async def _blank(pr_url, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_head_sha", _blank)


# The #347 App-only check-run guards (`_no_real_post_review_check` / `_no_real_read_review_check`)
# are gone: bd-doo0 deleted `worktree.post_review_check` / `read_review_check` (the path 403s under
# the board's user/PAT credential). The #354 status seam below is the only publication seam left to
# pin, so a stale check-run guard would only fail at `monkeypatch.setattr` on a missing attribute.


@pytest.fixture(autouse=True)
def _no_real_review_status_seam(monkeypatch):
    """#354: the gate now publishes via ``worktree.post_review_status`` + a findings PR comment
    (``post_or_update_pr_comment``) and the reconcile reads back ``worktree.read_review_status``.
    Pin all three to fail-closed / no-op defaults so no gate/reconcile test shells a real
    ``gh api`` — a test that asserts the new seam installs its own recorder. This is the only
    verdict-publication seam left to pin (bd-doo0 removed the App-only check-run guards)."""

    async def _ok(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(worktree, "post_review_status", _ok)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _ok)
    monkeypatch.setattr(worktree, "read_review_status", _none)


def _record_review_statuses(monkeypatch):
    """Install recorders over the #354 publication seam — ``worktree.post_review_status`` (the
    commit status) and ``worktree.post_or_update_pr_comment`` (the findings comment) — and return
    ``(statuses, comments)``, the lists the gate tests assert against without a real ``gh``."""
    statuses: list[dict] = []
    comments: list[dict] = []

    async def _status(repo_slug, head_sha, *, state, description, target_url="", context="QA panel", cwd="."):
        statuses.append(
            {
                "repo_slug": repo_slug,
                "head_sha": head_sha,
                "state": state,
                "description": description,
                "target_url": target_url,
                "context": context,
            }
        )
        return True

    async def _comment(pr_url, body, *, marker=worktree.REVIEW_COMMENT_MARKER, cwd="."):
        comments.append({"pr_url": pr_url, "body": body})
        return True

    monkeypatch.setattr(worktree, "post_review_status", _status)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _comment)
    return statuses, comments


def _gate_loop(monkeypatch, output, cfg=None):
    """A review_gate loop whose review-workflow run returns ``output`` (None = the
    run could not happen, with the no-runner-no-reviewer reason) and whose PR-diff
    fetch is stubbed."""
    loop = BoardLoop({"review_gate": True, **(cfg or {})})

    async def _run(fid, pr_url):
        if output is None:
            return None, "no workflow runner available and no reviewer configured"
        return output, None

    async def _diff(pr_url, cwd="."):
        return "diff --git a/x b/x"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    return loop


def test_review_gate_config():
    loop = BoardLoop({})
    assert loop.review_gate is False and loop.review_workflow == "code-review" and loop.review_fix_max == 2
    assert BoardLoop({"review_gate": True, "review_fix_max": 0}).review_fix_max == 0
    assert BoardLoop({"review_workflow": " my-review "}).review_workflow == "my-review"


_BLOCKER = '[{"file": "a.py", "line": 3, "severity": "blocker", "claim": "drops data", "evidence": "x", "verdict": "confirmed"}]'
_MINOR = '[{"file": "a.py", "line": 3, "severity": "nit", "claim": "naming", "evidence": "x", "verdict": "confirmed"}]'
_REFUTED = (
    '[{"file": "a.py", "line": 3, "severity": "blocker", "claim": "drops data", "evidence": "x", "verdict": "refuted"}]'
)


async def test_review_gate_bounces_with_findings_in_the_retry_prompt(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, f"brief…\n```json\n{_BLOCKER}\n```")
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert ("requeue", "bd-1") in store.calls
    # sub-state walked pending → changes-requested, findings recorded on the bead
    assert store.review_states[0][0] == "review-pending"
    assert store.review_states[-1][0] == "changes-requested"
    assert "drops data" in store.review_states[-1][1]
    # the retry prompt carries the findings + the reviewed diff (the CI-bounce levers)
    assert "REQUESTED CHANGES" in loop._ci_feedback["bd-1"]
    assert "drops data" in loop._ci_feedback["bd-1"]
    assert loop._ci_prior_diff["bd-1"].startswith("diff --git")
    assert loop._review_fix_attempts["bd-1"] == 1
    # and the injected feedback lands in the next build prompt
    prompt = loop._build_prompt({**FEATURE})
    assert "REQUESTED CHANGES" in prompt and "drops data" in prompt


async def test_review_gate_clean_and_nonblocking_findings_pass(monkeypatch):
    _inject_fake_findings(monkeypatch)
    for output in ("clean.\n```json\n[]\n```", _MINOR, _REFUTED):
        store = _GateStore()
        loop = _gate_loop(monkeypatch, output)
        await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
        assert ("requeue", "bd-1") not in store.calls
        assert not any(c[0] == "flag_blocked" for c in store.calls)
        assert store.review_states[-1][0] == "review-clean"  # the POSITIVE verdict the merge edge keys off
        assert "bd-1" not in loop._ci_feedback


async def test_review_gate_exhausted_budget_blocks_never_merges_silently(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, _BLOCKER, cfg={"review_fix_max": 1})
    loop._review_fix_attempts["bd-1"] = 1  # budget already spent
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "needs human review" in blocked[0][2]
    assert ("requeue", "bd-1") not in store.calls
    assert loop._review_fix_attempts.get("bd-1", 0) == 0  # budget cleared (pinned 0) with the block


async def test_review_gate_exhausted_persisted_budget_blocks_after_restart(monkeypatch):
    """#259: the review-fix budget rides the bead (`budget:review-fix:<n>`) — a fresh
    loop (empty dicts) derives the spent count via store.get_feature and blocks instead
    of bouncing again (the pre-#259 loop, sibling test above, needed the count pre-seeded
    in its dict to block; with an empty dict it re-dispatched)."""
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    store.labels = ["budget:review-fix:1"]  # the spent budget, persisted by the old process
    loop = _gate_loop(monkeypatch, _BLOCKER, cfg={"review_fix_max": 1})
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "needs human review" in blocked[0][2]
    assert ("requeue", "bd-1") not in store.calls  # no re-dispatch on a spent budget


async def test_review_gate_unrunnable_leaves_pending_for_the_reconcile_retry(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, None)  # no runner + no reviewer
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert store.review_states == [("review-pending", "")]  # left pending — retried next poll
    assert ("requeue", "bd-1") not in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)


def test_parse_pr_url():
    from project_board.loop import _parse_pr_url

    assert _parse_pr_url("https://github.com/protoLabsAI/protoContent/pull/421") == (
        "421",
        "protoLabsAI/protoContent",
    )
    assert _parse_pr_url("https://example.com/not-a-pr") == ("", "")


# ── fail-closed gate + delta re-review carry (ADR 0078 Phase A2) ─────────────────


def test_review_gate_config_run_max():
    assert BoardLoop({}).review_run_max == 3
    assert BoardLoop({"review_run_max": 0}).review_run_max == 1  # floor: at least one try


async def test_review_gate_partial_panel_is_not_a_review(monkeypatch):
    """A workflow result with failed steps must NOT be judged — the gate treats it
    as unreviewed (fail closed): review-pending stays, no requeue, no block."""
    _inject_fake_findings(monkeypatch)
    import sys as _sys
    import types as _types

    calls = []

    async def _runner(name, inputs):
        calls.append(inputs)
        return {"output": "clean.\n```json\n[]\n```", "steps": {}, "failed": ["find_crossfile"]}

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)

    store = _GateStore()
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)  # no reviewer fallback
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert calls, "the runner must have been invoked"
    # Fail closed: the clean-looking partial output was NOT judged.
    assert store.review_states == [("review-pending", "")]
    assert ("requeue", "bd-1") not in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    assert loop._review_run_failures["bd-1"] == 1


async def test_review_gate_unrunnable_escalates_after_run_max(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, None, cfg={"review_run_max": 2})
    loop._review_run_failures["bd-1"] = 1  # one prior unrunnable attempt
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "operator attention" in blocked[0][2]
    # #180: the block reason carries the ACTUAL cause, not the generic three-hypothesis text
    assert "no workflow runner available and no reviewer configured" in blocked[0][2]
    assert loop._review_run_failures.get("bd-1", 0) == 0  # budget cleared (pinned 0) with the block
    # #181: review-pending is PRESERVED through the block (never cleared) so an
    # operator unblock re-arms the gate on the next reconcile poll — cleared, the
    # feature would sit in_review indistinguishable from a clean review.
    assert store.review_states == [("review-pending", "")]
    assert ("set_review_substate", "bd-1", None) not in store.calls


class _RoundTripStore(_GateStore):
    """_GateStore + the reconcile surface — one feature whose labels/state mirror
    what set_review_substate / flag_blocked / an operator unblock would do."""

    _SUBSTATES = ("review-pending", "changes-requested", "review-clean")

    def __init__(self):
        super().__init__()
        self.labels = []
        self.pr_url = "https://github.com/o/r/pull/9"

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.labels = [l for l in self.labels if l not in self._SUBSTATES]
        if label:
            self.labels.append(label)
        # Mirror the real store's head pin (#323): a review-clean verdict stamps
        # `review-clean-sha:<head>`; any other substate drops a stale pin. This is what
        # the auto-merge edge reads to head-pin the merge.
        self.labels = [l for l in self.labels if not str(l).startswith("review-clean-sha:")]
        if label == "review-clean" and head_sha:
            self.labels.append(f"review-clean-sha:{head_sha}")
        return super().set_review_substate(fid, label, note)

    def record_reviewed_head(self, fid, sha):
        # #328: single replaced `reviewed-head:<sha>` label; "" clears it. Survives a
        # set_review_substate swap (it isn't one of the three review sub-state labels).
        self.labels = [l for l in self.labels if not l.startswith("reviewed-head:")]
        if sha:
            self.labels.append(f"reviewed-head:{sha}")
        return super().record_reviewed_head(fid, sha)

    def flag_blocked(self, fid, reason):
        self.state = "blocked"
        return super().flag_blocked(fid, reason)

    def unblock(self, fid):
        self.state = "in_review"

    def list_features(self, state=None):
        if state == "in_review" and self.state == "in_review":
            return [{"id": "bd-1", "pr_url": self.pr_url, "labels": list(self.labels), "repo": "/repo"}]
        return []

    def get_feature(self, fid):
        return {"id": fid, "board_state": self.state, "labels": list(self.labels)}


async def test_review_gate_unrunnable_block_then_unblock_rearms_the_gate(monkeypatch):
    """The full #181 round trip: gate exhausts review_run_max → blocked WITH
    review-pending kept → the label is inert while blocked (no reconcile) →
    operator unblocks → the next reconcile poll sees review-pending and re-runs
    the gate."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    # merge_poll off → ci_poll/auto_rebase default off, so the reconcile exercises
    # ONLY the review-gate resume edge.
    loop = BoardLoop({"review_gate": True, "review_run_max": 1, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return None, "stub: unrunnable"  # no runner, dead run, or partial panel

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    # Round 1: the only allowed attempt fails → blocked, review-pending intact.
    await loop._review_gate(store, "bd-1", store.pr_url, "/repo")
    assert store.state == "blocked"
    assert "review-pending" in store.labels

    # Blocked features aren't reconciled — the label is inert, the gate stays quiet.
    await loop._reconcile_prs()
    assert runs == [store.pr_url]

    # Operator fixes the environment and unblocks → back in_review with the label
    # still set — the next poll re-arms the gate.
    store.unblock("bd-1")
    await loop._reconcile_prs()
    assert runs == [store.pr_url, store.pr_url]


async def test_review_gate_in_flight_is_not_rearmed_by_the_reconcile(monkeypatch):
    """#205: the drive's gate is RUNNING (pending label set, panel mid-flight) when
    the reconcile poll comes round and sees review-pending on an in_review card.
    That is not an interrupted gate — the reconcile must NOT start a second panel
    on the same head: exactly one workflow run, one verdict, one bounce increment."""
    import asyncio as _asyncio

    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []
    started, release = _asyncio.Event(), _asyncio.Event()

    async def _run(fid, pr_url):
        runs.append(pr_url)
        started.set()
        await release.wait()  # hold the panel open — minutes in real life
        return f"brief…\n```json\n{_BLOCKER}\n```", None

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _diff(url, *, cwd=".", max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    # The drive's gate starts and parks inside the panel with review-pending set.
    gate = _asyncio.create_task(loop._review_gate(store, "bd-1", store.pr_url, "/repo"))
    await started.wait()
    assert "review-pending" in store.labels and "bd-1" in loop._review_inflight

    # The reconcile poll fires mid-gate: the resume edge sees the pending label …
    await loop._reconcile_prs()
    # … and does nothing — no second panel.
    assert runs == [store.pr_url]

    # The first gate lands its own verdict: ONE bounce, budget 1 (was 2 — 1/2 → 2/2
    # with no fix between — when the duplicate run raced it).
    release.set()
    await gate
    assert runs == [store.pr_url]
    assert loop._review_fix_attempts["bd-1"] == 1
    assert "changes-requested" in store.labels
    assert "bd-1" not in loop._review_inflight  # guard released for the real re-review

    # A gate that actually DIED (restart: empty set, label still pending) is still
    # resumed by the reconcile — the edge keeps its job.
    store.state = "in_review"
    store.labels = ["review-pending"]
    release.set()
    await loop._reconcile_prs()
    assert runs == [store.pr_url, store.pr_url]


async def test_review_gate_passes_prior_findings_on_the_next_run(monkeypatch):
    """Round 1 findings ride into round 2's workflow inputs (delta re-review)."""
    _inject_fake_findings(monkeypatch)
    import sys as _sys
    import types as _types

    seen_inputs = []

    async def _runner(name, inputs):
        seen_inputs.append(dict(inputs))
        return {"output": f"```json\n{_BLOCKER}\n```", "steps": {}, "failed": []}

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)

    async def _diff(pr_url, cwd="."):
        return "diff --git a/x b/x"

    monkeypatch.setattr(worktree, "pr_diff", _diff)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_fix_max": 5})
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "prior_findings" not in seen_inputs[0]  # first pass — nothing to carry
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "prior_findings" in seen_inputs[1]
    assert "drops data" in seen_inputs[1]["prior_findings"]


# ── #354: publish the in-loop gate verdict as a PAT-compatible `QA panel` commit status ──


_FULL_HEAD = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha


def _head_returning(sha):
    async def _head(pr_url, *, cwd="."):
        return sha

    return _head


async def test_gate_publishes_a_success_status_on_the_reviewed_head_when_clean(monkeypatch):
    """r1: a CLEAN verdict publishes a `QA panel` COMMIT STATUS with a success state, pinned to
    the EXACT reviewed head (the full sha — the reconcile stamp is cleared for a clean verdict,
    but the status must land on the head the gate examined), with the PR as the target url and no
    findings comment. A `pending` status is published first (while the gate runs), then success."""
    _inject_fake_findings(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(_FULL_HEAD))
    statuses, comments = _record_review_statuses(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert store.review_states[-1][0] == "review-clean"
    assert [s["state"] for s in statuses] == ["pending", "success"]  # live-then-resolved
    final = statuses[-1]
    assert final["head_sha"] == _FULL_HEAD  # full immutable head, not the 12-char stamp
    assert final["repo_slug"] == "o/r" and final["context"] == "QA panel"
    assert final["target_url"] == "https://github.com/o/r/pull/9"
    assert comments == []  # a clean verdict posts no findings comment


async def test_gate_publishes_a_nonsuccess_status_and_findings_comment_when_blocking(monkeypatch):
    """r2: a BLOCKING verdict publishes a NON-success status pinned to the reviewed head AND
    posts a PR comment carrying the surviving blocking findings + a durable PR reference."""
    _inject_fake_findings(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(_FULL_HEAD))
    statuses, comments = _record_review_statuses(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, f"brief…\n```json\n{_BLOCKER}\n```")
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert ("requeue", "bd-1") in store.calls  # the fix round is active
    final = statuses[-1]
    assert final["state"] != "success"  # a blocking result is a non-success status
    assert final["head_sha"] == _FULL_HEAD
    assert len(comments) == 1
    (comment,) = comments
    assert "drops data" in comment["body"]  # the surviving blocking finding, in full
    assert "github.com/o/r/pull/9" in comment["body"]  # a durable reference to it


async def test_gate_publishes_a_failure_status_and_findings_comment_when_the_budget_is_exhausted(monkeypatch):
    """r2: the terminal (budget-exhausted) block publishes a `failure` status AND a findings PR
    comment with the persisting findings — a human owns it, and the PR shows why."""
    _inject_fake_findings(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(_FULL_HEAD))
    statuses, comments = _record_review_statuses(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, _BLOCKER, cfg={"review_fix_max": 1})
    loop._review_fix_attempts["bd-1"] = 1  # budget already spent → blocks this round
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert any(c[0] == "flag_blocked" for c in store.calls)
    final = statuses[-1]
    assert final["state"] == "failure"
    assert final["head_sha"] == _FULL_HEAD
    assert len(comments) == 1
    assert "drops data" in comments[0]["body"] and "needs human review" in comments[0]["body"]


async def test_gate_posts_no_status_when_the_reviewed_head_is_unknown(monkeypatch):
    """r2/r7: when the immutable head can't be read (the autouse `_no_real_pr_head_sha` returns
    ""), the gate posts NO status and NO comment — never a verdict against a head it cannot prove
    it reviewed. The verdict still lands on the bead (the audit record)."""
    _inject_fake_findings(monkeypatch)
    # pr_head_sha stays the autouse blank ("") — the unreadable-head path.
    statuses, comments = _record_review_statuses(monkeypatch)
    store = _GateStore()
    loop = _gate_loop(monkeypatch, f"brief…\n```json\n{_BLOCKER}\n```")
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert store.review_states[-1][0] == "changes-requested"  # the verdict still lands on the bead
    assert statuses == [] and comments == []  # …but nothing against a head the gate can't prove it saw


async def test_gate_status_is_reconciled_not_duplicated_across_the_seam(monkeypatch):
    """r4 at the loop seam: two identical clean verdicts on the same head each call the
    idempotent poster (which supersedes the single per-context status) — the loop never opens a
    second, parallel posting path, so publication is driven only through `post_review_status`."""
    _inject_fake_findings(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(_FULL_HEAD))
    statuses, _comments = _record_review_statuses(monkeypatch)
    for _ in range(2):
        store = _GateStore()
        loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
        await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    successes = [s for s in statuses if s["state"] == "success"]
    assert [s["head_sha"] for s in successes] == [_FULL_HEAD, _FULL_HEAD]
    assert all(s["context"] == "QA panel" for s in successes)


# ── #328: re-arm the review gate after an external push stales the verdict ────────


async def test_review_gate_stamps_the_reviewed_head_on_a_changes_requested_verdict(monkeypatch):
    """The changes-requested verdict records the SHORT head sha it was rendered against
    (#328) — the recorded-SHA identity the reconcile re-arm turns on. A 40-char head is
    abbreviated to 12 (the same beads label cap that forced merged-verified short), and
    the findings history is preserved on the bead alongside the stamp."""
    from project_board.loop import _REVIEWED_HEAD_SHA_LEN

    _inject_fake_findings(monkeypatch)
    full_head = "0123456789abcdef0123456789abcdef01234567"  # a real 40-char sha
    assert len(full_head) == 40
    store = _RoundTripStore()
    store.state = "in_review"
    loop = _gate_loop(monkeypatch, f"brief…\n```json\n{_BLOCKER}\n```")

    async def _head(pr_url, *, cwd="."):
        return full_head

    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    await loop._review_gate(store, "bd-1", store.pr_url, "/repo")
    assert store.review_states[-1][0] == "changes-requested"
    assert f"reviewed-head:{full_head[:_REVIEWED_HEAD_SHA_LEN]}" in store.labels
    assert "drops data" in store.review_states[-1][1]  # findings history not erased


async def test_rearm_review_promotes_pending_when_an_external_push_moved_the_head(monkeypatch):
    """r1: a `changes-requested` verdict stamped for H1, but the live PR head is now H2
    (a direct/human push) → swap to `review-pending` so the established gate re-reviews
    the new head, recording WHY on the bead."""
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True})

    async def _head(pr_url, *, cwd="."):
        return "bbb222"  # the new head after the external push

    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._rearm_review_for_new_head(store, feature, store.pr_url, "/repo") is True
    assert "review-pending" in store.labels and "changes-requested" not in store.labels
    assert store.review_states[-1][0] == "review-pending"
    assert "external push" in store.review_states[-1][1]


async def test_rearm_review_is_a_noop_on_an_unchanged_head(monkeypatch):
    """r2: the live head still matches the reviewed head → the rejection stands, no
    re-arm and no duplicate review (exactly-once per unchanged head)."""
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True})

    async def _head(pr_url, *, cwd="."):
        return "aaa111"

    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._rearm_review_for_new_head(store, feature, store.pr_url, "/repo") is False
    assert store.labels == ["changes-requested", "reviewed-head:aaa111"]
    assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_rearm_review_fails_closed_on_unreadable_absent_or_ambiguous_identity(monkeypatch):
    """r3: an unreadable live head, an absent stamp, an ambiguous (double) stamp, or a
    non-blocking sub-state each leave `changes-requested` untouched — a rejection is
    never re-armed on unproven identity, so the card can't merge on an un-reviewed head."""
    loop = BoardLoop({"review_gate": True})

    async def _head_ok(pr_url, *, cwd="."):
        return "bbb222"

    async def _head_blank(pr_url, *, cwd="."):
        return ""  # a gh hiccup — the live head can't be read

    # 1) unreadable live head (stamp present, head can't be read)
    monkeypatch.setattr(worktree, "pr_head_sha", _head_blank)
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    assert (
        await loop._rearm_review_for_new_head(
            store, {"id": "bd-1", "labels": list(store.labels)}, store.pr_url, "/repo"
        )
        is False
    )
    assert "changes-requested" in store.labels
    assert not any(c[0] == "set_review_substate" for c in store.calls)

    # 2) absent stamp — staleness can't be proven even though the head reads fine
    monkeypatch.setattr(worktree, "pr_head_sha", _head_ok)
    store = _RoundTripStore()
    store.labels = ["changes-requested"]
    assert (
        await loop._rearm_review_for_new_head(
            store, {"id": "bd-1", "labels": list(store.labels)}, store.pr_url, "/repo"
        )
        is False
    )
    assert store.labels == ["changes-requested"]
    assert not any(c[0] == "set_review_substate" for c in store.calls)

    # 3) ambiguous — two conflicting stamps can't identify the reviewed head
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111", "reviewed-head:ccc333"]
    assert (
        await loop._rearm_review_for_new_head(
            store, {"id": "bd-1", "labels": list(store.labels)}, store.pr_url, "/repo"
        )
        is False
    )
    assert not any(c[0] == "set_review_substate" for c in store.calls)

    # 4) not a blocking verdict — nothing to re-arm
    store = _RoundTripStore()
    store.labels = ["review-clean", "reviewed-head:aaa111"]
    assert (
        await loop._rearm_review_for_new_head(
            store, {"id": "bd-1", "labels": list(store.labels)}, store.pr_url, "/repo"
        )
        is False
    )
    assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_reconcile_rearms_and_runs_one_fresh_review_for_the_new_head(monkeypatch):
    """r1 end-to-end (r6 regression): an in_review PR sitting in `changes-requested` for
    H1 gets an external push to H2 → the reconcile re-arms and the resume edge runs
    EXACTLY ONE fresh review for H2, which (clean this time) lands review-clean and clears
    the head stamp. Before #328 the reconcile ran the gate only on `review-pending`, so
    nothing ever re-reviewed the pushed head."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return "clean.\n```json\n[]\n```", None  # the new head passes review

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "bbb222"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert runs == [store.pr_url]  # exactly one review for the new head
    assert "review-clean" in store.labels and "changes-requested" not in store.labels
    assert not any(l.startswith("reviewed-head:") for l in store.labels)  # clean pins no head


async def test_reconcile_does_not_rearm_or_review_an_unchanged_rejected_head(monkeypatch):
    """r2 end-to-end: the live head still matches the reviewed head → the reconcile
    leaves `changes-requested` in place and starts NO review; the card stays rejected."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return "clean.\n```json\n[]\n```", None

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"  # unchanged since the verdict

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert runs == []  # no review re-run for a head already rejected
    assert store.labels == ["changes-requested", "reviewed-head:aaa111"]


async def test_concurrent_reconcile_ticks_start_one_review_for_the_new_head(monkeypatch):
    """r5: two reconcile ticks race on the same `changes-requested` card whose head just
    moved. The in-flight guard lets only ONE gate run the panel for the new head — the
    overlapping tick starts no second review."""
    import asyncio as _asyncio

    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []
    started, release = _asyncio.Event(), _asyncio.Event()

    async def _run(fid, pr_url):
        runs.append(pr_url)
        started.set()
        await release.wait()  # hold the panel open so the second tick overlaps
        return "clean.\n```json\n[]\n```", None

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "bbb222"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    tick1 = _asyncio.create_task(loop._reconcile_prs())
    await started.wait()  # the first tick re-armed and its gate is mid-panel
    assert "bd-1" in loop._review_inflight
    await loop._reconcile_prs()  # the second tick fires while the first holds the panel
    assert runs == [store.pr_url]  # … and starts NO second review for the new head
    release.set()
    await tick1
    assert runs == [store.pr_url]


async def test_rearm_review_does_not_reset_the_review_fix_budget(monkeypatch):
    """r4: an external push re-arms the gate but must NOT hand the new head a fresh
    review-fix budget. A card at its spent budget whose re-armed review still finds a
    blocker goes straight to blocked (human review), never an unbounded re-bounce."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111", "budget:review-fix:1"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False, "review_fix_max": 1})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return f"brief…\n```json\n{_BLOCKER}\n```", None  # the new head STILL has a blocker

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "bbb222"

    async def _diff(url, *, cwd=".", max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert runs == [store.pr_url]
    assert store.state == "blocked"  # spent budget → blocked, not a fresh bounce
    assert not any(c[0] == "requeue" for c in store.calls)


# ── #340: requeue a shutdown-aborted changes-requested fix round ─────────────────


class _StrandedFixStore(_RoundTripStore):
    """_RoundTripStore + the bead comment history the #340 recovery reads back to
    re-derive the fix feedback a shutdown dropped from the in-memory ``_ci_feedback``.
    ``set_review_substate`` records its note the way the real store does (a comment), so
    a findings block written by the gate before the crash is discoverable afterwards."""

    def __init__(self):
        super().__init__()
        self.comments = []  # oldest-first, like store.feature_comments

    def set_review_substate(self, fid, label, note="", head_sha=""):
        if note:
            self.comments.append(note)
        return super().set_review_substate(fid, label, note)

    def feature_comments(self, fid):
        return list(self.comments)


def _findings_comment():
    return f"## {_REVIEW_FINDINGS_TITLE}\n- a.py:3 [blocker] drops data"


async def test_reconcile_requeues_a_shutdown_stranded_changes_requested_fix_round(monkeypatch):
    """r1/r2/r6: a fix drive aborted by shutdown leaves the card in_review +
    changes-requested (the gate's requeue never landed) with the head UNCHANGED and no
    live drive. #328 can't re-arm it (nothing was pushed) and the gate re-runs only on
    review-pending, so it would sit in_review forever while merged-state verify churns.
    The reconcile requeues it to ready (the PR preserved via external_ref) and re-injects
    the recorded findings + live diff so the resumed dispatch leads with them and pushes to
    the same branch."""
    store = _StrandedFixStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111", "budget:review-fix:1"]
    store.comments = [_findings_comment()]  # the pre-crash gate recorded its findings
    loop = BoardLoop({"review_gate": True, "merge_poll": False})

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"  # unchanged since the verdict — NOT a #328 head move

    async def _diff(pr_url, cwd="."):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert ("requeue", "bd-1") in store.calls and store.state == "ready"  # r1: back to ready
    assert "drops data" in loop._ci_feedback["bd-1"]  # r2: leads with the recorded findings
    assert loop._ci_prior_diff["bd-1"] == "diff --git a/a.py b/a.py"  # r2: the diff to fix
    assert store.review_states == []  # no re-arm (#328) and no fresh review outcome invented
    assert "budget:review-fix:1" in store.labels  # r5: the fix budget preserved, not spent
    assert loop._review_fix_attempts.get("bd-1", 0) == 0  # r5: no in-memory spend either


async def test_reconcile_recovers_a_stranded_fix_round_with_no_head_stamp(monkeypatch):
    """r1: the shutdown can also land BEFORE the gate stamped the reviewed head (between
    set-changes-requested and the stamp). #328 fails closed with no stamp to compare, but
    the liveness trigger still requeues the stranded round — the trigger is a dead drive,
    not head identity."""
    store = _StrandedFixStore()
    store.state = "in_review"
    store.labels = ["changes-requested"]  # aborted before the reviewed-head stamp landed
    store.comments = [_findings_comment()]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"  # reads fine, but there's no stamp to prove staleness against

    async def _diff(pr_url, cwd="."):
        return "d"

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert ("requeue", "bd-1") in store.calls and store.state == "ready"
    assert "drops data" in loop._ci_feedback["bd-1"]


async def test_reconcile_never_requeues_a_live_fix_round(monkeypatch):
    """r3: while ANY liveness signal holds — a claimed worktree, a gate mid-transition, or
    a registered drive task — the card is a LIVE fix round (the review gate is still
    mid-transition inside the drive, briefly in_review + changes-requested before its own
    requeue lands), never a stranded one. The recovery must leave it alone: no requeue, no
    duplicate."""
    import asyncio as _asyncio

    from project_board.loop import _register_drive, _unregister_drive

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)

    def _fresh():
        store = _StrandedFixStore()
        store.state = "in_review"
        store.labels = ["changes-requested", "reviewed-head:aaa111"]
        loop = BoardLoop({"review_gate": True, "merge_poll": False})
        monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
        return store, loop

    # (a) a live drive owns the worktree
    store, loop = _fresh()
    loop._inflight_files["bd-1"] = {("default", "a.py")}
    await loop._reconcile_prs()
    assert not any(c[0] == "requeue" for c in store.calls) and store.state == "in_review"

    # (b) the gate is mid-transition (changes-requested set, its requeue not yet landed)
    store, loop = _fresh()
    loop._review_inflight.add("bd-1")
    await loop._reconcile_prs()
    assert not any(c[0] == "requeue" for c in store.calls) and store.state == "in_review"

    # (c) a registered drive task is still running (process-stable across a reload, #211)
    store, loop = _fresh()
    release = _asyncio.Event()

    async def _park():
        await release.wait()

    task = _asyncio.create_task(_park())
    _register_drive("bd-1", task)
    try:
        await loop._reconcile_prs()
        assert not any(c[0] == "requeue" for c in store.calls) and store.state == "in_review"
    finally:
        release.set()
        await task
        _unregister_drive("bd-1", task)


async def test_reconcile_head_move_takes_the_328_path_not_the_340_recovery(monkeypatch):
    """r4: a changes-requested card whose external head MOVED follows #328's stale-head
    re-arm (→ review-pending, one fresh review) — the #340 recovery must NOT also requeue
    it. #328 runs first and flips it off changes-requested, so this recovery never sees a
    head that actually moved."""
    _inject_fake_findings(monkeypatch)
    store = _StrandedFixStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return "clean.\n```json\n[]\n```", None  # the pushed head passes a fresh review

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "bbb222"  # an external push moved the head off the reviewed one

    async def _diff(pr_url, cwd="."):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert runs == [store.pr_url]  # #328 re-armed → exactly one fresh review for the new head
    assert "review-clean" in store.labels  # that review landed; the head was NOT requeued blind
    assert not any(c[0] == "requeue" for c in store.calls)  # NOT the #340 recovery path


async def test_shutdown_recovery_is_idempotent_and_never_stuck_in_review(monkeypatch):
    """r5/r6: repeated reconcile sweeps (a restart loop) requeue the stranded card EXACTLY
    once per in_review stint and never spend a review-fix budget to do it — proving the
    card does not remain in_review indefinitely, and that restoring liveness is free."""
    store = _StrandedFixStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111", "budget:review-fix:1"]
    store.comments = [_findings_comment()]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"

    async def _diff(pr_url, cwd="."):
        return "d"

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    await loop._reconcile_prs()  # a second sweep: the card is `ready` now → not re-scanned
    assert [c for c in store.calls if c[0] == "requeue"] == [("requeue", "bd-1")]  # exactly once
    assert store.state == "ready"  # not stuck in_review
    assert "budget:review-fix:1" in store.labels  # r5: the fix budget preserved across sweeps
    assert loop._review_fix_attempts.get("bd-1", 0) == 0


async def test_reinject_review_feedback_never_clobbers_a_live_in_memory_copy(monkeypatch):
    """r2: when the process did NOT restart (an in-process sweep after a cancelled drive),
    the in-loop ``_ci_feedback`` may still hold the exact findings the gate wrote. The
    recovery must not overwrite that live copy with a comment re-read."""
    store = _StrandedFixStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    store.comments = [_findings_comment()]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    loop._ci_feedback["bd-1"] = "LIVE in-memory feedback"  # survived (no restart)

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert store.state == "ready"  # still recovered
    assert loop._ci_feedback["bd-1"] == "LIVE in-memory feedback"  # the live copy is untouched


# ── #323: reconcile a trusted current-head QA PASS into local review state ────────


_QA_PR = "https://github.com/o/r/pull/9"


def _qa_loop(monkeypatch, *, head, verdict, cfg=None):
    """A review_gate loop whose live PR head is ``head`` and whose head-pinned QA read
    (``worktree.read_review_status`` — the PAT-compatible successor to the #347 check read)
    returns ``verdict`` (a dict or None) for that exact head — the two worktree reads #323's
    inbound reconcile turns on."""
    loop = BoardLoop({"review_gate": True, "merge_poll": False, **(cfg or {})})
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(head))

    async def _read(repo_slug, head_sha, *, cwd="."):
        return verdict if head_sha == head else None

    monkeypatch.setattr(worktree, "read_review_status", _read)
    return loop


async def test_trusted_qa_pass_repairs_changes_requested_to_clean(monkeypatch):
    """r1: a promoted, current-head QA PASS repairs a stale ``changes-requested`` to
    ``review-clean`` and clears the reviewed-head stamp (a clean verdict pins no head). It
    ADOPTS the external verdict — no internal review is invented."""
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = _qa_loop(monkeypatch, head="aaa111", verdict={"state": "success", "head_sha": "aaa111", "passed": True})
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is True
    assert "review-clean" in store.labels and "changes-requested" not in store.labels
    assert not any(l.startswith("reviewed-head:") for l in store.labels)  # clean pins no head
    assert store.review_states[-1][0] == "review-clean"


async def test_trusted_qa_pass_repairs_absent_review_state_to_clean(monkeypatch):
    """r1: an in_review card with NO review substate (pre-upgrade / operator unblock / inert
    gate — merge_posture's "no review-clean verdict") is likewise repaired to review-clean
    on a trusted current-head PASS."""
    store = _RoundTripStore()
    store.labels = []
    loop = _qa_loop(monkeypatch, head="h1", verdict={"state": "success", "head_sha": "h1", "passed": True})
    assert await loop._reconcile_trusted_qa_pass(store, {"id": "bd-1", "labels": []}, _QA_PR, "/repo") is True
    assert "review-clean" in store.labels


async def test_trusted_qa_fail_never_promotes(monkeypatch):
    """r2: a trusted, current-head FAIL is authoritative the OTHER way — it never promotes
    or clears the blocking ``changes-requested`` state."""
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = _qa_loop(monkeypatch, head="aaa111", verdict={"state": "failure", "head_sha": "aaa111", "passed": False})
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert store.labels == ["changes-requested", "reviewed-head:aaa111"]  # untouched
    assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_trusted_qa_none_fails_closed(monkeypatch):
    """r3: an unreadable / absent / malformed / ambiguous / another-head marker — all of
    which ``read_review_status`` collapses to None — leaves the card unpromoted. A PASS for
    another head cannot alter review state (this is also the stale-head case: the live head
    moved, so there is no ``QA panel`` verdict for the current head)."""
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = _qa_loop(monkeypatch, head="bbb222", verdict=None)  # no readable verdict for the live head
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert store.labels == ["changes-requested", "reviewed-head:aaa111"]
    assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_trusted_qa_fails_closed_on_an_unreadable_live_head(monkeypatch):
    """r3: the live PR head can't be read (a gh hiccup) → fail closed; a verdict is never
    adopted without knowing which commit is current, and the check is not even consulted."""
    store = _RoundTripStore()
    store.labels = ["changes-requested"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    monkeypatch.setattr(worktree, "pr_head_sha", _head_returning(""))  # unreadable
    consulted = []

    async def _read(repo_slug, head_sha, *, cwd="."):
        consulted.append(head_sha)
        return {"state": "success", "head_sha": head_sha, "passed": True}

    monkeypatch.setattr(worktree, "read_review_status", _read)
    feature = {"id": "bd-1", "labels": ["changes-requested"]}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert consulted == []  # short-circuited before reading the check


async def test_trusted_qa_pass_does_not_overwrite_an_in_flight_gate(monkeypatch):
    """r4: while the internal review gate is in flight (or a drive/worktree is live), the
    reconcile must NOT adopt an external PASS — the running gate owns the verdict it is about
    to land. Even a valid current-head PASS is skipped, under EACH liveness signal."""
    verdict = {"state": "success", "head_sha": "aaa111", "passed": True}
    # (a) the gate is mid-transition (its own set-changes-requested not yet requeued)
    store = _RoundTripStore()
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = _qa_loop(monkeypatch, head="aaa111", verdict=verdict)
    loop._review_inflight.add("bd-1")
    feature = {"id": "bd-1", "labels": list(store.labels)}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert "review-clean" not in store.labels
    # (b) a live drive owns the worktree
    loop._review_inflight.discard("bd-1")
    loop._inflight_files["bd-1"] = {("default", "a.py")}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert "review-clean" not in store.labels
    assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_trusted_qa_reconcile_is_idempotent_on_a_pending_or_clean_card(monkeypatch):
    """r4/r5: a ``review-pending`` card (the internal gate's LIVE verdict) is never touched,
    and a ``review-clean`` card is a no-op — repeated polls converge, they don't churn or
    overwrite the internal gate's own verdicts."""
    loop = _qa_loop(monkeypatch, head="aaa111", verdict={"state": "success", "head_sha": "aaa111", "passed": True})
    for existing in (["review-pending"], ["review-clean"]):
        store = _RoundTripStore()
        store.labels = list(existing)
        feature = {"id": "bd-1", "labels": list(existing)}
        assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
        assert store.labels == existing  # unchanged
        assert not any(c[0] == "set_review_substate" for c in store.calls)


async def test_trusted_qa_reconcile_is_a_noop_when_the_gate_is_off(monkeypatch):
    """r5: with review_gate off there is no review substate to repair — the inbound reconcile
    is inert (it never manufactures a review-clean the board never gated for)."""
    store = _RoundTripStore()
    store.labels = ["changes-requested"]
    loop = _qa_loop(monkeypatch, head="aaa111", verdict={"state": "success", "head_sha": "aaa111", "passed": True})
    loop.review_gate = False
    feature = {"id": "bd-1", "labels": ["changes-requested"]}
    assert await loop._reconcile_trusted_qa_pass(store, feature, _QA_PR, "/repo") is False
    assert "review-clean" not in store.labels


async def test_reconcile_promotes_a_trusted_current_head_pass_and_never_re_reviews(monkeypatch):
    """r1/r5 end-to-end: a full reconcile of an in_review PR stuck in ``changes-requested``
    for the SAME (unchanged) head — so #328 does not re-arm and #340 would otherwise requeue
    the "stranded" round — instead adopts a trusted, promoted current-head QA PASS: it repairs
    to ``review-clean``, clears the stamp, requeues nothing, and never re-runs the internal
    review gate."""
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return "clean.\n```json\n[]\n```", None

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "aaa111"  # unchanged since the verdict → NOT a #328 head move

    async def _read(repo_slug, head_sha, *, cwd="."):
        assert head_sha == "aaa111"  # the loop asks about the LIVE head
        return {"state": "success", "head_sha": head_sha, "passed": True}

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "read_review_status", _read)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert "review-clean" in store.labels and "changes-requested" not in store.labels
    assert not any(l.startswith("reviewed-head:") for l in store.labels)
    assert runs == []  # adopted the external PASS — the internal gate never re-ran
    assert not any(c[0] == "requeue" for c in store.calls)  # #340 did not requeue it either


async def test_reconcile_head_move_prefers_a_fresh_internal_review_over_the_qa_trust_path(monkeypatch):
    """r5/ordering: a ``changes-requested`` card whose head actually MOVED takes #328's
    fresh-internal-review path, NOT the #323 trust path — even when a promoted PASS exists for
    the new head. #328 flips it to ``review-pending`` first, which #323 then skips, so the
    internal gate (finding a blocker here) is what lands the verdict."""
    _inject_fake_findings(monkeypatch)
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = ["changes-requested", "reviewed-head:aaa111"]
    loop = BoardLoop({"review_gate": True, "merge_poll": False})
    runs = []

    async def _run(fid, pr_url):
        runs.append(pr_url)
        return f"brief…\n```json\n{_BLOCKER}\n```", None  # the internal gate rejects the new head

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _head(pr_url, *, cwd="."):
        return "bbb222"  # an external push moved the head off the reviewed one

    async def _read(repo_slug, head_sha, *, cwd="."):
        return {"state": "success", "head_sha": head_sha, "passed": True}  # a PASS is available…

    async def _diff(url, *, cwd=".", max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "read_review_status", _read)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert runs == [store.pr_url]  # #328 re-armed → exactly one fresh internal review for the new head
    assert "review-clean" not in store.labels  # …but the QA trust path did NOT promote it
    assert "changes-requested" in store.labels  # the internal gate's own verdict stands


def _automerge_reconcile_env(monkeypatch, *, head, verdict, mss="CLEAN"):
    """Stub the worktree reads a full reconcile+auto-merge pass makes for #323: the PR is
    OPEN, its live head is ``head``, its head-pinned QA read returns ``verdict``, and GitHub
    reports ``mss`` (CLEAN → mergeable, not a draft). Records each ``merge_pr`` call."""
    merges = []

    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _pr_head(url, *, cwd="."):
        return head

    async def _read(repo_slug, head_sha, *, cwd="."):
        return verdict if head_sha == head else None

    async def _info(url, *, cwd="."):
        return {"mergeStateStatus": mss, "isDraft": False}

    async def _merge(url, *, method="squash", cwd=".", expected_head=""):
        merges.append((url, method, expected_head))
        return (True, "")

    async def _delete(repo, branch):
        return True

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_head_sha", _pr_head)
    monkeypatch.setattr(worktree, "read_review_status", _read)
    monkeypatch.setattr(worktree, "pr_merge_info", _info)
    monkeypatch.setattr(worktree, "merge_pr", _merge)
    monkeypatch.setattr(worktree, "delete_remote_branch", _delete)
    return merges


async def test_reconcile_promotes_then_auto_merges_a_trusted_current_head_pass(monkeypatch):
    """r1/r3 end-to-end: an in_review PR with an ABSENT review verdict (auto-merge held on
    "no review-clean verdict") gets a trusted, promoted current-head QA PASS → the reconcile
    repairs it to ``review-clean`` and the ORDINARY auto-merge gate then lands the PR."""
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = []  # absent verdict → #328/#340 never fire; auto-merge is held pre-promotion
    loop = BoardLoop({"review_gate": True, "auto_merge": True, "merge_poll": False})
    merges = _automerge_reconcile_env(
        monkeypatch, head="h1", verdict={"state": "success", "head_sha": "h1", "passed": True}
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert "review-clean" in store.labels  # r1: repaired
    # r3: the ordinary merge gate then landed it — and PINNED to the reviewed head so a
    # push racing the merge can't sneak an unreviewed commit past the gate (--match-head-commit).
    assert merges == [(store.pr_url, "squash", "h1")]


async def test_reconcile_fail_closed_leaves_auto_merge_held(monkeypatch):
    """r3 end-to-end: with no provable current-head verdict (``read_review_status`` → None),
    the absent review substate is NOT repaired — auto-merge stays held on "no review-clean
    verdict" and the PR is never merged."""
    store = _RoundTripStore()
    store.state = "in_review"
    store.labels = []
    loop = BoardLoop({"review_gate": True, "auto_merge": True, "merge_poll": False})
    merges = _automerge_reconcile_env(monkeypatch, head="h1", verdict=None)  # no promotion evidence
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await loop._reconcile_prs()
    assert "review-clean" not in store.labels  # unpromoted
    assert merges == []  # auto-merge held — the fail-closed card never merged


# ── surfaced unrunnable-gate causes (#180) ───────────────────────────────────────


def _inject_runtime_runner(monkeypatch, runner):
    """Stand in for the HOST's runtime.state.STATE (the workflows plugin's published
    runner) — ``runner=None`` models the plugin being disabled."""
    import sys as _sys
    import types as _types

    rt = _types.ModuleType("runtime")
    rt_state = _types.ModuleType("runtime.state")
    rt_state.STATE = _types.SimpleNamespace(workflow_run=runner)
    rt.state = rt_state
    monkeypatch.setitem(_sys.modules, "runtime", rt)
    monkeypatch.setitem(_sys.modules, "runtime.state", rt_state)


def _inject_fake_adapters(monkeypatch, dispatch):
    """Stand in for the HOST's plugins.delegates.adapters (absent in this suite) so
    the reviewer-fallback path can be exercised."""
    import sys as _sys
    import types as _types

    adapters = _types.ModuleType("plugins.delegates.adapters")
    adapters.ADAPTERS = {"a2a": _types.SimpleNamespace(dispatch=dispatch)}
    plugins = _types.ModuleType("plugins")
    delegates = _types.ModuleType("plugins.delegates")
    plugins.delegates = delegates
    delegates.adapters = adapters
    monkeypatch.setitem(_sys.modules, "plugins", plugins)
    monkeypatch.setitem(_sys.modules, "plugins.delegates", delegates)
    monkeypatch.setitem(_sys.modules, "plugins.delegates.adapters", adapters)


async def test_run_review_workflow_reason_no_runner_no_reviewer(monkeypatch):
    """#180 (a) — the live incident: workflows plugin disabled (no STATE.workflow_run)
    and no reviewer resolves. The reason names BOTH missing dependencies."""
    _inject_runtime_runner(monkeypatch, None)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert why == "no workflow runner available and no reviewer configured"


async def test_run_review_workflow_reason_names_failed_steps(monkeypatch):
    """#180 (b): a partial panel fails closed with the failed step NAMES in the reason."""

    async def _runner(name, inputs):
        return {"output": "clean.\n```json\n[]\n```", "steps": {}, "failed": ["find_crossfile", "judge"]}

    _inject_runtime_runner(monkeypatch, _runner)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "failed step(s)" in why and "find_crossfile" in why and "judge" in why


async def test_run_review_workflow_reason_includes_workflow_exception(monkeypatch):
    """#180 (c): a dying workflow with no reviewer fallback — the reason carries the
    exception message and says the fallback was missing."""

    async def _runner(name, inputs):
        raise RuntimeError("panel exploded")

    _inject_runtime_runner(monkeypatch, _runner)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "call failed" in why and "panel exploded" in why
    assert "no reviewer fallback configured" in why


async def test_run_review_workflow_reason_includes_reviewer_exception(monkeypatch):
    """#180 (c), reviewer flavor: no runner, the a2a dispatch raises — the reason
    names the reviewer and carries the exception message."""
    _inject_runtime_runner(monkeypatch, None)

    async def _dispatch(reviewer, msg):
        raise RuntimeError("a2a unreachable")

    _inject_fake_adapters(monkeypatch, _dispatch)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: object())
    output, why = await loop._run_review_workflow("bd-1", "https://github.com/o/r/pull/9")
    assert output is None
    assert "reviewer" in why and "call failed" in why and "a2a unreachable" in why


async def test_review_gate_retry_and_block_name_the_actual_cause(monkeypatch, caplog):
    """End-to-end through the REAL _run_review_workflow: the retry warning AND the
    eventual block reason say exactly what is missing — the operator no longer
    correlates plugin toggles with generic gate warnings in the server log (#180)."""
    _inject_fake_findings(monkeypatch)
    _inject_runtime_runner(monkeypatch, None)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_run_max": 2})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    assert "no workflow runner available and no reviewer configured" in caplog.text  # the retry warning
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")  # exhausts run_max
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "no workflow runner available and no reviewer configured" in blocked[0][2]
    assert "runner missing" not in blocked[0][2]  # the generic three-hypothesis text is gone


async def test_review_gate_block_reason_names_failed_steps(monkeypatch):
    """A gate exhausted by partial panels blocks with the failed step named (#180 (b))."""
    _inject_fake_findings(monkeypatch)

    async def _runner(name, inputs):
        return {"output": "clean", "steps": {}, "failed": ["find_crossfile"]}

    _inject_runtime_runner(monkeypatch, _runner)
    store = _GateStore()
    loop = BoardLoop({"review_gate": True, "review_run_max": 1})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
    await loop._review_gate(store, "bd-1", "https://github.com/o/r/pull/9", "/repo")
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "find_crossfile" in blocked[0][2]


async def test_boot_warns_when_review_gate_has_no_runner(monkeypatch, caplog):
    """#180: review_gate=True with neither a workflow runner nor a reviewer → ONE
    loud actionable WARNING at loop start naming the missing dependencies."""
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())
    _inject_runtime_runner(monkeypatch, None)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        loop = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
        loop.start()
    if loop._task:
        loop._task.cancel()
    warn = [r.message for r in caplog.records if "review_gate is on but no review runner available" in r.message]
    assert warn, "expected a boot WARNING when the review gate cannot run"
    assert "workflows plugin disabled" in warn[0] and "fail closed" in warn[0]


async def test_boot_warning_silent_when_a_runner_or_reviewer_resolves(monkeypatch, caplog):
    import logging

    monkeypatch.setattr("project_board.loop.BoardLoop._run", lambda self: _noop_coro())
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        # a live workflow runner → no warning
        _inject_runtime_runner(monkeypatch, lambda name, inputs: None)
        loop = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop, "_resolve_delegate", lambda n, t: None)
        loop.start()
        if loop._task:
            loop._task.cancel()
        # no runner but a reviewer resolves → no warning either
        _inject_runtime_runner(monkeypatch, None)
        loop2 = BoardLoop({"review_gate": True, "loop_enabled": True})
        monkeypatch.setattr(loop2, "_resolve_delegate", lambda n, t: object())
        loop2.start()
        if loop2._task:
            loop2._task.cancel()
        # review_gate off → the preflight is skipped entirely
        loop3 = BoardLoop({"loop_enabled": True})
        loop3.start()
        if loop3._task:
            loop3._task.cancel()
    assert "review_gate is on but no review runner available" not in caplog.text


# ── per-feature project resolution (#90 slice 2) ─────────────────────────────────

# A board serving several repos: the flat top-level keys are the INSTANCE defaults;
# the `projects:` map carries each repo's own execution surface. The tests below prove
# a feature resolves repo/gate/coders/solve-knobs from ITS project, not the instance.
_MULTI_CFG = {
    "repo": "/instance/repo",
    "base_branch": "main",
    "local_gate_cmd": "instance-gate",
    "coders": {"fast": "instance-coder"},
    "default_project": "board-plugin",
    "projects": {
        "board-plugin": {
            "repo": "/repos/board-plugin",
            "base_branch": "develop",
            "local_gate_cmd": "ruff check .",
            "coders": {"fast": "bp-fast", "smart": "bp-smart"},
            "gate_files": ["CHANGELOG.md"],
            "repo_conventions": "CI runs ruff",
            "coder_solve_budget": 9,
        },
        "other": {"repo": "/repos/other", "local_gate_cmd": "make test"},
    },
}


def test_feature_resolves_repo_gate_and_coders_from_its_project():
    # r1/r7: a labeled feature resolves its repo, base, gate and coders from the project
    # config — overriding the instance default the store stamped on `feature["repo"]`.
    lp = BoardLoop(_MULTI_CFG)
    feat = {"id": "bd-9", "project": "board-plugin", "repo": "/instance/repo", "base_branch": "main"}
    assert lp._repo_for(feat) == "/repos/board-plugin"  # project repo, NOT the stamped instance one
    assert lp._base_branch_for(feat) == "develop"
    assert lp._local_gate_cmd_for(feat) == "ruff check ."
    assert lp._coders_for(feat) == {"fast": "bp-fast", "smart": "bp-smart"}
    # …and the instance defaults are genuinely DIFFERENT — proving we didn't read them.
    assert lp.local_gate_cmd == "instance-gate"
    assert lp.coders == {"fast": "instance-coder"}
    assert lp._store_kw["repo"] == "/instance/repo"


def test_unlabeled_feature_resolves_to_the_default_project():
    # r8 back-compat: a feature with no project label falls back to the default project.
    lp = BoardLoop(_MULTI_CFG)  # default_project = board-plugin
    feat = {"id": "bd-x"}  # no `project`
    assert lp._project_cfg(feat)["name"] == "board-plugin"
    assert lp._repo_for(feat) == "/repos/board-plugin"
    assert lp._coders_for(feat) == {"fast": "bp-fast", "smart": "bp-smart"}


def test_back_compat_no_projects_map_is_a_single_implicit_project():
    # r8: absent a `projects:` map, one implicit "default" project is synthesized from
    # the flat keys — every feature resolves to the instance repo/gate/coders as before.
    lp = BoardLoop({"repo": "/solo", "local_gate_cmd": "make gate", "coders": {"fast": "solo"}})
    assert lp._default_project == "default"
    feat = {"id": "bd-1"}  # unlabeled
    assert lp._repo_for(feat) == "/solo"
    assert lp._local_gate_cmd_for(feat) == "make gate"
    assert lp._coders_for(feat) == {"fast": "solo"}


def test_build_prompt_uses_the_features_project_gate_files_and_conventions():
    # r4: the dispatch prompt carries the FEATURE's project gate_files + repo_conventions.
    lp = BoardLoop(_MULTI_CFG)
    prompt = lp._build_prompt({**FEATURE, "project": "board-plugin"})
    assert "CHANGELOG.md" in prompt  # board-plugin's gate_files
    assert "CI runs ruff" in prompt  # board-plugin's repo_conventions
    # a project that declares NEITHER (and no instance-level default) gets no blocks.
    p2 = lp._build_prompt({**FEATURE, "project": "other"})
    assert "Repo standing gate files" not in p2
    assert "## Repo conventions" not in p2


def test_coder_solve_settings_resolve_per_project():
    # r5: the solve() search knobs + verifier command resolve from the feature's project.
    lp = BoardLoop(_MULTI_CFG)
    s = lp._coder_solve_settings({"id": "bd-9", "project": "board-plugin"})
    assert s["budget"] == 9  # board-plugin's coder_solve_budget
    assert s["test_cmd"] == "ruff check ."  # its gate doubles as the solve verifier
    # a project with no coder_solve_budget override falls back to the instance default.
    other = lp._coder_solve_settings({"id": "bd-o", "project": "other"})
    assert other["budget"] == lp.coder_solve_budget


async def test_drive_builds_in_the_features_project_repo(monkeypatch):
    # r1 end-to-end: a project added by live reload is used by the very next drive to
    # create its worktree + PR — no construction-time routing leaks through.
    captured = {}
    store = FakeLoopStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        captured["repo"], captured["base"] = repo, base
        return (f"/wt/feat-{fid}", f"feat/{fid}")

    async def _dispatch(*a, **k):
        return "the coder reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        captured["pr_base"] = base
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _aret(None))
    loop = BoardLoop(
        {
            "coder": "proto",
            "coder_solve": False,  # force the single-shot path (no solve ladder)
            "repo": "/instance/repo",
            "projects": {"existing": {"repo": "/repos/existing"}},
        }
    )
    assert loop.reload(
        {
            "projects": {
                "existing": {"repo": "/repos/existing"},
                "board-plugin": {"repo": "/repos/board-plugin", "base_branch": "develop"},
            }
        }
    )["projects"] == (("existing",), ("existing", "board-plugin"))
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive({**FEATURE, "project": "board-plugin"})
    assert captured["repo"] == "/repos/board-plugin"  # NOT /instance/repo
    assert captured["base"] == "develop" and captured["pr_base"] == "develop"
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls


# ── gate preflight (fail-closed: never start work a broken gate can't accept) ─────


class _PreflightStore(FakeLoopStore):
    """FakeLoopStore + the ready list, ready_queue, claim, and clear_blocked the
    per-project preflight hold/release + _spawn_ready use. ``ready`` is a list of fids
    (default project) or ``(fid, project)`` tuples so a test can spread ready work
    across projects (#90)."""

    def __init__(self, ready):
        super().__init__()
        self._ready = []
        for r in ready:
            fid, project = r if isinstance(r, tuple) else (r, "")
            f = {"id": fid, "board_state": "ready", "blocked": False, "files_to_modify": []}
            if project:
                f["project"] = project
            self._ready.append(f)

    def list_features(self, state=None):
        return [dict(f) for f in self._ready] if state == "ready" else []

    def ready_queue(self, relaxed=False):
        return [dict(f) for f in self._ready]

    def claim(self, fid, assignee=""):
        for f in self._ready:
            if f["id"] == fid:
                self.calls.append(("claim", fid))
                return dict(f)
        return None

    def clear_blocked(self, fid):
        self.calls.append(("clear_blocked", fid))
        return {"id": fid}


class _FakeProc:
    def __init__(self, rc, out=b""):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def _clean_base_checkout(monkeypatch):
    """Every preflight test below runs against a CLEAN checkout unless it says otherwise.

    Without this the suite shells real git in the repo it is running from, so the same
    tests pass on a clean tree and fail on a dirty one — the developer's uncommitted work
    would decide whether fail-closed is asserted. The dirty path has its own explicit
    tests; this pins the default."""
    monkeypatch.setattr(
        "project_board.worktree.base_checkout_dirt",
        _async_return(""),
    )


def _async_return(value):
    async def _f(*_a, **_k):
        return value

    return _f


def test_preflight_config_defaults():
    assert BoardLoop({}).preflight is True  # on by default
    assert BoardLoop({})._preflight_state == {}  # per-project dict, empty = nothing checked
    assert BoardLoop({"preflight": False}).preflight is False


async def test_preflight_noop_when_no_gate(monkeypatch):
    # No local_gate_cmd → nothing to smoke → the project is treated as runnable.
    lp = BoardLoop({"preflight": True})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True


async def test_preflight_passes_when_gate_exits_zero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True


async def test_preflight_fails_closed_on_nonzero(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state["default"], str)
    assert "tsc: not found" in lp._preflight_state["default"]


async def test_preflight_fails_closed_when_gate_cannot_launch(monkeypatch):
    # The exact case this exists for: the gate binary isn't installed.
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        raise FileNotFoundError("pnpm")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    assert isinstance(lp._preflight_state["default"], str)
    assert "could not run" in lp._preflight_state["default"]


async def test_preflight_identical_refailure_one_error_one_line_warning(monkeypatch, caplog):
    """#263 (r1): the ~60s re-check of a held gate must not repeat the multi-KB tail
    at ERROR while the failure is unchanged — the full tail is logged ONCE, and each
    identical re-check collapses to a one-line "still held (Ns)" WARNING."""
    import logging

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await lp._maybe_preflight()
        lp._last_preflight["default"] = -10_000.0  # bypass the re-check throttle
        await lp._maybe_preflight()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "PREFLIGHT" in r.message]
    assert len(errors) == 1 and "tsc: not found" in errors[0].message
    stills = [r for r in caplog.records if r.levelno == logging.WARNING and "still held" in r.message]
    assert len(stills) == 1
    assert len(stills[0].message.splitlines()) == 1  # one line — no repeated tail
    assert "tsc: not found" not in stills[0].message
    assert isinstance(lp._preflight_state["default"], str)  # still fail-closed


async def test_preflight_different_failure_logs_full_error_again(monkeypatch, caplog):
    """#263: a DIFFERENT failure is new diagnostic signal — the full tail is logged
    at ERROR again, not swallowed by the still-held rate limit."""
    import logging

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    outs = [b"sh: 1: tsc: not found", b"sh: 1: eslint: not found"]

    async def _shell(*a, **k):
        return _FakeProc(1, outs.pop(0))

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await lp._maybe_preflight()
        lp._last_preflight["default"] = -10_000.0
        await lp._maybe_preflight()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "PREFLIGHT" in r.message]
    assert len(errors) == 2
    assert "tsc: not found" in errors[0].message and "eslint: not found" in errors[1].message
    assert not [r for r in caplog.records if "still held" in r.message]


async def test_preflight_launch_failure_repeat_also_rate_limited(monkeypatch, caplog):
    """#263: the cannot-launch path re-checks on the same cadence — an unchanged
    launch failure gets the same one-ERROR-then-WARNING treatment."""
    import logging

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))

    async def _shell(*a, **k):
        raise FileNotFoundError("pnpm")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await lp._maybe_preflight()
        lp._last_preflight["default"] = -10_000.0
        await lp._maybe_preflight()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "PREFLIGHT" in r.message]
    assert len(errors) == 1 and "could not run" in errors[0].message
    stills = [r for r in caplog.records if r.levelno == logging.WARNING and "still held" in r.message]
    assert len(stills) == 1


async def test_preflight_refailure_after_recovery_logs_full_error(monkeypatch, caplog):
    """#263: recovery resets the rate limit — a project that recovers and then fails
    again (even with the SAME reason) gets the full ERROR, not a "still held" line."""
    import logging

    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    procs = [_FakeProc(1, b"tsc: not found"), _FakeProc(0), _FakeProc(1, b"tsc: not found")]

    async def _shell(*a, **k):
        return procs.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await lp._maybe_preflight()  # fail → full ERROR
        lp._last_preflight["default"] = -10_000.0
        await lp._maybe_preflight()  # recover
        lp._preflight_state["default"] = None  # eligible for a fresh check (passed is sticky)
        await lp._maybe_preflight()  # same failure again → full ERROR again
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "PREFLIGHT" in r.message]
    assert len(errors) == 2
    assert not [r for r in caplog.records if "still held" in r.message]


async def test_spawn_ready_holds_all_work_when_preflight_failed(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1: tsc: not found"}  # simulate a failed preflight
    store = _PreflightStore(ready=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    spawned = await lp._spawn_ready()

    assert spawned is False  # dispatched nothing — every ready card is in the held project
    blocked = {c[1]: c[2] for c in store.calls if c[0] == "flag_blocked"}
    assert set(blocked) == {"bd-1", "bd-2"}  # both held, visibly
    assert all("preflight" in reason.lower() for reason in blocked.values())


async def test_preflight_recovery_releases_holds(monkeypatch):
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1"}  # previously failed
    lp._preflight_held = {"default": {"bd-1", "bd-2"}}  # and it held these
    # A recheck of a KNOWN-failed preflight is throttled by (monotonic() - _last_preflight);
    # put the last check far enough back that the recheck fires regardless of the absolute
    # monotonic value (a fresh CI container's clock can be < the 60s throttle window).
    lp._last_preflight = {"default": -10_000.0}
    # No ready work left (the held cards dropped out of `ready`) — recovery must STILL
    # fire for a project still marked failed, else the holds never release.
    store = _PreflightStore(ready=[])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _shell(*a, **k):
        return _FakeProc(0)  # gate now passes

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    assert lp._preflight_state["default"] is True
    assert lp._preflight_held == {}  # the project's hold set was cleared and dropped
    assert {c[1] for c in store.calls if c[0] == "clear_blocked"} == {"bd-1", "bd-2"}


async def test_spawn_ready_holds_only_the_failed_project(monkeypatch):
    """Per-project isolation (#90, r2/r6): preflight failed for project A, but a ready
    feature in project B is still claimed + driven this tick."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build", "max_concurrent": 2})
    lp._preflight_state = {"proj-a": "gate exited 1: tsc missing", "proj-b": True}
    # bd-a builds in the broken project A; bd-b in the healthy project B.
    store = _PreflightStore(ready=[("bd-a", "proj-a"), ("bd-b", "proj-b")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    finish = await _hold_drives(lp, monkeypatch)
    try:
        spawned = await lp._spawn_ready()
        assert spawned is True  # project B's feature was dispatched despite A's broken gate
        assert len(lp._drives) == 1  # ONLY B — A is held
        claims = [c[1] for c in store.calls if c[0] == "claim"]
        assert claims == ["bd-b"]  # A never claimed; B claimed
        blocked = {c[1] for c in store.calls if c[0] == "flag_blocked"}
        assert blocked == {"bd-a"}  # only the failed project's card is held
    finally:
        await finish()


async def test_preflight_isolation_a_fails_b_passes(monkeypatch):
    """_maybe_preflight smokes each project's gate independently (#90): project A's gate
    fails-closed while project B's passes — the two states never cross-contaminate."""
    lp = BoardLoop(
        {
            "preflight": True,
            "projects": {
                "proj-a": {"repo": "/repos/a", "local_gate_cmd": "gate-a"},
                "proj-b": {"repo": "/repos/b", "local_gate_cmd": "gate-b"},
            },
        }
    )
    store = _PreflightStore(ready=[("bd-a", "proj-a"), ("bd-b", "proj-b")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _shell(cmd, **k):
        return _FakeProc(1, b"gate-a: boom") if cmd == "gate-a" else _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()

    assert isinstance(lp._preflight_state["proj-a"], str)  # A held closed
    assert "boom" in lp._preflight_state["proj-a"]
    assert lp._preflight_state["proj-b"] is True  # B runnable — unaffected by A


# ── boot recovery of orphaned preflight holds (#186) ────────────────────────────


_PREFLIGHT_HOLD_COMMENT = "blocked: gate preflight failed — the coder environment can't run the gate: tsc: not found"


class _BootPreflightStore(_PreflightStore):
    """_PreflightStore + the boot-recovery reads (#186): `_recover`'s in_progress scan
    (inherited — [] for any non-ready state) and the blocked-card scan WITH comment
    history (``raw_features_with_comments``)."""

    def __init__(self, ready=(), blocked=()):
        super().__init__(list(ready))
        self._blocked = list(blocked)  # raw `br` dicts: {"id", "comments"}

    def raw_features_with_comments(self, states=("done", "blocked")):
        return [dict(f) for f in self._blocked] if "blocked" in states else []


async def test_recover_unblocks_orphaned_preflight_holds(monkeypatch):
    """#186 (r1): `_preflight_held` dies with the process, so a restart orphans the
    previous loop's holds — the cards are blocked (not ready), invisible to both
    `_ready_projects` and a fresh `_preflight_state`, and would stay held forever.
    Boot recovery clear_blocked's every card whose CURRENT block reason carries the
    preflight marker — and ONLY those."""
    store = _BootPreflightStore(
        blocked=[
            {"id": "bd-1", "comments": [_PREFLIGHT_HOLD_COMMENT]},
            # `br` comments can be dict-shaped — the text field must parse too
            {"id": "bd-2", "comments": [{"text": _PREFLIGHT_HOLD_COMMENT}]},
            # blocked for an unrelated reason (operator/CI block) → never touched
            {"id": "bd-3", "comments": ["blocked: CI failed: pytest exploded"]},
            # once preflight-held but later re-blocked for another reason: the LAST
            # `blocked:` comment is the current reason → not a preflight orphan
            {"id": "bd-4", "comments": [_PREFLIGHT_HOLD_COMMENT, "blocked: coder timeout"]},
            # the reverse — an old unrelated block, currently preflight-held → released
            {"id": "bd-5", "comments": ["blocked: coder timeout", _PREFLIGHT_HOLD_COMMENT]},
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await BoardLoop({})._recover()
    cleared = {c[1] for c in store.calls if c[0] == "clear_blocked"}
    assert cleared == {"bd-1", "bd-2", "bd-5"}


async def test_recover_cleared_card_reheld_when_gate_still_broken(monkeypatch):
    """#186 (r2): boot releases the orphaned hold, and the first tick re-checks — a
    STILL-broken gate re-holds the card one tick later (fail-closed survives the
    restart; nothing is ever dispatched against the broken gate)."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    store = _BootPreflightStore(
        ready=["bd-1"],  # clear_blocked put the card back in ready
        blocked=[{"id": "bd-1", "comments": [_PREFLIGHT_HOLD_COMMENT]}],
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await lp._recover()
    assert ("clear_blocked", "bd-1") in store.calls  # boot released the orphan

    async def _shell(*a, **k):
        return _FakeProc(1, b"apps/x build: sh: 1: tsc: not found")  # STILL broken

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()  # the first tick smokes the gate again…
    assert isinstance(lp._preflight_state["default"], str)
    spawned = await lp._spawn_ready()  # …and the claim scan re-holds instead of dispatching
    assert spawned is False
    blocked = {c[1]: c[2] for c in store.calls if c[0] == "flag_blocked"}
    assert set(blocked) == {"bd-1"}  # re-held, visibly, one tick after boot
    assert blocked["bd-1"].startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX)


def test_hold_reason_carries_the_recovery_marker(monkeypatch):
    """#186 (r3): the hold path stamps the SAME module constant the boot scan matches
    on — a drifted prefix would orphan every future hold across restarts."""
    lp = BoardLoop({"local_gate_cmd": "pnpm -r build"})
    lp._preflight_state = {"default": "gate exited 1: tsc: not found"}
    store = _PreflightStore(ready=["bd-1"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    lp._hold_ready_for_preflight()
    reasons = [c[2] for c in store.calls if c[0] == "flag_blocked"]
    assert reasons and all(r.startswith(loop_mod.PREFLIGHT_BLOCK_PREFIX) for r in reasons)


# ── auto gate resolution (_resolve_gate_cmd) ────────────────────────────────────


def _write(p, name, body):
    f = p / name
    f.write_text(body)
    return f


def test_resolve_gate_explicit_command_passes_through(tmp_path):
    # An explicit gate is never rewritten, even if the repo declares a ci script.
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm test"}}')
    assert _resolve_gate_cmd("pytest -q", str(tmp_path)) == "pytest -q"


def test_resolve_gate_blank_stays_gateless(tmp_path):
    # Blank still means "no gate" — auto must be opt-in, never inferred from blank.
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm test"}}')
    assert _resolve_gate_cmd("", str(tmp_path)) == ""
    assert _resolve_gate_cmd("  ", str(tmp_path)) == ""


def test_resolve_gate_auto_prefers_declared_ci_script(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"ci": "pnpm typecheck && pnpm -r test", "test": "x"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run ci"


def test_resolve_gate_auto_falls_to_check_then_verify(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"check": "x", "verify": "y"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run check"
    _write(tmp_path, "package.json", '{"scripts": {"verify": "y"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run verify"


def test_resolve_gate_auto_convention_fallback_when_no_entrypoint(tmp_path):
    # A node repo with no ci/check/verify → the --if-present standard-checks superset.
    _write(tmp_path, "package.json", '{"scripts": {"test": "vitest run"}}')
    got = _resolve_gate_cmd("auto", str(tmp_path))
    assert got == (
        f"{loop_install()} && pnpm -r --if-present typecheck && pnpm -r --if-present build && pnpm -r --if-present test"
    )


def test_resolve_gate_auto_prefers_gate_over_ci_script(tmp_path):
    # A complex-CI repo declares a dedicated fast `gate` alongside a heavy `ci`;
    # the coder must gate on `gate`, not the whole `ci`.
    _write(tmp_path, "package.json", '{"scripts": {"gate": "x", "ci": "everything"}}')
    assert _resolve_gate_cmd("auto", str(tmp_path)) == f"{loop_install()} && pnpm run gate"


def test_resolve_gate_auto_reads_makefile_ci_target(tmp_path):
    _write(tmp_path, "Makefile", "build:\n\tgo build ./...\nci:\n\tgo test ./...\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "make ci"


def test_resolve_gate_auto_makefile_gate_beats_ci(tmp_path):
    # Python/Go/Rust path: `make gate` (fast slice) wins over a heavy `make ci`.
    _write(tmp_path, "Makefile", "ci:\n\tpytest -q -m 'integration'\ngate:\n\truff check . && pytest -q\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "make gate"


def test_resolve_gate_auto_justfile_check_target(tmp_path):
    _write(tmp_path, "justfile", "default:\n\techo hi\ncheck:\n\tcargo test\n")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == "just check"


def test_resolve_gate_auto_unrecognized_repo_is_gateless(tmp_path):
    # Nothing recognized → "" (fail-open, gateless) rather than a wrong guess.
    _write(tmp_path, "README.md", "# a repo with no known toolchain")
    assert _resolve_gate_cmd("auto", str(tmp_path)) == ""


def test_resolve_gate_auto_malformed_package_json_falls_through(tmp_path):
    # Broken package.json must not crash construction — treat as no scripts → fallback.
    _write(tmp_path, "package.json", "{ not valid json ")
    got = _resolve_gate_cmd("auto", str(tmp_path))
    assert got.startswith(loop_install()) and "--if-present" in got


def loop_install():
    from project_board.loop import _PNPM_INSTALL

    return _PNPM_INSTALL


# ── source-issue → PR "Fixes #N" line (#93) ─────────────────────────────────────


def test_source_issue_prefers_field_then_scans_description():
    """An explicit `source_issue` field wins (full URL / owner-repo#n / bare number);
    otherwise the FIRST GitHub issue URL in the feature text is used."""
    assert _source_issue({"source_issue": "https://github.com/acme/widgets/issues/8"}) == ("acme/widgets", 8)
    assert _source_issue({"source_issue": "other/repo#12"}) == ("other/repo", 12)
    assert _source_issue({"source_issue": "#4"}) == ("", 4)
    assert _source_issue({"source_issue": "4"}) == ("", 4)
    # No field → the FIRST issue URL in the text (spec here) wins over the later one.
    feat = {"spec": "per https://github.com/acme/widgets/issues/1 then https://github.com/acme/widgets/issues/2"}
    assert _source_issue(feat) == ("acme/widgets", 1)
    assert _source_issue({"spec": "no issue linked here"}) is None


def test_inject_same_repo_appends_fixes_and_is_idempotent():
    body = "## Summary\n\n- did the thing"
    out = _inject_source_issue_line(body, "acme/widgets", 7, "acme/widgets")
    assert out == body + "\n\nFixes #7"
    # A same-repo closing reference already present suppresses (no duplicate).
    assert _inject_source_issue_line(out, "acme/widgets", 7, "acme/widgets") == out
    assert _inject_source_issue_line("Closes #7 already", "acme/widgets", 7, "acme/widgets") == "Closes #7 already"


def test_inject_cross_repo_appends_refs_link():
    body = "## Summary\n\n- did it"
    out = _inject_source_issue_line(body, "other/repo", 9, "acme/widgets")
    assert out == body + "\n\nRefs https://github.com/other/repo/issues/9"


def test_inject_url_dedup_is_word_bounded():
    """FIX-NOW item 1: the URL suppression must be \\b-bounded — issues/12 is a substring
    of issues/123, so a body that only references #123 must NOT suppress a #12 line."""
    body = "See https://github.com/acme/widgets/issues/123 for context."
    out = _inject_source_issue_line(body, "acme/widgets", 12, "acme/widgets")
    assert out.endswith("Fixes #12")  # the 123 URL is not a match for #12
    # The EXACT-issue URL already present does suppress (a real duplicate reference).
    exact = "Already linked https://github.com/acme/widgets/issues/12 here."
    assert _inject_source_issue_line(exact, "acme/widgets", 12, "acme/widgets") == exact


def test_inject_cross_repo_shorthand_does_not_suppress():
    """FIX-NOW item 2: a bare `Fixes #42` can't name another repo's issue, so cross-repo
    it must NOT suppress the Refs line — only a full-URL match may."""
    body = "Unrelated: Fixes #42 in this repo."
    out = _inject_source_issue_line(body, "other/repo", 42, "acme/widgets")
    assert out.endswith("Refs https://github.com/other/repo/issues/42")
    # The cross-repo URL already present DOES suppress (no duplicate link).
    linked = "Refs https://github.com/other/repo/issues/42 already."
    assert _inject_source_issue_line(linked, "other/repo", 42, "acme/widgets") == linked


def test_inject_bare_number_is_same_repo_even_when_target_unknown():
    # A bare number (slug "") is same-repo by construction — Fixes #n even if the target
    # repo couldn't be resolved (repo_slug failed open); never a Refs to an empty slug.
    assert _inject_source_issue_line("body", "", 5, "") == "body\n\nFixes #5"


def test_inject_unknown_target_degrades_to_refs_for_a_slugged_issue():
    # repo_slug failed open (target ""): a slugged issue can't be confirmed same-repo, so
    # the safe degrade is a Refs link, never a possibly-wrong Fixes that auto-closes here.
    out = _inject_source_issue_line("body", "acme/widgets", 3, "")
    assert out == "body\n\nRefs https://github.com/acme/widgets/issues/3"


async def test_repo_slug_fails_open_on_gh_error(monkeypatch):
    """FIX-NOW item 3: worktree.repo_slug must fail OPEN — a raising _gh (WorktreeError /
    timeout) yields "" rather than propagating; a non-zero rc also yields ""."""

    async def _boom(*args, cwd, timeout=60):
        raise worktree.WorktreeError("gh repo view timed out after 60s")

    monkeypatch.setattr(worktree, "_gh", _boom)
    assert await worktree.repo_slug(cwd="/repo") == ""

    async def _rc1(*args, cwd, timeout=60):
        return (1, "", "not a gh repo")

    monkeypatch.setattr(worktree, "_gh", _rc1)
    assert await worktree.repo_slug(cwd="/repo") == ""

    async def _ok(*args, cwd, timeout=60):
        return (0, "acme/widgets\n", "")

    monkeypatch.setattr(worktree, "_gh", _ok)
    assert await worktree.repo_slug(cwd="/repo") == "acme/widgets"


async def test_drive_injects_fixes_line_into_the_pr_body(monkeypatch):
    """End-to-end: a feature carrying a same-repo source issue gets a `Fixes #n` line
    appended to the body the loop hands to open_pr (the coder stays out of the loop)."""
    bodies = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        bodies.append(body)
        return "https://example/pr/1"

    async def _slug(*, cwd):
        return "acme/widgets"

    monkeypatch.setattr(worktree, "repo_slug", _slug)
    feature = {**FEATURE, "source_issue": "https://github.com/acme/widgets/issues/7"}
    _loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, feature=feature)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert bodies and bodies[0].endswith("Fixes #7")


# ── #113: the requirement ledger — parse dispositions, carry in prompt, gate the PR ──


def test_parse_requirements_reply_reads_done_and_declined_lines():
    from project_board.loop import _parse_requirements_reply

    reply = (
        "I did the work.\n"
        "## Requirements\n"
        "- r1: done\n"
        "- r2: declined — dicts not reachable through SqliteSaver+pickle\n"
        "r3 - done\n"
        "not a disposition line\n"
        "## Summary\n\n- r4: done (this is PAST the section — must not parse)\n"
    )
    assert _parse_requirements_reply(reply) == [
        {"id": "r1", "status": "done"},
        {"id": "r2", "status": "declined", "decline_reason": "dicts not reachable through SqliteSaver+pickle"},
        {"id": "r3", "status": "done"},
    ]


def test_parse_requirements_reply_keeps_the_last_heading_and_tolerates_no_section():
    from project_board.loop import _parse_requirements_reply

    reply = "## Requirements\n- r1: done\nnarration…\n## Requirements\n- r2: declined: not applicable\n"
    assert _parse_requirements_reply(reply) == [{"id": "r2", "status": "declined", "decline_reason": "not applicable"}]
    assert _parse_requirements_reply("no section here") == []
    assert _parse_requirements_reply("") == []


def test_parse_requirements_reply_ignores_open_and_junk_statuses():
    from project_board.loop import _parse_requirements_reply

    reply = "## Requirements\n- r1: open\n- r2: wontfix\n- r3: done\n"
    assert _parse_requirements_reply(reply) == [{"id": "r3", "status": "done"}]


def test_build_prompt_carries_the_requirement_ledger_with_statuses():
    feature = {
        **FEATURE,
        "requirements": [
            {"id": "r1", "text": "restore dict tolerance", "status": "open"},
            {"id": "r2", "text": "update CHANGELOG.md", "status": "done"},
        ],
    }
    prompt = BoardLoop({})._build_prompt(feature)
    assert "Requirements ledger" in prompt
    assert "`r1` [open] restore dict tolerance" in prompt
    assert "`r2` [done] update CHANGELOG.md" in prompt  # statuses ride along on re-dispatch
    assert "## Requirements" in prompt  # the reporting contract is named
    assert "SILENCE IS NOT" in prompt  # silence is not disposition
    # no ledger → no block (the prose-AC prompt is unchanged)
    assert "Requirements ledger" not in BoardLoop({})._build_prompt(FEATURE)


async def test_drive_requirement_gate_bounces_open_items_then_opens(monkeypatch):
    """An undisposed item bounces the build back (same tier, keep-worktree) with the
    open list injected; once every item is done/declined the PR opens. The loop —
    not the coder — writes the dispositions back to the bead each round."""
    prompts = []
    replies = iter(
        [
            "did some of it\n## Requirements\n- r1: done\n",  # r2 silent → stays open → bounce
            "## Requirements\n- r2: declined — repo has no CHANGELOG\n## Summary\n\n- done\n",
        ]
    )

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return next(replies)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/3"

    feature = {
        **FEATURE,
        "requirements": [
            {"id": "r1", "text": "restore dict tolerance", "status": "open"},
            {"id": "r2", "text": "update CHANGELOG.md", "status": "open"},
        ],
    }
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch, feature=feature)
    assert len(prompts) == 2  # initial + 1 ledger bounce
    assert store.creates == ["bd-1"]  # keep-worktree: the impl is not thrown away
    assert "r2" in prompts[1] and "update CHANGELOG.md" in prompts[1]  # the open list re-injected
    # the loop wrote dispositions back each round; the final ledger is fully closed
    writes = [c for c in store.calls if c[0] == "set_requirements"]
    assert len(writes) == 2
    final = writes[-1][2]
    assert {i["id"]: i["status"] for i in final} == {"r1": "done", "r2": "declined"}
    assert final[1]["decline_reason"] == "repo has no CHANGELOG"  # a decline with a reason is a valid close
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls
    assert loop._req_fix_attempts.get("bd-1", 0) == 0  # budget reset once the PR opened


async def test_drive_requirement_gate_exhausted_blocks_never_opens(monkeypatch):
    """The hard invariant: a feature cannot reach in_review with open items — an
    exhausted bounce budget is a capability failure (block with a single coder),
    NEVER a PR with unaddressed requirements (unlike the local gate's fail-open)."""

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "no requirements section at all"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("no PR may open while ledger items are open")

    feature = {**FEATURE, "requirements": [{"id": "r1", "text": "restore dict tolerance", "status": "open"}]}
    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        feature=feature,
        cfg={"coder": "proto", "goal_fix_max": 0},
    )
    assert "open_review" not in store.names()
    blocked = next(c for c in store.calls if c[0] == "flag_blocked")
    assert "requirements unresolved" in blocked[2] and "r1" in blocked[2]


async def test_drive_without_a_ledger_never_touches_set_requirements(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/4"

    _loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)  # FEATURE has no ledger
    assert "set_requirements" not in store.names()
    assert ("open_review", "bd-1", "https://example/pr/4") in store.calls


# ── shutdown-aware dispatch guards (#149) ──────────────────────────────────────


async def test_drive_shutdown_suppresses_coder_timeout_escalation(monkeypatch):
    """A dispatch killed during host shutdown must NOT escalate and must NOT write a
    tier label or block the feature (r1/r3/r4/r5). The feature stays in_progress so
    boot recovery (_recover → _reconcile_orphan) handles it on the next start."""

    class _EscStore(FakeLoopStore):
        def __init__(self):
            super().__init__()
            self.escalated = []

        def escalate(self, fid, reason):
            self.escalated.append((fid, reason))
            return "smart"  # would climb a tier without the shutdown guard

    store = _EscStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        raise worktree.CoderTimeout("coder killed during shutdown")

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("open_pr must not be reached when shutting down")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    # Escalation is ON (two distinct coders) so without the guard this would escalate.
    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}})
    assert loop.escalation_on
    loop._shutting_down = True  # simulate shutdown in progress
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    await loop._drive(FEATURE)

    assert store.escalated == []  # no tier escalation (no tier label written)
    assert "flag_blocked" not in store.names()  # feature stays in_progress
    assert loop._inflight == {}  # cleanup still runs


async def test_preflight_cancelled_by_shutdown_does_not_produce_failure(monkeypatch):
    """A preflight cancelled during host shutdown must NOT set _preflight_state to a
    failure string (r2/r6): no PREFLIGHT FAILED log, no HOLDING verdict. The state
    stays at its pre-call value (None = unchecked) so the next boot re-runs cleanly."""
    loop = BoardLoop({"local_gate_cmd": "ruff check .", "preflight": True})
    loop._shutting_down = True  # simulate shutdown in progress

    async def _cancelled(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _cancelled)

    await loop._preflight("default", "ruff check .", "/repo")

    # The project's state must remain unset (unchecked) — never a failure string that
    # would hold work.
    assert loop._preflight_state.get("default") is None
    assert not isinstance(loop._preflight_state.get("default"), str)


# ── the auto-merge edge: green + current + reviewed + not held → gh pr merge ──────


class _MergeStore:
    def __init__(self, feature):
        self.feature = dict(feature)
        self.comments = []

    def get_feature(self, fid):
        return dict(self.feature)

    def comment(self, fid, text):
        self.comments.append((fid, text))


def _reviewed(**over):
    f = {
        "id": "bd-1",
        "board_state": "in_review",
        "blocked": False,
        "labels": ["in-review", "review-clean", "merged-verified:abcdef123456"],
        "pr_url": "https://github.com/o/r/pull/1",
    }
    f.update(over)
    return f


def _merge_env(monkeypatch, *, head="abcdef123456" + "0" * 28, mss="CLEAN", merge_ok=True, draft=False):
    calls = {"merge": []}

    async def _head(repo, ref):
        return head

    async def _mss(pr_url, *, cwd="."):
        return mss

    async def _info(pr_url, *, cwd="."):
        # the combined read the merge edge uses (#207): status + isDraft in one gh call
        return {"mergeStateStatus": mss, "isDraft": draft}

    async def _merge(pr_url, *, method="squash", cwd=".", expected_head=""):
        calls["merge"].append((pr_url, method, cwd, expected_head))
        return (merge_ok, "" if merge_ok else "Pull request is not mergeable: required status check pending")

    async def _state(pr_url, *, cwd="."):
        return "MERGED" if (merge_ok or calls.get("landed_anyway")) else "OPEN"

    async def _delete(repo, branch):
        calls.setdefault("deleted", []).append(branch)
        return True

    monkeypatch.setattr(worktree, "origin_head_sha", _head)
    monkeypatch.setattr(worktree, "pr_merge_state", _mss)
    monkeypatch.setattr(worktree, "pr_merge_info", _info)
    monkeypatch.setattr(worktree, "merge_pr", _merge)
    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "delete_remote_branch", _delete)
    return calls


async def test_auto_merge_merges_when_every_gate_is_green_and_current(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    # No `review-clean-sha:` pin on this card → the grandfathered path merges unconstrained.
    assert calls["merge"] == [("https://github.com/o/r/pull/1", "squash", "/repo", "")]


async def test_auto_merge_pins_the_merge_to_the_reviewed_head(monkeypatch):
    """#323 residual TOCTOU: when the review-clean verdict is pinned to a head and it still
    matches the live head, the merge itself is constrained to that head
    (``--match-head-commit``) so a push landing in the window between the head read and the
    merge call cannot make ``gh pr merge`` land an unreviewed commit."""
    head = "abcdef123456" + "0" * 28
    calls = _merge_env(monkeypatch, head=head)

    async def _live(pr_url, *, cwd="."):
        return head  # the live head still matches the pin at check time

    monkeypatch.setattr(worktree, "pr_head_sha", _live)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    # The pin is stored SHORT — beads caps a label at 50 chars and `review-clean-sha:` is a
    # 17-char prefix, so a full sha would make 57 and `br` refuses the whole update. The
    # gate therefore matches the live head's 12-char prefix against it.
    store = _MergeStore(
        _reviewed(
            labels=[
                "in-review",
                "review-clean",
                f"review-clean-sha:{head[:12]}",
                "merged-verified:abcdef123456",
            ]
        )
    )
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert calls["merge"] == [("https://github.com/o/r/pull/1", "squash", "/repo", head)]  # pinned to the reviewed head


async def test_auto_merge_holds_when_pinned_head_moved_before_the_merge(monkeypatch):
    """#323/#328: a review-clean verdict pinned to one head but a live head that MOVED
    re-arms review and never reaches the merge call — the belt to ``--match-head-commit``'s
    suspenders."""
    live_head = "beforethepush" + "0" * 27
    calls = _merge_env(monkeypatch, head=live_head)

    async def _live(pr_url, *, cwd="."):
        return live_head  # the head moved off the pinned/reviewed one before the merge

    monkeypatch.setattr(worktree, "pr_head_sha", _live)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(
        _reviewed(
            labels=[
                "in-review",
                "review-clean",
                "review-clean-sha:" + "reviewedhead" + "0" * 28,  # pinned to a DIFFERENT head than live
                "merged-verified:abcdef123456",
            ]
        )
    )
    reset = []
    store.set_review_substate = lambda fid, label, note="", head_sha="": reset.append((label, head_sha))
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []  # never reached the merge — re-armed instead
    assert reset == [("review-pending", "")]


async def test_auto_merge_is_off_by_default_and_never_called(monkeypatch):
    loop = BoardLoop({})
    assert loop.auto_merge is False  # opt-in: a pre-upgrade board keeps parking PRs for its adjudicator


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"labels": ["in-review", "review-clean", "merged-verified:abcdef123456", "merge-hold"]}, "merge-hold"),
        ({"labels": ["in-review", "review-pending", "merged-verified:abcdef123456"]}, "review in progress"),
        ({"labels": ["in-review", "changes-requested", "merged-verified:abcdef123456"]}, "changes requested"),
        ({"labels": ["in-review", "merged-verified:abcdef123456"]}, "no review-clean verdict"),
        ({"labels": ["in-review", "review-clean", "merged-verified:000000000000"]}, "merged-verified stamp"),
        ({"labels": ["in-review", "review-clean"]}, "merged-verified stamp (none)"),
        ({"blocked": True}, "blocked"),
        ({"board_state": "in_progress"}, "state=in_progress"),
    ],
)
async def test_auto_merge_holds_on_each_board_side_blocker(monkeypatch, over, reason):
    calls = _merge_env(monkeypatch)
    # A local gate is what writes the merged-verified stamp — the currency rows
    # above only mean something on a board that has one (#209).
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "local_gate_cmd": "pytest -q"})
    store = _MergeStore(_reviewed(**over))
    why = await loop._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
    assert any(reason in w for w in why), why
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []


@pytest.mark.parametrize("mss", ["BLOCKED", "UNSTABLE", "BEHIND", "DIRTY", "UNKNOWN", "DRAFT", ""])
async def test_auto_merge_requires_github_clean(monkeypatch, mss):
    """Only GitHub's CLEAN — required checks + branch protection satisfied, not draft,
    mergeable. UNSTABLE (a non-required check red) is deliberately NOT a merge."""
    calls = _merge_env(monkeypatch, mss=mss)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []


async def test_auto_merge_holds_on_a_draft_as_a_named_blocker_without_spending_an_attempt(monkeypatch, caplog):
    """#207: GitHub reports mergeStateStatus=CLEAN for a draft whose checks pass, so
    the status alone never said "draft" — `gh pr merge` then refused ("pull request
    is in draft state") and each poll burned an auto_merge_max attempt until the card
    parked on "merge attempts exhausted" with no hint. Now `isDraft` rides the same
    read and is a named blocker with the one-line fix; no merge call, no attempt."""
    calls = _merge_env(monkeypatch, mss="CLEAN", draft=True)
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "auto_merge_max": 1})
    store = _MergeStore(_reviewed())
    why = await loop._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
    assert len(why) == 1 and why[0].startswith("draft"), why
    assert "gh pr ready https://github.com/o/r/pull/1" in why[0]
    assert "never spends a merge attempt" in why[0]
    with caplog.at_level("DEBUG", logger="protoagent.plugins.project_board"):
        for _ in range(3):
            assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert calls["merge"] == []  # never `gh pr merge`
    assert "bd-1" not in loop._auto_merge_failures  # never an attempt spent — auto_merge_max=1 did NOT exhaust
    # ONE bead comment for the hold (the review on #213: a failed `gh pr ready` — fork PR,
    # no write on base — was otherwise a silent permanent hold at DEBUG) — not a give-up
    (comment,) = store.comments
    assert comment[0] == "bd-1" and "auto-merge is holding: the PR is a draft" in comment[1]
    assert "gh pr ready https://github.com/o/r/pull/1" in comment[1] and "gave up" not in comment[1]
    assert "not auto-merging: draft" in caplog.text
    # …and once someone runs `gh pr ready` (isDraft False) the same card merges.
    calls = _merge_env(monkeypatch, mss="CLEAN", draft=False)
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1
    assert len(store.comments) == 1  # the merge added no draft note


async def test_draft_hold_comment_is_once_per_hold_and_survives_a_comment_failure(monkeypatch):
    """Drafted → noted once; un-drafted (still held on something else) → mark cleared;
    drafted again → noted once more. A failing comment never breaks the reconcile."""
    _merge_env(monkeypatch, mss="CLEAN", draft=True)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    for _ in range(3):
        await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo")
    assert len(store.comments) == 1 and "bd-1" in loop._draft_noted
    _merge_env(monkeypatch, mss="BLOCKED", draft=False)  # ready, but checks pending — held, not a draft
    await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo")
    assert "bd-1" not in loop._draft_noted and len(store.comments) == 1
    _merge_env(monkeypatch, mss="CLEAN", draft=True)  # re-drafted as a hold → one more note
    await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo")
    assert len(store.comments) == 2

    def _boom(fid, text):
        raise RuntimeError("br down")

    store.comment = _boom
    loop._draft_noted.clear()
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False


async def test_drive_promotes_a_draft_only_on_the_cards_first_adoption(monkeypatch):
    """#207 review: `_promote_adopted_draft` ran on EVERY "already exists" adoption, so
    an operator who drafted the loop's OWN PR as a hold got it un-drafted by the next
    CI-fail re-dispatch. The drive passes promote_draft=False when the card already
    owns a pr_url; a first adoption (no pr_url) still promotes."""
    seen = []

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        seen.append(promote_draft)
        return "https://example/pr/1"

    await _drive_with(monkeypatch, open_pr=_open_pr)  # FEATURE has no pr_url → first adoption
    await _drive_with(monkeypatch, open_pr=_open_pr, feature={**FEATURE, "pr_url": "https://example/pr/1"})
    assert seen == [True, False]


async def test_auto_merge_treats_unknown_isdraft_as_not_a_draft(monkeypatch):
    """`isDraft: None` (an older gh / field absent) must not hold a CLEAN PR — the
    status gates as before; only an explicit True is the draft blocker."""
    calls = _merge_env(monkeypatch, mss="CLEAN", draft=None)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1


async def test_auto_merge_without_review_gate_needs_no_review_label(monkeypatch):
    """review_gate off ⇒ there is no review verdict to wait for (the fleet pipeline
    reviews on its own); CI + verdict currency still gate."""
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1


async def test_auto_merge_without_a_local_gate_does_not_wait_for_a_stamp_nobody_writes(monkeypatch):
    """#209: `local_gate_cmd` blank (the default) ⇒ `_verify_merged_state` never
    stamps `merged-verified:` — so the merge edge must not require it, or auto_merge
    is unreachable on a default board (review-clean + CI-green cards sat in_review
    forever on orbisEngineer with `merged-verified stamp (none)` at debug level).
    CI + GitHub CLEAN are the gates."""
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})  # auto_rebase defaults ON, no local gate
    assert loop.auto_rebase and not loop.local_gate_cmd
    store = _MergeStore(_reviewed(labels=["in-review", "review-clean"]))  # no stamp — nothing ever writes one
    assert (
        await loop._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
        == []
    )
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1
    # …and WITH a local gate the stamp is still required (the #131 contract is intact).
    gated = BoardLoop({"auto_merge": True, "review_gate": True, "local_gate_cmd": "pytest -q"})
    why = await gated._auto_merge_blockers(store, store.get_feature("bd-1"), "https://github.com/o/r/pull/1", "/repo")
    assert any("merged-verified stamp (none)" in w for w in why), why


async def test_auto_merge_without_auto_rebase_skips_the_currency_check(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "auto_rebase": False})
    store = _MergeStore(_reviewed(labels=["in-review", "review-clean"]))  # no stamp — auto_rebase never writes one
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert len(calls["merge"]) == 1


async def test_auto_merge_refusal_retries_then_gives_up_on_the_bead_never_blocks(monkeypatch, caplog):
    calls = _merge_env(monkeypatch, merge_ok=False)
    loop = BoardLoop({"auto_merge": True, "review_gate": True, "auto_merge_max": 2})
    store = _MergeStore(_reviewed())
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
        # exhausted: no third gh call, one give-up comment, and the feature is NOT blocked
        assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is False
    assert len(calls["merge"]) == 2
    assert len(store.comments) == 1 and "auto-merge gave up after 2 attempt(s)" in store.comments[0][1]
    assert "gave up" in caplog.text
    assert store.feature["blocked"] is False


async def test_auto_merge_trusts_pr_state_over_gh_exit_code(monkeypatch):
    """gh can exit non-zero AFTER the merge landed (the --delete-branch local-branch
    trap, 2026-08-20) or lose a race to a concurrent merge — the PR's state is the
    verdict, and a landed merge must not spend the retry budget."""
    calls = _merge_env(monkeypatch, merge_ok=False)
    calls["landed_anyway"] = True
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    store = _MergeStore(_reviewed())
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert loop._auto_merge_failures.get("bd-1", 0) == 0  # the landed merge resets (pins 0), never spends
    assert calls["deleted"] == ["feat/bd-1"]  # remote cleanup rides the success path


async def test_auto_merge_deletes_the_remote_branch_after_a_clean_merge(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    assert await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo") is True
    assert calls["deleted"] == ["feat/bd-1"]


async def test_auto_merge_honours_merge_method(monkeypatch):
    calls = _merge_env(monkeypatch)
    loop = BoardLoop({"auto_merge": True, "review_gate": False, "merge_method": "rebase"})
    store = _MergeStore(_reviewed(labels=["in-review", "merged-verified:abcdef123456"]))
    await loop._maybe_auto_merge(store, "bd-1", "https://github.com/o/r/pull/1", "/repo")
    assert calls["merge"][0][1] == "rebase"


class _BlockedReconcileStore(_ReconcileStore):
    """A merged-but-blocked card (#196): the reconciler must poll blocked features'
    PRs and drive MERGED to done, but never touch CLOSED/OPEN ones (their blocked
    reason is deliberate)."""

    def __init__(self, in_review, blocked):
        super().__init__(in_review)
        self._blocked = blocked

    def list_features(self, state=None):
        if state == "blocked":
            return self._blocked
        return super().list_features(state)


async def test_reconcile_prs_promotes_a_blocked_card_whose_pr_merged(monkeypatch):
    """#196 (protoEngineer friction 2026-08-19): bd-2ti/bd-q1i had MERGED PRs but sat
    in blocked forever because the reconcile scanned in_review only."""
    store = _BlockedReconcileStore(
        [],
        [{"id": "bd-blk", "pr_url": "https://example/pr/7", "board_state": "blocked"}],
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_state(url, *, cwd="."):
        return "MERGED"

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({})
    await loop._reconcile_prs()
    assert store.merged == ["https://example/pr/7"]
    assert store.blocked == []  # never re-flagged


async def test_reconcile_prs_leaves_a_blocked_card_with_an_open_or_closed_pr_alone(monkeypatch):
    """The blocked scan takes ONLY the merged edge: a CLOSED PR must not rewrite the
    card's deliberate blocked reason, and the OPEN gates must not run against it."""
    for pr_state_val in ("OPEN", "CLOSED"):
        store = _BlockedReconcileStore(
            [],
            [{"id": "bd-blk", "pr_url": "https://example/pr/7", "board_state": "blocked"}],
        )
        monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

        async def _pr_state(url, *, cwd="."):
            return pr_state_val

        monkeypatch.setattr(worktree, "pr_state", _pr_state)
        loop = BoardLoop({})
        ci_called = []

        async def _ci_spy(store_, fid, pr_url, repo):
            ci_called.append(fid)

        monkeypatch.setattr(loop, "_reconcile_ci", _ci_spy)
        await loop._reconcile_prs()
        assert store.merged == [] and store.blocked == [] and ci_called == [], pr_state_val


async def test_spawn_ready_does_not_collide_same_filename_across_projects(monkeypatch):
    """#197 (protoEngineer friction 2026-08-20): every plugin repo carries PROTO.md —
    two doc-touching cards in DIFFERENT projects must both claim; only a same-project
    overlap defers."""
    store = _ClaimStore(
        [
            _ready("bd-ke7", ["PROTO.md"], project="discord"),
            _ready("bd-qjd", ["PROTO.md"], project="promptlab"),
            _ready("bd-dup", ["PROTO.md"], project="discord"),
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 3})
    monkeypatch.setattr(loop, "_project_name", lambda f: f.get("project") or "")
    finish = await _hold_drives(loop, monkeypatch)
    try:
        await loop._spawn_ready()
        # cross-project twin claims; the SAME-project twin defers.
        assert store.claimed == ["bd-ke7", "bd-qjd"]
        assert loop._inflight_files == {
            "bd-ke7": {("discord", "PROTO.md")},
            "bd-qjd": {("promptlab", "PROTO.md")},
        }
    finally:
        await finish()


# ── #211: operator cancel during a coder run must not leave an orphaned PR ───────────


class _CancellableStore(FakeLoopStore):
    """A loop store whose card can be cancelled mid-drive: ``cancelled`` flips the
    state ``get_feature`` reports (what the real store reads back from br after the
    operator's cancel verb), and ``comment`` records the trail."""

    def __init__(self):
        super().__init__()
        self.cancelled = False
        self.comments = []

    def get_feature(self, fid):
        return {"id": fid, "board_state": "cancelled" if self.cancelled else "in_progress"}

    def comment(self, fid, text):
        self.comments.append((fid, text))


async def _cancel_drive_with(monkeypatch, *, cancel_at, open_review_raises=False, pr_by_branch=""):
    """Run a drive with a coder that completes AFTER the operator cancelled the card.
    ``cancel_at`` = "dispatch" (cancel lands while the coder runs → seen before
    open_pr), "open_pr" (lands during the push/create → seen after open_pr returns),
    "open_pr_killed" (the task cancel lands INSIDE the push/create — open_pr raises
    CancelledError, but GitHub already has the PR: ``pr_by_branch``), or
    "open_review" (the real store refuses: the pre-#211 failure, now a cancel edge).
    Returns (store, opened, closed)."""
    store = _CancellableStore()
    store.removes = []
    opened, closed = [], []
    store.branch_lookups = []
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        if cancel_at == "dispatch":
            store.cancelled = True  # the operator cancels while the coder is finishing
        return "## Summary\n\n- did the thing\n"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        opened.append(branch)
        if cancel_at == "open_pr":
            store.cancelled = True  # cancel lands during the push/create
        if cancel_at == "open_pr_killed":
            store.cancelled = True  # the verb cancelled the task while `gh pr create` ran …
            raise asyncio.CancelledError()  # … _gh killed the child, but the PR already exists
        return "https://example/pr/211"

    async def _close_pr(pr_url, *, comment, cwd="."):
        closed.append((pr_url, comment, cwd))
        return True, ""

    async def _pr_by_branch(branch, *, cwd="."):
        store.branch_lookups.append((branch, cwd))
        return pr_by_branch

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_by_branch)

    if open_review_raises:

        def _open_review(fid, *, pr_url):
            store.cancelled = True
            raise BoardError("open_review expects in_progress, got 'cancelled'")

        store.open_review = _open_review

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "close_pr", _close_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto", "repo": "/repo"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(FEATURE)
    assert loop._inflight == {}  # every exit releases the slot
    return store, opened, closed


async def test_cancel_before_open_pr_skips_the_pr_and_reaps(monkeypatch):
    """(a) The #211 race: the coder completes after the card was cancelled → NO PR is
    opened, the worktree is reaped, the card gets the trail, nothing is blocked."""
    store, opened, closed = await _cancel_drive_with(monkeypatch, cancel_at="dispatch")
    assert opened == [] and closed == []
    assert store.removes == ["/wt/feat-bd-1"]
    assert "flag_blocked" not in store.names() and "open_review" not in store.names()
    (comment,) = store.comments
    assert comment[0] == "bd-1"
    assert "cancelled by operator" in comment[1] and "without a PR" in comment[1] and "feat/bd-1" in comment[1]
    assert "worktree reaped" in comment[1]
    assert store.branch_lookups == [("feat/bd-1", "/repo")]  # "no PR" is checked against GitHub, not assumed


async def test_cancel_landing_inside_the_push_or_create_still_finds_and_closes_the_pr(monkeypatch):
    """The blocker on #215: a task cancel arriving while the drive is inside `git push`
    / `gh pr create` re-raises CancelledError out of open_pr — the drive never gets a
    URL back, but GitHub may already have the PR. The cancel path looks the branch up
    (pr_url_for_branch) and closes what it finds, instead of commenting "without a PR"."""
    store, opened, closed = await _cancel_drive_with(
        monkeypatch, cancel_at="open_pr_killed", pr_by_branch="https://example/pr/orphan"
    )
    assert opened == ["feat/bd-1"]
    assert store.branch_lookups == [("feat/bd-1", "/repo")]
    assert closed == [("https://example/pr/orphan", "cancelled by operator — see card bd-1", "/repo")]
    assert store.removes == ["/wt/feat-bd-1"]
    assert "open_review" not in store.names() and "flag_blocked" not in store.names()
    (comment,) = store.comments
    assert "closed https://example/pr/orphan" in comment[1] and "without a PR" not in comment[1]


async def test_cancel_inside_the_create_with_no_pr_on_github_says_no_pr(monkeypatch):
    """Same kill, but the child died before GitHub created anything: the lookup finds
    nothing → the honest "without a PR" trail, no close attempted."""
    store, opened, closed = await _cancel_drive_with(monkeypatch, cancel_at="open_pr_killed", pr_by_branch="")
    assert closed == [] and store.removes == ["/wt/feat-bd-1"]
    (comment,) = store.comments
    assert "without a PR" in comment[1]


async def test_cancel_lookup_failure_is_not_a_cancel_failure(monkeypatch):
    """pr_url_for_branch raising (gh timeout) must not abort the reap or the trail."""
    store = _CancellableStore()
    store.removes = []

    async def _boom(branch, *, cwd="."):
        raise worktree.WorktreeError("gh pr view timed out after 60s")

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "pr_url_for_branch", _boom)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto"})
    loop._inflight["bd-1"] = ("/repo", "/wt/feat-bd-1", "feat/bd-1")
    await loop._end_cancelled_drive(store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1")
    assert store.removes == ["/wt/feat-bd-1"] and loop._inflight == {}
    (comment,) = store.comments
    assert "without a PR" in comment[1] and "worktree reaped" in comment[1]


async def test_cancel_during_open_pr_closes_the_pr_it_just_opened(monkeypatch):
    """(b) The cancel lands between push/create and open_review → the PR is closed
    with a comment pointing at the card, the worktree reaped, open_review never
    called (it would refuse a cancelled card and block it)."""
    store, opened, closed = await _cancel_drive_with(monkeypatch, cancel_at="open_pr")
    assert opened == ["feat/bd-1"]
    assert closed == [("https://example/pr/211", "cancelled by operator — see card bd-1", "/repo")]
    assert store.removes == ["/wt/feat-bd-1"]
    assert "open_review" not in store.names() and "flag_blocked" not in store.names()
    assert any("closed https://example/pr/211" in c[1] for c in store.comments)
    assert store.branch_lookups == []  # the drive HAS the url — no lookup


async def test_open_review_refusing_a_cancelled_card_is_a_cancel_not_a_block(monkeypatch):
    """The pre-#211 symptom itself — open_review raises on the cancelled card — now
    ends as a cancel: PR closed, worktree reaped, no flag_blocked on a closed bead."""
    store, opened, closed = await _cancel_drive_with(monkeypatch, cancel_at="open_review", open_review_raises=True)
    assert opened == ["feat/bd-1"]
    assert closed and closed[0][0] == "https://example/pr/211"
    assert store.removes == ["/wt/feat-bd-1"]
    assert "flag_blocked" not in store.names()


async def test_uncancelled_drive_is_unchanged_by_the_cancel_checks(monkeypatch):
    """(c) A card that stays in_progress opens its PR and enters review exactly as
    before — the checks are a store read, nothing else."""
    store, opened, closed = await _cancel_drive_with(monkeypatch, cancel_at="never")
    assert opened == ["feat/bd-1"] and closed == []
    assert ("open_review", "bd-1", "https://example/pr/211") in store.calls
    assert store.removes == [] and store.comments == []


async def test_cancel_check_fails_open_on_a_store_read_error(monkeypatch):
    """A get_feature failure is NOT a cancel — the drive proceeds (the old path)."""

    class _Broken:
        def get_feature(self, fid):
            raise BoardError("br unavailable")

    assert BoardLoop._cancelled(_Broken(), "bd-1") is False
    assert BoardLoop._cancelled(FakeLoopStore(), "bd-1") is False  # no get_feature at all


async def test_pr_close_failure_leaves_a_by_hand_note_and_still_reaps(monkeypatch):
    store = _CancellableStore()
    store.removes = []
    store.cancelled = True

    async def _close_pr(pr_url, *, comment, cwd="."):
        return False, "gh: HTTP 422 already closed"

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "close_pr", _close_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto"})
    loop._inflight["bd-1"] = ("/repo", "/wt/feat-bd-1", "feat/bd-1")
    await loop._end_cancelled_drive(store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1", pr_url="https://x/pr/1")
    assert store.removes == ["/wt/feat-bd-1"] and loop._inflight == {}
    (comment,) = store.comments
    assert "could not close https://x/pr/1" in comment[1] and "close it by hand" in comment[1]


@pytest.mark.parametrize("already", [worktree.PR_ALREADY_MERGED, worktree.PR_ALREADY_CLOSED])
async def test_pr_already_merged_or_closed_is_said_so_never_close_it_by_hand(monkeypatch, already):
    """close_pr reports a MERGED/CLOSED PR as a skip: the trail says which, and never
    asks the operator to close merged work by hand."""
    store = _CancellableStore()
    store.removes = []

    async def _close_pr(pr_url, *, comment, cwd="."):
        return True, already

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "close_pr", _close_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto"})
    await loop._end_cancelled_drive(store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1", pr_url="https://x/pr/1")
    (comment,) = store.comments
    assert f"https://x/pr/1 {already}, nothing to close" in comment[1]
    assert "close it by hand" not in comment[1] and "closed https://x/pr/1" not in comment[1]
    assert store.removes == ["/wt/feat-bd-1"]


async def test_end_cancelled_drive_runs_once_per_drive(monkeypatch):
    """A second cancel verb (a second call) must not close the PR or comment twice."""
    store = _CancellableStore()
    store.removes = []
    closed = []

    async def _close_pr(pr_url, *, comment, cwd="."):
        closed.append(pr_url)
        return True, ""

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "close_pr", _close_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto"})
    for _ in range(2):
        await loop._end_cancelled_drive(store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1", pr_url="https://x/pr/1")
    assert closed == ["https://x/pr/1"] and store.removes == ["/wt/feat-bd-1"] and len(store.comments) == 1
    # the mark is per DRIVE: the done callback forgets it so a later drive of the same card has its own edge
    loop._make_drive_done_cb("bd-1")(asyncio.get_running_loop().create_future())
    assert "bd-1" not in loop._cancel_done


async def test_repeat_cancel_mid_cleanup_finishes_the_cleanup_once(monkeypatch):
    """The re-entry race: the first cancel's cleanup is awaiting remove_worktree when a
    second cancel verb cancels the drive task again. The cleanup must run to completion
    exactly once — one close, one reap, one trail comment — and the task still ends
    cleanly (no CancelledError escapes)."""
    store = _CancellableStore()
    store.removes = []
    closed = []
    reaping = asyncio.Event()
    release = asyncio.Event()

    async def _close_pr(pr_url, *, comment, cwd="."):
        closed.append(pr_url)
        return True, ""

    async def _remove(repo, wt, branch=""):
        reaping.set()
        await release.wait()  # hold the cleanup here while the second cancel lands
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "close_pr", _close_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    loop = BoardLoop({"coder": "proto"})

    async def _drive_tail():
        # what _drive's CancelledError handler does
        try:
            await loop._end_cancelled_drive(
                store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1", pr_url="https://x/pr/1"
            )
        except asyncio.CancelledError:
            await loop._end_cancelled_drive(
                store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1", pr_url="https://x/pr/1"
            )

    task = asyncio.create_task(_drive_tail())
    await asyncio.wait_for(reaping.wait(), 1)
    task.cancel()  # the second verb
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(task, 1)
    assert task.exception() is None and not task.cancelled()
    assert closed == ["https://x/pr/1"] and store.removes == ["/wt/feat-bd-1"] and len(store.comments) == 1
    assert loop._inflight == {}


async def test_shutdown_cancel_mid_cleanup_still_propagates(monkeypatch):
    """A SHUTDOWN cancel during the cleanup is not absorbed — stop() owns that reap."""
    store = _CancellableStore()
    reaping = asyncio.Event()

    async def _remove(repo, wt, branch=""):
        reaping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "pr_url_for_branch", _nothing_by_branch)
    loop = BoardLoop({"coder": "proto"})
    task = asyncio.create_task(loop._end_cancelled_drive(store, "bd-1", "/repo", "/wt/feat-bd-1", "feat/bd-1"))
    await asyncio.wait_for(reaping.wait(), 1)
    loop._shutting_down = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.comments == []


async def _nothing_by_branch(branch, *, cwd="."):
    return ""


async def test_request_drive_cancel_stops_a_running_coder_and_ends_the_drive_cleanly(monkeypatch):
    """The cancel verb reaches the in-flight drive through the process-stable fid →
    task registry: the coder await is cancelled, the drive's own handler reaps + comments
    (no PR was opened → nothing to close), and the task COMPLETES — no CancelledError
    escapes into the loop."""
    store = _CancellableStore()
    store.removes = []
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    started = asyncio.Event()

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        started.set()
        await asyncio.Event().wait()  # a coder that never finishes on its own

    async def _open_pr(*a, **k):
        raise AssertionError("open_pr must not run for a cancelled drive")

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return True

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "pr_url_for_branch", _nothing_by_branch)
    loop = BoardLoop({"coder": "proto", "repo": "/repo"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    task = asyncio.create_task(loop._drive(FEATURE))
    loop_mod._register_drive("bd-1", task)
    task.add_done_callback(loop._make_drive_done_cb("bd-1"))
    await asyncio.wait_for(started.wait(), 1)
    assert loop_mod.live_drive("bd-1") is task
    store.cancelled = True  # the operator's cancel landed on the board…
    assert loop_mod.request_drive_cancel("bd-1") is True  # …and stops the coder
    await asyncio.wait_for(task, 1)  # completes — the CancelledError is handled inside
    assert task.exception() is None
    assert store.removes == ["/wt/feat-bd-1"] and loop._inflight == {}
    assert any("cancelled by operator" in c[1] for c in store.comments)
    assert loop_mod.live_drive("bd-1") is None  # unregistered by the done callback
    assert loop_mod.request_drive_cancel("bd-1") is False  # nothing left to cancel


async def test_shutdown_cancel_still_propagates(monkeypatch):
    """stop() cancels drives and owns their reap — a shutdown cancel must NOT be
    swallowed as an operator cancel (no PR close, no comment, CancelledError raised)."""
    store = _CancellableStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    started = asyncio.Event()

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    task = asyncio.create_task(loop._drive(FEATURE))
    await asyncio.wait_for(started.wait(), 1)
    loop._shutting_down = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.comments == [] and "bd-1" in loop._inflight  # stop() sweeps _inflight


def test_cancel_side_effects_closes_pr_and_signals_the_drive(monkeypatch):
    closed = []
    monkeypatch.setattr(
        worktree,
        "close_pr_sync",
        lambda url, *, comment, cwd=".", timeout=60: closed.append((url, comment, cwd)) or (True, ""),
    )
    monkeypatch.setattr(loop_mod, "request_drive_cancel", lambda fid: fid == "bd-live")
    assert loop_mod.cancel_side_effects("bd-live", "https://x/pr/3", cwd="/repo") == {
        "pr_closed": True,
        "pr_detail": "",
        "drive_cancelled": True,
    }
    assert closed == [("https://x/pr/3", "cancelled by operator — see card bd-live", "/repo")]
    assert loop_mod.cancel_side_effects("bd-idle") == {"pr_closed": False, "pr_detail": "", "drive_cancelled": False}
    assert len(closed) == 1  # no pr_url → gh untouched

    monkeypatch.setattr(worktree, "close_pr_sync", lambda *a, **k: (False, "boom"))
    side = loop_mod.cancel_side_effects("bd-idle", "https://x/pr/4")
    assert side["pr_closed"] is False and side["pr_detail"] == "boom"  # logged, not raised

    monkeypatch.setattr(worktree, "close_pr_sync", lambda *a, **k: (True, worktree.PR_ALREADY_MERGED))
    side = loop_mod.cancel_side_effects("bd-idle", "https://x/pr/5")
    assert side["pr_closed"] is False and side["pr_detail"] == "already merged"  # nothing was closed — say so


# ── preflight only convicts a CLEAN checkout (#255) ────────────────────────────────


async def _run_preflight(monkeypatch, *, rc, dirt, out=b"gate is red"):
    lp = BoardLoop({"local_gate_cmd": "pytest -q"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    monkeypatch.setattr("project_board.worktree.base_checkout_dirt", _async_return(dirt))

    async def _shell(*a, **k):
        return _FakeProc(rc, out)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    await lp._maybe_preflight()
    return lp


async def test_a_red_gate_on_a_dirty_checkout_does_not_hold_the_project(monkeypatch):
    """The bug: preflight runs with cwd=<repo>, the operator's main checkout. Its
    "coders only touch worktrees" premise is about coders, not the operator — a local
    uncommitted edit that reddens the gate would freeze that project's entire ready
    queue, and the only symptom on the board is `selected: []`."""
    lp = await _run_preflight(monkeypatch, rc=1, dirt="uncommitted changes to store.py")
    # The PROPERTY is "this project is not held": the claim scan skips a project only
    # when its state is a REASON STRING. (This used to assert `is True` — the mechanism
    # of the first cut, which also acquitted the base and released holds; #300 made dirt
    # verdict-neutral, so the state simply stays unset.)
    assert not isinstance(lp._preflight_state.get("default"), str)
    assert "store.py" in lp._preflight_dirty["default"]


async def test_a_red_gate_on_a_clean_checkout_still_fails_closed(monkeypatch):
    """The downgrade must not cost us the case the preflight exists for."""
    lp = await _run_preflight(monkeypatch, rc=1, dirt="", out=b"tsc: not found")
    assert isinstance(lp._preflight_state["default"], str)
    assert "tsc: not found" in lp._preflight_state["default"]
    assert "default" not in lp._preflight_dirty


async def test_a_green_gate_clears_a_previous_dirty_mark(monkeypatch):
    lp = await _run_preflight(monkeypatch, rc=1, dirt="uncommitted changes to x.py")
    assert lp._preflight_dirty
    lp._preflight_state.clear()  # force a re-check
    monkeypatch.setattr("project_board.worktree.base_checkout_dirt", _async_return(""))

    async def _green(*a, **k):
        return _FakeProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _green)
    await lp._maybe_preflight()
    assert lp._preflight_state["default"] is True and not lp._preflight_dirty


async def test_a_held_project_is_published_for_status(monkeypatch):
    """An idle board must be able to explain itself without the operator reading logs."""
    from project_board import health

    await _run_preflight(monkeypatch, rc=1, dirt="", out=b"tsc: not found")
    snap = health.preflight_snapshot()
    assert "tsc: not found" in snap["held"]["default"]


async def test_an_allowed_dirty_project_is_reported_as_dirty_not_held(monkeypatch):
    from project_board import health

    await _run_preflight(monkeypatch, rc=1, dirt="uncommitted changes to store.py")
    snap = health.preflight_snapshot()
    assert snap["held"] == {}  # nothing frozen
    assert "store.py" in snap["dirty"]["default"]


# ── #258 (F2b): BoardLoop store calls stay OFF the event-loop thread ─────────────


class _ThreadProbeStore:
    """Mimics the store verbs the tick paths use, routing EVERY verb through a
    ``_run`` seam (the real store's blocking chokepoint — subprocess.run + the
    contention time.sleep) that records whether it executed on the MAIN thread.
    pytest-asyncio runs the event loop on the main thread, so `main is True` here
    means a blocking `br` call would have stalled every coroutine (#258); the loop
    must reach the store only via asyncio.to_thread."""

    def __init__(self, features=None):
        self.features = [dict(f) for f in (features or [])]
        self.ops = []  # (op, ran_on_main_thread)

    def _run(self, op):
        self.ops.append((op, threading.current_thread() is threading.main_thread()))

    def assert_offloaded(self):
        assert self.ops, "expected at least one store call to reach the _run seam"
        on_loop = [op for op, main in self.ops if main]
        assert not on_loop, f"store calls ran on the event-loop thread: {on_loop}"

    def list_features(self, state=None, include_archived=False):
        self._run("list")
        return [dict(f) for f in self.features if state in (None, f.get("board_state"))]

    def ready_queue(self, relaxed=False):
        self._run("ready")
        return [dict(f) for f in self.features if f.get("board_state") == "ready"]

    def claim(self, fid, assignee=""):
        self._run("claim")
        f = next((x for x in self.features if x["id"] == fid), None)
        return dict(f, board_state="in_progress") if f else None

    def raw_features_with_comments(self, states=("done", "blocked")):
        self._run("raw")
        return []

    def get_feature(self, fid):
        self._run("show")
        return next((dict(x) for x in self.features if x["id"] == fid), None)

    def requeue(self, fid):
        self._run("requeue")
        return {"id": fid}

    def record_merge(self, pr_url=""):
        self._run("record_merge")
        return True

    def archive_stale(self, days):
        self._run("archive")
        return []

    def open_review(self, fid, *, pr_url):
        self._run("open_review")
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self._run("flag_blocked")
        return {"id": fid}


async def test_claim_scan_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: the claim scan's store calls — the review-WIP count, the ready_queue
    read, and the atomic claim — all reach the blocking ``_run`` seam on a worker
    thread, never on the event loop that runs the tick and every route."""
    store = _ThreadProbeStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1, "max_pending_reviews": 5})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert await loop._spawn_ready() is True
    finally:
        await finish()
    store.assert_offloaded()
    assert [op for op, _ in store.ops] == ["list", "ready", "claim"]


async def test_recovery_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: boot recovery (the in_progress scan, the orphan reconcile's reads and
    requeue, and the preflight-hold release scan) is fully offloaded."""
    store = _ThreadProbeStore([{"id": "bd-1", "title": "T", "board_state": "in_progress"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, cwd=""):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    await BoardLoop({})._recover()
    store.assert_offloaded()
    assert ("requeue", False) in store.ops  # the orphan actually took the requeue edge
    assert ("raw", False) in store.ops  # …and the boot preflight-hold scan ran too


async def test_sweep_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: the health sweep (in_progress scan, orphan reconcile, archive pass)."""
    store = _ThreadProbeStore([{"id": "bd-1", "title": "T", "board_state": "in_progress"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _no_pr(branch, cwd=""):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    await BoardLoop({})._sweep()
    store.assert_offloaded()
    assert ("archive", False) in store.ops


async def test_pr_reconcile_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: the PR reconcile's scans and the MERGED edge's record_merge."""
    feat = {"id": "bd-1", "title": "T", "board_state": "in_review", "pr_url": "https://x/pr/1", "labels": []}
    store = _ThreadProbeStore([feat])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _state(pr_url, cwd=""):
        return "MERGED"

    async def _reap(repo, root, fid):
        return True

    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    await BoardLoop({})._reconcile_prs()
    store.assert_offloaded()
    assert ("record_merge", False) in store.ops


async def test_preflight_ready_scan_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: the gate preflight's ready-projects scan (its only store touch when no
    gate command is configured) runs on a worker thread."""
    store = _ThreadProbeStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await BoardLoop({})._maybe_preflight()
    store.assert_offloaded()
    assert [op for op, _ in store.ops] == ["list"]


async def test_drive_reaches_the_store_off_the_event_loop_thread(monkeypatch):
    """#258: a clean drive's board writes — the cancel re-reads at the PR seam and
    open_review — go through asyncio.to_thread like the tick's scans."""
    store = _ThreadProbeStore([dict(FEATURE, board_state="in_progress")])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "the coder's reply"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/9"

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(dict(FEATURE))
    store.assert_offloaded()
    assert ("open_review", False) in store.ops


async def test_record_bg_runs_the_write_off_loop_and_the_barrier_flushes_it():
    """#258: coder_seam's record_gens/record_verified callbacks are sync and fire ON
    the event loop mid-dispatch — _record_bg must run the store write on a worker
    thread, and _await_bg_records must land it before the drive proceeds (so the
    pre-offload ordering — records on the bead before the PR opens — is kept)."""
    loop = BoardLoop({})
    seen = []

    def _write(n):
        seen.append((n, threading.current_thread() is threading.main_thread()))

    loop._record_bg("bd-1", "record_gens", _write, 4)
    await loop._await_bg_records("bd-1")
    assert seen == [(4, False)]  # landed, and NOT on the event-loop thread
    assert loop._bg_records == {}  # the barrier drained the parking

    # A failing write is swallowed (fire-and-forget) — the barrier never raises.
    def _boom(n):
        raise RuntimeError("br hiccup")

    loop._record_bg("bd-2", "record_gens", _boom, 1)
    await loop._await_bg_records("bd-2")
    assert loop._bg_records == {}


# ── _drive: a rate-limited dispatch is the PROVIDER refusing, not a capability failure (#280)


def _ladder_drive_env(monkeypatch, dispatch, tiers):
    """The escalation-path wiring the empty-reply test uses, factored: an
    _EscalatingStore handing out ``tiers`` climbs, the worktree helpers stubbed, and a
    two-coder ladder so ``escalation_on`` is True. Returns (loop, store)."""
    store = _EscalatingStore(tiers=tiers)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _remove(repo, wt, branch=""):
        return None

    async def _reap(repo, root, fid):
        return None

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("open_pr must not run — every dispatch in this test fails")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop = BoardLoop({"coders": {"smart": "sonnet", "reasoning": "opus"}})
    assert loop.escalation_on
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    return loop, store


# What the ACP adapter surfaces for a Claude session limit (2026-08-28, bd-cwpv.12/.16):
# classify() reads the `rate_limit` errorKind → the retryable rate_limit policy.
_SESSION_LIMIT = (
    "coder dispatch failed: Internal error: You've hit your session limit · resets 1:30pm "
    '(America/Los_Angeles) (JSON-RPC -32603): {"errorKind": "rate_limit"}'
)


async def test_drive_rate_limited_dispatch_retries_same_tier_and_never_escalates(monkeypatch):
    """RED-IS-REACHABLE: before #280 the first attempt escalated (`escalated` had an
    entry and the ladder was burned in three attempts). A rate-limited dispatch must
    spend the rate_limit policy's retry budget on the SAME tier, then block with the
    rate_limit reason — never consulting the ladder, so a requeue after the reset
    restarts at the tier the card actually deserves."""
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        raise worktree.WorktreeError(_SESSION_LIMIT)

    loop, store = _ladder_drive_env(monkeypatch, _dispatch, tiers=["reasoning"])
    await loop._drive(FEATURE)

    policy = classify(_SESSION_LIMIT)
    assert policy.category == "rate_limit" and policy.retryable
    assert len(dispatches) == policy.max_attempts  # the retry budget, all on one tier
    assert store.escalated == []  # the ladder was never consulted
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and blocked[0][2].startswith("rate_limit:")
    assert loop._inflight == {}


async def test_drive_terminal_dispatch_failure_blocks_for_triage_never_escalates(monkeypatch):
    """#339 narrows #280 further: a terminal dispatch failure that never reached the
    model (an adapter/session refusal, no tool/thought/answer/token recorded) is a
    HOST-infrastructure incident, not a model-capability ceiling — a stronger model
    can't clear it. It must block DIRECTLY for triage: ONE dispatch, the ladder never
    consulted, no `tier:` label, under the `dispatch-infra` class the operator sees."""
    dispatches = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(prompt)
        raise worktree.WorktreeError("coder dispatch failed: adapter rejected the session (unknown error)")

    loop, store = _ladder_drive_env(monkeypatch, _dispatch, tiers=["reasoning"])
    await loop._drive(FEATURE)

    assert len(dispatches) == 1  # no re-dispatch at a stronger tier
    assert store.escalated == []  # the ladder was never consulted — no bogus tier: label
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra"
    assert "adapter rejected the session" in blocked[0][2]  # infra evidence preserved
    assert loop._inflight == {}


# ── a dirty checkout yields NO preflight verdict, in EITHER direction (#300) ──────


async def _preflight_with(monkeypatch, *, rc, dirt, prior=None):
    """One `_maybe_preflight` pass with a canned gate exit code and dirt verdict, over a
    loop whose `_preflight_state` starts at `prior` (None = never checked, a str = held
    for an earlier clean red)."""
    lp = BoardLoop({"local_gate_cmd": "pytest -q"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _PreflightStore(ready=["bd-1"]))
    monkeypatch.setattr("project_board.worktree.base_checkout_dirt", _async_return(dirt))
    released = []
    monkeypatch.setattr(lp, "_release_preflight_holds", lambda name: released.append(name))

    async def _shell(*a, **k):
        return _FakeProc(rc, b"gate output")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _shell)
    if prior is not None:
        lp._preflight_state["default"] = prior
        # A KNOWN-failed project's re-check is throttled against `_last_preflight`,
        # which defaults to 0.0 — so the window has "elapsed" only when
        # `time.monotonic()` is already larger than the interval. That is true on a
        # long-running dev box and FALSE in a fresh CI container, where monotonic
        # starts near zero: these tests passed locally and skipped the preflight
        # entirely on CI. Seed the timestamp so the elapsed window is explicit.
        lp._last_preflight["default"] = time.monotonic() - 10_000
    await lp._maybe_preflight()
    return lp, released


async def test_green_gate_on_a_dirty_checkout_does_not_release_an_existing_hold(monkeypatch):
    """RED-IS-REACHABLE: the first cut only distrusted a RED result, so a green gate on
    a dirty tree cleared the state to True and released the holds — an operator's local
    fix acquitting a base no gate has actually cleared, and coders dispatched onto it."""
    lp, released = await _preflight_with(
        monkeypatch, rc=0, dirt="uncommitted changes to store.py", prior="tsc: not found"
    )
    assert lp._preflight_state["default"] == "tsc: not found"  # the clean red still stands
    assert released == []  # nothing was un-held on evidence that can't acquit
    assert "store.py" in lp._preflight_dirty["default"]


async def test_red_gate_on_a_dirty_checkout_does_not_release_an_existing_hold(monkeypatch):
    """The other half of the same bug: a red gate on a dirty tree took the downgrade
    path, which ALSO called _release_preflight_holds — so an unrelated local edit
    un-held a project that was genuinely broken when the tree was clean."""
    lp, released = await _preflight_with(
        monkeypatch, rc=1, dirt="uncommitted changes to loop.py", prior="tsc: not found"
    )
    assert lp._preflight_state["default"] == "tsc: not found"
    assert released == []


async def test_dirty_checkout_never_newly_holds_an_unchecked_project(monkeypatch):
    """The property the downgrade existed for is kept: with no prior verdict, dirt
    leaves state None — the claim scan only skips a project whose state is a REASON
    STRING, so work keeps dispatching instead of freezing on a local edit."""
    lp, released = await _preflight_with(monkeypatch, rc=1, dirt="uncommitted changes to x.py")
    assert lp._preflight_state.get("default") is None
    assert released == []


async def test_clean_gate_verdicts_are_unchanged(monkeypatch):
    """Fail-closed and recovery both still decide on a CLEAN checkout."""
    lp, released = await _preflight_with(monkeypatch, rc=1, dirt="")
    assert isinstance(lp._preflight_state["default"], str)  # clean red still convicts
    lp2, released2 = await _preflight_with(monkeypatch, rc=0, dirt="", prior="tsc: not found")
    assert lp2._preflight_state["default"] is True  # clean green still acquits
    assert released2 == ["default"]  # …and releases the hold


# ── the blocked lane: self-heal or escalate, never die silently ─────────────────────


class _BlockedStore:
    """A store whose blocked lane a sweep can drive: list_features(state="blocked")
    returns the given rows, and clear_blocked/requeue/record_budget/record_notified are
    recorded. The `notified:<kind>` marker (#341) is written onto the ROW's labels so a
    fresh loop over the SAME rows reads it back — the durable half a restart depends on;
    clear_blocked strips it, mirroring the real store's genuine-recovery edge."""

    def __init__(self, rows):
        self._rows = rows
        self.cleared: list[str] = []
        self.requeued: list[str] = []
        self.budgets: list[tuple] = []
        self.notified: list[tuple] = []  # (fid, kind) — record_notified writes (#341)

    def _row(self, fid):
        return next((r for r in self._rows if r.get("id") == fid), None)

    def list_features(self, state=None, **_kw):
        return list(self._rows) if state == "blocked" else []

    def clear_blocked(self, fid):
        self.cleared.append(fid)
        r = self._row(fid)
        if r is not None:  # a genuine unblock drops the block flag AND supersedes the marker (#341)
            r["blocked"] = False
            r["board_state"] = "ready"
            r["labels"] = [l for l in (r.get("labels") or []) if not str(l).startswith("notified:") and l != "blocked"]
        return {"id": fid}

    def requeue(self, fid):
        self.requeued.append(fid)
        return {"id": fid, "board_state": "ready"}

    def record_budget(self, fid, kind, n):
        self.budgets.append((fid, kind, n))

    def record_notified(self, fid, kind="blocked"):
        r = self._row(fid)
        # Mirror the real store's LIVE-condition guard (#341 review): a `blocked` marker is
        # stamped only while the card is STILL blocked, so a concurrent unblock that landed
        # between the alert and this delayed write is never overwritten by a stale re-add.
        if kind == "blocked" and (r is None or not r.get("blocked")):
            return {"id": fid}
        self.notified.append((fid, kind))
        if r is not None and f"notified:{kind}" not in (r.get("labels") or []):
            r.setdefault("labels", []).append(f"notified:{kind}")
        return {"id": fid}


def _blocked(fid, cls, *, reason="boom", title="A card", budget=None):
    f = {
        "id": fid,
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": cls,
        "blocked_reason": reason,
        "title": title,
        "labels": [],
    }
    if budget is not None:
        f["labels"] = [f"budget:unblock-retry:{budget}"]
    return f


async def test_a_transient_block_clears_itself_instead_of_dying(monkeypatch):
    """A coder timeout is not a reason to retire a card. The sweep clears the block,
    requeues it, and spends one retry — before this, a transient failure and a bad
    credential died in exactly the same silent way and dependents waited forever."""
    store = _BlockedStore([_blocked("bd-t", "transient", reason="coder timed out after 1800.0s")])
    loop = BoardLoop({"coder": "proto"})
    await loop._recover_blocked(store)
    assert store.cleared == ["bd-t"] and store.requeued == ["bd-t"]
    assert ("bd-t", "unblock-retry", 1) in store.budgets


async def test_an_auth_block_is_never_auto_retried_and_pages_the_operator(monkeypatch):
    """No amount of waiting fixes a bad credential, so it must NOT burn retries looking
    like progress — it goes straight to a human, naming the card and the real reason."""
    store = _BlockedStore([_blocked("bd-a", "auth", reason="403 forbidden", title="Ship the thing")])
    loop = BoardLoop({"coder": "proto"})
    seen = []
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, **_kw: seen.append((fid, text)))
    await loop._recover_blocked(store)
    assert store.cleared == [] and store.requeued == []
    (fid, text) = seen[0]
    assert fid == "bd-a"
    assert "bd-a" in text and "403 forbidden" in text and "Ship the thing" in text


async def test_a_card_that_keeps_failing_transiently_stops_retrying_and_escalates(monkeypatch):
    """Three transient failures is not bad luck. Once the retry budget is spent the card
    stays blocked and the operator is told, rather than the loop re-running it forever."""
    store = _BlockedStore([_blocked("bd-t", "transient", budget=2)])
    loop = BoardLoop({"coder": "proto"})
    seen = []
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, **_kw: seen.append(text))
    await loop._recover_blocked(store)
    assert store.requeued == []
    assert "auto-retries spent" in seen[0]


async def test_an_unclassified_block_escalates_rather_than_silently_retrying(monkeypatch):
    """A block with no class is an unknown failure — the one case where guessing is
    worst. It escalates."""
    store = _BlockedStore([_blocked("bd-u", "")])
    loop = BoardLoop({"coder": "proto"})
    seen = []
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, **_kw: seen.append(text))
    await loop._recover_blocked(store)
    assert store.requeued == [] and "unclassified" in seen[0]


async def test_an_unresolvable_inbox_falls_back_to_the_loud_log_never_a_crash(monkeypatch, caplog):
    """If the inbox path can't be resolved the alert must still be LOUD, not swallowed
    and not raised into the sweep — a notification failure must never stop the pass."""
    store = _BlockedStore([_blocked("bd-a", "auth")])
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: None)
    with caplog.at_level("WARNING"):
        await loop._recover_blocked(store)
    assert any("bd-a" in str(r.getMessage()) for r in caplog.records)


def test_the_inbox_path_never_guesses_between_two_name_keyed_stores(monkeypatch, tmp_path):
    """An agent renamed before the host's #2382 fix can leave TWO name-keyed databases.
    Nothing on disk says which is current, and filing an alert into the wrong one is
    worse than not filing it — the operator would see silence either way, but with a
    guess we would also log success. So it resolves to None and the caller says so."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "oldName.db").touch()
    (inbox / "newName.db").touch()

    class _Paths:
        def store(self, name):
            return inbox

    fake = types.ModuleType("infra.paths")
    fake.instance_paths = lambda: _Paths()
    monkeypatch.setitem(sys.modules, "infra.paths", fake)
    assert loop_mod._inbox_db_path() is None

    # …but the constant name wins outright when it is there
    (inbox / "agent.db").touch()
    assert loop_mod._inbox_db_path() == inbox / "agent.db"


async def test_a_host_without_an_inbox_still_says_so_loudly(monkeypatch, caplog):
    """The inbox is feature-detected. A host without it must still leave a WARNING —
    strictly louder than the silence a block used to leave — never an exception."""
    import sys

    monkeypatch.setitem(sys.modules, "inbox.store", None)  # import raises
    loop = BoardLoop({"coder": "proto"})
    with caplog.at_level("WARNING"):
        loop._notify_operator("bd-x", "Board card bd-x is blocked")
    assert any("bd-x" in r.message or "bd-x" in str(r.args) for r in caplog.records)


async def test_escalation_re_reads_the_reason_because_br_list_carries_no_comments(monkeypatch):
    """The block reason rides a COMMENT, and `br list` carries none — so the sweep's list
    row always projects "". Escalating "no reason recorded" tells the operator nothing
    and sends them digging, which is exactly what the alert exists to prevent. The one
    card being escalated is re-read through get_feature."""
    store = _BlockedStore([_blocked("bd-a", "auth", reason="")])
    store.get_feature = lambda fid: {"id": fid, "blocked_reason": "auth: 403 forbidden on push"}
    loop = BoardLoop({"coder": "proto"})
    seen = []
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, **_kw: seen.append(text))
    await loop._recover_blocked(store)
    assert "403 forbidden on push" in seen[0]


async def test_a_failed_reason_re_read_still_escalates(monkeypatch):
    """The alert matters more than its detail: if the re-read fails the operator is still
    told, with whatever is known."""
    store = _BlockedStore([_blocked("bd-a", "auth", reason="")])

    def _boom(fid):
        raise RuntimeError("br exploded")

    store.get_feature = _boom
    loop = BoardLoop({"coder": "proto"})
    seen = []
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, **_kw: seen.append(text))
    await loop._recover_blocked(store)
    assert seen and "bd-a" in seen[0]


# ── the operator-notified marker is DURABLE, not process scratch (#341) ──────────────


def _use_fake_inbox(monkeypatch, tmp_path):
    """Install the host `inbox.InboxStore` seam over a temp db and return the list every
    `add()` appends to (text, priority, dedup_key). Mirrors the REAL constructor —
    InboxStore(db_path) — like test_the_operator_is_told_once_per_card_not_once_per_sweep."""
    added: list[tuple] = []

    fake = types.ModuleType("inbox")

    class _Inbox:
        def __init__(self, db_path, *, dedup_window_s=300):
            self.path = db_path

        def add(self, text, *, priority="next", source="", dedup_key=""):
            added.append((text, priority, dedup_key))

    fake.InboxStore = _Inbox
    monkeypatch.setitem(sys.modules, "inbox", fake)
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")
    return added


# ── alert dedup lives in the KEY, not in state we have to keep correct ──────────────


def _spy_inbox(monkeypatch, tmp_path, added):
    import sys
    import types as _types

    fake = _types.ModuleType("inbox")

    class _Inbox:
        def __init__(self, db_path, *, dedup_window_s=300):
            self.window = dedup_window_s

        def add(self, text, *, priority="next", source="", dedup_key=""):
            added.append({"key": dedup_key, "priority": priority, "text": text, "window": self.window})

    fake.InboxStore = _Inbox
    monkeypatch.setitem(sys.modules, "inbox", fake)
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")


async def test_the_same_incident_dedups_by_key_across_sweeps_and_restarts(monkeypatch, tmp_path):
    """A stayed-blocked card is ONE alert. Nothing on our side remembers that — the key
    carries the incident, so repeated sweeps (and a fresh process, which is what a restart
    is) produce an identical key and the inbox dedups it. Earlier cuts kept a memo, then a
    bead label, then a rollback, then a generation counter, and review found a race in
    each; none of that exists now, so none of it can go stale."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    store = _BlockedStore([_blocked("bd-a", "auth", reason="403 forbidden")])

    await BoardLoop({"coder": "proto"})._recover_blocked(store)
    await BoardLoop({"coder": "proto"})._recover_blocked(store)  # a "restart": brand-new loop
    assert len({a["key"] for a in added}) == 1, "the same incident must produce the same key"
    assert added[0]["priority"] == "now"
    # and the window outlives a blocked card's real lifetime, so a restart cannot re-alert
    assert added[0]["window"] >= 24 * 3600


async def test_a_different_failure_on_the_same_card_is_a_new_incident(monkeypatch, tmp_path):
    """The point of keying on the incident: a card that recovers and blocks again for a
    DIFFERENT reason is news, and must alert even though the card id is unchanged."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    loop = BoardLoop({"coder": "proto"})
    await loop._recover_blocked(_BlockedStore([_blocked("bd-a", "auth", reason="403 forbidden")]))
    await loop._recover_blocked(_BlockedStore([_blocked("bd-a", "terminal", reason="worktree add failed")]))
    assert len({a["key"] for a in added}) == 2, "a different failure must not be deduped away"


async def test_the_key_is_stable_for_one_incident_and_carries_the_card(monkeypatch, tmp_path):
    """The key must be derived, not random: same class + reason ⇒ same key, and it names
    the card so an operator reading the inbox can tell which one it is."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    row = _blocked("bd-xyz", "auth", reason="403 forbidden")
    for _ in range(3):
        await BoardLoop({"coder": "proto"})._recover_blocked(_BlockedStore([row]))
    assert len({a["key"] for a in added}) == 1
    assert added[0]["key"].startswith("blocked:bd-xyz:")


async def test_no_notification_state_is_written_to_the_bead(monkeypatch, tmp_path):
    """The bead is not where alert bookkeeping belongs. An escalation writes NOTHING to
    the card — no marker, no generation — so there is no state to strand, roll back, or
    reconcile, and a blocked card's labels mean exactly what they say."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    store = _BlockedStore([_blocked("bd-a", "auth", reason="403 forbidden")])
    store.updates = []
    store.record_notified = lambda *a, **k: store.updates.append(a)  # must never be called
    await BoardLoop({"coder": "proto"})._recover_blocked(store)
    assert added, "the operator was still told"
    assert store.updates == []


async def test_a_failed_recovery_cycle_alerts_again_even_for_an_identical_failure(monkeypatch, tmp_path):
    """#346 r7: keying the incident on class+reason alone was wrong. A card that auto-healed,
    rebuilt, and failed in exactly the same way is a NEW failed recovery cycle — the
    self-heal did not work, which is precisely what an operator needs to hear — and it was
    being suppressed for the whole seven-day window.

    The unblock-retry budget already counts those cycles, so it joins the key and costs no
    new state."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    same = dict(cls="transient", reason="coder timed out after 1800.0s")
    # cycle 2 exhausted its retries → alert. A FRESH loop each time so the budget is read
    # from the bead rather than the per-process cache, which is also what a restart does.
    await BoardLoop({"coder": "proto"})._recover_blocked(
        _BlockedStore([_blocked("bd-a", same["cls"], reason=same["reason"], budget=2)])
    )
    # …recovered, rebuilt, failed IDENTICALLY — a new cycle, so the budget advanced
    await BoardLoop({"coder": "proto"})._recover_blocked(
        _BlockedStore([_blocked("bd-a", same["cls"], reason=same["reason"], budget=3)])
    )
    assert len({a["key"] for a in added}) == 2, "a new failed recovery cycle must reach the operator"


async def test_repeated_sweeps_within_one_cycle_still_dedup(monkeypatch, tmp_path):
    """The other half stays true: while the card sits blocked on the SAME cycle, every
    sweep produces the same key and the operator is told once."""
    added = []
    _spy_inbox(monkeypatch, tmp_path, added)
    row = _blocked("bd-a", "auth", reason="403 forbidden", budget=2)
    for _ in range(4):
        await BoardLoop({"coder": "proto"})._recover_blocked(_BlockedStore([row]))
    assert len({a["key"] for a in added}) == 1

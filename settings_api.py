"""Guarded scalar settings boundary for plugin-owned Configure pages (#617)."""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path

import yaml

from .loop._common import LIVE_KNOBS
from .project_registry import _live_section, _MUTATION_LOCK

log = logging.getLogger("protoagent.plugins.project_board")

# Defaults remain owned by manifest.config (ADR 0019).
#
# This allowlist was written out explicitly so a field could be RETAINED here after moving
# out of the manifest's settings UI. No field does that today, and the suite asserts the
# two key sets are EQUAL, so the explicit copy bought nothing and cost the drift below. If
# a setting ever must outlive its manifest entry, add it to the derived mapping by hand and
# relax that assertion — deliberately, rather than by letting 30 labels rot.
_MANIFEST = yaml.safe_load(Path(__file__).with_name("protoagent.plugin.yaml").read_text())

# Operator-facing metadata for each scalar setting, DERIVED from the manifest — the one
# place this text already lives (the console renders the manifest; `_DEFAULTS` below reads
# the same file for `config`). It used to be restated here as a 140-line literal, which is
# a copy that cannot be kept honest: #624 reworded all 30 labels into plain language while
# this module was in flight with the old wording, so each PR was green alone and the MERGED
# state failed — every one of the 30 labels disagreed. `test_allowlist_metadata_and_masking`
# compares the two, which is what caught it; deriving them makes the comparison impossible
# to fail and the next rewording a one-file change.
_META_FIELDS = ("label", "type", "minimum", "maximum", "options")
SETTINGS = {entry["key"]: {field: entry.get(field) for field in _META_FIELDS} for entry in _MANIFEST["settings"]}

# `loop_enabled` OUTLIVES its manifest `settings:` entry (#611, S1a): the Overview Configure
# tab owns its on/off switch, so it was removed from the generic schema UI — but the switch
# still saves through this boundary (PUT /settings) and the value must still appear in
# pending_restart (it is restart-only, not a LIVE_KNOB). This is the deliberate hand-add the
# header comment above describes: add it to the derived mapping rather than let the Overview
# page re-implement the guarded write. Its boot value/default still come from `config:` below.
SETTINGS.setdefault(
    "loop_enabled",
    {"label": "Build ready cards automatically", "type": "bool", "minimum": None, "maximum": None, "options": None},
)

# The Coders Configure tab (S3, #613) and the Review & merge tab (S4, #614) own these by the
# SAME mechanism as loop_enabled: each key was removed from the manifest `settings:` block (so
# the host's generic schema renderer no longer draws it), but the page still reads and writes it
# through PUT /settings — so they stay in this allowlist by hand, carrying the label/type/bounds
# the manifest `settings:` entries used to declare (their defaults still come from `config:`
# below). Keep each line in step with the loop's LIVE_KNOBS classification and with the page's
# own copy (view/routing.js, view/review.html).
_PAGE_OWNED = {
    "coder": {"label": "Coder for builds", "type": "string", "minimum": None, "maximum": None, "options": None},
    "max_concurrent": {
        "label": "Cards building at once",
        "type": "number",
        "minimum": 1,
        "maximum": 64,
        "options": None,
    },
    "max_pending_reviews": {
        "label": "Pull requests waiting for review",
        "type": "number",
        "minimum": 0,
        "maximum": 1000,
        "options": None,
    },
    "max_concurrent_sessions": {
        "label": "Parallel coding sessions per build",
        "type": "number",
        "minimum": 0,
        "maximum": 32,
        "options": None,
    },
    "coder_solve": {
        "label": "Try solutions until tests pass",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "coder_solve_test_cmd": {
        "label": "Tests for candidate solutions",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "max_mode_n": {
        "label": "Alternative builds to compare",
        "type": "number",
        "minimum": 1,
        "maximum": 16,
        "options": None,
    },
    "decompose_after_timeouts": {
        "label": "Timeouts before asking to split a card",
        "type": "number",
        "minimum": 0,
        "maximum": 10,
        "options": None,
    },
    "goal_verify": {
        "label": "Check acceptance criteria before review",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    # ── Review & merge tab (S4, #614): the Review→CI→Rebase→Merge pipeline, the
    # workflow/reviewer/merge-method pickers and the dependent-cards radio. ───────────────
    "review_gate": {
        "label": "Require review before merging",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "review_workflow": {
        "label": "Workflow for required reviews",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "reviewer": {
        "label": "Agent for pull request reviews",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "review_dispatch": {
        "label": "Request an extra review on opening",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "review_fix_max": {
        "label": "Attempts to fix review findings",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "ci_poll": {
        "label": "Send failed checks back to the coder",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "ci_rerun_max": {
        "label": "Rerun failed checks before changing code",
        "type": "number",
        "minimum": 0,
        "maximum": 5,
        "options": None,
    },
    "ci_fix_max": {
        "label": "Attempts to fix failed checks",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "auto_rebase": {
        "label": "Update pull requests with base changes",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "rebase_fix_max": {
        "label": "Attempts to fix merge conflicts",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "auto_merge": {
        "label": "Merge approved pull requests automatically",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "merge_method": {
        "label": "How to merge pull requests",
        "type": "select",
        "minimum": None,
        "maximum": None,
        "options": ["squash", "merge", "rebase"],
    },
    "merged_verify_max": {
        "label": "Failed checks before holding a merge",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "merge_poll": {
        "label": "Check pull request status automatically",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "dep_gate": {
        "label": "When dependent cards can start",
        "type": "select",
        "minimum": None,
        "maximum": None,
        "options": ["merge", "review"],
    },
}
for _key, _meta in _PAGE_OWNED.items():
    SETTINGS.setdefault(_key, _meta)

_DEFAULTS = _MANIFEST["config"]
_boot_values: dict | None = None


def capture_boot_settings(cfg):
    """Capture before the loop starts; reload mutates the shared cfg in place."""
    global _boot_values
    _boot_values = {key: copy.deepcopy(cfg.get(key, _DEFAULTS.get(key))) for key in SETTINGS}


def _boot():
    if _boot_values is None:
        raise ValueError("board settings boot snapshot is unavailable")
    return _boot_values


def _pending(live):
    boot = _boot()
    return [
        {"key": key, "label": meta["label"]}
        for key, meta in SETTINGS.items()
        if key not in LIVE_KNOBS and live.get(key, _DEFAULTS.get(key)) != boot[key]
    ]


def settings_snapshot():
    """Return live saved values, immutable boot values, and restart metadata."""
    live = _live_section(required=True)
    boot = _boot()
    settings = {}
    for key, meta in SETTINGS.items():
        value = live.get(key, _DEFAULTS.get(key))
        boot_value = boot[key]
        if meta["type"] == "secret":
            value, boot_value = {"set": bool(value)}, {"set": bool(boot_value)}
        settings[key] = {
            **copy.deepcopy(meta),
            "value": value,
            "boot_value": boot_value,
            "restart": key not in LIVE_KNOBS,
        }
    return {"settings": settings, "pending_restart": _pending(live)}


def _validate(updates):
    if not isinstance(updates, dict):
        raise ValueError("settings updates must be an object")
    for key, value in updates.items():
        if key not in SETTINGS:
            raise ValueError(f"unknown setting: {key}")
        meta = SETTINGS[key]
        kind = meta["type"]
        # All current numeric settings count attempts, candidates, or sessions.
        # Reject bools (Python ints) and fractional numbers instead of truncating.
        valid = (
            type(value) is bool
            if kind == "bool"
            else type(value) is int
            if kind == "number"
            else isinstance(value, str)
        )
        if not valid:
            raise ValueError(f"{key} must be {'an integer' if kind == 'number' else kind}")
        if meta["minimum"] is not None and value < meta["minimum"]:
            raise ValueError(f"{key} must be at least {meta['minimum']}")
        if meta["maximum"] is not None and value > meta["maximum"]:
            raise ValueError(f"{key} must be at most {meta['maximum']}")
        if meta["options"] is not None and value not in meta["options"]:
            raise ValueError(f"{key} must be one of the declared options")


async def apply_setting_updates(updates, actor):
    """Validate the whole batch before one host save; never log or echo values."""
    log.info(
        "[project_board] settings write actor=%s keys=%s", actor, list(updates) if isinstance(updates, dict) else []
    )
    try:
        _validate(updates)
        updates = copy.deepcopy(updates)
        _boot()
    except ValueError as exc:
        return {"ok": False, "reason": str(exc), "saved": [], "restart_required": [], "pending_restart": []}

    from graph.plugins.host import HOST

    async with _MUTATION_LOCK:
        if HOST.apply_settings is None:
            return {
                "ok": False,
                "reason": "host config apply is unavailable",
                "saved": [],
                "restart_required": [],
                "pending_restart": [],
            }

        def compute(live):
            # Only the allowlisted scalar patch crosses the host save boundary.
            # The host routes manifest-declared secrets to its secret store.
            _validate(updates)
            return {"project_board": updates}

        try:
            ok, _messages = await asyncio.to_thread(HOST.apply_settings, compute)
        except Exception:  # Host errors can contain submitted secret values.
            ok = False
        if not ok:
            return {
                "ok": False,
                "reason": "host refused settings update",
                "saved": [],
                "restart_required": [],
                "pending_restart": [],
            }
        return {
            "ok": True,
            "saved": list(updates),
            "restart_required": [key for key in updates if key not in LIVE_KNOBS],
            "pending_restart": _pending(_live_section(required=True)),
        }

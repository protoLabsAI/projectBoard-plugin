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

# Explicit allowlist retained when fields move out of the manifest settings UI.
# Defaults remain owned by manifest.config (ADR 0019).
SETTINGS = {
    "loop_enabled": {
        "label": "Run orchestration loop",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "decompose_after_timeouts": {
        "label": "Ask to split after N timeouts",
        "type": "number",
        "minimum": 0,
        "maximum": 10,
        "options": None,
    },
    "coder": {"label": "Coder delegate", "type": "string", "minimum": None, "maximum": None, "options": None},
    "br_autofetch": {
        "label": "Fetch br automatically",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "max_concurrent": {"label": "Parallel builds", "type": "number", "minimum": 1, "maximum": 64, "options": None},
    "max_concurrent_sessions": {
        "label": "Coder sessions per build",
        "type": "number",
        "minimum": 0,
        "maximum": 32,
        "options": None,
    },
    "max_pending_reviews": {
        "label": "Review-queue limit",
        "type": "number",
        "minimum": 0,
        "maximum": 1000,
        "options": None,
    },
    "goal_verify": {
        "label": "Verify acceptance goals",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "base_refresh": {
        "label": "Keep base checkouts current",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "local_gate_cmd": {
        "label": "Default local gate",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "coder_solve": {
        "label": "Execution-grounded solve ladder",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "coder_solve_test_cmd": {
        "label": "Solve verification command",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "max_mode_n": {"label": "Max-Mode candidates", "type": "number", "minimum": 1, "maximum": 16, "options": None},
    "auto_merge": {"label": "Auto-merge green PRs", "type": "bool", "minimum": None, "maximum": None, "options": None},
    "merged_verify_max": {
        "label": "Merged-state verification budget",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "merge_method": {
        "label": "Merge method",
        "type": "select",
        "minimum": None,
        "maximum": None,
        "options": ["squash", "merge", "rebase"],
    },
    "review_gate": {
        "label": "Blocking adversarial review",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "review_workflow": {
        "label": "Review workflow",
        "type": "string",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "reviewer": {"label": "Reviewer delegate", "type": "string", "minimum": None, "maximum": None, "options": None},
    "review_dispatch": {
        "label": "Advisory reviewer dispatch",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "review_fix_max": {"label": "Review fix attempts", "type": "number", "minimum": 0, "maximum": 20, "options": None},
    "merge_poll": {
        "label": "Reconcile pull requests",
        "type": "bool",
        "minimum": None,
        "maximum": None,
        "options": None,
    },
    "ci_poll": {"label": "Bounce failing CI", "type": "bool", "minimum": None, "maximum": None, "options": None},
    "ci_fix_max": {"label": "CI fix attempts", "type": "number", "minimum": 0, "maximum": 20, "options": None},
    "ci_rerun_max": {"label": "CI reruns before a fix", "type": "number", "minimum": 0, "maximum": 5, "options": None},
    "auto_rebase": {"label": "Rebase stale PRs", "type": "bool", "minimum": None, "maximum": None, "options": None},
    "rebase_fix_max": {
        "label": "Conflict fix attempts",
        "type": "number",
        "minimum": 0,
        "maximum": 20,
        "options": None,
    },
    "dep_gate": {
        "label": "Dependency release gate",
        "type": "select",
        "minimum": None,
        "maximum": None,
        "options": ["merge", "review"],
    },
    "webhook_secret": {"label": "Webhook secret", "type": "secret", "minimum": None, "maximum": None, "options": None},
    "npm_token": {"label": "npm registry token", "type": "secret", "minimum": None, "maximum": None, "options": None},
}
_DEFAULTS = yaml.safe_load(Path(__file__).with_name("protoagent.plugin.yaml").read_text())["config"]
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

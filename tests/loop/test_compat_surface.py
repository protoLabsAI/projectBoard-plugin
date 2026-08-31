"""Compatibility-surface seams for the loop/ package split (#268).

r2: existing external imports and plugin initialization must keep resolving the
historical ``project_board.loop`` API after the module became a package. The suite
already imports these names all over the place; this file pins the contract in one
targeted place so a re-export dropped from ``__init__`` fails here immediately.
"""

from __future__ import annotations

import project_board.loop as loop_mod

# The module-level API that external code and the rest of the suite import straight
# from ``project_board.loop`` — helpers, constants, live state, and the rebindable
# seams. Every one must remain a package attribute.
REEXPORTED = [
    # public class + lifecycle knobs
    "BoardLoop",
    "LIVE_KNOBS",
    "LIVE_BOOL_KNOBS",
    "LIVE_STR_KNOBS",
    "PREFLIGHT_BLOCK_PREFIX",
    # prompt / build-context helpers
    "_pr_body",
    "_no_test_marker",
    "_inject_source_issue_line",
    "_source_issue",
    "_source_issue_still_open",
    "_issue_closed_by_board_sibling",
    # reconcile / review helpers + constants
    "_ci_failure_reason",
    "_parse_pr_url",
    "_parse_requirements_reply",
    "_requirement_gate_diagnostics",
    "_requirement_gate_diag_line",
    "_REVIEW_FINDINGS_TITLE",
    "_REVIEWED_HEAD_SHA_LEN",
    "_MERGED_VERIFIED_SHA_LEN",
    "reset_merged_verify_budget",
    # preflight / gate helpers
    "_resolve_gate_cmd",
    "_is_code_path",
    "_is_test_path",
    "_PNPM_INSTALL",
    # drive / cancel + live registries
    "queue_review_feedback",
    "_PENDING_FEEDBACK",
    "cancel_side_effects",
    "request_drive_cancel",
    "live_drive",
    "_register_drive",
    "_unregister_drive",
    # rebindable store seams (tests monkeypatch these on the package)
    "get_store",
    "merge_posture",
    "reconfigure_cached_store",
    "_inbox_db_path",
]


def test_historical_module_api_is_reexported():
    missing = [name for name in REEXPORTED if not hasattr(loop_mod, name)]
    assert not missing, f"compat surface dropped: {missing}"


def test_board_loop_resolves_and_lives_in_the_core_module():
    """r2: ``from project_board.loop import BoardLoop`` yields the assembled class,
    now homed in the ``core`` edge module rather than a flat ``loop.py``."""
    from project_board.loop import BoardLoop

    assert BoardLoop is loop_mod.BoardLoop
    assert BoardLoop.__module__ == "project_board.loop.core"


def test_plugin_init_import_path_resolves_board_loop():
    """The top-level plugin ``register()`` does ``from .loop import BoardLoop`` — the
    same relative path resolves through the package unchanged."""
    from project_board.loop import BoardLoop

    assert BoardLoop({}) is not None

"""Monkeypatch-seam routing across the split (#268).

The split moved methods and helpers out of the single ``loop`` module namespace
into sibling edge modules. The suite monkeypatches a handful of names ON the
package (``project_board.loop.get_store``, ``merge_posture``, ``request_drive_cancel``,
``_inbox_db_path``, ``_source_issue_still_open``, ``reconfigure_cached_store``,
``live_drive``) and expects code that moved into the edge modules to still observe
the patch. Each edge module reads those seams through the live package (``_loop``),
so the patch is preserved. These tests pin that indirection directly — the exact
property that keeps ~130 existing monkeypatch call sites working without edits.
"""

from __future__ import annotations

import project_board.loop as loop_mod
import project_board.loop._common as common_mod
import project_board.loop.core as core_mod
import project_board.loop.drive as drive_mod
import project_board.loop.preflight as preflight_mod
import project_board.loop.prompt as prompt_mod
import project_board.loop.reconcile as reconcile_mod

EDGE_MODULES = [common_mod, core_mod, drive_mod, reconcile_mod, preflight_mod, prompt_mod]


def test_every_edge_module_points_loop_at_the_package():
    """``_loop`` in each edge module IS the package object, so ``_loop.<name>`` is a
    live attribute lookup that sees whatever a test patched on the package."""
    for mod in EDGE_MODULES:
        assert mod._loop is loop_mod, f"{mod.__name__}._loop is not the package"


def test_patching_get_store_on_the_package_is_seen_by_edge_modules(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(loop_mod, "get_store", sentinel)
    # every edge module resolves get_store through the package, not a stale local copy
    assert drive_mod._loop.get_store is sentinel
    assert reconcile_mod._loop.get_store is sentinel
    assert common_mod._loop.get_store is sentinel


def test_cancel_side_effects_routes_request_drive_cancel_through_the_package(monkeypatch):
    """``cancel_side_effects`` (a _common helper) calls ``request_drive_cancel``. Both
    moved together, but the test patches the seam on the package — the helper must read
    it there. This mirrors test_loop.test_cancel_side_effects_closes_pr_and_signals."""
    monkeypatch.setattr(loop_mod, "request_drive_cancel", lambda fid: fid == "bd-live")
    assert loop_mod.cancel_side_effects("bd-live", "", cwd="/repo")["drive_cancelled"] is True
    assert loop_mod.cancel_side_effects("bd-idle", "", cwd="/repo")["drive_cancelled"] is False


def test_pending_feedback_is_one_shared_object_across_modules():
    """``_PENDING_FEEDBACK`` rides a process-stable slot; the package and _common see
    the SAME dict, so a queued /review bounce drains through _build_prompt regardless
    of which module wrote it (#256 identity contract, preserved by the split)."""
    assert loop_mod._PENDING_FEEDBACK is common_mod._PENDING_FEEDBACK


def test_process_stable_slot_keys_are_rooted_at_the_plugin_package():
    """r5: the split moved the slot helpers one package level deeper. Their key derives
    from the plugin ROOT (``project_board``), NOT the module's parent — so the key is
    byte-identical to the pre-split loop.py and a reload still finds the same slot.
    A naive ``rsplit('.', 1)`` would key off ``project_board.loop`` and silently fork
    the dict on reload (#256)."""
    import sys

    assert common_mod._FEEDBACK_SLOT_PREFIX + "project_board" in sys.modules
    # the derivation itself, independent of which slots have been lazily realized
    for name in (
        common_mod._FEEDBACK_SLOT_PREFIX,
        common_mod._DRIVE_SLOT_PREFIX,
        common_mod._LOOP_SLOT_PREFIX,
    ):
        assert name.startswith("project_board.")

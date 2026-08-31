"""Edge-ownership seams for the loop/ package split (#268).

The ~5,100-line ``loop.py`` was split by execution edge into a ``loop/`` package.
These tests pin the structural invariant the split exists to create: each edge's
implementation lives in its OWN module (as a mixin), the public ``BoardLoop``
composes them, and a change confined to one edge lands on that edge's module —
NOT on the central compatibility surface. If a future edit collapses an edge back
onto the surface (or drops a mixin from the composition) these fail loudly.
"""

from __future__ import annotations

import project_board.loop as loop_mod
from project_board.loop.core import BoardLoop
from project_board.loop.drive import DriveMixin
from project_board.loop.preflight import PreflightMixin
from project_board.loop.prompt import PromptMixin
from project_board.loop.reconcile import ReconcileMixin


def test_loop_is_a_package_with_the_expected_edge_modules():
    """r1: loop.py is replaced by a package whose edges are separately importable."""
    assert hasattr(loop_mod, "__path__")  # it's a package, not a single module
    import importlib

    for edge in ("_common", "drive", "reconcile", "preflight", "prompt", "core"):
        mod = importlib.import_module(f"project_board.loop.{edge}")
        assert mod is not None


def test_board_loop_composes_the_four_edge_mixins_in_mro():
    """r1/r3: the assembled surface inherits every edge, so cross-edge ``self.x()``
    calls resolve through the MRO exactly as they did on the monolith."""
    mro = BoardLoop.__mro__
    assert mro[0] is BoardLoop
    for mixin in (DriveMixin, ReconcileMixin, PreflightMixin, PromptMixin):
        assert mixin in mro
    assert mro[-1] is object


# One representative method per edge → the module that must own it.
EDGE_OWNERSHIP = [
    (DriveMixin, ["_drive", "_dispatch_task", "_spawn_ready", "start", "stop", "reload", "_run"]),
    (
        ReconcileMixin,
        [
            "_reconcile_prs",
            "_verify_merged_state",
            "_review_gate",
            "_maybe_auto_merge",
            "_maybe_rebase",
            "_reconcile_ci",
        ],
    ),
    (PreflightMixin, ["_preflight", "_maybe_preflight", "_record_preflight_failure"]),
    (PromptMixin, ["_build_prompt", "_build_task_prompt"]),
]


def test_each_edge_method_is_defined_on_its_own_mixin():
    """r7: edge logic is owned by the edge module. A method belongs to exactly one
    mixin's ``__dict__`` and is NOT redefined on the BoardLoop surface, so a fix to
    that edge modifies the mixin file rather than the compatibility surface."""
    for mixin, methods in EDGE_OWNERSHIP:
        for name in methods:
            assert name in mixin.__dict__, f"{name} should be owned by {mixin.__name__}"
            assert name not in BoardLoop.__dict__, f"{name} leaked onto the BoardLoop surface"


def test_no_edge_method_is_owned_by_two_mixins():
    """No method is defined on more than one edge mixin — the split partitions the
    class body, it does not duplicate it (guards against a copy/paste regression)."""
    seen: dict[str, str] = {}
    for mixin in (DriveMixin, ReconcileMixin, PreflightMixin, PromptMixin):
        for name, val in mixin.__dict__.items():
            if name.startswith("__") or not callable(val):
                continue
            assert name not in seen, f"{name} defined on both {seen[name]} and {mixin.__name__}"
            seen[name] = mixin.__name__


def test_construction_and_cross_edge_resolution():
    """r3: BoardLoop({}) constructs and every edge's methods are reachable on the
    instance — the mixin composition preserves the public method surface."""
    bl = BoardLoop({})
    for _mixin, methods in EDGE_OWNERSHIP:
        for name in methods:
            assert hasattr(bl, name)

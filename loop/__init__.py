"""project_board.loop — the orchestration loop, split by execution edge (#268).

Compatibility surface. Re-exports the historical ``project_board.loop`` API — the
public ``BoardLoop`` plus the module-level helpers, constants, and process-stable
state — so existing imports and plugin registration resolve unchanged. The
edge-specific implementation lives in the sibling modules over a shared kernel:

    _common     module-level kernel (imports, constants, helpers, live state)
    drive       claim / dispatch / drive lifecycle
    reconcile   CI / rebase / merged-state / review gate / auto-merge / recovery
    preflight   fail-closed gate preflight
    prompt      dispatch prompt + build-context construction
    core        the assembled ``BoardLoop``

A fix confined to one edge touches that module, not this surface.
"""

from __future__ import annotations

from ._common import *  # noqa: F401,F403 — re-export the loop kernel
from ._common import __all__ as _kernel_all
from .core import BoardLoop
from .drive import request_dispatch  # the board_dispatch tool seam (#390)

__all__ = [*_kernel_all, "BoardLoop", "request_dispatch"]

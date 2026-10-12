"""Board cards in chat (#522) — the ``board-cards`` frame component.

A status check-in ("what's blocked?", "how's epic X?") answers with live board cards
rendered INSIDE the transcript instead of paragraphs. The pieces:

* ``board_show_cards`` (the tool, in ``__init__._board_tools``) returns a short text
  summary — what the model and a plain A2A client read — plus a ``board-cards``
  component-v1 tail whose props are POINTERS only (ids, or a state/project query, and a
  label). Props persist in chat history, so they never carry card content.
* ``component/board-cards.html`` + ``.js`` (served on the PUBLIC ``/plugins/project_board``
  router, listed in the manifest's ``public_paths``) is the page the console frames (ADR
  0118 D5). It holds no bearer: it re-reads the cards through the host-mediated read
  channel (``protoComponent:read``, protoAgent#4143) every ~15 s, and every button is a
  ``protoComponent:send`` PROPOSAL the operator confirms, so no write ever bypasses a
  visible, audited turn.

Registration is feature-detected (:func:`register_component`): a host without
``register_component`` — or one whose ``register_component`` predates ``reads=`` — gets
ONE setup gap and the tool answers in text only.
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

log = logging.getLogger("protoagent.plugins.project_board")

COMPONENT = "board-cards"
# Relative to the plugin's PUBLIC route prefix (/plugins/project_board) — the manifest's
# `public_paths` must cover it, because the opaque-origin frame sends no bearer.
FRAME = "component/board-cards.html"
# The plugin's own GET routes (relative to /api/plugins/project_board/) the frame may ask
# the host to read on its behalf. `*` is exactly one path segment.
READS = ["features", "features/*", "status"]

SETUP_GAP_KEY = "board_cards"
SETUP_GAP_MESSAGE = (
    "Board cards in chat need a protoAgent with host-mediated component reads "
    "(register_component(..., reads=[...]), protoAgent#4143). Until the host is upgraded, "
    "board_show_cards answers in text only."
)

STATES = ("backlog", "ready", "in_progress", "in_review", "blocked", "done")
MAX_IDS = 20
TITLE_MAX = 80
PROJECT_MAX = 128
# A bead id: `<prefix>-<hash>[.<n>…]`, e.g. `bd-vbyd.24`. One path segment by
# construction (no `/`, `?`, `#`, whitespace), because the page reads `features/<id>`.
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Same shape the project registry accepts for a `projects:` entry name.
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROPS = {"ids", "state", "project", "title"}

# Set by register_component(): True only when the host accepted the frame kind WITH reads.
# The tool emits the component tail only then; otherwise it answers in text alone.
EMIT = False


def validate_props(props: dict) -> str | None:
    """Why ``props`` is not a valid ``board-cards`` payload, or ``None`` when it is.

    Pointers only: ``{ids?: [≤ 20 bead ids], state?: one of STATES, project?: str,
    title?: str ≤ 80}``. Either ``ids`` (show exactly those cards) or a ``state`` and/or
    ``project`` query (re-evaluated on every refresh) — never both, never neither. Any
    other key is rejected, so card content can't ride into chat history."""
    if not isinstance(props, dict):
        return "props must be an object"
    extra = set(props) - _PROPS
    if extra:
        return f"unexpected board-cards props: {', '.join(sorted(map(str, extra)))}"
    ids = props.get("ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            return "board-cards ids must be a non-empty list"
        if len(ids) > MAX_IDS:
            return f"board-cards ids may name at most {MAX_IDS} cards"
        for i in ids:
            if not isinstance(i, str) or not _ID_RE.match(i) or ".." in i:
                return f"board-cards id {i!r} is not a board card id (e.g. bd-a1b2)"
        if len(set(ids)) != len(ids):
            return "board-cards ids must not repeat"
    state = props.get("state")
    if state is not None and state not in STATES:
        return f"board-cards state must be one of: {', '.join(STATES)}"
    project = props.get("project")
    if project is not None and (not isinstance(project, str) or not _PROJECT_RE.match(project)):
        return "board-cards project must be a project name (letters, digits, - and _)"
    title = props.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > TITLE_MAX or "\n" in title):
        return f"board-cards title must be one line of at most {TITLE_MAX} chars"
    has_query = state is not None or project is not None
    if ids is not None and has_query:
        return "board-cards takes ids OR a state/project query, not both"
    if ids is None and not has_query:
        return "board-cards needs ids, or a state and/or project"
    return None


def build_props(ids=None, state: str = "", project: str = "", title: str = "") -> dict:
    """The tool's arguments → the component props (empty values dropped). ``ids`` may be
    a list or a comma/space-separated string (a model often passes one)."""
    if isinstance(ids, str):
        ids = [s for s in re.split(r"[,\s]+", ids) if s]
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    props: dict = {}
    if ids:
        props["ids"] = list(dict.fromkeys(ids))  # keep order, drop repeats
    if str(state or "").strip():
        props["state"] = str(state).strip()
    if str(project or "").strip():
        props["project"] = str(project).strip()
    if " ".join(str(title or "").split()):
        props["title"] = " ".join(str(title).split())
    return props


def _accepts_reads(fn) -> bool:
    """Does the host's ``register_component`` take the ``reads=`` keyword (protoAgent#4143)?
    True when the signature names ``reads`` or accepts ``**kwargs``. Anything that can't be
    introspected is treated as NOT accepting it — degrade to text rather than risk a
    TypeError at register time."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind is p.VAR_KEYWORD:
            return True
        if p.name == "reads" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return True
    return False


def _report_gap(registry) -> None:
    report = getattr(registry, "report_setup_gap", None)
    log.warning("[project_board] setup gap (%s): %s", SETUP_GAP_KEY, SETUP_GAP_MESSAGE)
    if callable(report):
        try:
            report(SETUP_GAP_KEY, SETUP_GAP_MESSAGE)
        except Exception:  # noqa: BLE001 — a warning that can't land must not break registration
            log.debug("[project_board] report_setup_gap(%s) failed", SETUP_GAP_KEY, exc_info=True)


def register_component(registry) -> bool:
    """Register the ``board-cards`` frame kind with its reads, feature-detected.

    Returns (and records in :data:`EMIT`) whether the host took it. An older host — no
    ``register_component``, or one without ``reads=`` — records ONE setup gap and the tool
    answers in text only. A host that refuses the registration (it logs-and-skips a frame
    outside ``public_paths``, a bad read pattern…) leaves the kind out of
    ``registry.components``; that also reads as not emitted, so the tool never points chat
    at a kind the console can't resolve."""
    global EMIT
    EMIT = False
    fn = getattr(registry, "register_component", None)
    if not callable(fn) or not _accepts_reads(fn):
        _report_gap(registry)
        return False
    try:
        fn(COMPONENT, validate_props, frame=FRAME, reads=list(READS))
    except TypeError:
        # A signature that over-promised (**kwargs) but rejects `reads=` at call time.
        _report_gap(registry)
        return False
    except Exception:  # noqa: BLE001 — a component must never break registration
        log.exception("[project_board] registering the board-cards component failed")
        return False
    components = getattr(registry, "components", None)
    if isinstance(components, dict) and COMPONENT not in components:
        log.warning("[project_board] the host refused the board-cards component — text-only cards")
        return False
    EMIT = True
    return True


def component_tail(props: dict) -> str:
    """``"\\n" + <component-v1 payload>`` for the tool reply, or ``""`` when the kind isn't
    registered with this host or the props don't validate (never let a pointer break the
    reply)."""
    if not EMIT or validate_props(props) is not None:
        return ""
    try:
        from graph.sdk import encode_component
    except Exception:  # noqa: BLE001 — no host seam ⇒ text only
        return ""
    return "\n" + encode_component(COMPONENT, props)


# ── the card projection (shared by the tool summary and GET /features?fields=card) ──

_CARD_KEYS = (
    "id",
    "title",
    "board_state",
    "blocked",
    "blocked_reason",
    "blocked_class",
    "dag_blocked",
    "project",
    "pr_url",
    "issue_type",
    "difficulty",
    "attempts",
    "next_action",
    "next_action_hint",
    "awaiting_merge",
    "ci_status",
)


def card_row(f: dict) -> dict:
    """The slim row a card needs — no spec, criteria, design or comments, so a listing
    stays far under the host's 256 KB read cap however big the board is."""
    return {k: f[k] for k in _CARD_KEYS if k in f}


STATE_LABEL = {
    "backlog": "backlog",
    "ready": "on deck",
    "in_progress": "building",
    "in_review": "in review",
    "blocked": "blocked",
    "done": "done",
    "cancelled": "cancelled",
}


def _is_open(f: dict) -> bool:
    return f.get("board_state") not in ("done", "cancelled")


def header_line(props: dict, feats: list[dict]) -> str:
    """``Epic #4076 · 3 open · 1 blocked`` — the same line the page renders."""
    label = props.get("title") or (
        " · ".join(
            x
            for x in (
                f"project {props['project']}" if props.get("project") else "",
                STATE_LABEL.get(props.get("state", ""), props.get("state", "")) if props.get("state") else "",
            )
            if x
        )
        or "Board cards"
    )
    n_open = sum(1 for f in feats if _is_open(f))
    n_blocked = sum(1 for f in feats if f.get("blocked") or f.get("board_state") == "blocked")
    parts = [label, f"{n_open} open"]
    if n_blocked:
        parts.append(f"{n_blocked} blocked")
    return " · ".join(parts)


def summary_text(props: dict, feats: list[dict], missing: list[str] | None = None) -> str:
    """The model-facing text: a header, then one line per card (≤ 20)."""
    lines = [header_line(props, feats)]
    for f in feats[:MAX_IDS]:
        state = "blocked" if f.get("blocked") else f.get("board_state", "")
        line = f"- {f.get('id')} · {f.get('title', '')} — {STATE_LABEL.get(state, state)}"
        why = (f.get("blocked_reason") or "").strip() if state == "blocked" else ""
        if why:
            line += f" ({why[:160]})"
        elif f.get("next_action"):
            line += f" ({f['next_action']})"
        if f.get("pr_url"):
            line += f" {f['pr_url']}"
        lines.append(line)
    if len(feats) > MAX_IDS:
        lines.append(f"… and {len(feats) - MAX_IDS} more")
    if not feats and not missing:
        lines.append("(no cards match)")
    for fid in missing or []:
        lines.append(f"- {fid} · not on this board")
    return "\n".join(lines)


# ── the frame page ───────────────────────────────────────────────────────────────

_COMPONENT_DIR = Path(__file__).resolve().parent / "component"


def _assemble_page() -> str:
    """Inline ``board-cards.css`` + ``board-cards.js`` into ``board-cards.html`` (one GET,
    no extra asset fetches — the same no-build assembly as the board view)."""
    html = (_COMPONENT_DIR / "board-cards.html").read_text(encoding="utf-8")
    css = (_COMPONENT_DIR / "board-cards.css").read_text(encoding="utf-8")
    js = (_COMPONENT_DIR / "board-cards.js").read_text(encoding="utf-8")
    return html.replace("__CARDS_CSS__", css).replace("__CARDS_JS__", js)


CARDS_PAGE = _assemble_page()

"""The new-card form in chat — the ``board-card-form`` frame component.

The card-from-chat flow used to have the model WRITE an HTML form every time
(``show_artifact(kind="html")``): 30–40 s of model output, a look that varied per run, and
bugs that came back with it (a narrow column with labels floating beside their fields; a
``<form>`` submit the sandbox blocks). Now the form is a fixed page this plugin serves, and
the agent only points at it:

* ``board_card_form`` (the tool, in ``__init__._board_tools``) returns one model-facing line
  plus a ``board-card-form`` component-v1 tail. Its props are short labels only — the
  board's project NAMES, a preselected project and two placeholder hints — because props
  persist in chat history.
* ``component/board-card-form.html`` + ``.css`` + ``.js`` (on the PUBLIC
  ``/plugins/project_board`` router, covered by the manifest's ``public_paths``) is the page
  the console frames (ADR 0118 D5). Its **Create card** button is ``type="button"`` (no
  ``<form>`` submit — the frame is sandboxed without ``allow-forms``), validates, and
  PROPOSES one compact message through the frame bridge's ``protoComponent:send``. The
  operator confirms it in the chat's ConfirmChip and it lands as an ordinary user turn, so
  the agent stays in the loop: it creates the card and can ask a follow-up.

The page makes no reads and holds no bearer. Registration is feature-detected: a host
without frame components leaves ``EMIT`` off and the tool tells the model to ask for the
fields in plain chat instead.
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

log = logging.getLogger("protoagent.plugins.project_board")

COMPONENT = "board-card-form"
# Relative to the plugin's PUBLIC route prefix (/plugins/project_board) — covered by the
# manifest's `public_paths: /plugins/project_board/component/`.
FRAME = "component/board-card-form.html"

# The first line of every message the form proposes. The card-from-chat skill keys on it.
MESSAGE_PREFIX = "Create board card · "

MAX_PROJECTS = 30
HINT_MAX = 160
PROJECT_MAX = 128
_PROJECT_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{PROJECT_MAX - 1}}}")
_PROPS = {"projects", "project", "title_hint", "why_hint"}

# Set by register_component(): True only when the host accepted the frame kind.
EMIT = False


def _one_line(s) -> str:
    return " ".join(str(s or "").split())


def validate_props(props: dict) -> str | None:
    """Why ``props`` is not a valid ``board-card-form`` payload, or ``None`` when it is.

    ``{projects?: [≤ 30 project names], project?: a name, title_hint?: str ≤ 160,
    why_hint?: str ≤ 160}`` — short labels only (they persist in chat history). Hints are
    one line. ``project``, when given alongside ``projects``, must be one of them."""
    if not isinstance(props, dict):
        return "props must be an object"
    extra = set(props) - _PROPS
    if extra:
        return f"unexpected board-card-form props: {', '.join(sorted(map(str, extra)))}"
    projects = props.get("projects")
    if projects is not None:
        if not isinstance(projects, list):
            return "board-card-form projects must be a list of project names"
        if len(projects) > MAX_PROJECTS:
            return f"board-card-form projects may name at most {MAX_PROJECTS} projects"
        for p in projects:
            if not isinstance(p, str) or not _PROJECT_RE.fullmatch(p):
                return f"board-card-form project {p!r} is not a project name (letters, digits, - and _)"
        if len(set(projects)) != len(projects):
            return "board-card-form projects must not repeat"
    project = props.get("project")
    if project is not None:
        if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
            return "board-card-form project must be a project name (letters, digits, - and _)"
        if projects and project not in projects:
            return f"board-card-form project {project!r} is not one of the board's projects"
    for key in ("title_hint", "why_hint"):
        v = props.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > HINT_MAX or "\n" in v or "\r" in v):
            return f"board-card-form {key} must be one line of at most {HINT_MAX} chars"
    return None


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_props(
    projects=None, project: str = "", default_project: str = "", title_hint: str = "", why_hint: str = ""
) -> dict:
    """The tool's arguments + the board's projects → the component props.

    ``project`` (what the agent asked for) wins over ``default_project`` when it is one of
    ``projects``; an unknown name is dropped (the picker falls back to the default) rather
    than failing the form. Hints are collapsed to one line and clipped, never refused —
    a long hint must not cost the user the form."""
    names = [str(p).strip() for p in (projects or []) if str(p or "").strip()]
    names = [p for p in dict.fromkeys(names) if _PROJECT_RE.fullmatch(p)][:MAX_PROJECTS]
    props: dict = {}
    if names:
        props["projects"] = names
    want = str(project or "").strip()
    pick = want if want and (want in names or not names) else str(default_project or "").strip()
    if pick and _PROJECT_RE.fullmatch(pick) and (not names or pick in names):
        props["project"] = pick
    for key, val in (("title_hint", title_hint), ("why_hint", why_hint)):
        v = _clip(_one_line(val), HINT_MAX)
        if v:
            props[key] = v
    return props


def _accepts_frame(fn) -> bool:
    """Does the host's ``register_component`` take ``frame=`` (ADR 0118 D5)? True when the
    signature names ``frame`` or accepts ``**kwargs``; anything that can't be introspected
    is treated as NOT accepting it."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind is p.VAR_KEYWORD:
            return True
        if p.name == "frame" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return True
    return False


def register_component(registry) -> bool:
    """Register the ``board-card-form`` frame kind, feature-detected.

    Returns (and records in :data:`EMIT`) whether the host took it. No setup gap of its own:
    a core too old for frame components already gets the ``board_cards`` gap, and the tool
    falls back to asking in plain chat."""
    global EMIT
    EMIT = False
    fn = getattr(registry, "register_component", None)
    if not callable(fn) or not _accepts_frame(fn):
        return False
    try:
        fn(COMPONENT, validate_props, frame=FRAME)
    except TypeError:
        return False
    except Exception:  # noqa: BLE001 — a component must never break registration
        log.exception("[project_board] registering the board-card-form component failed")
        return False
    components = getattr(registry, "components", None)
    if isinstance(components, dict) and COMPONENT not in components:
        log.warning("[project_board] the host refused the board-card-form component")
        return False
    EMIT = True
    return True


def component_tail(props: dict) -> str:
    """``"\\n" + <component-v1 payload>``, or ``""`` when the kind isn't registered with
    this host, the props don't validate, or there is no host encoder."""
    if not EMIT or validate_props(props) is not None:
        return ""
    try:
        from graph.sdk import encode_component
    except Exception:  # noqa: BLE001 — no host seam ⇒ text only
        return ""
    return "\n" + encode_component(COMPONENT, props)


# ── the frame page ───────────────────────────────────────────────────────────────

_COMPONENT_DIR = Path(__file__).resolve().parent / "component"


def _assemble_page() -> str:
    """Inline the form's CSS + JS into its HTML (one GET, no asset fetches)."""
    html = (_COMPONENT_DIR / "board-card-form.html").read_text(encoding="utf-8")
    css = (_COMPONENT_DIR / "board-card-form.css").read_text(encoding="utf-8")
    js = (_COMPONENT_DIR / "board-card-form.js").read_text(encoding="utf-8")
    return html.replace("__FORM_CSS__", css).replace("__FORM_JS__", js)


FORM_PAGE = _assemble_page()

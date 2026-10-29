"""project_board — lean 6-state coding board + ACP spawn loop.

Composition over construction: the board is a **projection over beads** (`br`) — no
separate store of its own (store.py shells the `br` CLI; beads owns the `.beads/*.db`
+ JSONL) — plus an HTTP API and the orchestration loop, which dispatches through the
already-built ``delegates`` plugin (ADR 0024/0025) — it does NOT reimplement the
spawn primitive.

Reach used (no core edits): ``register_router`` (the board API), ``register_surface``
(the background puller), ``register_tool`` (board ops the agent can drive headlessly).

Ships DISABLED. Enable with ``plugins: { enabled: [delegates, project_board] }``,
declare an ``acp`` coder delegate and set ``project_board.coder`` to its name (there is
NO default coder — see setup_check.py), optionally a ``quinn`` (a2a) reviewer. The
Ready gate + single Done edge live in store.py; the teardown + error paths live in
loop.py/worktree.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import subprocess

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

log = logging.getLogger("protoagent.plugins.project_board")

# board_watch (card-from-chat): the plugin event the Board view follows a card on (the bus
# publishes it as `project_board.focus`) and the view `registry.navigate` opens.
FOCUS_EVENT = "focus"
BOARD_VIEW = "board"


def register(registry) -> None:
    # Resolve `project:` against the host's ADR 0095 registry BEFORE anything reads
    # cfg — every consumer below (router, loop, tools) takes cfg from here, so this
    # is the single place the two layers meet. Deliberately OUTSIDE the try/except
    # blocks below: an unresolvable project name must abort registration rather than
    # let the board start building in cwd (see projects.py). The host records the
    # message on the plugin entry and skips us; boot is unaffected.
    from .projects import resolve_project_cfg

    cfg = resolve_project_cfg(registry.config or {})

    # Setup preflight (v0.42.0): can this board run at all — `br` on PATH, `gh` on
    # PATH, every configured coder resolvable in the delegate roster, a bound repo?
    # Computed ONCE here and forwarded to the host's setup-gap seam
    # (`registry.report_setup_gap(key, message)` → operator warnings in the console's
    # runtime status), GUARDED because the seam may not exist on this host yet. The
    # loop re-reports at every tick boundary where a check changes (GapReporter is
    # edge-triggered), and pauses instead of ticking while a blocker stands.
    from .setup_check import GapReporter, setup_status

    # `br` fetched on first run (v0.43.0): no `br` on PATH + `br_autofetch` on (the
    # default) → fetch the pinned beads-rust release for this platform into the
    # instance plugin-data dir, off the event loop, once per process. Returns at once;
    # the loop's setup gate / the /status route see it land (the store is re-pointed in
    # place). A failure is a `br` setup gap with the download error in the hint.
    try:
        from .br_fetch import ensure_br

        ensure_br(cfg, host=getattr(registry, "host", None))
    except Exception:  # noqa: BLE001 — the fetch is best-effort; the preflight reports the gap
        log.exception("[project_board] br auto-fetch could not start")

    gap_reporter = GapReporter(registry)
    try:
        gaps = gap_reporter.report(setup_status(cfg))
        for key, msg in gaps.items():
            if msg:
                log.warning("[project_board] setup gap (%s): %s", key, msg)
    except Exception:  # noqa: BLE001 — the preflight is advisory; registration proceeds
        log.exception("[project_board] setup preflight failed")

    # Board HTTP API + console view, mounted as ONE router under the UNGATED prefix
    # /plugins/project_board (matching the sibling agent-browser/doom plugins, whose
    # browser-loaded views are also ungated). UNGATED because two of these routes are
    # browser navigations that can't attach a bearer: GET /board is loaded as an
    # iframe src, and POST /webhook/pr is hit by GitHub (which can't send a bearer and
    # whose public URL must not change). The board view's GET /board route is folded
    # into this same router so the declared view path (/plugins/project_board/board)
    # is genuinely served. We must NOT register a second router at the same prefix:
    # the host dedupes routers by (plugin_id, prefix), so a second one would be
    # silently dropped → the view 404s.
    # …and the operator CRUD/transition routes ride a SECOND router at the gated
    # /api/plugins/project_board prefix (rule 2) — distinct prefixes, so the host's
    # (plugin_id, prefix) de-dupe keeps both.
    try:
        from .api import build_data_router, build_router

        registry.register_router(build_router(cfg), prefix="/plugins/project_board")
        registry.register_router(build_data_router(cfg, gap_reporter=gap_reporter), prefix="/api/plugins/project_board")
    except Exception:  # noqa: BLE001 — API is best-effort
        log.exception("[project_board] mounting board API + view failed")

    # Background orchestration loop (off unless project_board.loop_enabled).
    try:
        from .loop import BoardLoop

        loop = BoardLoop(cfg, gap_reporter=gap_reporter)
        # `reload=` (ADR 0018): the host fires it on every config reload with the new
        # config, so the concurrency knobs (LIVE_KNOBS — max_concurrent et al.) and
        # project routing edited at runtime land on the RUNNING loop. Without it the
        # surface wires once and onboarding a repo would need a restart.
        registry.register_surface(loop.start, stop=loop.stop, reload=loop.reload, name="project-board-loop")
    except Exception:  # noqa: BLE001 — loop is best-effort; the API still serves
        log.exception("[project_board] registering loop surface failed")

    # Board cards in chat (#522): the `board-cards` frame component board_show_cards emits.
    # Feature-detected — a host without `register_component(..., reads=)` (protoAgent#4143)
    # gets one setup gap and the tool answers in text only.
    try:
        from .board_cards import register_component as _register_board_cards

        _register_board_cards(registry)
    except Exception:  # noqa: BLE001 — chat cards are best-effort; the board runs without them
        log.exception("[project_board] registering the board-cards component failed")
    # The new-card form (card-from-chat): the `board-card-form` frame component
    # board_card_form emits. Feature-detected; without it the tool asks in plain chat.
    try:
        from .board_card_form import register_component as _register_card_form

        _register_card_form(registry)
    except Exception:  # noqa: BLE001 — the form is best-effort; the board runs without it
        log.exception("[project_board] registering the board-card-form component failed")

    # A few board tools so the agent (or A2A) can drive the board headlessly.
    # `registry` rides along for board_watch (the one tool that drives the console: it
    # publishes the Board view's `focus` event, then navigates to the view).
    for t in _board_tools(cfg, registry=registry):
        registry.register_tool(t)

    # Reload-stability (#178): a hot-reload (`reload_plugins`, graph reload) imports
    # a FRESH coder_seam whose live-monitor buffer would otherwise start empty while
    # the previous instance's still-running dispatch loop keeps streaming into its
    # own — the drawer then answers "No live coder run" for a gen that is actively
    # streaming. coder_seam attaches to a process-stable shared buffer at import;
    # going through the hook HERE pins the adoption (and its carried-over warning)
    # to plugin mount time instead of the first progress poll after the reload.
    try:
        from . import coder_seam

        coder_seam.ensure_progress_attached()

        # Persist side of the coder monitor (#226): wire the store accessor so a
        # finished gen's snapshot lands as a `coder-monitor:` bead comment. Scoped to
        # the board's default project — the same base store the board tools' ops use —
        # and lazily resolved (get_store memoizes) so registration stays cheap.
        from .projects import default_project as _resolve_default_project
        from .projects import resolve_projects as _resolve_projects
        from .projects import store_db_path as _store_db_path
        from .store import get_store

        _persist_store_kw = dict(
            # D3 (#260): the db is resolved at the config seam — a blank db_path rides
            # the ONE instance-default store, an explicit path stays the operator pin.
            db=_store_db_path(cfg),
            repo=cfg.get("repo", "."),
            base_branch=cfg.get("base_branch", "main"),
            max_files_by_difficulty=cfg.get("max_files_by_difficulty"),
            projects=_resolve_projects(cfg),
            default_project=_resolve_default_project(cfg),
        )
        coder_seam.set_store_factory(lambda: get_store(**_persist_store_kw))
    except Exception:  # noqa: BLE001 — monitoring must never break registration
        log.exception("[project_board] attaching the coder-monitor buffer failed")

    # Planning layer (D9): the decompose/plan_antagonist subagents + the orchestration
    # skill that turns an idea into the docs tree + the board (per-epic human gate).
    try:
        from .subagents import ANTAGONIST_CONFIG, DECOMPOSE_CONFIG

        registry.register_subagent(DECOMPOSE_CONFIG)
        registry.register_subagent(ANTAGONIST_CONFIG)
        registry.register_skill_dir("skills")
    except Exception:  # noqa: BLE001 — planning layer is best-effort
        log.exception("[project_board] registering planning subagents/skill failed")

    # The board's open cards in the AGENT'S OWN working state (protoAgent ADR 0079).
    # Feature-detected: this seam is newer than the plugin's `min_protoagent_version`
    # floor, so on an older host it is simply absent and the board contributes nothing —
    # the same posture `coder_seam.resolve_self_invoke` takes for `HOST.invoke`. MERGED is
    # not RELEASED; never assume a host seam exists because it exists upstream.
    from . import work_snapshot

    register_work_provider = getattr(registry, "register_work_provider", None)
    if callable(register_work_provider):
        register_work_provider("cards", work_snapshot.provider, label="Open board cards")
    else:
        log.debug("[project_board] host has no register_work_provider seam — board work not surfaced")

    log.info(
        "[project_board] registered board API + loop + tools + planning (coder=%s reviewer=%s)",
        # No default coder (v0.42.0): an unset `coder` logs as <unset> and is a
        # preflight gap above — never a phantom delegate name.
        str(cfg.get("coder") or "").strip() or "<unset>",
        cfg.get("reviewer", "quinn"),
    )


# Board states where a feature is DONE and can't be a live duplicate — a new
# creation of the same title is legitimately re-doing closed work. Everything else
# (backlog/ready/in_progress/in_review/blocked) is "open" and a same-title create
# would stack (mirrors portfolio-plugin's _TERMINAL_LANES/#25 dedup precedent — this
# is the same class of bug one layer down: an agent's OWN reasoning calling
# board_create_feature twice for one task, not just a PM re-dispatching).
_TERMINAL_STATES = {"done", "cancelled"}


def _norm_title(t: str) -> str:
    """Normalize a feature title for duplicate comparison: trimmed, lowercased,
    internal whitespace collapsed. Exact-after-normalize only — no fuzzy match (a
    false positive silently drops a real task, worse than an occasional missed
    near-dup)."""
    return " ".join(str(t or "").strip().lower().split())


def _open_duplicate(features: list, title: str, depends_on: list | None = None) -> dict | None:
    """The first board feature this new card would DUPLICATE, or None. Two patterns,
    checked in order:

    1. TITLE — the first OPEN feature whose title matches ``title`` (normalized). A
       board's own agent (onboarding, or its own reasoning about a dispatched task) can
       call board_create_feature more than once for the same piece of work within a
       single turn; this catches it at the tool boundary, same as portfolio_dispatch's
       dedup guards the PM's re-dispatch one tier up.

    2. DEPENDS_ON — a card whose ``depends_on`` names a feature that is ``in_review``
       with an open ``pr_url``: the "work based on work that hasn't landed" pattern.
       Title matching can't see it (the new card has a legitimately different title), so
       it slips past pattern 1 and stacks a second card subordinate to in-flight work.
       The matched blocker is returned tagged ``_dup_reason="depends_on_in_review"`` so
       the caller can steer the author to board_requeue_feature (a fix round belongs on
       the in-flight feature's OWN PR, not a new card).

    ``features`` is the live board projection; ``depends_on`` is the new card's declared
    blocker ids."""
    features = features or []
    want = _norm_title(title)
    if want:
        for f in features:
            if str(f.get("board_state", "")).lower() in _TERMINAL_STATES:
                continue
            if _norm_title(f.get("title", "")) == want:
                return f
    by_id = {str(f.get("id", "")): f for f in features}
    for dep in depends_on or []:
        blocker = by_id.get(str(dep).strip())
        if blocker is None:
            continue
        if str(blocker.get("board_state", "")).lower() == "in_review" and str(blocker.get("pr_url", "")).strip():
            return {**blocker, "_dup_reason": "depends_on_in_review"}
    return None


def _pr_references_issue(pr: dict, n: int, slug: str) -> bool:
    """True when the PR's title/body mechanically references issue ``n`` — either the
    ``#N`` shorthand (word-bounded, so ``#12`` never matches inside ``#123``) or the
    full issue URL for ``slug`` (``owner/repo``). ANY mention counts, not just closing
    keywords: a PR that says ``see #12`` is still in-flight work on that issue and a
    new card would duplicate it."""
    text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
    if re.search(rf"#{n}\b", text):
        return True
    return bool(re.search(rf"github\.com/{re.escape(slug)}/issues/{n}\b", text, re.IGNORECASE))


def _find_open_pr_for_issue(source_issue: str) -> dict | None:
    """The first OPEN PR in the source issue's repo whose title/body references the
    issue number, or None — the PR-side dedup for board_create_feature (#174): a card
    created for an issue that already has an open PR dispatches a coder against work
    that's already in flight.

    BEST-EFFORT, fail-open (same posture as the title dedup's store-read failure): a
    gh failure of any kind — missing binary, network, non-zero exit, bad JSON — logs
    a warning and returns None rather than blocking creation. An unnormalizable
    ``source_issue`` (e.g. a bare ``#N`` with no repo to search) also returns None;
    create_feature rejects it with its own named error right after."""
    from .store import BoardError, normalize_source_issue

    try:
        try:
            src = normalize_source_issue(source_issue)
        except BoardError:
            return None  # no owner/repo to search; create_feature raises the named error
        slug, _, n_str = src.rpartition("#")
        n = int(n_str)
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--state",
                "open",
                "--json",
                "number,title,body,url",
                "--limit",
                "100",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            log.warning(
                "[project_board] PR-dedup check for %s failed (gh exit %s: %s) — proceeding without it",
                src,
                proc.returncode,
                (proc.stderr or "").strip(),
            )
            return None
        for pr in json.loads(proc.stdout or "[]"):
            if _pr_references_issue(pr, n, slug):
                return pr
    except Exception:  # noqa: BLE001 — best-effort: never block creation on a gh/parse failure
        log.warning(
            "[project_board] PR-dedup check for %r errored — proceeding without it", source_issue, exc_info=True
        )
    return None


def _strip_wrapping_quotes(s: str) -> str:
    """Peel ONE symmetric layer of literal wrapping double quotes off a string arg.

    Agents (and shells) sometimes hand us a value that arrives already wrapped in a
    literal pair of double quotes — an outer pair that is part of the value, not a
    delimiter — which, stored verbatim, then renders quoted in every downstream view
    and can even defeat the title dedup. Strip exactly one balanced outer layer (BOTH
    ends must be a double-quote char); inner or lopsided quotes are left untouched, and
    a non-string passes straight through."""
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _split_list(raw: str) -> list[str]:
    """Comma- or newline-separated string → clean list (the shared normalization for
    files_to_modify and depends_on across create/update — round-4 DRY finding on #88)."""
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


def _feature_reply(f: dict, ready_check: dict | None = None) -> str:
    """Serialize a store feature for a tool return — carrying the success-with-warning
    trio through the boundary when set (QA panel on #88: stripping it hides the repair
    contract from the agent), the hot-file edges the create added (#458), and the Ready
    gate's dry run (#455) when the caller ran one."""
    out = {"id": f["id"], "state": f["board_state"], "title": f["title"]}
    if f.get("enrichment_failed"):
        out["enrichment_failed"] = True
        out["missing_fields"] = f.get("missing_fields", [])
        out["warning"] = f.get("warning", "")
    if f.get("hot_file_chain"):
        out["hot_file_chain"] = f["hot_file_chain"]
    if ready_check is not None:
        out["ready_check"] = ready_check
    return json.dumps(out)


def _ready_dry_run(store, fid: str) -> dict:
    """The Ready gate's dry run for a card just created or updated (#455): every check
    ``board_mark_ready`` would refuse on, ALL of them, each with its fix — so the author
    fixes them in one pass now instead of meeting them one at a time at mark_ready. The
    card is written either way. Compact: ``ok``, then only what needs acting on. Never
    raises — a read failure says so instead of failing the create that already landed."""
    try:
        res = store.ready_check(fid)
    except Exception as exc:  # noqa: BLE001 — the card exists; the dry run is advice
        return {"ok": None, "error": f"could not dry-run the Ready gate: {exc}"}
    out: dict = {"ok": res["ok"]}
    if res["refusals"]:
        out["will_be_refused_at_ready"] = [f"{r['message']} Fix: {r['fix']}" for r in res["refusals"]]
        out["summary"] = (
            f"will be refused at ready: {', '.join(r['gate'] for r in res['refusals'])} — the card was "
            "saved; fix each item (board_update_feature) before board_mark_ready."
        )
    if res.get("advisories"):
        out["advisories"] = [f"{a['message']} Fix: {a['fix']}" for a in res["advisories"]]
    if res.get("suggested_edges"):
        out["suggested_edges"] = res["suggested_edges"]
    breadth = res.get("breadth") or {}
    if breadth.get("excluded"):
        out["breadth"] = breadth
    if res.get("note"):
        out["note"] = res["note"]
    return out


def _card_resolver(store):
    """card id → projected card (or None) for a `contains:` gate anchored on a card."""

    def _resolve(fid):
        try:
            return store.get_feature(fid)
        except Exception:  # noqa: BLE001 — an unreadable card is "not found", never a crash
            return None

    return _resolve


def _waits_for_refusal(waits_for) -> str | None:
    """``Error: …`` when a `waits_for` spec parses but the REMOTE says it cannot mean what
    the author thinks — today a bare `release:` range on a repo that tags per package
    (changesets), which any package's version would satisfy. Best-effort: an unreadable
    remote is not a refusal (evaluation re-detects the case on the card). A spec that
    does not parse is left for the store to refuse with its own message."""
    from . import gates as publish_gates

    try:
        specs = publish_gates.parse_specs(waits_for)
    except publish_gates.GateSpecError:
        return None
    for spec in specs:
        why = publish_gates.remote_refusal(spec)
        if why:
            return f"Error: waits_for spec {spec.raw!r}: {why}"
    return None


def _dedup_skip_message(store, title: str, deps: list, source_issue: str) -> str | None:
    """The ``Skipped — …`` message a create verb returns when a new card would DUPLICATE
    existing/in-flight work, or None when it's clear to create — the ONE dedup contract
    board_create_feature and board_create_task (#217) share, so the two verbs can never
    drift apart. Runs the same three checks in order:

    1. an OPEN same-title card already on the board (the mid-turn double-create);
    2. a `depends_on` that names a feature already `in_review` with an open PR (work built
       on in-flight work — a fix round that belongs on THAT feature's PR, steered to
       board_requeue_feature);
    3. an open PR in `source_issue`'s repo that already references the issue.

    BEST-EFFORT: a store read (or the gh probe inside `_find_open_pr_for_issue`) failing
    never blocks creation — a possible dup beats a stuck board."""
    from .store import BoardError, BoardTimeout

    try:
        existing = store.list_features()
    except BoardTimeout:
        # A STALL is not a read failure to shrug off (#404). The store is wedged, and the
        # likeliest reason a caller is creating again is that its last create timed out —
        # which may well have committed. Creating blind now is how that becomes a duplicate.
        raise
    except BoardError:
        existing = []  # can't check → don't block creation on a read failure
    dup = _open_duplicate(existing, title, deps)
    if dup is not None:
        if dup.get("_dup_reason") == "depends_on_in_review":
            # The new card builds on an in-flight feature (in_review, open PR). That's a fix
            # round, and a fix round belongs on the in-flight feature's OWN PR — point the
            # author at the requeue verb (#112), not a second card the loop would churn on.
            return (
                f"Skipped — this card's depends_on names {dup.get('id', '?')}, which is "
                f"already in_review with an open PR ({dup.get('pr_url', '')}). Work built "
                "on an in-flight feature belongs on THAT feature's PR, not a fresh card: "
                f"call board_requeue_feature({dup.get('id', '?')!r}, findings=...) to carry "
                "your changes onto the same branch. Pass force=true to stack a new card anyway."
            )
        return (
            f"Skipped — a feature titled {title!r} is already open on this board "
            f"({dup.get('id', '?')}, {dup.get('board_state', 'open')}). It's likely "
            "the same work; re-check the board before creating again, or pass "
            "force=true to create a second copy anyway."
        )
    # PR-side dedup (#174): an open PR that already references the source issue means the
    # work is IN FLIGHT — a new card would dispatch a second coder at it. Mechanical,
    # best-effort: a gh failure logs a warning inside the helper and never blocks creation.
    if str(source_issue or "").strip():
        pr = _find_open_pr_for_issue(source_issue)
        if pr is not None:
            return (
                f"Skipped — open PR #{pr.get('number', '?')} ({pr.get('url', '')}) already "
                f"references issue {source_issue}: that work is in flight. Review or "
                "contribute to the existing PR instead of creating a duplicate card. "
                "Pass force=true to create anyway."
            )
    return None


def _board_tools(cfg: dict, registry=None):
    from .failures import PREFLIGHT_HOLD_CLASS
    from .projects import default_project as resolve_default_project
    from .projects import rehome_hint, resolve_projects, store_db_path, unresolved_project
    from .store import (
        AGENT_HOLD_CLASS,
        MANUAL_BLOCK_CLASS,
        BoardError,
        annotate_next_action,
        escalation_enabled,
        get_store,
        open_requirement_ids,
        open_requirements_note,
    )

    # Per-project resolution (#90 slice 3): the board's `projects:` map (name →
    # execution settings) + the default project a create falls back to. Threaded into
    # store_kw so the resolved store carries the SAME map the loop does — its own
    # per-feature reads (the Ready gate's `_repo_for`) then resolve to the same repo.
    projects = resolve_projects(cfg)
    default_proj = resolve_default_project(cfg)

    store_kw = dict(
        # D3 (#260): the db is resolved at the config seam — an unset/blank db_path
        # rides the ONE instance-default store (store.default_db_path), an explicit
        # path stays the operator pin. Every per-project store below (`_store_kw_for`)
        # shares this db while keying its own repo/base_branch.
        db=store_db_path(cfg),
        repo=cfg.get("repo", "."),
        base_branch=cfg.get("base_branch", "main"),
        max_files_by_difficulty=cfg.get("max_files_by_difficulty"),
        projects=projects,
        default_project=default_proj,
    )

    def _store_kw_for(project: str = "") -> dict:
        """store_kw whose `repo`/`base_branch` resolve to ``project``'s entry in the
        board's `projects:` map (#90) — so a project-scoped tool op (create/list) targets
        that project's checkout, not the instance default. An empty or unknown name falls
        back to the base (default-project) repo; the shared `projects`/`default_project`
        ride along unchanged so the resolved store keeps the same per-feature resolution
        the loop uses. Every project store shares the one resolved db (the instance
        default, or the operator's explicit db_path pin — D3, #260) while carrying its
        own repo for the Ready gate's path validation."""
        name = str(project or "").strip() or default_proj
        entry = projects.get(name) or {}
        kw = dict(store_kw)
        repo = str(entry.get("repo") or "").strip()
        if repo:
            kw["repo"] = repo
        base = str(entry.get("base_branch") or "").strip()
        if base:
            kw["base_branch"] = base
        return kw

    @tool
    def board_create_epic(title: str, description: str = "") -> str:
        """Create a top-level epic (a container for milestones/features)."""
        try:
            e = get_store(**store_kw).create_epic(title, description)
            return json.dumps({"id": e["id"], "title": e["title"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_create_feature(
        title: str,
        spec: str = "",
        acceptance_criteria: str = "",
        files_to_modify: str = "",
        design: str = "",
        parent: str = "",
        priority: int = 2,
        difficulty: str = "",
        depends_on: str = "",
        foundation: bool = False,
        force: bool = False,
        source_issue: str = "",
        project: str = "",
        waits_for: str = "",
    ) -> str:
        """Create a coding feature (starts in backlog). Ready needs a self-sufficient `spec`,
        testable `acceptance_criteria` and `files_to_modify`: comma-separated paths that exist in
        the project's repo, with `(new)` after any file this card creates (`src/a.js,
        .changeset/add-accent.md (new)`). `parent`: epic/milestone id. `difficulty`:
        small|medium|large (model tier). `depends_on`: comma-separated blocker ids on THIS board.
        `foundation=True`: dependents gate on its merge, not its review. `source_issue`: the
        originating GitHub issue (URL or `owner/repo#N`); its PR gets `Fixes #N`. `project`: the
        `projects:` entry it builds in (default_project if omitted). `waits_for`: comma-separated
        publish gates that must all hold before the card is claimed, for work needing something
        another repo SHIPS (npm:/release:/pr: specs); for a consumer of another card's change use
        `npm:<pkg>@contains:<owner>/<repo>@<card-id>`, never a version floor.

        The reply's `ready_check` dry-runs the Ready gate; fix what it lists before
        board_mark_ready. Refuses a likely duplicate: a same-title open card, a `depends_on` onto
        an in_review card with an open PR (that is a fix round: board_requeue_feature(fid,
        findings=...)), or a `source_issue` an open PR already references. `force=true` bypasses.
        Breadth caps, shared files, gate grammar: board-tool-reference skill."""
        try:
            # Resolve the store against THIS feature's project (#90): its repo/base_branch
            # for the get_store call, so the dedup read + the create land against the right
            # checkout (with a shared db_path, the same board DB but the project's repo for
            # the Ready gate). Strip the project's wrapping quotes FIRST so the resolution
            # (and the store's own validation) sees the bare name.
            project = _strip_wrapping_quotes(project)
            store = get_store(**_store_kw_for(project))
            # Input hygiene: an agent (or a shell one layer up) sometimes hands us a
            # value already wrapped in a literal pair of double quotes, which — stored
            # verbatim — renders quoted in every downstream view and even defeats the
            # title dedup below. Peel one symmetric outer layer off each string field
            # BEFORE anything is compared or stored.
            title = _strip_wrapping_quotes(title)
            spec = _strip_wrapping_quotes(spec)
            acceptance_criteria = _strip_wrapping_quotes(acceptance_criteria)
            files_to_modify = _strip_wrapping_quotes(files_to_modify)
            design = _strip_wrapping_quotes(design)
            parent = _strip_wrapping_quotes(parent)
            difficulty = _strip_wrapping_quotes(difficulty)
            depends_on = _strip_wrapping_quotes(depends_on)
            source_issue = _strip_wrapping_quotes(source_issue)
            waits_for = _strip_wrapping_quotes(waits_for)
            if (refused := _waits_for_refusal(waits_for)) is not None:
                return refused
            deps = _split_list(depends_on)
            files = _split_list(files_to_modify)
            if not force:
                skip = _dedup_skip_message(store, title, deps, source_issue)
                if skip is not None:
                    return skip
            f = store.create_feature(
                title,
                spec=spec,
                acceptance_criteria=acceptance_criteria,
                design=design,
                files_to_modify=files,
                parent=parent,
                priority=priority,
                difficulty=difficulty,
                depends_on=deps,
                foundation=foundation,
                source_issue=source_issue,
                project=project,
                waits_for=waits_for,
            )
            return _feature_reply(f, _ready_dry_run(store, f["id"]))
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_create_task(
        title: str,
        spec: str = "",
        acceptance_criteria: str = "",
        assignee: str = "",
        priority: int = 2,
        parent: str = "",
        depends_on: str = "",
        project: str = "",
        source_issue: str = "",
        force: bool = False,
        waits_for: str = "",
        execute: bool = False,
    ) -> str:
        """Create a TASK (starts in backlog): work that ships a deliverable (doc, decision, artifact
        ref) instead of a PR. Same lifecycle as a feature, but board_deliver moves it to in_review
        and board_verify closes it. Ready needs `spec` + `acceptance_criteria`, no
        `files_to_modify`. `assignee` pre-assigns; `priority` 0 = highest. The other args, the
        dedup refusals and `force` behave as on board_create_feature.
        Execution tasks (evals, live checks, release verification: anything that runs commands)
        go to a coder: pass `execute=true` and leave `assignee` empty; never assign one to yourself.
        Writing-only tasks (a doc, a decision) may be self-assigned."""
        try:
            project = _strip_wrapping_quotes(project)
            store = get_store(**_store_kw_for(project))
            title = _strip_wrapping_quotes(title)
            spec = _strip_wrapping_quotes(spec)
            acceptance_criteria = _strip_wrapping_quotes(acceptance_criteria)
            assignee = _strip_wrapping_quotes(assignee)
            parent = _strip_wrapping_quotes(parent)
            depends_on = _strip_wrapping_quotes(depends_on)
            source_issue = _strip_wrapping_quotes(source_issue)
            waits_for = _strip_wrapping_quotes(waits_for)
            if (refused := _waits_for_refusal(waits_for)) is not None:
                return refused
            deps = _split_list(depends_on)
            if not force:
                skip = _dedup_skip_message(store, title, deps, source_issue)
                if skip is not None:
                    return skip
            f = store.create_feature(
                title,
                spec=spec,
                acceptance_criteria=acceptance_criteria,
                parent=parent,
                priority=priority,
                depends_on=deps,
                source_issue=source_issue,
                project=project,
                issue_type="task",
                assignee=assignee,
                waits_for=waits_for,
                execute=execute,
            )
            return _feature_reply(f, _ready_dry_run(store, f["id"]))
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_update_feature(
        feature_id: str,
        title: str = "",
        spec: str = "",
        acceptance_criteria: str = "",
        files_to_modify: str = "",
        design: str = "",
        difficulty: str = "",
        depends_on: str = "",
        foundation: bool = False,
        source_issue: str = "",
        priority: int | None = None,
        waits_for: str = "",
        project: str = "",
    ) -> str:
        """Partially update a feature: the repair path for a card the Ready gate refused (no need to
        cancel and recreate). Only non-empty args are written. `files_to_modify` REPLACES the list
        (mark created files `(new)`, e.g. `.changeset/x.md (new)`). `depends_on` ADDS edges
        (board_remove_dependency removes them); `foundation=True` sets the flag, never clears it.
        `source_issue` / `waits_for` replace the value; `none` clears it. `priority` None leaves
        it. `project` re-homes the card, allowed only while backlog/ready with no PR and never
        dispatched. The reply carries the same `ready_check` dry run as board_create_feature.
        Details: board-tool-reference skill."""
        try:
            store = get_store(**store_kw)
            title = _strip_wrapping_quotes(title)
            spec = _strip_wrapping_quotes(spec)
            acceptance_criteria = _strip_wrapping_quotes(acceptance_criteria)
            files_to_modify = _strip_wrapping_quotes(files_to_modify)
            design = _strip_wrapping_quotes(design)
            difficulty = _strip_wrapping_quotes(difficulty)
            depends_on = _strip_wrapping_quotes(depends_on)
            source_issue = _strip_wrapping_quotes(source_issue)
            files = _split_list(files_to_modify)
            deps = _split_list(depends_on)
            update_kw = dict(
                # strip BEFORE the truthiness check (the difficulty convention below) so a
                # whitespace-only title is a no-op (None), never a "set the title" signal.
                title=title.strip() or None,
                spec=spec or None,
                acceptance_criteria=acceptance_criteria or None,
                design=design or None,
                files_to_modify=files or None,
                # strip BEFORE the truthiness check so a whitespace-only difficulty is a
                # no-op (None), never a `"   "` that reaches the store as a "set it" signal.
                difficulty=difficulty.strip() or None,
                depends_on=deps or None,
                foundation=foundation or None,
                source_issue=source_issue.strip() or None,
            )
            if priority is not None:
                update_kw["priority"] = priority
            project = _strip_wrapping_quotes(project).strip()
            if project:
                update_kw["project"] = project
            waits_for = _strip_wrapping_quotes(waits_for).strip()
            if waits_for:
                clear = waits_for.lower() in ("none", "clear", "-")
                if not clear and (refused := _waits_for_refusal(waits_for)) is not None:
                    return refused
                update_kw["waits_for"] = [] if clear else waits_for
            f = store.update_feature(feature_id, **update_kw)
            return _feature_reply(f, _ready_dry_run(store, f["id"]))
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_remove_dependency(feature_id: str, depends_on: str) -> str:
        """Remove blocker edges from a feature, the inverse of board_update_feature(depends_on=…):
        e.g. a wrong edge from a mis-decomposed chain. `depends_on`: comma-separated blocker ids.
        Any unknown id returns an Error and removes nothing; a real card that isn't a blocker is
        listed in `not_found`. Returns `{id, removed, not_found, open_depends_on}` and records an
        audit comment."""
        try:
            store = get_store(**store_kw)
            feature_id = _strip_wrapping_quotes(feature_id).strip()
            depends_on = _strip_wrapping_quotes(depends_on)
            blockers = _split_list(depends_on)
            f = store.get_feature(feature_id)
            if f is None:
                return f"Error: unknown feature {feature_id!r}"
            # Validate EVERY named blocker exists before tearing down anything, so an
            # unknown id aborts the whole call (nothing removed) rather than leaving some
            # edges dropped and some not — the all-or-nothing contract the AC pins.
            for b in blockers:
                if store.get_feature(b) is None:
                    return f"Error: unknown feature {b!r}"
            existing = set(f.get("depends_on") or [])
            removed: list[str] = []
            not_found: list[str] = []
            for b in blockers:
                # Guard against a repeated blocker in the input: once removed, a second
                # occurrence is "no such edge", not a double tear-down that would raise.
                if b in existing:
                    store.remove_dependency(feature_id, b)
                    existing.discard(b)
                    removed.append(b)
                else:
                    not_found.append(b)
            if removed:
                store.comment(feature_id, "removed dependency edges: " + ", ".join(removed))
            after = store.get_feature(feature_id) or {}
            return json.dumps(
                {
                    "id": feature_id,
                    "removed": removed,
                    "not_found": not_found,
                    "open_depends_on": after.get("open_depends_on", []),
                }
            )
        except BoardError as exc:
            return f"Error: {exc}"

    def _preflight_failure_field(f: dict) -> dict:
        """``{"preflight_failure": …}`` for a card held by its project's gate preflight,
        from what the loop last published; ``{}`` otherwise (or when nothing is published)."""
        if not f.get("blocked") or f.get("blocked_class") != PREFLIGHT_HOLD_CLASS:
            return {}
        from . import health

        detail = health.preflight_failure(str(f.get("project") or "").strip() or default_proj)
        return {"preflight_failure": detail} if detail else {}

    @tool
    def board_get_feature(feature_id: str) -> str:
        """Read one card's full detail as JSON: spec, acceptance_criteria, state, labels, pr_url,
        merge_hold, files_to_modify, project, `requirements` (the ledger from the acceptance
        criteria; a PR is refused while any item is open), `depends_on` (every edge) vs
        `open_depends_on` (still blocking), `waits_for` with cached `gates` verdicts,
        `next_action`, and `blocked_reason`/`blocked_class` when blocked. A `preflight-hold` card
        carries `preflight_failure`: the project's gate failing on its clean base, an environment
        fault to report to the operator (the loop lifts the hold itself). A task also carries
        `deliverable`, `delivered_by`, `open_requirements`. The read half of read-modify-write
        before board_update_feature. Field reference: board-tool-reference skill."""
        try:
            f = get_store(**store_kw).get_feature(feature_id)
        except BoardError as exc:
            return f"Error: {exc}"
        if f is None:
            return f"Error: unknown feature {feature_id!r}"
        annotate_next_action([f], cfg)  # labels + config + cached gate verdicts, no network
        return json.dumps(
            {
                "id": f["id"],
                "title": f["title"],
                "spec": f["spec"],
                "acceptance_criteria": f["acceptance_criteria"],
                "design": f["design"],
                "state": f["board_state"],
                "labels": f.get("labels", []),
                "pr_url": f["pr_url"],
                # The operator's per-card auto-merge veto (#532): true when the card
                # carries `merge-hold`, so a reader can see the hold without re-decoding
                # `labels`. board_hold_merge sets it, board_release_merge_hold lifts it.
                "merge_hold": f.get("merge_hold", False),
                "difficulty": f["difficulty"],
                "files_to_modify": f.get("files_to_modify", []),
                "foundation": f.get("foundation", False),
                "priority": f.get("priority", 2),
                "source_issue": f.get("source_issue", ""),
                "requirements": f.get("requirements", []),
                "work_locations": f.get("work_locations", {}),
                **({"recovery_context": f["recovery_context"]} if f.get("recovery_context") else {}),
                "depends_on": f.get("depends_on", []),
                "open_depends_on": f.get("open_depends_on", []),
                # Which project (#90) this feature builds in — "" for a pre-#90 feature
                # or a single-repo board with no `projects:` map.
                "project": f.get("project", ""),
                "waits_for": f.get("waits_for", []),
                **({"gates": f.get("gates", [])} if f.get("waits_for") else {}),
                **(
                    {"next_action": f["next_action"], "next_action_hint": f.get("next_action_hint", "")}
                    if f.get("next_action")
                    else {}
                ),
                # Why it is blocked. A preflight hold also carries what the project's gate
                # printed: the agent has no shell and can't read the daemon log, so this
                # read (and the hold's card comment) is where it learns what broke.
                **(
                    {"blocked_reason": f.get("blocked_reason", ""), "blocked_class": f.get("blocked_class", "")}
                    if f.get("blocked")
                    else {}
                ),
                **_preflight_failure_field(f),
                # A task's work product (#399). Without it this "FULL detail" read showed a
                # delivered task with no deliverable at all, and the PM agent reading it
                # concluded the delivery was empty and set about "repairing" a card whose
                # 4,989-char record was intact on the bead. `open_requirements` is the
                # ledger's still-open ids, named so a verifier need not scan `requirements`.
                **(
                    {
                        "deliverable": f.get("deliverable", ""),
                        "delivered_by": f.get("delivered_by", ""),
                        "open_requirements": open_requirement_ids(f.get("requirements")),
                    }
                    if f.get("issue_type") == "task"
                    else {}
                ),
            }
        )

    @tool
    def board_record_work_location(feature_id: str, entry: dict) -> str:
        """Record external work. entry requires stable id, path and purpose; optional
        repo, branch, sha, remote_ref and session_id are caller-reported references.
        pushed is true/false/null (unknown); omitted git fields are empty for evidence
        directories. Updates supply complete state with the same id and path.
        Actor and timestamp are stamped by the store; identical retries add no history.
        Paths never authorize filesystem access or pushing. No git probes run here.
        """
        from . import work_locations

        try:
            feature_id = work_locations.feature_id(feature_id)
            entry = work_locations.validate(entry)
            return json.dumps(get_store(**store_kw).record_work_location(feature_id, entry))
        except (BoardError, ValueError, TypeError) as exc:
            return f"Error: {exc}"

    @tool
    def board_list_work_locations(feature_id: str) -> str:
        """Read a card's durable external-work ledger, including history and parse errors."""
        from .work_locations import feature_id as validate_id

        try:
            feature_id = validate_id(feature_id)
            return json.dumps(get_store(**store_kw).list_work_locations(feature_id))
        except (BoardError, ValueError, TypeError) as exc:
            return f"Error: {exc}"

    @tool
    def board_comments(feature_id: str, page_size: int = 50, offset: int = 0) -> str:
        """Read one feature's comment history as paginated JSON, oldest first. `page_size` is 1..100;
        follow `next_offset` while `has_more`."""
        try:
            feature_id = _strip_wrapping_quotes(feature_id).strip()
            page_size = int(page_size)
            offset = int(offset)
            if not feature_id:
                return "Error: feature_id is required"
            if page_size < 1 or page_size > 100:
                return "Error: page_size must be between 1 and 100"
            if offset < 0:
                return "Error: offset must be non-negative"
            comments = get_store(**store_kw).feature_comments(feature_id, records=True, unknown_ok=False)
        except (TypeError, ValueError):
            return "Error: page_size and offset must be integers"
        except BoardError as exc:
            return f"Error: {exc}"
        page = comments[offset : offset + page_size]
        next_offset = offset + len(page)
        has_more = next_offset < len(comments)
        return json.dumps(
            {
                "feature_id": feature_id,
                "offset": offset,
                "page_size": page_size,
                "count": len(page),
                "total": len(comments),
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "comments": page,
            }
        )

    @tool
    def board_mark_ready(feature_id: str) -> str:
        """Promote a feature backlog → ready. The Ready gate refuses it, naming EVERY
        failed check at once with its fix: a missing spec / acceptance_criteria /
        files_to_modify, a path that does not exist and is not marked `(new)`, more counted
        files than the breadth cap, a large card with no ADR-citing design, or a file
        another open card names with no depends_on path between them. Only `ready`
        features are pulled. board_check_ready asks the same question without promoting."""
        try:
            f = get_store(**store_kw).mark_ready(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_check_ready(feature_id: str) -> str:
        """Report what board_mark_ready would say about a card now, changing nothing. Returns `{id,
        state, ok, refusals, advisories, breadth, unserialised, suggested_edges}`; each refusal is
        `{gate, message, fix}`, and `suggested_edges` would serialise open cards that share a file.
        Use it after a batch of edits or before marking a set ready."""
        try:
            return json.dumps(get_store(**store_kw).ready_check(feature_id))
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_mark_designing(feature_id: str, note: str = "") -> str:
        """Park a backlog (or ready) feature ON PURPOSE while its design is worked out —
        the DESIGNING state (#406). A parked card is not "stranded": the board stops naming
        it `dependencies closed — promote` once its dependencies close, and the puller never
        claims it. `note` says why (recorded on the card). Unpark it with board_mark_ready,
        which runs the Ready gate as usual."""
        try:
            f = get_store(**store_kw).mark_designing(feature_id, _strip_wrapping_quotes(note))
            return json.dumps({"id": f["id"], "state": f["board_state"], "designing": True})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_cancel_feature(feature_id: str, reason: str = "") -> str:
        """Cancel a card created in error (bad decomposition, duplicate, scope cut). Closes it in a
        distinct `cancelled` state (not `done`) with an auditable `reason`; the bead and its
        history remain. Also closes its open PR and stops its in-flight coder, best-effort (see
        `pr_closed` / `drive_cancelled` in the reply). For genuinely shipped work use
        board_mark_done."""
        try:
            reason = _strip_wrapping_quotes(reason)
            store = get_store(**store_kw)
            before = store.get_feature(feature_id)
            pr_url = str((before or {}).get("pr_url") or "").strip()
            f = store.cancel_feature(feature_id, reason)
        except BoardError as exc:
            return f"Error: {exc}"
        # Lazy import (the loop imports from here — a top-level import would be circular).
        from .api import repo_for_feature
        from .loop import cancel_side_effects

        # #262: gh closes the PR in the FEATURE's project repo (its `project:<name>`
        # label, via the shared route/tool resolver), not the board default's.
        side = cancel_side_effects(feature_id, pr_url, cwd=repo_for_feature(before or f, store_kw))
        return json.dumps({"id": f["id"], "state": f["board_state"], **side})

    @tool
    def board_mark_done(feature_id: str, reason: str = "") -> str:
        """Mark an in-flight card (in_progress / in_review / blocked) done by hand, for work that
        shipped outside the board's PR lifecycle. Closes it so its dependents unblock; `reason` is
        the audit comment. Refused on a backlog/ready or already-terminal card. To retire a bad
        card use board_cancel_feature."""
        try:
            reason = _strip_wrapping_quotes(reason)
            f = get_store(**store_kw).mark_done(feature_id, reason=reason)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    async def board_attach_pr(feature_id: str, pr_url: str, reason: str = "") -> str:
        """Attach an open PR the board did NOT open (hand-pushed recovery, a fix made outside the
        loop) to its existing coding card. The card moves to in_review and the normal reconcile
        (CI, review gate, merge) takes over: no duplicate card, no re-dispatch.

        Refused, changing nothing, unless the PR is OPEN, in the card's project repo (not a fork),
        on the card's branch `feat/<id>-<slug>` and targeting the project's base; AND the card is
        a coding feature that passed the Ready gate, is ready / in_progress / blocked, with no PR
        in review, no open dependency and no live drive. A MERGED PR is refused: use
        board_mark_done. The attached code skipped the board's pre-PR checks; CI and the review
        gate still apply. `reason` goes in the audit comment. Edge cases: board-tool-reference skill."""
        # Lazy imports: the loop imports from here, so a top-level import would cycle.
        from .api import base_branch_for_feature, repo_for_feature
        from .loop import attach_external_pr
        from .store import knob_bool

        try:
            store = get_store(**store_kw)
            feature = await asyncio.to_thread(store.get_feature, feature_id)
            if feature is None:
                return f"Error: unknown feature {feature_id!r}"
            result = await attach_external_pr(
                store,
                feature,
                _strip_wrapping_quotes(pr_url).strip(),
                repo=repo_for_feature(feature, store_kw),
                base=base_branch_for_feature(feature, store_kw),
                review_gate=knob_bool(cfg, "review_gate", False, strict=False),
                reason=_strip_wrapping_quotes(reason),
            )
        except BoardError as exc:
            return f"Error: {exc}"
        return json.dumps(result)

    @tool
    def board_deliver(feature_id: str, text: str = "", ref: str = "") -> str:
        """Record a TASK's deliverable, moving it in_progress → in_review with no PR; then
        board_verify it. `text` is the deliverable; `ref` an optional doc URL or artifact path.
        Task-only and in_progress-only; an empty delivery is refused. Repeating the same delivery
        is a no-op; a different one for a task already in review is refused (reject it with
        board_verify(approved=false) first). If the task has a requirement ledger, END `text` with
        a `## Requirements` section of `- <id>: done` or `- <id>: declined — <reason>` lines,
        exactly that form, outside any code fence; unreported items stay open for the verifier.
        Details: board-tool-reference skill."""
        try:
            text = _strip_wrapping_quotes(text)
            ref = _strip_wrapping_quotes(ref)
            f = get_store(**store_kw).record_delivery(feature_id, text, ref)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_verify(feature_id: str, approved: bool = True, feedback: str = "", by: str = "") -> str:
        """Verify a TASK in in_review (task-only: a coding feature closes only through its merge).
        `approved=true` closes it done with reason `verified: <by>`. `approved=false` records
        `feedback` (the next dispatch prompt leads with it), reopens the requirement items that
        round closed, and requeues it to ready. `by` names the verifier; leave it empty to record
        your own actor. Self-approval is allowed but labelled `self-verified`. Open requirements
        don't block approval (the result notes them): read board_get_feature's
        `open_requirements` before deciding."""
        try:
            feedback = _strip_wrapping_quotes(feedback)
            f = get_store(**store_kw).record_verification(feature_id, approved, feedback, by=by)
        except BoardError as exc:
            return f"Error: {exc}"
        out = {"id": f["id"], "state": f["board_state"]}
        note = open_requirements_note(f.get("requirements"))
        if note:
            out["note"] = note
        return json.dumps(out)

    @tool
    def board_requeue_feature(feature_id: str, findings: str = "", escalate: bool = False) -> str:
        """Put a card back to ready for re-dispatch, keeping its open PR. With `findings` (an adverse
        review's requested changes; the card must be in_review) this is the fix round on the SAME
        PR: a review bounce is recorded and the next dispatch prompt leads with the findings.
        Without `findings` it is a plain retry against the original prompt. `escalate=true` (with
        findings) climbs the model ladder; already at the top, the card is blocked instead.
        Refused while the loop is still working the card (a live drive or review gate): wait for
        the round to end, or cancel. A CI failure on a bounced PR is board_requeue_ci_fix.
        Recovery map: board-tool-reference skill."""
        try:
            # Never under a live round (#398): pulling the card out from under a build that
            # is still running is how a requeued card went terminal. Refused, not overridden.
            from .loop import requeue_refusal

            refusal = requeue_refusal(feature_id)
            if refusal:
                raise BoardError(refusal)
            store = get_store(**store_kw)
            findings = _strip_wrapping_quotes(findings)
            if findings.strip():
                # The SAME bounce as POST /features/{fid}/review (#473, api.review_bounce):
                # record the distinct review-bounce comment, hand the findings to the loop
                # so its next dispatch prompt LEADS with them, THEN requeue onto the same
                # PR (climbing the ladder when `escalate`). Without this the findings would
                # silently not travel and the coder would burn a run re-producing the
                # rejected output. Imported lazily — the loop imports from here, so a
                # top-level import would be circular.
                from .api import review_bounce

                out = review_bounce(
                    store, feature_id, findings, escalate=bool(escalate), escalate_on=escalation_enabled(cfg)
                )
                f = out["feature"]
                return json.dumps({"id": f["id"], "state": f["board_state"]})
            f = store.requeue(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_requeue_ci_fix(feature_id: str, ci_failure: str = "") -> str:
        """Requeue a coding feature that CI bounced (in_progress with an open `pr_url`) for a CI-fix
        round on that same PR: records `ci_failure` (required) so the next dispatch prompt leads
        with it, then requeues it to ready with the PR kept. Refused while the loop is still
        working the card (a card mid-round has this same shape). Human review findings go through
        board_requeue_feature."""
        try:
            # Never under a live round (#398): a card mid-round has exactly the in_progress +
            # open-PR shape this verb accepts, and requeuing it pulled `bd-p8ft` out from
            # under its own CI-fix build, which then went terminal at the hand-off.
            from .loop import queue_ci_feedback, requeue_refusal

            refusal = requeue_refusal(feature_id)
            if refusal:
                raise BoardError(refusal)
            store = get_store(**store_kw)
            ci_failure = _strip_wrapping_quotes(ci_failure)
            store.record_ci_fix_feedback(feature_id, ci_failure)

            queue_ci_feedback(feature_id, ci_failure)
            f = store.requeue(feature_id)
            return json.dumps(
                {
                    "id": f["id"],
                    "state": f["board_state"],
                    "pr_url": f.get("pr_url", ""),
                }
            )
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_block_feature(feature_id: str, reason: str, owner: str = "operator") -> str:
        """Flag a card blocked with a required `reason`; it stays visible and the puller skips it. Use
        it for an external stall (missing dep, open question); use board_update_feature when the
        spec needs fixing. Never cleared automatically: lift it with board_unblock_feature.
        `owner`: `operator` (default) or `agent` (an agent-owned hold, no operator alert)."""
        try:
            if owner not in ("operator", "agent"):
                raise BoardError("owner must be one of: operator, agent")
            reason = _strip_wrapping_quotes(reason)
            # A hand-set block is a hold, never a self-healing failure (#406) — the class is
            # stated, not guessed from the reason's words. See store.MANUAL_BLOCK_CLASS.
            category = AGENT_HOLD_CLASS if owner == "agent" else MANUAL_BLOCK_CLASS
            f = get_store(**store_kw).flag_blocked(feature_id, reason, category=category)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_unblock_feature(feature_id: str, override_review: bool = False) -> str:
        """Clear a card's blocked flag so it can be re-dispatched (a `too-wide` card's timeout count
        resets too). For an in_review PR the review gate blocked after exhausting its fix budget,
        this also decides the gate on the PR's live head: the default re-arms it (a fresh review
        of the current head, a fresh fix budget); `override_review=True` records YOUR acceptance of
        the findings as a human (a success review status pinned to the live head; CI and merge
        gates still apply, and a later push unpins it). Override only after you have read the
        findings and judged them acceptable. Details: board-tool-reference skill."""
        try:
            store = get_store(**store_kw)
            # Read the card BEFORE the clear: a blocked in_review PR still carries the in-review
            # label, pr_url and block reason the review side effects need.
            before = store.get_feature(feature_id)
            f = store.clear_blocked(feature_id)
            # The running loop's cached timeout count wins over the label the store just
            # reset (#259) — drop it too (#378). Lazy import: the loop imports from here.
            from .loop import forget_timeout_count

            forget_timeout_count(feature_id)
        except BoardError as exc:
            return f"Error: {exc}"
        # #512: decide the review gate on the PR's live head (re-arm, or operator override).
        # #262: act in the FEATURE's project repo (the shared route/tool resolver), not the
        # board default's. The helper never raises; it reports every gh/store failure in `side`.
        from .api import repo_for_feature
        from .loop import unblock_side_effects

        # Pass the tool's own store (the operator's configured db_path) so the helper's
        # substate/budget writes land in the same database clear_blocked just wrote to (#512).
        side = unblock_side_effects(
            feature_id, before or f, store, override_review=override_review, cwd=repo_for_feature(before or f, store_kw)
        )
        return json.dumps({"id": f["id"], "state": f["board_state"], "review": side})

    @tool
    def board_routing_get() -> str:
        """Read the effective coder ladder and exclusive allowlist for every project."""
        from .routing import routing_get

        try:
            return json.dumps(routing_get())
        except ValueError as exc:
            return f"Error: {exc}"

    @tool
    async def board_routing_set(
        config: RunnableConfig,
        coders: dict | None = None,
        exclusive: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        """Persist and apply routing live. Requires host-injected operator-tier context.

        Omit project for board defaults; coders replaces the ladder, and an empty
        exclusive list disables strict routing. Existing drives retain their route.
        """
        from .routing import operator_actor, routing_set

        try:
            actor = operator_actor(config)
            return json.dumps(await routing_set(actor=actor, coders=coders, exclusive=exclusive, project=project))
        except (PermissionError, ValueError) as exc:
            return f"Error: {exc}"

    @tool
    def board_hold_merge(feature_id: str, reason: str) -> str:
        """Place the operator's auto-merge veto (`merge-hold` label) on a card: the loop never
        auto-merges it, so a human merges by hand (e.g. a change to QA first). The card still
        builds, verifies and is reviewed. `reason` is required (why it must be merged by hand).
        Valid in any non-terminal state; idempotent. Lift it with board_release_merge_hold."""
        try:
            reason = _strip_wrapping_quotes(reason).strip()
            if not reason:
                return "Error: a merge-hold needs a reason — say why this card must be merged by hand"
            f = get_store(**store_kw).set_merge_hold(feature_id, reason)
            return json.dumps({"id": f["id"], "state": f["board_state"], "merge_hold": f.get("merge_hold", False)})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_release_merge_hold(feature_id: str) -> str:
        """Lift the operator's per-card auto-merge veto (the `merge-hold` label) — the
        inverse of board_hold_merge (#532). Removes the label and records an audit comment,
        so the auto-merge edge can resume for this card. An unknown id returns an `Error:`;
        idempotent on a card that is not held (a no-op with no comment)."""
        try:
            f = get_store(**store_kw).release_merge_hold(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"], "merge_hold": f.get("merge_hold", False)})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_reset_merged_verify_budget(feature_id: str) -> str:
        """Reset one in_review card's merged-state re-verify budget: the fix when its `next_action`
        reads `auto-merge held: merged-verify budget exhausted`. Clears the persisted budget label
        and the loop's in-process cache so the next reconcile re-verifies; touches no other
        budget. Raising `merged_verify_max` in Settings ▸ Project Board is the board-wide
        alternative. Background: board-tool-reference skill."""
        try:
            feature_id = _strip_wrapping_quotes(feature_id).strip()
            if not feature_id:
                return "Error: feature_id is required to reset a merged-verify budget"
            store = get_store(**store_kw)
            f = store.reset_merged_verify_budget(feature_id)
            # Invalidate the running loop's in-process budget too — the label clear alone
            # is not enough (the loop's cache wins over the labels, #259). This runs AFTER
            # the store's durable clear: it pins the live count to 0 and re-clears the
            # label under the loop's reset lock, so a reconcile that is mid-flight arming
            # the exhaustion sentinel can't leave the card held (ADR 0326). The store is
            # handed in for that re-clear. Lazy import (the loop imports from here, so a
            # top-level import would cycle).
            from .loop import reset_merged_verify_budget as _invalidate_cache

            cache_cleared = _invalidate_cache(feature_id, store)
            return json.dumps(
                {
                    "id": f["id"],
                    "state": f["board_state"],
                    "merged_verify_budget_reset": True,
                    "cache_cleared": cache_cleared,
                }
            )
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_list(
        state: str = "",
        include_archived: bool = False,
        with_ci: bool = False,
        failing_only: bool = False,
        project: str = "",
    ) -> str:
        """List board features in priority order, optionally filtered by `state`
        (backlog/ready/in_progress/in_review/done/blocked) or `project`. Archived terminal cards
        are left out unless `include_archived=true`. Rows that something other than a coder must
        move carry `next_action` (with the verb in `next_action_hint`); values: board-tool-reference skill. When any row
        reads `awaiting-merge (auto_merge off)` (`awaiting_merge: true`), LEAD your status report
        with it and offer the two verbs that move it, merge that PR or enable auto_merge, NOT a
        re-review. A `project_unresolved: true` row's `project_hint` names the
        board_update_feature(project=…) call that re-homes it. `with_ci=true` adds live
        `ci_status`/`ci_summary` (one gh call per PR, so only when needed); `failing_only=true`
        implies with_ci and keeps red rows (in_review unless you pass `state`)."""
        if failing_only:
            with_ci = True
            state = state or "in_review"
        project = _strip_wrapping_quotes(project).strip()
        # Guarded like every other tool: an unusable board (no repo bound, no .beads,
        # br missing) must reach the model as an `Error: …` tool RESULT it can adapt
        # to — an escaped BoardError kills the whole turn and discards every tool
        # call already made in it (observed: 21 completed calls thrown away).
        try:
            store = get_store(**_store_kw_for(project))
            feats = store.list_features(state=state or None, include_archived=include_archived)
            # Project filter (#90): keep only the features stamped for `project`. Applied
            # BEFORE the CI join so a filtered listing never spends a `gh` call on a row it
            # would drop.
            if project:
                feats = [f for f in feats if str(f.get("project") or "") == project]
            if with_ci:
                feats = store.annotate_ci_status(feats)
                if failing_only:
                    feats = [f for f in feats if f.get("ci_status") == "failing"]
        except BoardError as exc:
            return f"Error: {exc}"
        annotate_next_action(feats, cfg)  # #208: labels + config only, no network
        rows = []
        for f in feats:
            row = {
                "id": f["id"],
                "title": f["title"],
                "state": f["board_state"],
                "blocked": f["blocked"],
                "dag_blocked": f.get("dag_blocked", False),
                "pr_url": f["pr_url"],
                "priority": f["priority"],
                "difficulty": f["difficulty"],
                "project": f.get("project", ""),
            }
            if f.get("recovery_context"):
                row["recovery_context"] = f["recovery_context"]
            if f.get("blocked"):
                row["blocked_class"] = f.get("blocked_class", "")
                row["blocked_reason"] = f.get("blocked_reason", "")
            if f.get("waits_for"):
                row["waits_for"] = f["waits_for"]
            orphan = unresolved_project(f, projects)
            if orphan and f["board_state"] not in ("done", "cancelled"):
                row["project_unresolved"] = True
                row["project_hint"] = rehome_hint(f["id"], orphan, projects, default_proj)
            if f.get("next_action"):
                row["next_action"] = f["next_action"]
                row["awaiting_merge"] = bool(f.get("awaiting_merge"))
                if f.get("next_action_hint"):
                    row["next_action_hint"] = f["next_action_hint"]
            if with_ci:
                row["ci_status"] = f.get("ci_status", "")
                if f.get("ci_summary"):
                    row["ci_summary"] = f["ci_summary"]
            rows.append(row)
        return json.dumps(rows)

    @tool
    def board_retro() -> str:
        """Read-only: mine the whole board history (archived cards included) into recurring failure
        classes and flow stats (escalation / block / multi-attempt rates, blocked cards and why).
        The loop-retro skill reads it."""
        from . import retro

        try:
            d = retro.summarize(get_store(**store_kw).raw_features_with_comments())
        except BoardError as exc:
            return f"Error: {exc}"
        return json.dumps(
            {
                "n_features": d["n_features"],
                "recurring_classes": d["recurring_classes"],
                "escalation_rate": d["escalation_rate"],
                "block_rate": d["block_rate"],
                "multi_attempt_rate": d["multi_attempt_rate"],
                "blocked_features": d["blocked_features"],
            },
            indent=2,
        )

    @tool
    def board_check_gates(feature_id: str = "") -> str:
        """Check publish gates (`waits_for`) now against npm / GitHub instead of waiting for the
        loop's next sweep. With `feature_id`, that card's gates; empty, every open card that has
        any. Rows carry `gates` verdicts and `clear` (a backlog card still needs
        board_mark_ready). A failed check reads unmet (fail closed). Results land in the cache the
        loop reads."""
        from . import gates as publish_gates

        try:
            store = get_store(**store_kw)
            if str(feature_id or "").strip():
                f = store.get_feature(feature_id.strip())
                if f is None:
                    return f"Error: unknown feature {feature_id!r}"
                feats = [f]
            else:
                feats = [
                    f
                    for f in store.list_features()
                    if f.get("waits_for") and f.get("board_state") in ("backlog", "ready")
                ]
        except BoardError as exc:
            return f"Error: {exc}"
        token = publish_gates.npm_token(cfg)
        rows = []
        for f in feats:
            results = publish_gates.evaluate(
                f.get("waits_for") or [], token=token, force=True, resolve_card=_card_resolver(store)
            )
            rows.append(
                {
                    "id": f["id"],
                    "state": f["board_state"],
                    "waits_for": f.get("waits_for") or [],
                    "gates": results,
                    "clear": all(r["met"] for r in results),
                }
            )
        return json.dumps(rows)

    @tool
    async def board_dispatch() -> str:
        """Ask the running board loop to evaluate its ready queue now (its own claim path, not a
        second scheduler) and return a JSON decision record: `outcome` (dispatched, empty-queue,
        held, at-capacity, review-wip-limit, parked, all-candidates-held, loop-disabled,
        loop-not-running, error), a one-sentence `detail`, `held` reasons with the step that moves
        each, and the live slot picture. Every scheduling gate and the claim lock apply, so
        repeated calls never double-dispatch. Outcome meanings: board-tool-reference skill."""
        from .loop import request_dispatch

        try:
            return json.dumps(await request_dispatch())
        except Exception as exc:  # noqa: BLE001 — never raise into the agent loop (mirror the store verbs)
            return f"Error: {type(exc).__name__}: {exc}"

    @tool
    async def board_salvage_feature(feature_id: str, force: bool = False, tree: str = "") -> str:
        """Operator override: publish a STRANDED card's finished worktree without dispatching a coder
        (commit, run the pre-PR gate, push, open the PR, move it to in_review). Skips the drive's
        goal, requirement-ledger and source-issue checks; CI and the review gate still apply. Only
        for a card in_progress with no live drive, or blocked. Refuses, changing nothing, while a
        drive owns the card, when no worktree has changes vs base, or when several do and `tree`
        doesn't pick one. A red gate publishes nothing (`outcome: gate-red`); `force=true`
        publishes it as a DRAFT with the gate output. Trust the returned `draft`, not `force`.
        Outcomes: board-tool-reference skill."""
        from .loop import request_salvage

        try:
            return json.dumps(await request_salvage(feature_id, force=bool(force), tree=str(tree or "")))
        except Exception as exc:  # noqa: BLE001 — never raise into the agent loop (mirror the store verbs)
            return f"Error: {type(exc).__name__}: {exc}"

    @tool
    def board_show_cards(ids: list[str] = [], state: str = "", project: str = "", title: str = "") -> str:
        """Show live board cards inline in the chat to answer a status check-in ("status?", "what's
        blocked?", "how's the epic?"). Cards show state, next_action, block reason, PR link and
        action buttons, and refresh themselves.

        AFTER CALLING IT, WRITE AT MOST 2 LINES OF PROSE: only what the cards can't say. Never
        restate the cards.

        No args: every open card, most urgent first. Or pass `ids` (≤ 20), or a `state` and/or
        `project`; `title` (≤ 80 chars) labels the header. The returned text summary is for you;
        don't paste it back. Not for your own reasoning (use board_list / board_get_feature). Card
        buttons send messages you act on with board_* tools: see the showing-board-cards skill."""
        from .board_cards import MAX_IDS, build_props, component_tail, summary_text, validate_props

        props = build_props(ids, state=state, project=_strip_wrapping_quotes(project), title=title)
        why = validate_props(props)
        if why is not None:
            return f"Error: {why}"
        try:
            store = get_store(**_store_kw_for(props.get("project", "")))
            missing: list[str] = []
            total: int | None = None  # a capped listing's match count (#522)
            if props.get("ids"):
                feats = []
                for fid in props["ids"][:MAX_IDS]:
                    f = store.get_feature(fid)
                    if f is None:
                        missing.append(fid)
                    else:
                        feats.append(f)
            else:
                # The same scope the cards read (board_cards.scope_cards): a state's cards
                # (done capped to the most recent), or open work for a project-only query.
                from .board_cards import scope_cards

                st = props.get("state", "")
                feats = store.list_features(state=st or None)
                if props.get("project"):
                    feats = [f for f in feats if str(f.get("project") or "") == props["project"]]
                feats, total = scope_cards(feats, st)
                # A listing row carries no block reason (only `br show` reads comments) —
                # fill it for the first few blocked cards so the summary says WHY.
                for f in [f for f in feats if f.get("blocked") and not f.get("blocked_reason")][:5]:
                    full = store.get_feature(f["id"]) or {}
                    f["blocked_reason"] = full.get("blocked_reason", "")
        except BoardError as exc:
            return f"Error: {exc}"
        annotate_next_action(feats, cfg)  # labels + config only, no network
        return summary_text(props, feats, missing, total) + component_tail(props)

    def _project_rows() -> tuple[list[dict], str]:
        """The board's registered projects and the default a create falls back to."""
        try:
            from .project_registry import project_registry_snapshot

            snap = project_registry_snapshot()
            rows = [
                {"name": r.get("name", ""), "repo": r.get("repo", ""), "base_branch": r.get("base_branch", "")}
                for r in snap.get("projects") or []
            ]
            default = snap.get("default_project") or ""
        except Exception:  # noqa: BLE001 — no live host config (a host-free run): the register-time map
            rows, default = [], ""
        if not rows:
            # The registry is empty (or unreadable) on a flat single-repo board (`repo:`, no
            # `projects:` map): its one project is the implicit one every card is stamped with
            # (`project:default`). Without this the form's picker got nothing and the agent
            # told the user creation might fail on a board that builds fine.
            rows = [
                {"name": n, "repo": str(e.get("repo") or ""), "base_branch": str(e.get("base_branch") or "")}
                for n, e in sorted(projects.items())
            ]
            default = default or default_proj
        return rows, default

    @tool
    def board_projects() -> str:
        """List the board's registered projects (the names `board_create_feature(project=…)`
        accepts) and the default a create falls back to. Read-only. Use it instead of
        guessing names. (The new-card form already carries them — see board_card_form.)
        Returns JSON `{projects: [{name, repo, base_branch}], default_project}`."""
        rows, default = _project_rows()
        return json.dumps({"projects": rows, "default_project": default})

    @tool
    def board_card_form(project: str = "", title_hint: str = "", why_hint: str = "") -> str:
        """Show the operator the NEW BOARD CARD form inline in the chat (Project, Title, What & why,
        Acceptance criteria, Priority, a Create card button). Call it RIGHT AWAY when the user asks
        to add a feature, task, bug fix or idea to the board. Never write an HTML form for this
        yourself. `title_hint` / `why_hint` (one line each, from the request) become placeholders;
        `project` preselects a project.

        AFTER CALLING IT, WRITE ONE SHORT LINE ("Fill this in and press Create card.") and nothing
        else. The submitted form arrives as a `Create board card · <title>` message: create the
        card per the card-from-chat skill."""
        from .board_card_form import EMIT, build_props, component_tail

        rows, default = _project_rows()
        names = [str(r.get("name") or "") for r in rows if r.get("name")]
        props = build_props(
            names,
            project=_strip_wrapping_quotes(str(project or "")),
            default_project=default,
            title_hint=_strip_wrapping_quotes(str(title_hint or "")),
            why_hint=_strip_wrapping_quotes(str(why_hint or "")),
        )
        tail = component_tail(props)
        if not EMIT or not tail:
            return (
                "The new-card form can't render in this console (it needs frame components, "
                "protoAgent ADR 0118 D5). Ask the user in ONE short message for the title, what "
                "and why, the acceptance criteria (one per line) and a priority (P0-P4, default "
                f"P2){'; projects: ' + ', '.join(names) if len(names) > 1 else ''}. Then create the "
                "card as the card-from-chat skill says."
            )
        where = props.get("project", "")
        picker = (
            f"picker: {', '.join(props['projects'])}"
            if len(props.get("projects", [])) > 1
            else "one project, no picker"
        )
        return f"Showing the new-card form (project {where or 'default'}; {picker})." + tail

    @tool
    def board_watch(feature_id: str) -> str:
        """Open the Board view on the console's main stage and FOLLOW one card. The card gets
        an accent ring and a "Following" tag, and the view keeps it in view as the loop moves
        it backlog → on deck → building → in review → done. Following ends at done, or when
        the operator clicks or scrolls the board. Call it right after you create (and ready)
        a card the operator wants to watch, as in the card-from-chat flow. Changes nothing
        on the board. Returns one confirmation line."""
        from .board_cards import _ID_RE

        fid = _strip_wrapping_quotes(str(feature_id or "")).strip()
        if not _ID_RE.fullmatch(fid):
            return f"Error: {feature_id!r} is not a board card id (e.g. bd-a1b2)."
        try:
            f = get_store(**store_kw).get_feature(fid)
        except BoardError as exc:
            return f"Error: {exc}"
        if f is None:
            return f"Error: no board card {fid!r}."
        emit = getattr(registry, "emit", None)
        navigate = getattr(registry, "navigate", None)
        if not callable(emit) or not callable(navigate):
            return f"Error: this host can't drive the console. Open the Board view and find {fid} there."
        # Publish, THEN navigate (protoAgent plugin-view-bridge "Events"): a Board view that is
        # already open gets the event live; one the navigate mounts is caught up by the host's
        # replay of what this plugin published just before it.
        emit(FOCUS_EVENT, {"id": fid})
        navigate(BOARD_VIEW)
        state = str(f.get("board_state") or f.get("state") or "")
        lane = {"ready": "on deck", "in_progress": "building", "in_review": "in review"}.get(state, state)
        return f"Following {fid} on the Board ({lane or 'unknown lane'}); it stays in view until it lands in done."

    tools = [
        board_create_epic,
        board_create_feature,
        board_create_task,
        board_update_feature,
        board_remove_dependency,
        board_get_feature,
        board_comments,
        board_record_work_location,
        board_list_work_locations,
        board_mark_ready,
        board_check_ready,
        board_mark_designing,
        board_cancel_feature,
        board_mark_done,
        board_attach_pr,
        board_deliver,
        board_verify,
        board_requeue_feature,
        board_requeue_ci_fix,
        board_block_feature,
        board_unblock_feature,
        board_routing_get,
        board_routing_set,
        board_hold_merge,
        board_release_merge_hold,
        board_reset_merged_verify_budget,
        board_list,
        board_retro,
        board_check_gates,
        board_dispatch,
        board_salvage_feature,
        board_show_cards,
        board_projects,
        board_card_form,
        board_watch,
    ]

    # Runtime project registration (#167). Appended rather than folded in above because
    # it writes CONFIG, not board rows — it is the only tool here that doesn't touch a
    # store, and it refuses on its own (the operator's onboarding space) rather than on
    # the board's state. `None` when langchain isn't importable in a host-free run.
    from .project_registry import build_register_tool

    if (register_tool := build_register_tool(cfg)) is not None:
        tools.append(register_tool)
    # Every description rides on every model call, and @tool keeps the docstring's
    # source indentation verbatim (8 spaces a line). Dedent it: same text, fewer tokens.
    for t in tools:
        t.description = inspect.cleandoc(t.description or "")
    return tools

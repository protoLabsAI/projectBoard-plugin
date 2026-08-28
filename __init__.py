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

import json
import logging
import re
import subprocess

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.project_board")


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

    # A few board tools so the agent (or A2A) can drive the board headlessly.
    for t in _board_tools(cfg):
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

    # Planning layer (D9): the decompose/antagonist subagents + the orchestration
    # skill that turns an idea into the docs tree + the board (per-epic human gate).
    try:
        from .subagents import ANTAGONIST_CONFIG, DECOMPOSE_CONFIG

        registry.register_subagent(DECOMPOSE_CONFIG)
        registry.register_subagent(ANTAGONIST_CONFIG)
        registry.register_skill_dir("skills")
    except Exception:  # noqa: BLE001 — planning layer is best-effort
        log.exception("[project_board] registering planning subagents/skill failed")

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


def _feature_reply(f: dict) -> str:
    """Serialize a store feature for a tool return — carrying the success-with-warning
    trio through the boundary when set (QA panel on #88: stripping it hides the repair
    contract from the agent)."""
    out = {"id": f["id"], "state": f["board_state"], "title": f["title"]}
    if f.get("enrichment_failed"):
        out["enrichment_failed"] = True
        out["missing_fields"] = f.get("missing_fields", [])
        out["warning"] = f.get("warning", "")
    return json.dumps(out)


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
    from .store import BoardError

    try:
        existing = store.list_features()
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


def _board_tools(cfg: dict):
    from .projects import default_project as resolve_default_project
    from .projects import resolve_projects, store_db_path
    from .store import BoardError, annotate_next_action, get_store

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
    ) -> str:
        """Create a board feature (a bead; starts in `backlog`). To pass the Ready
        gate a feature needs a self-sufficient `spec`, testable `acceptance_criteria`,
        AND `files_to_modify` (comma-separated paths to create/modify — vague tasks
        make a coding agent produce nothing). `parent` is the epic/milestone id;
        `difficulty` (small|medium|large) seeds the model tier; `depends_on` is a
        comma-separated list of blocking feature ids; set `foundation=True` for a
        feature others build on (dependents gate on its merge, never its review).
        `source_issue` names the ORIGINATING GitHub issue — a full issue URL or
        `owner/repo#N`, stored normalized as `owner/repo#N` (off-label, in the bead's
        notes metadata — a label can't carry `/`/`#`) — so the feature's PR gets a
        `Fixes #N` line and the issue auto-closes on merge. `project` names the entry
        in the board's `projects:` map this feature builds in (#90) — it determines
        which repo the feature's worktree/PR target and which repo the Ready gate
        validates its files against; it's stamped as an immutable `project:<name>`
        label. Defaults to the board's `default_project` when omitted; a single-repo
        board can ignore it.

        DEDUP: refuses to create when a feature with the same title is already OPEN
        on this board (backlog/ready/in_progress/in_review/blocked) — calling this
        twice for the same task (e.g. reconsidering mid-turn) stacks a duplicate the
        loop then churns on. It ALSO refuses when `depends_on` names a feature that is
        already `in_review` with an open PR: work built on in-flight work is a fix
        round and belongs on that feature's OWN PR — the warning points you at
        `board_requeue_feature(fid, findings=...)` instead. When `source_issue` is
        set it ALSO refuses when an open PR in that issue's repo already references
        the issue number (title/body) — the issue is already being worked; review or
        contribute to that PR instead of dispatching a second coder at it. Pass
        `force=true` to create anyway (bypasses all three checks). A store read or
        GitHub API failure never blocks creation (better a possible dup than a
        stuck board)."""
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
            )
            return _feature_reply(f)
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
    ) -> str:
        """Create a board TASK (a `task`-type bead; starts in `backlog`) — the sibling of
        board_create_feature for work that ships a DELIVERABLE (a doc, a decision, an
        artifact ref) instead of a PR (#217). A task rides the SAME rails as a coding
        feature (ready → claim → in_progress → in_review) but takes the delivery/verify
        edges: board_deliver moves it to in_review on a recorded deliverable (no PR), and
        board_verify is its Done edge. To pass the Ready gate a task needs a self-sufficient
        `spec` + testable `acceptance_criteria` — but NO `files_to_modify` (a task doesn't
        touch repo files, so that coding-feature requirement is relaxed for tasks).

        `assignee` pre-assigns the bead; `priority` (0 = highest) ranks it in the ready
        queue; `parent` is the epic/milestone id; `depends_on` is a comma-separated list of
        blocking feature ids; `project` (#90) and `source_issue` (#97) behave exactly as on
        board_create_feature. DEDUP is identical to board_create_feature — a same-title open
        card, a `depends_on` on an in-flight PR, or a `source_issue` an open PR already
        references is refused unless `force=true` (a store/GitHub read failure never blocks
        creation). Inputs are stripped of any literal wrapping double quotes first (same
        hygiene as board_create_feature)."""
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
            )
            return _feature_reply(f)
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
    ) -> str:
        """Partially update an existing feature — the REPAIR path for a bead the Ready
        gate rejects. Only the non-empty arguments are written; every other field is left
        as-is. Use it to fill a missing `spec`, `acceptance_criteria`, or `files_to_modify`
        (comma-separated paths) on a feature `board_mark_ready` refused, then mark it ready
        again — no need to cancel and recreate the bead. `title` renames the feature
        (empty = leave unchanged). `difficulty` (small|medium|large)
        re-seeds the model tier. `depends_on` (comma-separated feature ids) ADDS blocking
        edges, and `foundation=True` restores the foundation flag — the repairs for
        dependencies/foundation dropped by a create-time failure (False = leave as-is;
        this tool never removes the flag). `source_issue` (a full GitHub issue URL or
        `owner/repo#N`, stored normalized off-label in the bead's notes metadata)
        sets/replaces the originating issue the feature's PR will reference as
        `Fixes #N`. There is deliberately NO `project` argument: a feature's project
        (#90) is immutable once stamped — it determines which repo the feature's
        worktree/PR target, and re-homing an in-flight card mid-stream would strand its
        branch; cancel and recreate to move a feature to another project. Inputs are
        stripped of any literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
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
            f = store.update_feature(
                feature_id,
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
            return _feature_reply(f)
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_get_feature(feature_id: str) -> str:
        """Read a single feature's FULL detail as JSON — `title`, `spec`,
        `acceptance_criteria`, `design`, `state`, `labels`, `pr_url`, `difficulty`,
        `files_to_modify`, `foundation`, `priority`, `source_issue`, `project`
        (the entry in the board's `projects:` map this feature builds in, #90),
        `requirements`
        (the tracked requirement ledger decomposed from the acceptance criteria at
        mark_ready — each item `{id, text, status, decline_reason?}`, status one of
        open/done/declined; the completion gate refuses a PR while any item is
        open), plus two dependency views: `depends_on` (EVERY blocking edge — the
        historical ledger, including already-merged blockers) and `open_depends_on`
        (only the edges whose blocker is still OPEN — the live, actionable "what's
        blocking me now" signal). The READ half of a read-modify-write: fetch the
        current criteria/spec, revise them, then `board_update_feature` — no
        operator round-trip. Returns `Error: unknown feature …` for an id that
        isn't on the board."""
        try:
            f = get_store(**store_kw).get_feature(feature_id)
        except BoardError as exc:
            return f"Error: {exc}"
        if f is None:
            return f"Error: unknown feature {feature_id!r}"
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
                "difficulty": f["difficulty"],
                "files_to_modify": f.get("files_to_modify", []),
                "foundation": f.get("foundation", False),
                "priority": f.get("priority", 2),
                "source_issue": f.get("source_issue", ""),
                "requirements": f.get("requirements", []),
                "depends_on": f.get("depends_on", []),
                "open_depends_on": f.get("open_depends_on", []),
                # Which project (#90) this feature builds in — "" for a pre-#90 feature
                # or a single-repo board with no `projects:` map.
                "project": f.get("project", ""),
            }
        )

    @tool
    def board_mark_ready(feature_id: str) -> str:
        """Promote a feature backlog → ready. Fails if it lacks a spec +
        acceptance_criteria (the Ready gate). Only `ready` features are pulled."""
        try:
            f = get_store(**store_kw).mark_ready(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_cancel_feature(feature_id: str, reason: str = "") -> str:
        """Cancel a feature created in error (bad decomposition, duplicate, scope cut) —
        the verb that RETIRES a bad card. Tags the bead `cancelled` and closes it with an
        auditable `reason` (`br close -r`), a second terminal edge that keeps the card (and
        its history) visible in a distinct `cancelled` state — NOT `done`, so the merge/CI
        reconcilers and loop-retro never mistake it for shipped work. The bead survives (a
        cancel, not a hard delete). `reason` is stripped of any literal wrapping double
        quotes before storage (same hygiene as board_create_feature). A cancel also
        closes the card's open PR (with a comment pointing at the card) and stops its
        in-flight coder, if any (#211) — best-effort; the reply carries `pr_closed` /
        `drive_cancelled` so you can say what happened."""
        try:
            reason = _strip_wrapping_quotes(reason)
            store = get_store(**store_kw)
            before = store.get_feature(feature_id)
            pr_url = str((before or {}).get("pr_url") or "").strip()
            f = store.cancel_feature(feature_id, reason)
        except BoardError as exc:
            return f"Error: {exc}"
        # Lazy import (the loop imports from here — a top-level import would be circular).
        from .loop import cancel_side_effects

        side = cancel_side_effects(feature_id, pr_url, cwd=store_kw["repo"])
        return json.dumps({"id": f["id"], "state": f["board_state"], **side})

    @tool
    def board_mark_done(feature_id: str, reason: str = "") -> str:
        """Mark a feature `done` by hand — the MANUAL Done edge (#228), for work that
        shipped OUTSIDE the board's PR lifecycle (the change landed via another repo/tool,
        or the feature completed off-board), where record_merge's automatic pr_url match
        never fires. Accepts only an in-flight card (in_progress / in_review / blocked) and
        errors on a backlog/ready card (nothing shipped yet) or an already-terminal one
        (done/cancelled). Closes the bead like record_merge — clears the `blocked` flag,
        drops open blocker edges, then closes — so its dependents unblock. `reason` is
        recorded as a comment (the audit trail for WHY it was hand-closed) and is stripped
        of any literal wrapping double quotes first (same hygiene as board_create_feature).
        Use board_cancel_feature instead to RETIRE a bad card (a distinct `cancelled`
        state); this marks GENUINELY-completed work done."""
        try:
            reason = _strip_wrapping_quotes(reason)
            f = get_store(**store_kw).mark_done(feature_id, reason=reason)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_deliver(feature_id: str, text: str = "", ref: str = "") -> str:
        """Record a TASK's deliverable (#217) — the task sibling of a coder's open_pr edge,
        moving the bead in_progress → in_review with NO PR. `text` is the deliverable itself
        (recorded as a `deliverable:` comment the board reads back into the `deliverable`
        field); optional `ref` (a doc URL, an artifact path) lands on the same `external_ref`
        slot a coding feature's pr_url occupies, so link consumers just work. Then verify it
        with board_verify. TASK-ONLY and in_progress-ONLY: a coding feature (or a task not
        yet claimed/in review) is refused with an `Error: …` — a coding feature entering
        review with no pr_url would strand the merge reconciler. `text`/`ref` are stripped
        of any literal wrapping double quotes first (same hygiene as board_create_feature)."""
        try:
            text = _strip_wrapping_quotes(text)
            ref = _strip_wrapping_quotes(ref)
            f = get_store(**store_kw).record_delivery(feature_id, text, ref)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_verify(feature_id: str, approved: bool = True, feedback: str = "") -> str:
        """Verify a TASK's delivered work (#217) — the task Done edge, record_merge's
        verify sibling. `approved=true` CLOSES the task `done` with an auditable
        `verified: <actor>` reason (a task has no PR to merge, so a verifier's approval is
        what closes it). `approved=false` records `feedback` as a comment (the next dispatch
        prompt leads with it, the adverse-review shape) and requeues the bead to `ready` for
        another pass. TASK-ONLY and in_review-ONLY: a coding feature (closed here it would
        dodge record_merge, the ONE Done edge for code) or a task not in review is refused
        with an `Error: …`. `feedback` is stripped of any literal wrapping double quotes
        first (same hygiene as board_create_feature)."""
        try:
            feedback = _strip_wrapping_quotes(feedback)
            f = get_store(**store_kw).record_verification(feature_id, approved, feedback)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_requeue_feature(feature_id: str, findings: str = "") -> str:
        """Put a feature back to `ready` for re-dispatch, keeping its open PR — the verb
        the fix-round doctrine needs to carry review findings to the SAME branch.

        WITH `findings` (an adverse review's requested changes): mirrors
        `POST /features/{fid}/review` — records a DISTINCT review-bounce comment on the
        bead (`record_review_bounce`), queues the findings so the loop's NEXT dispatch
        prompt LEADS with them (`queue_review_feedback`), THEN requeues onto the same
        open PR (`requeue`). This is the fix round: the coder re-dispatches knowing
        exactly what to fix, instead of blindly re-running the original prompt. Requires
        `in_review` (record_review_bounce enforces the state an adverse review lands
        from).

        WITHOUT `findings` (empty): a BARE re-dispatch — just `requeue`. Back to `ready`,
        open PR preserved, assignee cleared, NO new feedback; the coder re-runs against
        the ORIGINAL prompt (a plain retry, e.g. after a transient failure). The puller
        re-claims it and the loop re-dispatches (at the higher tier if it was just
        escalated; the open PR is pushed to, not reopened). `findings` is stripped of any
        literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            store = get_store(**store_kw)
            findings = _strip_wrapping_quotes(findings)
            if findings.strip():
                # Mirror the non-escalating POST /features/{fid}/review path: record the
                # distinct review-bounce comment, hand the findings to the loop so its
                # next dispatch prompt LEADS with them, THEN requeue onto the same PR.
                # Without this the findings would silently not travel and the coder would
                # burn a run re-producing the rejected output. Import queue_review_feedback
                # lazily (as the API route does) — the loop imports from here, so a
                # top-level import would be circular.
                store.record_review_bounce(feature_id, findings)
                from .loop import queue_review_feedback

                queue_review_feedback(feature_id, findings)
            f = store.requeue(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_block_feature(feature_id: str, reason: str) -> str:
        """Flag a feature `blocked` with a `reason` — it stays ON the board (blocked is a
        flag, not a lane) with the reason visible, and is skipped by the puller until
        cleared. Complements the board_update_feature repair path: block when the feature
        is stuck on something external (a missing dep, an unanswered question) and you want
        the card parked-and-visible; update when the spec itself needs fixing. `reason` is
        stripped of any literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            reason = _strip_wrapping_quotes(reason)
            f = get_store(**store_kw).flag_blocked(feature_id, reason)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_unblock_feature(feature_id: str) -> str:
        """Clear the `blocked` flag so the feature can be re-dispatched — the inverse of
        board_block_feature. Removes the blocked label; the puller can claim it again once
        it's otherwise `ready`."""
        try:
            f = get_store(**store_kw).clear_blocked(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
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
        """List board features, optionally filtered by board `state` (backlog/ready/
        in_progress/in_review/done/blocked). Priority order. Terminal features
        (done/cancelled) past the archive window carry an `archived` label and are
        EXCLUDED from this default response (#115) — the live board, not all of
        history. Pass `include_archived=true` for the exhaustive view; nothing is
        ever deleted, so archived features stay fully queryable.

        `project` (a name in the board's `projects:` map, #90) narrows the listing to
        the features that build in that project — the multi-repo board's "just this
        repo's cards" view. Omitted (the default) lists every project; each row carries
        its `project` field so an unfiltered listing stays legible on a mixed board.

        Every `in_review` row carries `next_action` — the ONE sentence that says what
        moves it (#208): `awaiting-merge (auto_merge off)` (reviewed, nothing
        board-side in the way, and the loop will NOT merge — a human must),
        `auto-merge pending` (the loop merges once GitHub reports CLEAN), `review in
        progress`, `changes requested`, `awaiting review verdict (no review-clean)`,
        `merge-hold (operator veto)`, `blocked`, `draft (run gh pr ready)` — plus
        `awaiting_merge: true` and a `next_action_hint` ("auto_merge is off — merge #N
        or turn it on in Settings ▸ Project Board") for the first case. When ANY row is
        `awaiting_merge`, LEAD your status report with it and offer the two verbs that
        actually move it — merge that PR, or enable `auto_merge` — NOT a re-review: the
        review gate has already cleared it. Derived from labels + the LIVE config (a
        Settings save to `auto_merge` changes it without a restart); costs no network.
        With `with_ci=true`, a row whose rollup is red reads `ci failing` instead
        (never "merge #N" on a red PR).

        `with_ci=true` joins each live PR-bearing row with its LIVE CI rollup
        (#107): `ci_status` (passing|failing|pending|none; "" = no PR probed) plus
        the failing check names in `ci_summary`. OPT-IN, never default — each
        PR-bearing feature costs a `gh` network call (see
        store.annotate_ci_status for the full cost rationale). `failing_only=true`
        (implies `with_ci`) narrows the listing to rows whose CI is red; with no
        explicit `state` it defaults to `in_review` — the PM's "which PRs are red
        and waiting" query — but honors a `state` you pass alongside it."""
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
        """Retro the board: mine the attempt/outcome history of completed + blocked
        features into recurring failure CLASSES + flow stats (escalation / block /
        multi-attempt rates + the blocked features and why). The loop-retro skill
        reads this to distill durable grounding (PROTO.md gotchas) so the next runs
        stop repeating known failures. Read-only. Spans ALL history: the store's
        retro read explicitly opts into `archived` features (#115), so a
        retrospective never silently shrinks to the archive window."""
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

    tools = [
        board_create_epic,
        board_create_feature,
        board_create_task,
        board_update_feature,
        board_get_feature,
        board_mark_ready,
        board_cancel_feature,
        board_mark_done,
        board_deliver,
        board_verify,
        board_requeue_feature,
        board_block_feature,
        board_unblock_feature,
        board_list,
        board_retro,
    ]

    # Runtime project registration (#167). Appended rather than folded in above because
    # it writes CONFIG, not board rows — it is the only tool here that doesn't touch a
    # store, and it refuses on its own (the operator's onboarding space) rather than on
    # the board's state. `None` when langchain isn't importable in a host-free run.
    from .project_registry import build_register_tool

    if (register_tool := build_register_tool(cfg)) is not None:
        tools.append(register_tool)
    return tools

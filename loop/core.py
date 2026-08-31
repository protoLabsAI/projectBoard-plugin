"""BoardLoop — the assembled public surface (#268).

The historical ``project_board.loop.BoardLoop``. It composes the per-edge mixins
(:mod:`.drive`, :mod:`.reconcile`, :mod:`.preflight`, :mod:`.prompt`) and owns only
construction, shared accessors, and per-project resolution. Edge logic lives in the
sibling modules, so a change confined to one edge lands there — not on this surface.
"""

from __future__ import annotations

import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace
from .drive import DriveMixin
from .preflight import PreflightMixin
from .prompt import PromptMixin
from .reconcile import ReconcileMixin

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


class BoardLoop(DriveMixin, ReconcileMixin, PreflightMixin, PromptMixin):
    def __init__(self, cfg: dict, *, gap_reporter: setup_check.GapReporter | None = None):
        self.cfg = cfg or {}
        # Setup preflight (v0.42.0): the host-gap reporter register() built around the
        # plugin registry (a no-op reporter when the host lacks the seam or — in the
        # suite — none is handed in), and the PATH probe the preflight uses. Both are
        # attributes so a test can swap them without touching the environment.
        self._gap_reporter = gap_reporter or setup_check.GapReporter(None)
        self._which = shutil.which
        # Per-feature project resolution (#90 slice 2): the board's `projects:` map
        # (name → execution settings, resolved by projects.resolve_projects) so a single
        # loop can serve several repos. Absent a map, resolve_projects synthesizes a
        # SINGLE implicit project from the flat top-level keys, so a pre-#90 config
        # behaves exactly as before — every feature resolves to `self._default_project`.
        # `_project_cfg(feature)` reads the `project:<name>` label stamped in slice 1 and
        # returns THAT project's settings; every repo/gate/coder/preflight decision below
        # resolves through it, falling back to the instance-level knobs the flat config
        # parsed. `_default_project` is the fallback for a feature carrying no label.
        self._projects = resolve_projects(self.cfg)
        self._default_project = resolve_default_project(self.cfg)
        # NO default coder name (v0.42.0). An unset `coder` is "" — a preflight failure
        # the operator sees (setup_check), never a phantom delegate name that exists on
        # one machine and blocks every feature with "delegate not configured" elsewhere.
        self.coder_name = str(self.cfg.get("coder") or "").strip()
        self.reviewer_name = self.cfg.get("reviewer", "quinn")
        # Review dispatch is OPT-IN (default off). The fleet's PR-review pipeline
        # already reviews PRs the moment they're opened, so the loop doesn't need to
        # `delegate_to(reviewer)` — it just opens the PR and lets the pipeline + CI +
        # the merge webhook gate it. Turn this on only for repos NOT covered by a
        # PR-review pipeline (then a reachable `reviewer` a2a delegate is required).
        self.review_dispatch = bool(self.cfg.get("review_dispatch", False))
        # BLOCKING review gate (plan M5, OPT-IN, default off — review_dispatch stays
        # the advisory alternative). After open_review the loop runs the host's
        # adversarial `code-review` workflow (ADR 0077) on the PR, parses the findings
        # convention, and: clean → the feature stays in_review for the merge edge;
        # blocking findings → bounce back to the coder with the findings injected into
        # the retry prompt, EXACTLY like the CI bounce, bounded by `review_fix_max`
        # (mirror of ci_fix_max); exhaustion → flag_blocked — never a silent merge.
        self.review_gate = bool(self.cfg.get("review_gate", False))
        self.review_workflow = str(self.cfg.get("review_workflow", "code-review")).strip() or "code-review"
        self.review_fix_max = max(0, int(self.cfg.get("review_fix_max", 2)))
        # Cap on consecutive UNRUNNABLE gate attempts (runner missing / workflow dying /
        # panel steps failing) before escalating to the operator via flag_blocked —
        # fail closed without re-burning the workflow every poll forever (ADR 0078 D3).
        self.review_run_max = max(1, int(self.cfg.get("review_run_max", 3)))
        # Goal-verification gate (OPT-IN, default off). When on, a DETERMINISTIC pre-PR
        # check (no LLM, no diff dump): a code change must ship a test — CI runs tests but
        # can't require their presence, so the gate does. A miss → re-dispatch/escalate
        # instead of opening a testless PR; correctness itself is CI's job. (Was an
        # LLM-eyeballs-the-diff check — it false-rejected tests it couldn't see past the
        # diff truncation, burning whole tier ladders on phantom gaps; see _verify_goal.)
        self.goal_verify = bool(self.cfg.get("goal_verify", False))
        # Max-Mode (MiMo Tier-2, OPT-IN, default 1 = off). When >1, a hard feature is
        # attempted with N parallel candidates and `_judge_candidates` picks the best
        # diff. Costs N× tokens, so gate it to hard work. The parallel-dispatch wiring
        # is tracked in #21; this ships the reusable best-of-N judge it composes.
        self.max_mode_n = max(1, int(self.cfg.get("max_mode_n", 1) or 1))
        self.interval = float(self.cfg.get("loop_interval_s", 30))
        self.root = self.cfg.get("worktrees_root", ".worktrees")
        self.enabled = bool(self.cfg.get("loop_enabled", False))
        # Escalation is OPT-IN: a `coders` map (tier → delegate name) with ≥2
        # distinct delegates. With a single ACP coder there's no ladder — one
        # dispatch, then Blocked on failure — so difficulty/tier stay irrelevant
        # and we never write redundant tier/attempt labels.
        # A rung may name ONE delegate ("sonnet") or SEVERAL interchangeable ones
        # (["codex", "sonnet"]) — see rung_delegates. Normalised to a list here so every
        # consumer sees one shape; a bare string stays a one-element rung, so existing
        # configs behave exactly as before.
        self.coders = {str(k): rung_delegates(v) for k, v in (self.cfg.get("coders") or {}).items()}
        self.escalation_on = escalation_enabled(self.cfg)
        # Concurrency: drive up to `max_concurrent` features at once, each in its own
        # worktree. 1 (the default) = serial — the safe default for token + merge-
        # integration cost; raise it on a repo that parallelizes cleanly. LIVE: a
        # config reload re-applies it to the running loop via `reload()` (ADR 0018
        # register_surface reload=), so the console Settings field takes effect on
        # the next tick — no restart. Lowering it never kills a drive: in-flight
        # builds finish and the loop just stops claiming until it's under the cap.
        self.max_concurrent = _knob_int(self.cfg, "max_concurrent", 1, floor=1)
        # Review-queue WIP limit: pause new claims when this many PRs already await
        # review, so the loop can't pile up PRs faster than they merge (flooding CI /
        # reviewers). 0 = unlimited. LIVE (see max_concurrent).
        self.max_pending_reviews = _knob_int(self.cfg, "max_pending_reviews", 5, floor=0)
        # Dependency gate: "merge" (default) — a dependent waits for every blocker to
        # merge (done); "review" — a NON-foundation blocker releases its dependents at
        # in_review (more parallelism, at the risk of building on un-merged code).
        # Foundation blockers always gate on merge.
        self.relaxed_gate = str(self.cfg.get("dep_gate", "merge")).lower() == "review"
        # Stuck-drive watchdog: hard cap on a single coder dispatch (the only
        # otherwise-unbounded await in a drive — git/gh calls already self-time-out).
        # 0 disables it. A timeout reaps the coder subprocess and is a capability
        # failure (escalate-or-block), not a transient retry.
        self.coder_timeout = float(self.cfg.get("coder_timeout_s", 1800))
        # Merge poll: a fallback to the /webhook/pr Done edge for deployments with no
        # public webhook URL. On by default (cheap; only probes `in_review` PRs).
        self.merge_poll = bool(self.cfg.get("merge_poll", True))
        self.merge_poll_interval = float(self.cfg.get("merge_poll_interval_s", 60))
        # Health sweep: periodic self-heal (reclaim slots from dead drives, reap
        # orphaned worktrees). 0 disables it.
        self.sweep_interval = float(self.cfg.get("health_sweep_interval_s", 300))
        # Archive window (#115): terminal features (done/cancelled) whose closed_at is
        # older than this many days get the `archived` label during the health sweep —
        # out of the default board view, NEVER deleted (query back via
        # include_archived). Rides the sweep cadence; no scheduler of its own.
        self.archive_after_days = float(self.cfg.get("archive_after_days", 7))
        # CI-feedback edge (closed-loop verify): poll in_review PRs' check-runs and,
        # on a FAILING rollup, bounce the feature back to the coder with the failure
        # injected as feedback (vs the old open-loop: a red PR sat in_review forever).
        # Rides the merge-poll cadence. `ci_fix_max` caps re-dispatches before the
        # feature is blocked for human triage (a real bug, not a self-fixable nit).
        self.ci_poll = bool(self.cfg.get("ci_poll", self.merge_poll))
        self.ci_fix_max = max(0, int(self.cfg.get("ci_fix_max", 2)))
        # Auto-rebase a stale/conflicting in_review PR onto base. Parallel PRs branch
        # off the SAME base, and the hot-file guard serializes DISPATCH not the branch
        # BASE — so each merge re-stales the others (a sibling's change lands in the
        # same files). On BEHIND (stale, no conflict) a clean rebase + force-push fixes
        # it with NO coder; on DIRTY (a real conflict) the rebase aborts and the coder
        # is re-dispatched to re-resolve, bounded by rebase_fix_max. Rides the
        # merge-poll cadence; defaults to merge_poll's value. The same flag also
        # gates the merged-state VERIFY (#131): a CLEAN PR whose base moved gets the
        # gate re-run against the merged state (no push) and the sha stamped as
        # `merged-verified:<sha>` — bounded by the same rebase_fix_max budget.
        self.auto_rebase = bool(self.cfg.get("auto_rebase", self.merge_poll))
        self.rebase_fix_max = max(0, int(self.cfg.get("rebase_fix_max", 1)))
        # Re-verify budget for the merged-state gate (#131): how many times a single
        # in_review card's verdict is re-run after a sibling merge moves base under it.
        # Used to ride rebase_fix_max (=1) — fine when a human adjudicates a stale
        # stamp, fatal when the loop is the adjudicator: with auto_merge on, a card
        # held for review across ONE sibling merge went stale and parked forever
        # (2026-08-20, bd-c3k/bd-ohx). Each re-verify costs one gate run and only
        # happens when base actually moved, so the bound is "how many sibling merges
        # can a held card survive". 0 = unlimited.
        self.merged_verify_max = max(0, int(self.cfg.get("merged_verify_max", 5)))
        # The MERGE edge (opt-in, LIVE): when an in_review PR is green by every gate the
        # loop itself runs — GitHub says CLEAN (required checks + branch protection),
        # the merged-state verdict is stamped against the CURRENT base, the review gate
        # recorded review-clean — merge it. Before this the loop built, verified, and
        # reviewed PRs and then parked them "for the adjudicator": a chat-driven sweep
        # that is only as durable as the session scheduling it (2026-08-20: 7 green PRs
        # sat overnight after a restart dropped that session's one-shot). A card
        # labelled `merge-hold` is never auto-merged — the operator's per-card veto.
        self.auto_merge = _knob_bool(self.cfg, "auto_merge", False)
        # `br` fetched on first run (v0.43.0, br_fetch.py): default on. Read here so the
        # live-knob machinery (reload) has an attribute to diff against; the fetch itself
        # is armed by register() and re-armed by the setup gate below.
        self.br_autofetch = _knob_bool(self.cfg, "br_autofetch", True)
        self.merge_method = str(self.cfg.get("merge_method", "squash")).strip().lower() or "squash"
        # Failed merge attempts (a refusal, a race) before the loop stops trying and
        # leaves the PR for a human — logged once on the bead, never a block.
        self.auto_merge_max = max(1, int(self.cfg.get("auto_merge_max", 3)))
        # Pre-PR goal-verify gap: a rejected diff (e.g. missing tests) is fixable by
        # the SAME coder told what's missing — NOT a model-capability failure. So
        # carry the gap as feedback + re-dispatch the same tier, bounded by
        # `goal_fix_max`, BEFORE escalating/blocking (else a top-tier diff:large
        # feature blocks on attempt 1 with no chance to add the tests).
        self.goal_fix_max = max(0, int(self.cfg.get("goal_fix_max", 2)))
        # Empty-result cap (#198, retry policy #2991): a dispatch that COMPLETES with
        # no worktree diff AND no tool-call activity is `empty_result` — the coder
        # connected but never executed (a wedged adapter/session, a refusal). Often a
        # transient ACP hiccup, so occurrences below this cap retry on the SAME tier
        # (same prompt) before any failure is counted — pre-escalation, no ladder
        # attempt spent. Once the cap is hit the failure IS recorded and the normal
        # escalation ladder proceeds (single coder / ladder top → blocked for triage
        # with the failure class + evidence in the reason).
        self.empty_result_max = max(1, int(self.cfg.get("empty_result_max", 2)))
        # Auto-fix command run in the worktree BEFORE opening the PR (e.g.
        # "ruff check --fix . && ruff format ."). The coder is edit-only — it can't run
        # the repo's linter/formatter, so trivial lint/format nits would otherwise fail
        # CI and burn a whole bounce/escalation (bd-2fd: a full opus fix blocked on one
        # unused import). Best-effort; CI is still the real gate. Empty = off.
        self.format_cmd = str(self.cfg.get("format_cmd", "")).strip()
        # Pre-PR LOCAL GATE: the repo's real check command(s) run in the worktree
        # AFTER fixups and BEFORE open_pr (e.g. "ruff check . && uv run --no-sync pytest
        # tests/ -q"). The coder is edit-only — it can't run the suite — so a failure on
        # a knowable fact (a lint nit, a golden-map test, a wrong schema/column, version
        # drift) only surfaces in CI, then thrashes the bounce/escalation ladder. Running
        # it here hands the SAME coder the actual output to fix in-worktree, so the PR
        # opens already-green. Best-effort early filter: if it can't pass within
        # local_gate_max same-tier tries, the PR opens anyway (CI + the ci-fix budget
        # stay the backstop) — a flaky/misconfigured gate never blocks good work. Empty = off.
        # ``auto`` ⇒ discover the gate from the bound repo (see _resolve_gate_cmd);
        # an explicit command or blank (= no gate) passes through. Resolved here once,
        # so every downstream reader (coder_solve_test_cmd, _run_local_gate, _preflight,
        # candidate preference) sees the concrete command with no further plumbing.
        self.local_gate_cmd = _resolve_gate_cmd(str(self.cfg.get("local_gate_cmd", "")), str(self.cfg.get("repo", ".")))
        self.local_gate_max = max(0, int(self.cfg.get("local_gate_max", 2)))
        self.local_gate_timeout = float(self.cfg.get("local_gate_timeout_s", 600))
        self.local_gate_output_chars = max(500, int(self.cfg.get("local_gate_output_chars", 4000)))
        # Gate PREFLIGHT (fail-CLOSED; default on when a gate is configured). Before
        # dispatching ANY work, smoke-run ``local_gate_cmd`` on the CLEAN base checkout.
        # If the gate can't launch (missing tool) or fails on the untouched base, the
        # coder environment is broken — HOLD all ready work (flag it blocked, with the
        # reason, so the stall is visible on the board) rather than burn generations on a
        # gate no coder could pass, and re-check each cycle so work resumes the moment
        # it's fixed. This is the fail-CLOSED complement to ``_run_local_gate``'s per-PR
        # fail-OPEN: a flaky gate must never block good work, but an UNRUNNABLE gate must
        # never start bad work. A healthy repo passes instantly — nothing changes. A
        # preflight timeout is treated as indeterminate → allow (never wedge on a slow
        # gate). Opt out with ``preflight: false``.
        self.preflight = bool(self.cfg.get("preflight", True))
        self.preflight_timeout = float(self.cfg.get("preflight_timeout_s", self.local_gate_timeout))
        # Per-PROJECT preflight isolation (#90 slice 2): keyed by project name, not a
        # single scalar — a broken gate in project A holds only A's ready work while B
        # keeps dispatching. Each value is None=unchecked, True=runnable, str=failure
        # reason; `_preflight_held[name]` is the set of fids THIS loop blocked for that
        # project's failed preflight; `_last_preflight[name]` throttles its re-checks.
        self._preflight_state: dict[str, bool | str | None] = {}
        # Projects whose last preflight ran against a non-base checkout (#255): the
        # verdict was downgraded to indeterminate, and /status says so rather than
        # leaving the operator with a silently-permissive gate.
        self._preflight_dirty: dict[str, str] = {}
        self._last_preflight: dict[str, float] = {}
        self._preflight_held: dict[str, set[str]] = {}
        # When each project's CURRENT failure reason was first logged in full (#263).
        # A held project re-checks every ~60s; the multi-KB gate tail is ERROR-worthy
        # once per DISTINCT reason, and identical repeats collapse to a one-line
        # "still held (Ns)" WARNING (see _record_preflight_failure).
        self._preflight_failed_at: dict[str, float] = {}
        # ── coder.solve() board seam (ADR 0064 P2, opt-in) ─────────────────────────
        # Route a FRESH build (not a keep-worktree/CI-bounce re-dispatch) through the
        # `coder` plugin's execution-grounded solve() ladder (greedy → best-of-k →
        # tree-search) instead of a single delegate_to(acp) shot — gated on the
        # feature's acceptance tests actually PASSING in a real worktree, never an
        # LLM judge. HONEST DEGRADE (coder_seam.should_use_solve): only fires when
        # the `coder` plugin is importable (host has it enabled) AND this feature
        # carries acceptance_criteria AND a runnable test command is configured
        # below — missing any of the three falls back to today's single shot, so an
        # existing deployment can't regress just by upgrading. Composes WITH (does
        # NOT replace) the coders-map tier ladder: solve() searches within the
        # CURRENT tier; a search that never passes raises SolveExhausted, which
        # `_drive` treats as the same capability failure as a no-diff dispatch
        # (escalate a tier, or block) — the tier ladder still climbs when search
        # itself stalls.
        #
        # Precedence vs. Max-Mode (`max_mode_n>1`, below): coder_solve ONLY preempts
        # Max-Mode when Max-Mode itself is off (`max_mode_n<=1`) — see
        # `_use_coder_solve`. Without this, a board already running the README's own
        # execution-grounded Max-Mode recipe (`max_mode_n>1` + `local_gate_cmd`) would
        # silently stop using Max-Mode the moment the separate `coder` plugin became
        # importable for any unrelated reason, with zero change to THIS board's own
        # config — and unlike Max-Mode's LLM-judge fallback (which always ships a
        # best-effort PR), an exhausted solve() ladder blocks the feature outright.
        # That's a behavior change an operator must opt into, not inherit for free.
        self.coder_solve = bool(self.cfg.get("coder_solve", True))
        # The ladder's verifier: the command that runs THIS feature's (coder-
        # authored) acceptance tests in a candidate worktree, e.g. "pytest tests/ -q".
        # Blank ⇒ falls back to local_gate_cmd (many repos already configure that as
        # the real test command); still blank ⇒ no runnable oracle ⇒ honest degrade.
        self.coder_solve_test_cmd = str(self.cfg.get("coder_solve_test_cmd", "")).strip() or self.local_gate_cmd
        self.coder_solve_test_timeout = float(self.cfg.get("coder_solve_test_timeout_s", 300))
        self.coder_solve_budget = max(1, int(self.cfg.get("coder_solve_budget", 6)))
        self.coder_solve_k = max(1, int(self.cfg.get("coder_solve_k", 3)))
        self.coder_solve_tree_depth = max(0, int(self.cfg.get("coder_solve_tree_depth", 2)))
        # max_concurrent is FEATURE-level (one drive per slot). Within each drive the
        # best-of-k rung dispatches `coder_solve_k` ACP sessions concurrently, so peak
        # ACP processes = max_concurrent × coder_solve_k. Set max_concurrent_sessions to
        # cap that (0 = unlimited within the k budget; 1 = serialise k candidates).
        # LIVE (see max_concurrent) — applies to the NEXT dispatch; a running
        # best-of-k fan-out keeps the semaphore it was built with.
        self.max_concurrent_sessions = _knob_int(self.cfg, "max_concurrent_sessions", 0, floor=0)
        # Rung 4 (ADR 0064 P3): a richer generator for the HARDEST features — reached
        # only after greedy AND best-of-k AND tree-search all fail their tests. Fusion
        # can't tool-call (it's a plain completion, not an ACP session), so it's an
        # `openai`-type delegate name, resolved per-dispatch in `_drive` (mirroring how
        # `coder`/`reviewer` are resolved) — never here, this is just config plumbing.
        # Blank ⇒ no fusion rung; the ladder stops at tree-search exactly as before.
        self.coder_solve_fusion_delegate = str(self.cfg.get("coder_solve_fusion_delegate", "")).strip()
        self.coder_solve_fusion_k = max(1, int(self.cfg.get("coder_solve_fusion_k", 2)))
        # Fusion can't tool-call and returns whole-file replacements with no diff —
        # a file over this cap risks a silent truncated "complete" rewrite (see
        # coder_seam.fusion_viable_for_files). Gated BEFORE dispatch, not after:
        # an oversized feature just skips the fusion rung (fusion_delegate=None
        # for that dispatch), it never gets to attempt-and-corrupt.
        self.coder_solve_fusion_max_file_chars = max(
            1, int(self.cfg.get("coder_solve_fusion_max_file_chars", coder_seam.FUSION_MAX_FILE_CHARS_DEFAULT))
        )
        self.coder_solve_fusion_max_total_chars = max(
            1, int(self.cfg.get("coder_solve_fusion_max_total_chars", coder_seam.FUSION_MAX_TOTAL_CHARS_DEFAULT))
        )
        # KG lessons (the flywheel READ half): before dispatching a coder, query the
        # knowledge graph (via graph.sdk) for distilled lessons relevant to THIS feature
        # and inject them into the prompt — so the coder heeds this area's known failure
        # modes on attempt 1. The loop-retro writes those lessons (domain `loop-lessons`).
        # Best-effort: any SDK/store error → no injection (never blocks a build). Off when
        # kg_lessons is false or no store is configured.
        self.kg_lessons = bool(self.cfg.get("kg_lessons", True))
        self.kg_lessons_k = max(1, int(self.cfg.get("kg_lessons_k", 3)))
        self.kg_lessons_domain = str(self.cfg.get("kg_lessons_domain", "loop-lessons")).strip()
        # Repo standing gate files (#108): files EVERY change in this repo must keep
        # green (a CHANGELOG, a coverage manifest, an API golden…). A tight card-level
        # `files_to_modify` would otherwise suppress them, since a card author can't
        # enumerate per-repo obligations. These ride the coder prompt as a separate
        # block — a prompt addition, NOT ledger items (#113) and NOT `files_to_modify`
        # entries. Per-repo, default empty. Accepts a list, or a comma/space-separated
        # string (mirrors env_passthrough), de-duplicated with order preserved.
        self.gate_files = _parse_gate_files(self.cfg.get("gate_files"))
        # Repo conventions (#108): free-text markdown the host repo populates with the
        # standing rules a coder must follow that no card author will enumerate per-card
        # — "CI runs ruff", "every PR needs a changelog fragment", "this file is
        # GENERATED, don't hand-edit it", "if a convention here doesn't exist, STOP".
        # Where `gate_files` names the PATHS that must stay green, this carries the
        # RULES + formats around them. Injected verbatim as a `## Repo conventions`
        # block in the coder prompt (no per-card opt-out). Empty by default → no block.
        self.repo_conventions = str(self.cfg.get("repo_conventions", "") or "")
        # Env hygiene (#78, tightened by F8a): the host identifies/authenticates THIS
        # agent via env vars (AGENT_NAME, PROTOAGENT_*, A2A_* — see config.py). None of
        # them belong to the gate preflight, the pre-PR local_gate_cmd, the format_cmd,
        # or the coder we spawn. The loop's own gate/format/preflight children get the
        # narrow allowlist baseline only (config.sanitized_env(mode="allowlist") via
        # _child_env); the coder's ACP session env stays blacklist-stripped.
        # ``env_passthrough`` is the escape hatch on both tiers: a deployment that
        # legitimately needs a specific var to reach children whitelists it here (a
        # list, or a comma/space-separated string).
        self.env_passthrough = config.parse_env_passthrough(self.cfg)
        self._store_kw = dict(
            db=self.cfg.get("db_path") or None,
            repo=self.cfg.get("repo", "."),
            base_branch=self.cfg.get("base_branch", "main"),
            max_files_by_difficulty=self.cfg.get("max_files_by_difficulty"),
            # #90: hand the store the same resolved map, so its own per-feature reads
            # (the Ready gate's `_repo_for`) resolve to the same project the loop does.
            projects=self._projects,
            default_project=self._default_project,
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._shutting_down: bool = False
        # The running drive tasks, and the worktrees they hold (fid → (repo, wt,
        # branch)) so shutdown can reap any a cancel mid-drive would orphan; the coder
        # subprocess itself is reaped by dispatch_coder's finally.
        self._drives: set[asyncio.Task] = set()
        self._inflight: dict[str, tuple[str, str, str]] = {}
        # files_to_modify of each in-flight feature, for the hot-file overlap guard
        # (don't run two parallel coders that edit the same file → sure conflict).
        self._inflight_files: dict[str, set[tuple[str, str]]] = {}  # fid -> {(project, path)} (#197)
        # #311: at most ONE self-dispatch (a task the board runs through HOST.invoke as
        # its own agent) in flight per board — a second self-assigned task parks rather
        # than invoking the host recursively. Set before the self-drive is spawned and
        # cleared by its done-callback (which fires for EVERY terminal state), so the
        # one-in-flight guard can never leak even on a cancel-before-start.
        self._self_inflight: bool = False
        self._last_poll = 0.0  # monotonic ts of the last merge poll
        self._last_sweep = 0.0  # monotonic ts of the last health sweep
        # CI-feedback state (in-memory, per run): fid → last failing-CI summary (fed
        # into the re-dispatch prompt) and fid → count of CI-fix re-dispatches so far.
        # The counter dicts below (every `_BUDGET_KINDS` entry) are CACHES over the
        # bead's persisted `budget:<kind>:<n>` labels (#259) — consult/spend/reset
        # them ONLY through _budget_get/_budget_set/_budget_reset, so a restart
        # resumes each budget from the bead and an exhausted budget still blocks.
        # The prompt-feedback dicts (_ci_feedback/_ci_prior_diff/_review_prior)
        # stay memory-only: they enrich the next prompt, they never gate anything.
        self._ci_feedback: dict[str, str] = {}
        self._ci_prior_diff: dict[str, str] = {}
        self._ci_fix_attempts: dict[str, int] = {}
        # Pre-PR goal-verify gap re-dispatches so far (fid → count), same-tier.
        self._goal_fix_attempts: dict[str, int] = {}
        # empty_result occurrences so far (fid → count, #198) — a completed dispatch
        # with no diff AND no tool-call activity. Retried once on the SAME tier
        # before any failure is counted (#2991); after empty_result_max occurrences
        # the failure is recorded and the normal escalation ladder proceeds. Reset
        # on a climb, so each tier gets its own retry window.
        self._empty_results: dict[str, int] = {}
        # Pre-PR local-gate failure re-dispatches so far (fid → count), same-tier.
        self._gate_fix_attempts: dict[str, int] = {}
        # Pre-PR requirement-ledger bounces so far (fid → count, #113) — open items
        # at the open_pr seam re-dispatch same-tier/keep-worktree with the open list,
        # sharing the goal-fix budget knob (goal_fix_max: the same "the coder didn't
        # deliver everything it was told; tell it what's missing" class).
        self._req_fix_attempts: dict[str, int] = {}
        # Rebase-conflict re-dispatches so far (fid → count) when a sibling merge
        # leaves a PR with a real (non-clean) conflict against base.
        self._rebase_attempts: dict[str, int] = {}
        # Merged-state verifications run so far (fid → count, #131) — the verdict
        # half of the rebase edge, bounded by the same rebase_fix_max budget so a
        # base that moves repeatedly doesn't burn a gate run every poll forever.
        self._merged_verify_attempts: dict[str, int] = {}
        # ADR 0326: serializes the ONE-TIME merged-verify exhaustion sentinel write
        # (`_arm_merged_verify_exhaustion`) against the operator budget reset
        # (`_invalidate_merged_verify_budget`), which run on different threads (the async
        # reconcile via to_thread vs the reset verb's worker thread). Held across the
        # cache mutation AND its label persist so the reset's pinned 0 + label clear and
        # the sentinel's max+1 can never interleave — the reset always wins.
        self._mv_reset_lock = threading.Lock()
        self._auto_merge_failures: dict[str, int] = {}
        # #211: drives whose operator-cancel cleanup already ran (or is running) — a
        # second cancel verb landing mid-cleanup re-enters _drive's CancelledError
        # handler; this keeps the close + trail comment from happening twice.
        self._cancel_done: set[str] = set()
        # #207: cards whose auto-merge is holding on a DRAFT PR and have been told so
        # on the bead (ONE comment per hold, mirroring the give-up comment — a failed
        # `gh pr ready` (fork PR, no write on base) is otherwise a silent permanent
        # hold at DEBUG). Cleared when the PR is seen non-draft again.
        self._draft_noted: set[str] = set()
        # Review-gate bounce re-dispatches so far (fid → count), same-tier — the
        # review sibling of _ci_fix_attempts (plan M5).
        self._review_fix_attempts: dict[str, int] = {}
        # Consecutive review runs that could not complete (panel step failed / no
        # runner) — after review_run_max the feature is Blocked for the operator
        # instead of re-burning the workflow every poll (ADR 0078 D3: fail closed,
        # escalate; never judge from a partial panel).
        self._review_run_failures: dict[str, int] = {}
        # How many times the blocked sweep has auto-cleared each card (see
        # _recover_blocked); past _UNBLOCK_RETRY_MAX the operator is told instead.
        self._unblock_retries: dict[str, int] = {}
        # Feature ids already reported to the operator as stuck, so a card that stays
        # blocked is announced once rather than every sweep (see _notify_operator). This
        # set is a per-process CACHE only: the DURABLE dedup is the bead's
        # `notified:blocked` marker (#341), which a restart reads back so it does not
        # re-alert a block the operator was already told about — this set is rebuilt empty
        # on every restart and must never be the sole record.
        self._notified_blocks: set[str] = set()
        # Last parsed findings JSON per fid — fed back as the recipe's
        # prior_findings input so a bounce re-review is a DELTA review
        # (GitHub-native review memory, ADR 0078 D5).
        self._review_prior: dict[str, str] = {}
        # Features whose review gate is RUNNING in this process right now (#205).
        # The gate has two call sites — the drive (PR just opened) and the
        # reconcile's resume edge (an in_review card still review-pending) — and
        # the resume edge cannot tell an *interrupted* gate from one that is simply
        # still running: the pending label is set the moment the gate starts and a
        # panel takes minutes, while the reconcile polls every merge_poll_interval.
        # Without this guard every PR was reviewed twice on the same head (2× panel
        # tokens), a duplicate verdict burned the bounce budget (1/2 → 2/2 with no
        # fix between), and two non-deterministic verdicts raced for the label.
        self._review_inflight: set[str] = set()
        # Consecutive failed reap attempts per worktree id (not path, so it survives
        # repo-path changes). After _REAP_WARN_CAP failures the noise is downgraded
        # from WARNING to DEBUG; a successful reap resets the counter.
        self._reap_failures: dict[str, int] = {}
        # In-flight background store write-backs per fid (#258): coder_seam's
        # record_gens/record_verified callbacks are SYNC and invoked ON the event loop
        # mid-dispatch, so they can't await asyncio.to_thread — _record_bg schedules
        # the br call on a worker thread instead and parks the future here; the drive
        # awaits the batch right after dispatch returns (_await_bg_records), keeping
        # the pre-#258 ordering (records land before the PR opens) minus the stall.
        self._bg_records: dict[str, list] = {}

    def _store(self):
        return _loop.get_store(**self._store_kw)

    # ── persisted fix budgets (#259): bead labels are truth, the dicts are caches ─
    def _budget_cache(self, kind: str) -> dict[str, int]:
        return getattr(self, _BUDGET_KINDS[kind])

    async def _budget_get(self, store, fid: str, kind: str, feature: dict | None = None) -> int:
        """The counter's current value: the cache when this process already consulted
        it, else DERIVED from the bead's `budget:<kind>:<n>` label — a freshly
        constructed loop resumes the budget where the last process left it. Pass the
        in-hand ``feature`` projection to derive without a store read; a read hiccup
        derives 0 (fail open: the budget re-counts, it never blocks spuriously). The
        cache always wins over the projection: a mid-flow ``_budget_reset`` pins 0
        there precisely so a caller's pre-reset ``feature`` (its labels still carry
        the old count) can never rehydrate a budget the reset just granted back."""
        cache = self._budget_cache(kind)
        if fid in cache:
            return cache[fid]
        if feature is None:
            try:
                feature = await asyncio.to_thread(store.get_feature, fid)
            except Exception:  # noqa: BLE001 — a derive hiccup must never break the edge
                feature = None
        n = budgets_from_labels((feature or {}).get("labels")).get(kind, 0)
        if n:  # keep the dicts' "no key ⇒ nothing ever spent" semantics; 0 re-derives
            cache[fid] = n
        return n

    async def _budget_set(self, store, fid: str, kind: str, n: int) -> None:
        """Spend: write the counter to the cache AND the bead (`budget:<kind>:<n>`,
        replaced — the `gens:` pattern). The label write is best-effort: a `br`
        hiccup must never fail the edge that spent the budget (the cache still
        carries it for this process; the next process derives one count lower)."""
        self._budget_cache(kind)[fid] = n
        try:
            await asyncio.to_thread(store.record_budget, fid, kind, n)
        except Exception:  # noqa: BLE001 — bookkeeping must never break the edge
            log.warning("[project_board] %s budget %s=%d not persisted", fid, kind, n, exc_info=True)

    async def _budget_reset(self, store, fid: str, *kinds: str) -> None:
        """Reset counters — cache and bead labels together. No ``kinds`` = ALL of
        them: the terminal edges (merge / PR-closed), where the fid leaves the
        flow, so the cache keys drop outright. Named ``kinds`` are the MID-FLOW
        edges (a tier climb, a gate pass, a clean review) — there the caller keeps
        driving with the ``feature`` projection it already holds, whose labels
        still carry the pre-reset counts, so the cache must PIN 0 (authoritative
        "freshly reset"), never just forget the fid: a popped key would let the
        very next ``_budget_get(..., feature)`` rehydrate the exhausted count from
        that stale snapshot and block the fresh window the reset granted.
        Best-effort on the bead, like ``_budget_set``."""
        names = kinds or tuple(_BUDGET_KINDS)
        for kind in names:
            if kinds:
                self._budget_cache(kind)[fid] = 0
            else:
                self._budget_cache(kind).pop(fid, None)
        try:
            await asyncio.to_thread(store.clear_budgets, fid, list(kinds) if kinds else None)
        except Exception:  # noqa: BLE001 — bookkeeping must never break the edge
            log.warning("[project_board] %s budget reset (%s) not persisted", fid, ", ".join(names), exc_info=True)

    def _child_env(self) -> dict[str, str]:
        """The sanitized environment for a subprocess the loop spawns directly (gate
        preflight, ``local_gate_cmd``, ``format_cmd``). These children run repo-defined
        commands over coder-written code, so they get the narrow ALLOWLIST baseline
        (PATH/HOME/locale/TMPDIR/TERM/SHELL/USER/CI and its Windows system mirror —
        see config.py) plus ``env_passthrough`` — not merely ``os.environ`` minus the
        host block (F8a, tightening #78). The coder's ACP session environment stays
        on the blacklist tier (see config.py)."""
        return config.sanitized_env(self.env_passthrough, mode="allowlist")

    # ── per-feature project resolution (#90 slice 2) ──────────────────────────────
    def _project_name(self, feature: dict) -> str:
        """The project a feature builds in: its `project:<name>` label (stamped in
        slice 1), or the board's default project when the feature carries none — a
        pre-#90 feature, or a board with no `projects:` map."""
        return str(feature.get("project") or "").strip() or self._default_project

    def _project_cfg(self, feature: dict) -> dict:
        """The resolved execution settings for THIS feature's project — repo,
        base_branch, local_gate_cmd, coders, gate_files, repo_conventions, every
        `coder_solve_*` knob, … (see projects.py). Falls back to the default project's
        settings when the feature's label names no known project (or names none), so a
        single-repo board and every pre-#90 feature resolve exactly as before."""
        entry = self._projects.get(self._project_name(feature))
        if entry is None:
            entry = self._projects.get(self._default_project)
        return entry or {}

    def _repo_for(self, feature: dict) -> str:
        """The repo root this feature builds in (#90). A feature carrying an explicit
        `project:<name>` label resolves STRICTLY to that project's `repo` — overriding
        the instance default the store stamped on it (this is the whole point of the
        slice: a labeled feature builds in ITS repo, not the board's). An unlabeled
        feature — pre-#90, or a board with no `projects:` map — keeps the store-stamped
        repo, then the default project's, then the instance default (back-compat)."""
        name = str(feature.get("project") or "").strip()
        if name:
            repo = str((self._projects.get(name) or {}).get("repo") or "").strip()
            if repo:
                return repo
        return (
            str(feature.get("repo") or "").strip()
            or str(self._project_cfg(feature).get("repo") or "").strip()
            or self._store_kw["repo"]
        )

    def _base_branch_for(self, feature: dict) -> str:
        """The base branch this feature's PR targets (#90). A labeled feature whose
        project declares a `base_branch` uses it; otherwise the store-stamped value, the
        default project's, then the instance default."""
        name = str(feature.get("project") or "").strip()
        if name:
            base = str((self._projects.get(name) or {}).get("base_branch") or "").strip()
            if base:
                return base
        return (
            str(feature.get("base_branch") or "").strip()
            or str(self._project_cfg(feature).get("base_branch") or "").strip()
            or self._store_kw.get("base_branch")
            or "main"
        )

    def _local_gate_cmd_for(self, feature: dict) -> str:
        """The pre-PR / preflight gate command for this feature's project (#90). When
        the project entry declares `local_gate_cmd` it is resolved against THAT project's
        repo (so ``auto`` discovers the right checkout); otherwise the instance-level
        gate the flat config already resolved at init."""
        pc = self._project_cfg(feature)
        if "local_gate_cmd" in pc:
            return _resolve_gate_cmd(str(pc.get("local_gate_cmd") or ""), self._repo_for(feature))
        return self.local_gate_cmd

    def _format_cmd_for(self, feature: dict) -> str:
        """The pre-PR auto-fix command (``format_cmd``) for this feature's project
        (#90), else the instance default."""
        pc = self._project_cfg(feature)
        if "format_cmd" in pc:
            return str(pc.get("format_cmd") or "").strip()
        return self.format_cmd

    def _gate_files_for(self, feature: dict) -> list[str]:
        """The repo standing gate files (#108) for this feature's project (#90), else
        the instance default."""
        pc = self._project_cfg(feature)
        if "gate_files" in pc:
            return _parse_gate_files(pc.get("gate_files"))
        return self.gate_files

    def _repo_conventions_for(self, feature: dict) -> str:
        """The repo conventions prose (#108) for this feature's project (#90), else the
        instance default."""
        pc = self._project_cfg(feature)
        if "repo_conventions" in pc:
            return str(pc.get("repo_conventions") or "")
        return self.repo_conventions

    def _coders_for(self, feature: dict) -> dict[str, list[str]]:
        """The tier→delegate `coders` map for this feature's project (#90), else the
        instance default — so escalation on a multi-repo board climbs the ladder the
        FEATURE's project declares, not the board's."""
        raw = self._project_cfg(feature).get("coders")
        if isinstance(raw, dict) and raw:
            return {str(k): rung_delegates(v) for k, v in raw.items()}
        return self.coders

    def _coder_solve_test_cmd_for(self, feature: dict) -> str:
        """The coder.solve() verifier command for this feature's project (#90): the
        project's `coder_solve_test_cmd`, else its resolved gate command (many repos
        configure that as the real test command), else the instance default."""
        pc = self._project_cfg(feature)
        v = str(pc.get("coder_solve_test_cmd", "")).strip()
        if v:
            return v
        gate = self._local_gate_cmd_for(feature)
        return gate or self.coder_solve_test_cmd

    def _coder_solve_settings(self, feature: dict) -> dict:
        """Resolve the coder.solve() search knobs for this feature's project (#90). Each
        `coder_solve_*` knob prefers the project entry, falling back to the instance-level
        value the flat config parsed — so a board with no `projects:` map is unchanged."""
        pc = self._project_cfg(feature)

        def _int(key: str, default: int, floor: int) -> int:
            if key in pc:
                try:
                    return max(floor, int(pc[key]))
                except (TypeError, ValueError):
                    return default
            return default

        def _float(key: str, default: float) -> float:
            if key in pc:
                try:
                    return float(pc[key])
                except (TypeError, ValueError):
                    return default
            return default

        fusion = self.coder_solve_fusion_delegate
        if "coder_solve_fusion_delegate" in pc:
            fusion = str(pc.get("coder_solve_fusion_delegate") or "").strip()
        return {
            "test_cmd": self._coder_solve_test_cmd_for(feature),
            "test_timeout": _float("coder_solve_test_timeout_s", self.coder_solve_test_timeout),
            "budget": _int("coder_solve_budget", self.coder_solve_budget, 1),
            "k": _int("coder_solve_k", self.coder_solve_k, 1),
            "tree_depth": _int("coder_solve_tree_depth", self.coder_solve_tree_depth, 0),
            "fusion_delegate": fusion,
            "fusion_k": _int("coder_solve_fusion_k", self.coder_solve_fusion_k, 1),
            "fusion_max_file_chars": _int(
                "coder_solve_fusion_max_file_chars", self.coder_solve_fusion_max_file_chars, 1
            ),
            "fusion_max_total_chars": _int(
                "coder_solve_fusion_max_total_chars", self.coder_solve_fusion_max_total_chars, 1
            ),
            "max_concurrent_sessions": _int("max_concurrent_sessions", self.max_concurrent_sessions, 0),
        }

    def _all_repos(self) -> list[str]:
        """Every distinct repo root the board builds in — one per project (#90). A
        filesystem sweep that isn't scoped to a single feature (the orphaned-worktree
        reap) must cover every project's checkout, not just the instance default."""
        seen: dict[str, None] = {}
        for entry in self._projects.values():
            repo = str((entry or {}).get("repo") or "").strip()
            if repo:
                seen.setdefault(repo, None)
        if not seen:
            seen.setdefault(self._store_kw["repo"], None)
        return list(seen)

    # ── #311: first-party self-dispatch through the host agent ────────────────────
    # A task assigned to the board's OWN agent — the reserved ``self``/``agent`` aliases,
    # or the board's configured ``coder`` name.
    _SELF_ASSIGNEE_ALIASES = ("self", "agent")

    # ── gate preflight (fail-closed, PER PROJECT: never start work a broken gate can't accept) ──
    def _ready_projects(self, store) -> list[str]:
        """The distinct projects with ready work right now — the set _maybe_preflight
        smokes each tick (order-preserving, deduped)."""
        names: list[str] = []
        seen: set[str] = set()
        for f in store.list_features(state="ready"):
            name = self._project_name(f)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

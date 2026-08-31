"""Prompt / build-context edge of the board loop (extracted from loop.py, #268).

Behavior-preserving move: these methods were lifted verbatim from ``BoardLoop``
and run as a mixin on the assembled ``BoardLoop`` in :mod:`.core`. Cross-edge
``self.<method>()`` calls resolve through the MRO, unchanged. The shared loop
kernel (constants, helpers, process-stable state) is re-exported from
:mod:`._common`; rebindable seams are read through the live package (``_loop``)
so tests that monkeypatch ``project_board.loop.<name>`` still take effect.
"""

from __future__ import annotations

import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


class PromptMixin:
    def _build_task_prompt(self, feature: dict) -> str:
        """The ``delegate_to`` prompt for a ``task`` bead (#217): the spec + acceptance
        criteria, framed as a request for a DELIVERABLE (a doc, a decision, an artifact
        ref), NOT a code change — a task has no worktree or PR, and the delegate's reply
        IS the deliverable ``record_delivery`` records."""
        title = feature.get("title", "")
        spec = feature.get("spec", "")
        criteria = feature.get("acceptance_criteria", "")
        criteria_block = f"\n## Acceptance criteria (definition of done)\n{criteria}\n" if criteria.strip() else ""
        return (
            f"You have been assigned ONE task. Complete it and reply with the "
            f"deliverable — a document, a decision, or a reference to the artifact you "
            f"produced. There is no code change, worktree, or PR: your reply IS the "
            f"deliverable, so make it self-contained.\n\n"
            f"# {title}\n\n"
            f"## Task\n{spec}\n"
            f"{criteria_block}"
        )

    def _build_prompt(self, feature: dict, lessons: str = "") -> str:
        """An imperative, fully-specified instruction (ProtoMaker discipline). A
        passive 'implement this feature' + a vague spec makes a coder produce
        nothing; naming the files + a direct 'make the edits now' makes it act.

        ``lessons`` (distilled gotchas from the knowledge graph, fetched async in
        ``_drive``) is injected so a coder gets this area's known failure modes on
        attempt 1 — the read half of the flywheel (retro grounds → coder heeds)."""
        files = feature.get("files_to_modify") or []
        files_block = (
            "\n".join(f"- {f}" for f in files) if files else "(none listed — create the files the task requires)"
        )
        # Repo standing gate files (#108): repo-wide obligations that a card's tight
        # `files_to_modify` would otherwise suppress — a card author can't enumerate
        # per-repo gates, so the loop carries them from config. Emitted as a SEPARATE
        # prompt block (not merged into `files_to_modify`, not a ledger item #113): a
        # standing reminder that these must stay green even if the change doesn't
        # centre on them. Empty by default → no block.
        # #90: gate files + conventions resolve from THIS feature's project, so a coder
        # on a multi-repo board gets the standing obligations of the repo it builds in.
        gate_files = self._gate_files_for(feature)
        gate_files_block = (
            "\n## Repo standing gate files (keep these green — repo-wide, not per-card)\n"
            + "\n".join(f"- {g}" for g in gate_files)
            + "\nThese are standing obligations for EVERY change in this repo (beyond the "
            "files listed above). If your change affects them, update them so the gate "
            "stays green.\n"
            if gate_files
            else ""
        )
        # Repo conventions (#108): the RULES around the repo-wide gates — what CI runs,
        # what format a required fragment takes, which files are GENERATED and must not
        # be hand-edited, and the standing "if a convention named here doesn't exist,
        # STOP and say so" guard. `gate_files` lists the paths; this carries the prose a
        # card author can't restate per-card. Emitted as a SEPARATE block right after
        # the gate files (its natural neighbour — both are repo-wide, not per-card),
        # verbatim from config. Injected unconditionally when set; empty → no block.
        repo_conventions = self._repo_conventions_for(feature)
        repo_conventions_block = (
            "\n## Repo conventions\n" + repo_conventions.strip() + "\n" if repo_conventions.strip() else ""
        )
        design = feature.get("design", "")
        design_block = f"\n## Design / context\n{design}\n" if design.strip() else ""
        # Standing scope-preservation block (#349 / bd-x01i): removed-behavior is a
        # recurring review-fix category (~21% of findings, ~one fix-round each). A
        # card's scope is ADDITIVE by default — a coder that deletes/narrows/bypasses
        # existing behavior it merely judged redundant burns a full round. This is
        # UNIVERSAL scope framing, not a policy ban: emitted UNCONDITIONALLY on every
        # coding dispatch (ordinary, retry, any task type, any config) — no `if`, so it
        # cannot silently disappear on a path. Removal stays legal when the card asks
        # for it OR the coder names the removed behavior + its reason in the final
        # summary, so review can judge it deliberately. Kept concise on purpose (AC r6):
        # if the measured category rate does not fall, this block is REMOVED, not grown
        # into a conditional policy system.
        preserve_scope_block = (
            "\n## Scope — this change is additive; preserve existing behavior\n"
            "A card's scope is ADDITIVE unless the card explicitly says otherwise. Do "
            "NOT delete, narrow, or bypass existing behavior outside the stated change "
            "— including apparently redundant guards, fallbacks, aliases, or defaults. "
            "Leave them in place even if they look unnecessary.\n"
            "If the change GENUINELY requires removing behavior, that is still allowed "
            "— but name the removed behavior and the reason for it in your final "
            "`## Summary`, so review can judge the removal deliberately.\n"
        )
        # CI-feedback re-dispatch (closed-loop verify): a prior attempt's PR failed
        # CI; lead with the failure so the coder FIXES it this pass (it can't run the
        # checks itself — edit-only). Also widen scope: the fix may touch tests/files
        # the original `files_to_modify` didn't list (the #1053 lesson).
        fid = feature.get("id", "")
        # Drain any externally-queued review-bounce feedback (the /review route stashed
        # it via queue_review_feedback) into THIS run's _ci_feedback, so an operator/CI
        # review bounce rides the exact same prompt path as an in-loop CI/review bounce.
        pending = _PENDING_FEEDBACK.pop(fid, None)
        if pending:
            self._ci_feedback[fid] = pending
        ci = self._ci_feedback.get(fid)
        prior = self._ci_prior_diff.get(fid)
        prior_block = (
            f"\n### The diff that failed (your previous attempt — fix it, don't restart from scratch)\n"
            f"```diff\n{prior}\n```\n"
            if prior
            else ""
        )
        ci_block = (
            "\n## ⚠ Your previous attempt was REJECTED — fix it this attempt\n"
            f"{ci}\n"
            f"{prior_block}"
            "Address the problem above. This may require editing files beyond the list "
            "below — e.g. ADD the missing tests, or update an e2e/unit test that assumed "
            "the old behavior.\n"
            if ci
            else ""
        )
        lessons_block = (
            f"\n## Known gotchas for this area (distilled from past retros — heed them)\n{lessons.strip()}\n"
            if lessons.strip()
            else ""
        )
        # Requirement ledger (#113): the tracked items decomposed from the acceptance
        # criteria at mark_ready, WITH their current statuses — so a re-dispatch
        # re-injects the still-open items and the coder must dispose of every one
        # (done, or declined with a reason). Silence is not disposition: an
        # unreported item stays open and the completion gate refuses the PR.
        reqs = feature.get("requirements") or []
        req_lines = "\n".join(
            f"- `{r.get('id')}` [{r.get('status', 'open')}] {r.get('text', '')}"
            + (f" (reason: {r['decline_reason']})" if r.get("decline_reason") else "")
            for r in reqs
        )
        req_block = (
            "\n## Requirements ledger (dispose of EVERY item)\n"
            f"{req_lines}\n\n"
            "Each item above is tracked on the board. Address every `open` item this "
            "round, and report a per-item disposition: include a `## Requirements` "
            "section in your final message (before the `## Summary`) with ONE line "
            "per item — `- <id>: done` or `- <id>: declined — <concrete reason>`. "
            "Declining with a real reason (e.g. not reachable/not applicable) is a "
            "valid closed state; SILENCE IS NOT — an unreported item stays open, and "
            "the PR cannot open while any item is open.\n"
            if reqs
            else ""
        )
        return (
            f"You are implementing ONE feature in this repository. Your working "
            f"directory is an isolated git worktree — **make all the edits here, now**. "
            f"Do not ask questions or just describe a plan; if something is ambiguous, "
            f"make the most reasonable choice and write the code.\n\n"
            f"# {feature['title']}\n\n"
            f"{ci_block}"
            f"{lessons_block}"
            f"## Task\n{feature.get('spec', '')}\n\n"
            f"## Files to create / modify\n{files_block}\n"
            f"{gate_files_block}"
            f"{repo_conventions_block}"
            f"{design_block}\n"
            f"{preserve_scope_block}"
            f"## Acceptance criteria (definition of done)\n{feature.get('acceptance_criteria', '')}\n"
            f"{req_block}\n"
            f"## Rules\n"
            f"- Make the edits directly in the working tree NOW — actually write the files.\n"
            f"- Touch only the files this task needs; mirror the surrounding code's style.\n"
            f"- **Write automated tests** covering the new/changed behavior (a new or "
            f"updated test file, matching the repo's existing test conventions). This is "
            f"part of the definition of done, not optional — a code change with no test "
            f"is rejected before the PR opens. If a test GENUINELY doesn't apply (a pure "
            f"refactor, config/docs-as-code, or a change with no behavior to exercise), "
            f"write a single line `NO_TEST_NEEDED: <reason>` inside the final `## Summary` "
            f"section of your final message instead — it does not count anywhere else.\n"
            f"- You cannot run shell commands (edit-only); the tests you write run in CI "
            f"on the PR, so they must be correct and self-contained.\n"
            f"- Push the branch if you can; do NOT open a PR (draft or otherwise) — the loop "
            f"opens it with the title/body it composes and owns the PR lifecycle.\n"
            f"- **Your FINAL message becomes the PR description, verbatim.** End with a "
            f"short, clean summary for a reviewer — what changed and why, 2-6 sentences "
            f"or a few bullet points. Do NOT narrate your process: no step-by-step "
            f'exploration, no "I first looked at..."/"Let me...", no restating these '
            f"instructions or the acceptance criteria back. If you used scratch "
            f"reasoning to get here, leave it out of this message entirely.\n"
            f"- You are done when the listed files exist, tests cover the change, and "
            f"every acceptance criterion is satisfied."
        )

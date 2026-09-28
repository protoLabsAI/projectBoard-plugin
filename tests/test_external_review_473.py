"""An external QA panel's FAIL at the PR head bounces the card into a fix round (#473).

Live (projectManager board, 2026-09-27): card ``bd-524n`` / protoAgent#3698. The protoreview
panel ("Vera") FAILED the PR with a confirmed major — CHANGES_REQUESTED, a ``QA panel`` check
run concluding failure, a ``Review at head`` status failure, and the findings as a JSON block
in the review body. The board's own review gate had said clean at the same head, so nothing
acted on the FAIL: the card sat ``in_review`` for seven hours and re-ran the ~12-minute
merged-state gate every time main moved, until ``merged-verify budget (5) spent``.

Here:

* the pure judge (``external_review``) against the incident's real payload shape;
* the reconcile edge on a fake store, one branch per test — bounce, once-per-head hold,
  a new head re-arming it, the liveness refusal, budget exhaustion, a check-only FAIL, the
  merged-state gate never running under a FAIL;
* the persisted bounce through REAL ``br`` (the stamp, the comment, the budget, the requeue);
* the bearer-gated operator twin of the signed ``/review`` route, and the tool's ``escalate``.

GitHub is faked at the ``worktree.pr_review_state`` seam (its real-``gh`` coverage lives in
tests/test_worktree_gh.py); ``br`` is real wherever the test says so.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
import project_board.loop as loop_mod
from project_board import api, external_review, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

HEAD = "e5c66aa9ad75731286916a06d2bd6059494e0389"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
PR = "https://github.com/protoLabsAI/protoAgent/pull/3698"

# The panel's CHANGES_REQUESTED review body from the incident, trimmed but in its real shape:
# the prose preamble, the hidden head marker, the table, and the machine-readable findings.
VERA_BODY = f"""_Checks are terminal (green) — arming the FAIL verdict below as a blocking review._

<!-- protoagent-qa-review head={HEAD} verdict=FAIL promoted=false diff=78877a157ec3 -->
## QA panel review — **FAIL**
_code-review-structural · head `e5c66aa9ad75` · formal_

### Findings

| | Severity | Location | Finding | Verified |
|---|---|---|---|---|
| 🟠 | major | `apps/web/src/keybindings/useKeybindings.ts:55` | The `focusin` listener … | confirmed |

<details>
<summary>findings JSON (machine-readable)</summary>

```json
[
  {{
    "file": "apps/web/src/keybindings/useKeybindings.ts",
    "line": 55,
    "severity": "major",
    "category": "correctness",
    "claim": "The `focusin` listener records `document.body` as `lastInteracted`.",
    "evidence": "const rememberInteraction = (e: Event) => {{\\n  if (e.target instanceof Element) lastInteracted = e.target;\\n}};",
    "verdict": "confirmed",
    "note": "Re-read at head e5c66aa."
  }},
  {{
    "file": "apps/web/src/keybindings/scope.ts",
    "line": 12,
    "severity": "minor",
    "claim": "naming nit",
    "evidence": "const x = 1;",
    "verdict": "confirmed"
  }},
  {{
    "file": "apps/web/src/keybindings/other.ts",
    "line": 3,
    "severity": "major",
    "claim": "a suspicion the panel refuted",
    "evidence": "y()",
    "verdict": "refuted"
  }}
]
```
</details>
"""


def _review(body, *, login="protoreview", at="2026-09-27T13:45:17Z", state="CHANGES_REQUESTED", commit=HEAD):
    return {"author": {"login": login}, "body": body, "state": state, "submittedAt": at, "commit": {"oid": commit}}


def _view(*, head=HEAD, reviews=None, checks=None):
    """A `gh pr view --json headRefOid,reviews,statusCheckRollup` payload, incident-shaped:
    the panel's check run FAILED, the board's own `QA panel` STATUS says clean."""
    if checks is None:
        checks = [
            {"__typename": "CheckRun", "name": "QA panel", "conclusion": "FAILURE", "workflowName": ""},
            {"__typename": "StatusContext", "context": "Review at head", "state": "FAILURE"},
            {"__typename": "StatusContext", "context": "QA panel", "state": "SUCCESS"},
            {"__typename": "CheckRun", "name": "test", "conclusion": "SUCCESS", "workflowName": "CI"},
        ]
    return {
        "headRefOid": head,
        "reviews": reviews if reviews is not None else [_review(VERA_BODY)],
        "statusCheckRollup": checks,
    }


# ── the pure judge ──────────────────────────────────────────────────────────────────────


def test_config_defaults_and_overrides():
    cfg = external_review.parse_config(None)
    assert cfg.reviewers == ("protoreview[bot]",) and cfg.marker == "protoagent-qa-review"
    assert cfg.check_runs == ("QA panel",) and cfg.statuses == ("Review at head",)
    assert external_review.parse_config(True) == cfg
    assert external_review.parse_config(False) is None
    assert external_review.parse_config("false") is None
    assert external_review.parse_config({"enabled": False}) is None
    assert external_review.parse_config({"reviewers": []}) is None  # nobody's verdict to read
    custom = external_review.parse_config(
        {"reviewers": "vera[bot]", "marker": "my-review", "check_runs": [], "statuses": ["Gate"]}
    )
    assert custom.reviewers == ("vera[bot]",) and custom.marker == "my-review"
    assert custom.check_runs == () and custom.statuses == ("Gate",)


def test_the_incident_payload_is_a_fail_at_the_head_with_findings():
    v = external_review.evaluate(_view(), external_review.Config())
    assert v.failed and v.head == HEAD
    assert v.review_verdict == "FAIL" and v.has_findings_review
    # GraphQL names the App `protoreview`; the config names it `protoreview[bot]` (REST).
    assert v.reviewer == "protoreview"
    summary = external_review.fail_summary(v)
    assert "verdict=FAIL" in summary and "check run `QA panel` FAILURE" in summary
    assert "status `Review at head` FAILURE" in summary
    # The board's OWN `QA panel` verdict is a commit STATUS, and it said clean: it must not
    # be read as the panel's (nor count against it).
    assert "status `QA panel`" not in summary


def test_a_marker_for_another_head_is_not_a_verdict_on_this_one():
    stale = _review(VERA_BODY)  # marker names HEAD
    v = external_review.evaluate(_view(head=NEW_HEAD, reviews=[stale], checks=[]), external_review.Config())
    assert not v.failed and v.review_verdict == ""


def test_a_marker_from_someone_else_is_ignored():
    forged = _review(VERA_BODY, login="random-contributor")
    v = external_review.evaluate(_view(reviews=[forged], checks=[]), external_review.Config())
    assert not v.failed


def test_the_latest_marked_review_at_the_head_wins_and_a_pass_clears_a_lingering_red():
    passed = VERA_BODY.replace("verdict=FAIL", "verdict=PASS")
    reviews = [
        _review(VERA_BODY, at="2026-09-27T13:45:17Z"),
        _review(passed, at="2026-09-27T15:00:00Z", state="APPROVED"),
    ]
    v = external_review.evaluate(_view(reviews=reviews), external_review.Config())
    assert v.review_verdict == "PASS" and not v.failed  # the red check lingering from before is not the verdict
    # …but ORDER comes from submittedAt, not list position.
    v = external_review.evaluate(_view(reviews=list(reversed(reviews))), external_review.Config())
    assert v.review_verdict == "PASS" and not v.failed


def test_a_red_check_with_no_marked_review_fails_without_findings():
    v = external_review.evaluate(_view(reviews=[]), external_review.Config())
    assert v.failed and not v.has_findings_review


def test_a_head_less_payload_is_no_verdict():
    assert external_review.evaluate({"reviews": []}, external_review.Config()) is None


def test_short_marker_heads_match_but_not_below_seven_chars():
    assert external_review.head_matches(HEAD[:12], HEAD)
    assert not external_review.head_matches(HEAD[:6], HEAD)
    assert not external_review.head_matches(NEW_HEAD[:12], HEAD)


def test_only_confirmed_blocker_or_major_findings_are_required():
    findings = external_review.parse_findings(VERA_BODY)
    assert len(findings) == 3
    must = external_review.blocking(findings)
    assert [f["file"] for f in must] == ["apps/web/src/keybindings/useKeybindings.ts"]


def test_rendered_findings_carry_file_line_claim_and_evidence():
    v = external_review.evaluate(_view(), external_review.Config())
    text = external_review.render_findings(v, PR)
    assert f"head {HEAD[:12]}" in text and PR in text
    assert "`apps/web/src/keybindings/useKeybindings.ts:55` [major, confirmed]" in text
    assert "records `document.body` as `lastInteracted`" in text
    assert "rememberInteraction" in text  # the evidence, quoted
    assert "scope.ts" not in text and "other.ts" not in text  # minor / refuted: not required
    assert "2 other finding(s)" in text


def test_a_fail_with_no_parsable_blocking_finding_quotes_the_review():
    body = f"<!-- protoagent-qa-review head={HEAD} verdict=FAIL -->\n## FAIL\nThe panel could not agree; see threads."
    v = external_review.evaluate(_view(reviews=[_review(body)]), external_review.Config())
    text = external_review.render_findings(v)
    assert "The panel could not agree" in text and "protoagent-qa-review" not in text


# ── the reconcile edge (fake store) ─────────────────────────────────────────────────────


class _Store:
    """One in_review card and the store surface the external-review edge touches."""

    def __init__(self, labels=()):
        self.state = "in_review"
        self.labels = list(labels)
        self.calls: list[tuple] = []
        self.comments: list[str] = []

    def _feature(self):
        return {"id": "bd-1", "board_state": self.state, "labels": list(self.labels), "pr_url": PR, "title": "t"}

    def get_feature(self, fid):
        return self._feature()

    def list_features(self, state=None):
        return [self._feature()] if state == self.state else []

    def record_review_bounce(self, fid, findings="", *, head=""):
        if self.state != "in_review":
            raise BoardError("review bounce expects in_review")
        self.calls.append(("record_review_bounce", head))
        if head:
            self.labels = [l for l in self.labels if not l.startswith("ext-review-bounced:")]
            self.labels.append(f"ext-review-bounced:{head[:12]}")
        self.comments.append(f"review requested changes: {findings}")
        return self._feature()

    def requeue(self, fid):
        self.calls.append(("requeue",))
        self.state = "ready"
        return self._feature()

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", reason, category))
        self.state = "blocked"
        return self._feature()

    def comment(self, fid, text):
        self.comments.append(text)

    def record_budget(self, fid, kind, n):
        self.labels = [l for l in self.labels if not l.startswith(f"budget:{kind}:")] + [f"budget:{kind}:{n}"]

    def clear_budgets(self, fid, kinds=None):
        self.labels = [l for l in self.labels if not any(l.startswith(f"budget:{k}:") for k in (kinds or [""]))]


def _loop(monkeypatch, store, *, view=None, cfg=None):
    """A loop reconciling ``store``'s one card, with the panel's verdict ``view`` (default: the
    incident). Records which later edges of the pass ran."""
    loop = BoardLoop({"merge_poll": True, "auto_merge": True, **(cfg or {})})
    monkeypatch.setattr(loop, "_store", lambda: store)
    loop_mod._PENDING_FEEDBACK.clear()
    ran: list[str] = []
    reads: list[str] = []

    async def _state(pr_url, *, cwd="."):
        return "OPEN"

    async def _review_state(pr_url, *, cwd="."):
        reads.append(pr_url)
        return _view() if view is None else view

    async def _diff(pr_url, *, cwd=".", max_chars=0):
        return "diff --git a/x b/x"

    def _edge(name, result=False):
        async def _run(*a, **k):
            ran.append(name)
            return result

        return _run

    monkeypatch.setattr(worktree, "pr_state", _state)
    monkeypatch.setattr(worktree, "pr_review_state", _review_state)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr(loop, "_maybe_rebase", _edge("rebase"))
    monkeypatch.setattr(loop, "_verify_merged_state", _edge("merged-verify"))
    monkeypatch.setattr(loop, "_reconcile_ci", _edge("ci"))
    monkeypatch.setattr(loop, "_maybe_auto_merge", _edge("auto-merge"))
    monkeypatch.setattr(loop, "_notify_operator", lambda fid, text, incident="": ran.append("notify"))
    loop.ran, loop.reads = ran, reads
    return loop


async def test_a_fail_at_the_head_bounces_into_a_fix_round_on_the_same_pr(monkeypatch):
    store = _Store(labels=["review-clean", "review-clean-sha:e5c66aa9ad75"])  # the gate said clean
    loop = _loop(monkeypatch, store)
    await loop._reconcile_prs()
    assert ("record_review_bounce", HEAD) in store.calls and store.calls[-1] == ("requeue",)
    assert store.state == "ready"
    assert f"ext-review-bounced:{HEAD[:12]}" in store.labels
    assert "budget:ext-review-fix:1" in store.labels
    # The next prompt LEADS with the findings, the failed diff beside them.
    queued = loop_mod._PENDING_FEEDBACK["bd-1"]
    assert "useKeybindings.ts:55" in queued and "lastInteracted" in queued and "rememberInteraction" in queued
    assert loop._ci_prior_diff["bd-1"].startswith("diff --git")
    # Nothing else ran on a PR the panel rejected: no merged-state gate, no merge.
    assert loop.ran == []


async def test_a_fail_never_spends_merged_verify_or_runs_the_merged_state_gate(monkeypatch):
    """The other half of the incident: while the panel's FAIL stands at the head — held, not
    bounced — every pass skips the merged-state gate, so its budget is never spent."""
    store = _Store(labels=[f"ext-review-bounced:{HEAD[:12]}"])  # already bounced for this head
    loop = _loop(monkeypatch, store)
    for _ in range(3):
        await loop._reconcile_prs()
    assert "merged-verify" not in loop.ran and "auto-merge" not in loop.ran and "rebase" not in loop.ran
    assert not any(l.startswith("budget:merged-verify") for l in store.labels)
    assert not any(c[0] == "record_review_bounce" for c in store.calls)  # once per head
    holds = [c for c in store.comments if c.startswith("auto-merge held:")]
    assert len(holds) == 1 and "needs a human" in holds[0]  # told once, not every poll
    assert loop.ran.count("notify") == 1


async def test_a_new_head_rearms_it(monkeypatch):
    """A push is a new head. The panel's old FAIL (marker for the old head) says nothing about
    it, so the pass goes on as normal; the panel's FAIL on the NEW head bounces again."""
    store = _Store(labels=[f"ext-review-bounced:{HEAD[:12]}"])
    loop = _loop(monkeypatch, store, view=_view(head=NEW_HEAD, checks=[]))
    await loop._reconcile_prs()
    assert "merged-verify" in loop.ran and "auto-merge" in loop.ran
    assert store.state == "in_review"
    body = VERA_BODY.replace(HEAD, NEW_HEAD)
    loop = _loop(monkeypatch, store, view=_view(head=NEW_HEAD, reviews=[_review(body, commit=NEW_HEAD)]))
    await loop._reconcile_prs()
    assert ("record_review_bounce", NEW_HEAD) in store.calls and store.state == "ready"
    assert f"ext-review-bounced:{NEW_HEAD[:12]}" in store.labels


async def test_never_bounced_under_a_live_round(monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store)
    loop._inflight_files["bd-1"] = set()  # a claimed build holds the card
    await loop._reconcile_prs()
    assert store.state == "in_review" and not store.calls
    assert loop.ran == []  # still held: no merged-state gate under a FAIL
    loop._inflight_files.clear()
    monkeypatch.setattr(loop_mod, "requeue_refusal", lambda fid: "a coder drive is still building it")
    await loop._reconcile_prs()
    assert store.state == "in_review" and not store.calls


async def test_a_spent_budget_blocks_for_a_human_as_the_gate_does(monkeypatch):
    store = _Store(labels=["budget:ext-review-fix:2"])
    loop = _loop(monkeypatch, store, cfg={"review_fix_max": 2})
    await loop._reconcile_prs()
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked and "external review FAILED" in blocked[0][1] and blocked[0][2] == "terminal"
    assert ("requeue",) not in store.calls
    assert any("useKeybindings.ts:55" in c for c in store.comments)  # the findings are on the bead
    assert "budget:ext-review-fix:2" not in store.labels  # reset, as the gate's exhaustion does


async def test_the_gates_clean_verdict_does_not_refill_the_external_budget(monkeypatch):
    """The gate resets `review-fix` on every clean verdict — which, in this incident, is every
    round. The panel's rounds are counted apart, or they would never run out."""
    store = _Store(labels=["budget:review-fix:0", "budget:ext-review-fix:1"])
    loop = _loop(monkeypatch, store, cfg={"review_fix_max": 2})
    await loop._reconcile_prs()
    assert "budget:ext-review-fix:2" in store.labels and store.state == "ready"


async def test_a_red_check_without_a_findings_review_holds_and_never_bounces(monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store, view=_view(reviews=[]))
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.state == "in_review" and not store.calls
    assert "merged-verify" not in loop.ran and "auto-merge" not in loop.ran
    holds = [c for c in store.comments if c.startswith("auto-merge held:")]
    assert len(holds) == 1 and "no findings review" in holds[0]


async def test_off_by_config_reads_nothing(monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store, cfg={"external_review": False})
    await loop._reconcile_prs()
    assert loop.reads == [] and store.state == "in_review"
    assert "merged-verify" in loop.ran


async def test_a_project_can_turn_it_off_for_its_own_repo(monkeypatch):
    store = _Store()
    cfg = {"projects": {"p": {"repo": "/r", "external_review": False}}, "default_project": "p"}
    loop = _loop(monkeypatch, store, cfg=cfg)
    await loop._reconcile_prs()
    assert loop.reads == [] and store.state == "in_review"


async def test_an_unreadable_review_state_fails_open_to_the_old_pass(monkeypatch):
    store = _Store()
    loop = _loop(monkeypatch, store)
    monkeypatch.setattr(worktree, "pr_review_state", lambda *a, **k: _none())  # gh failed
    await loop._reconcile_prs()
    assert store.state == "in_review" and "merged-verify" in loop.ran


async def _none():
    return None


# ── the persisted bounce, through REAL `br` ─────────────────────────────────────────────

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)


def _in_review_card(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    (tmp_path / "target.py").write_text("x = 1\n")
    fid = board.create_feature("fix(keys): guard focusin", spec="s", files_to_modify=["target.py"])["id"]
    board._run("update", fid, "--add-label", "ready")  # setup, not under test
    assert board.claim(fid, assignee="proto")
    board.open_review(fid, pr_url=PR)
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: board)
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    return board, fid


@requires_br
def test_record_review_bounce_stamps_the_head_and_replaces_it_real_br(tmp_path, monkeypatch):
    board, fid = _in_review_card(tmp_path, monkeypatch)
    f = board.record_review_bounce(fid, "fix the focusin guard", head=HEAD)
    assert f"ext-review-bounced:{HEAD[:12]}" in f["labels"]
    assert any("review requested changes: fix the focusin guard" in c for c in board.feature_comments(fid))
    f = board.record_review_bounce(fid, "again", head=NEW_HEAD)
    stamps = [l for l in f["labels"] if l.startswith("ext-review-bounced:")]
    assert stamps == [f"ext-review-bounced:{NEW_HEAD[:12]}"]  # replaced, never accumulated
    # Re-stamping the SAME head keeps exactly one copy (the self-cancelling remove trap, #338).
    f = board.record_review_bounce(fid, "same head", head=NEW_HEAD)
    assert [l for l in f["labels"] if l.startswith("ext-review-bounced:")] == [f"ext-review-bounced:{NEW_HEAD[:12]}"]
    # Without a head it is the plain bounce it always was: no label write.
    board.record_review_bounce(fid, "operator bounce")
    assert any("operator bounce" in c for c in board.feature_comments(fid))


@requires_br
async def test_the_reconcile_bounce_persists_on_a_real_board(tmp_path, monkeypatch):
    """End to end on real `br`: the pass reads the panel's FAIL, and the card is back in
    `ready` on the same PR, stamped with the head, the budget spent, the findings on the bead.
    A second loop — a restart — reading the same FAIL at the same head does NOT bounce it again."""
    board, fid = _in_review_card(tmp_path, monkeypatch)
    loop = _loop(monkeypatch, board)
    await loop._reconcile_prs()
    f = board.get_feature(fid)
    assert f["board_state"] == "ready" and f["pr_url"] == PR
    assert f"ext-review-bounced:{HEAD[:12]}" in f["labels"]
    assert "budget:ext-review-fix:1" in f["labels"]
    notes = board.feature_comments(fid)
    assert any("review requested changes" in c and "useKeybindings.ts:55" in c for c in notes)
    assert loop.ran == []

    # The fix round comes back to review WITHOUT a new head, and the process restarted.
    assert board.claim(fid, assignee="proto")
    board.open_review(fid, pr_url=PR)
    again = _loop(monkeypatch, board)
    await again._reconcile_prs()
    f = board.get_feature(fid)
    assert f["board_state"] == "in_review"  # held, not re-bounced: once per head
    assert "budget:ext-review-fix:1" in f["labels"]
    assert again.ran == ["notify"]  # the operator is told; no merged-state gate, no merge
    assert any(c.startswith("auto-merge held:") for c in board.feature_comments(fid))


# ── the operator route and the tool ─────────────────────────────────────────────────────


class _ApiStore:
    def __init__(self, escalate_to="smart"):
        self.calls: list[tuple] = []
        self.escalate_to = escalate_to

    def _f(self, state):
        return {"id": "bd-1", "board_state": state, "labels": [], "pr_url": PR}

    def record_review_bounce(self, fid, findings=""):
        self.calls.append(("record_review_bounce", findings))
        return self._f("in_review")

    def requeue(self, fid):
        self.calls.append(("requeue",))
        return self._f("ready")

    def escalate(self, fid, reason):
        self.calls.append(("escalate", reason))
        return self.escalate_to

    def block_from_review(self, fid, reason):
        self.calls.append(("block_from_review", reason))
        return self._f("blocked")


def _client(monkeypatch, store, cfg=None):
    monkeypatch.setattr(api, "get_store", lambda **_kw: store)
    app = FastAPI()
    app.include_router(api.build_router(cfg or {}), prefix="/plugins/project_board")
    app.include_router(api.build_data_router(cfg or {}), prefix="/api/plugins/project_board")
    return TestClient(app)


def test_the_operator_route_bounces_without_the_webhook_secret(monkeypatch):
    loop_mod._PENDING_FEEDBACK.clear()
    store = _ApiStore()
    c = _client(monkeypatch, store, cfg={"webhook_secret": ""})  # the signed route is 503 here…
    assert c.post("/plugins/project_board/features/bd-1/review", json={"findings": "x"}).status_code == 503
    r = c.post("/api/plugins/project_board/features/bd-1/review", json={"findings": "focusin records body"})
    assert r.status_code == 200, r.text  # …the bearer-gated twin is not
    body = r.json()
    assert body["requeued"] is True and body["escalated"] is False
    assert [c[0] for c in store.calls] == ["record_review_bounce", "requeue"]
    assert "focusin records body" in loop_mod._PENDING_FEEDBACK["bd-1"]


def test_the_operator_route_escalates_on_request(monkeypatch):
    ladder = {"coders": {"smart": "sonnet", "reasoning": "opus"}}
    store = _ApiStore(escalate_to="reasoning")
    c = _client(monkeypatch, store, cfg=ladder)
    body = c.post("/api/plugins/project_board/features/bd-1/review", json={"findings": "x", "escalate": True}).json()
    assert body["escalated"] is True and body["next_tier"] == "reasoning"
    top = _ApiStore(escalate_to=None)
    c = _client(monkeypatch, top, cfg=ladder)
    body = c.post("/api/plugins/project_board/features/bd-1/review", json={"findings": "x", "escalate": True}).json()
    assert body["exhausted"] is True and ("block_from_review", "review-fail: x") in top.calls


def test_the_operator_route_refuses_under_a_live_round(monkeypatch):
    store = _ApiStore()
    monkeypatch.setattr(
        loop_mod, "requeue_refusal", lambda fid: "bd-1 can't be requeued while a coder drive is still building it"
    )
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-1/review", json={"findings": "x"})
    assert r.status_code == 400 and "can't be requeued" in r.json()["detail"]
    assert not store.calls


@requires_br
def test_the_operator_route_on_a_real_board(tmp_path, monkeypatch):
    board, fid = _in_review_card(tmp_path, monkeypatch)
    loop_mod._PENDING_FEEDBACK.clear()
    c = _client(monkeypatch, board)
    r = c.post(f"/api/plugins/project_board/features/{fid}/review", json={"findings": "guard focusin against body"})
    assert r.status_code == 200, r.text
    f = board.get_feature(fid)
    assert f["board_state"] == "ready" and f["pr_url"] == PR
    assert any("review requested changes: guard focusin against body" in n for n in board.feature_comments(fid))
    # A card that is not in review is refused, changing nothing.
    r = c.post(f"/api/plugins/project_board/features/{fid}/review", json={"findings": "again"})
    assert r.status_code == 400 and "in_review" in r.json()["detail"]


def test_board_requeue_feature_escalates_with_findings(monkeypatch):
    store = _ApiStore(escalate_to="reasoning")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: store)
    tools = {t.name: t for t in pb._board_tools({"coders": {"smart": "sonnet", "reasoning": "opus"}})}
    out = tools["board_requeue_feature"].invoke({"feature_id": "bd-1", "findings": "x", "escalate": True})
    assert '"state": "ready"' in out
    assert [c[0] for c in store.calls] == ["record_review_bounce", "escalate", "requeue"]
    store.calls.clear()
    tools["board_requeue_feature"].invoke({"feature_id": "bd-1", "findings": "x"})
    assert [c[0] for c in store.calls] == ["record_review_bounce", "requeue"]  # same tier by default

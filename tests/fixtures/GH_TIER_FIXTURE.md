# CI fixture — do not merge, do not close

This branch exists only to keep **one permanently-open pull request** in this repository.

`tests/test_worktree_gh.py` (the `requires_gh` tier, #361 S2) exercises 12 `worktree.py`
seams against **real GitHub** rather than a mock — `pr_state`, `pr_head_sha`, `pr_diff`,
`pr_ci_status`, `repo_slug`, `post_review_status` and friends. Those seams were **0%
covered against the real API** until that card, which is how #354 shipped inert for a day:
`POST /check-runs` is GitHub-App-only and 403s under the board's PAT, and every unit test
mocked `gh`, so 1,650 tests passed while the review verdict published to nothing.

To read a real PR the tier needs a real PR. CI resolves it as:

```yaml
PB_GH_FIXTURE_PR: ${{ vars.PB_GH_FIXTURE_PR || github.event.pull_request.html_url }}
```

On a `pull_request` run the PR under test works. On a **push to `main`** there is no such
PR, and `PB_REQUIRE_GH=1` deliberately turns "no fixture" into a **build failure** rather
than a silent skip (the #136 lesson: a tier that quietly skips is not coverage). So the
repository variable `PB_GH_FIXTURE_PR` points here, and `main` keeps its real-GitHub
coverage.

## Why this file has content

`pr_diff` asserts a non-empty unified diff, so the fixture branch must differ from `main`
by something. This file is that something — a diff that explains itself.

## Rules

- **Never merge it.** It is a draft, and merging it removes the fixture.
- **Never close it.** The tier asserts the PR is `OPEN`; closing it turns CI red on `main`.
- **Never push to it.** The head sha is read (not written) by the tier; a moving head is
  fine but pointless churn. Status *writes* go to the commit under test, never here —
  GitHub caps statuses at 1,000 per `(sha, context)`, so writing to a stable head would be
  a slow time bomb.
- If it is ever lost, recreate any permanently-open PR and repoint `vars.PB_GH_FIXTURE_PR`.

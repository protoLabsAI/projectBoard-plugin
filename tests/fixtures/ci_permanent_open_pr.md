# CI fixture: permanently-open PR — DO NOT MERGE, DO NOT CLOSE

This branch backs the pinned, permanently-open pull request that the real-GitHub
integration tier (`tests/test_worktree_gh.py`, projectBoard-plugin #361 slice 2)
reads against. That tier exercises the 12 read-dominant `worktree.py` GitHub seams
(`repo_slug`, `pr_state`, `pr_head_sha`, `pr_url_for_branch`, `pr_merge_info`,
`pr_diff`, `pr_ci_status`, `post_review_status`/`read_review_status`,
`_find_marked_comment`/`post_or_update_pr_comment`, `merge_pr`) against the ACTUAL
`gh`/GitHub API — the class of failure a mocked `_gh` is blind to and that shipped
#354 (a PAT that cannot `POST /check-runs`, green through every mock).

The tier only ever READS this PR, plus two idempotent observability writes on it
(one commit status keyed on `(context, sha)`, one marker-keyed comment updated in
place — neither stacks duplicates). `merge_pr` is exercised with a deliberately
wrong `expected_head`, so GitHub refuses it atomically and this PR is never merged.

Keep this PR OPEN permanently. Its URL is pinned as the default fixture in
`tests/conftest.py` (`_DEFAULT_PINNED_FIXTURE_PR`); the `PB_GH_FIXTURE_PR` env var
/ repo variable overrides it. A PR URL is a public identifier, never a credential.

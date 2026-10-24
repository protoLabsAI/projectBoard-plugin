- **An in_review PR that is not CLEAN with unresolved Major/Critical review-bot threads is
  bounced into a same-PR fix round (#495).** #495 item 1 made such a PR READ `held: PR not
  clean on GitHub (UNSTABLE)`, but nothing acted on it: the #473 external-review edge only
  fires on a panel FAIL review with a marker, so a card whose PR had a WARN plus unresolved
  CodeRabbit Majors — findings left as inline review THREADS, not a FAIL review — sat
  `in_review` forever (bd-fgtf / protoAgent#3745). Now the reconcile reads a PR's unresolved
  review threads (`worktree.pr_unresolved_threads`, a bounded `gh api graphql` read) and, when
  the PR is not CLEAN and a configured bot (`external_review.thread_bots`, default
  `coderabbitai[bot]` / `protoreview`) has a thread marked Major or Critical open, bounces the
  card into a fix round on the SAME PR with those threads (file:line plus body) leading the
  next prompt. It reuses the #473 machinery: once per head, bounded by `review_fix_max` on the
  shared `budget:ext-review-fix`, and blocked for a human when the budget is spent. Minor-only
  threads never bounce, and an unreadable thread read (a `gh` timeout) changes nothing — it is
  never read as "no threads". Replying to or resolving the threads after the push is out of
  scope.
  Rebase/conflict and CI repair retain priority over thread fixes. Only non-outdated threads
  originally posted on the current head can trigger a bounce: unresolved findings from a prior
  head cannot spend another fix round while the new push awaits review. Unknown provenance
  leaves the card alone. BEHIND and DIRTY PRs remain owned by the rebase path.

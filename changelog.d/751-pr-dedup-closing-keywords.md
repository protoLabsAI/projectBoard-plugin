Fixed source-issue dedup to refuse only when an open PR uses a GitHub closing
keyword for the issue, rather than any mention. Refusals name the PR and suggest
`board_create_feature(allow_shared_source_issue=true)` to skip only the PR check
while retaining same-title and in-review dependency guards.

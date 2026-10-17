The Ready gate now accepts stacked cards (#537). A `files_to_modify` path that is missing
from the base checkout no longer trips the phantom-path refusal when it already exists on a
`depends_on` card's open-PR branch — the case where bd-8ac depends on bd-0d4 and bd-0d4's
open PR creates the files bd-8ac edits. The gate consults each depends_on card that has an
open `pr_url` or a feature branch and runs `git cat-file -e <ref>:<path>` against that card's
branch: for an open PR it refreshes origin with ONE bounded `git fetch` and reads the
remote-tracking ref `refs/remotes/origin/feat-<dep>` (an open PR reflects what is pushed, so
origin is authoritative); otherwise it reads the local branch refs with no network. Accepted
paths get an advisory `path from <card-id>'s branch` instead of forcing the author to mislabel
them `(new)`. A path with no depends_on edge is still refused, and any git error counts as
"not found" — the ordinary refusal, never an exception. `board_check_ready` and the project
rehome check share the same logic.

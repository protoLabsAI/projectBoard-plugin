The source-issue closed guard (#166) no longer throws away finished work (protoAgent#3832). It
used to cancel a card and discard its worktree when the card's `source_issue` was closed just
before the PR opened. Nine verified builds were lost that way after an unrelated PR closed a
design-decision issue in another repo that they all cited. Now a source issue in a different
repo than the card's own is ignored by the guard, since a PR can only close an issue in its own
repo, and the PR opens normally with its `Refs` link. A same-repo closure that no board sibling
caused still holds the PR, but it blocks the card (terminal, for a human) and keeps the worktree
and branch as they are. The block reason names the branch and worktree and says how to cancel
or salvage the work. The #253 multi-slice sibling exception is unchanged.

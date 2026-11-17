The ready check now advises when a `depends_on` edge joins two open cards that share no
file in `files_to_modify` (projectBoard-plugin#778). A 12-card batch boarded as one chain
had four cards waiting on work they shared no file with, and every create returned
`ready_check` ok — `ready_check` reported the opposite case (a shared file with no edge)
but nothing for an edge the files don't force. The new `dependency-no-shared-file`
advisory names both ids and offers `board_remove_dependency` so independent cards can keep
running in parallel. It is advisory only: never a refusal, never changing `ok`, and the
card is still created or updated. It stays silent when the blocker is terminal (merged or
cancelled — the cancelled case keeps its own advisory), when either card names no files (a
task), or when the files overlap; a `(new)` marker on one side still counts as the same
file. Both `board_create_feature` and `board_update_feature` surface it, since both return
`_ready_dry_run`.

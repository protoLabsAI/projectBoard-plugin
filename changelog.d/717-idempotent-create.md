- **Two turns reacting to the same event end up with exactly one live card (#717).**
  `board_create_feature`, `board_create_task` and `store.create_feature` take an optional
  `idempotency_key` naming the originating event or work intent. It is stamped on the card
  by `br create` itself as an `idem:<hash>` label, so it is durable across restarts. Under
  a board-wide lock the store looks for a card already carrying the key (in any state,
  cancelled and done included) and returns it instead of creating another; the tool reply
  says `created: false` and names the existing card. `force=true` skips only the heuristic
  dedup, never the key check. The canonical card for a key is the oldest one (created_at,
  then id). `board_cancel_feature` / `cancel_feature` take an optional `duplicate_of`. With
  it, the cancel is refused when the target is the canonical card for a key it shares with
  `duplicate_of`, when `duplicate_of` is cancelled, or when `duplicate_of` is already
  recorded as a duplicate of the target. The checks and the cancel run under the same lock,
  so two opposing duplicate cancels leave one card live. A guarded cancel stamps
  `dup-of:<id>` and names the card in its audit reason. Creates and cancels without these
  arguments behave as before.

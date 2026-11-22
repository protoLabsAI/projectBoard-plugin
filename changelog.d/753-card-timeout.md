- **A card can set its own coder timeout, and its first timeout earns a longer retry
  (#753, 1 of 2).** `board_create_feature` and `board_update_feature` take
  `coder_timeout_s` (positive seconds, stored as a `coder-timeout:<n>` label) and
  `board_get_feature` shows it. Zero, a negative or a non-number is refused with a plain
  error and leaves the card unchanged; an empty value on update leaves it alone. Every
  coder dispatch for the card uses it instead of the board-wide `coder_timeout_s`. After
  a card's first `CoderTimeout`, its next dispatches get 1.5x the timeout that expired,
  once per card (a durable `budget:timeout-raise-ms` label, so a restart neither forgets
  the raise nor grants a second one). A second timeout does not raise it again, and the
  tier climb, the too-wide park threshold and the provider-stall same-tier retry are
  unchanged.

- **`GET /features` no longer shows a board without a card that was just created, and says when a listing is not current (#820).**
  A polling client could read a default listing with no open cards while cards were being
  created: when the listing's refresh took longer than 1.5 s behind the drive loop, the
  route answered from the snapshot taken before the create. A listing that starts after a
  card is created by the board process (loop, tool or route) now waits for the refresh
  that has the card. Other slow refreshes are still answered from the previous snapshot
  within 1.5 s, and that answer carries `X-Board-Snapshot: stale`; so does a listing the
  board changed under while it was read, and one served because `br` stalled. A `br list`
  that returns something other than a list of issues is now an error (HTTP 400) instead
  of an empty board, and it never replaces the cached listing.

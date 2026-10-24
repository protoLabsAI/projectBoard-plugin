- **A PR GitHub holds not-clean for hours now shows on `/status` health, like a claim stall (#495).**
  The merge edge correctly refuses anything but CLEAN, and #495 made such a card READ `held:
  PR not clean on GitHub (UNSTABLE)` — but nothing surfaced a card that had been stuck that
  way for hours (a `QA panel` check that never settles, an unresolved review thread). The
  merge-state hold now stamps a first-seen time, and each reconcile pass publishes the
  in_review cards held longer than `merge_hold_alert_s` (default 3600) to `/status` under
  `merge_holds`, with their hold text and age. The entry clears when the hold clears or the PR
  merges or closes. `merge_hold_alert_s: 0` turns the health entry off; the per-card
  next-action hold still shows. No GitHub call was added — it reads the merge edge's own
  process state.

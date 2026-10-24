- **The merged-state verification budget now applies live (#490).** Changing
  `merged_verify_max` in Settings updates the running loop without a restart.
  Values below zero are clamped to zero (unlimited), matching startup behavior.

- **Send an operator's design decision directly to a review-gate-blocked PR (#701).**
  `board_requeue_feature(findings=...)` and the review routes now accept a card the
  review gate blocked, retaining its PR and leading the next coder prompt with the
  findings. The review-fix budget is reset without re-arming the gate before the
  coder runs. Other blocks, busy cards, and escalation behavior are unchanged.

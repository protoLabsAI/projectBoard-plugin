- **A release chore or a decompose task is never auto-decomposed on repeated timeout
  (#585).** A card that keeps timing out is normally parked and handed to the board's own
  agent to split (#378). Two kinds of card have no buildable slices, so splitting them is
  pointless: a **release chore** (title starts with `chore: release`, or every file it
  touches is a version manifest or lockfile — `pyproject.toml`, `*.lock`, `CHANGELOG.md`,
  `changelog.d/**`, …) and a **decompose task itself** (title starts with `Decompose bd-`,
  since splitting a split loops). The park path now recognises both through one pure
  predicate and blocks them with a plain timeout reason instead — `timed out Nx; not split:
  <why>; raise coder_timeout_s and unblock` — so the fix is a longer coder budget, not more
  slices. This stops the loop that let `chore: release v0.8.1` be parked too-wide, split,
  and its decompose task time out and nearly be split again. An ordinary feature card still
  parks and files its split exactly as before.

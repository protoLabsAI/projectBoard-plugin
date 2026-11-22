- **The Ready gate accepts a `files_to_modify` path that exists on the card's base branch,
  even when the project checkout has another branch checked out (#702).** A project whose
  `base_branch` is an epic branch no longer has its epic-only files refused as phantom paths
  (or needs them marked `(new)`) just because the team checkout sits on `main`; the same
  holds when a ready card is moved to such a project.

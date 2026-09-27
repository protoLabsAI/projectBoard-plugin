- **Publish gates: a card can wait for a release, not just a merge (`waits_for`).**
  `depends_on` releases a dependent when its blocker merges. A consumer that installs what
  the blocker *publishes* needs to wait longer: the changesets release PR has to merge
  and the package has to reach npm. A card (feature or task) can now carry
  `waits_for="npm:@protolabsai/ui@>0.62.0"`, `release:<owner>/<repo>@<tag-or-range>` or
  `pr:<owner>/<repo>#<n>`. The loop leaves it unclaimed until every gate holds, and the
  card says why: `waiting on publish: npm @protolabsai/ui >0.62.0 (latest 0.62.0)`. The
  wait is never counted as a livelock. Checks are cached per spec and shared by every
  card that names it. A failed check keeps the card held and shows the error, then backs
  off. `board_check_gates` / `POST /features/{fid}/gates/check` check now. Ranges follow
  node-semver, including its prerelease rule. Specs live in the bead's notes, not in a
  label, because a real spec is longer than beads' 50-character label cap. Private
  packages read with the new `npm_token` secret.
- **Auto-merge holds while the target repo is cutting a release.** Before merging, the
  loop checks the PR's repo for a `prepare-release*` branch, an open PR from one, or an
  active `prepare-release.yml` run. If it finds one, it holds the merge: the card stays
  in review reading `held: release freeze (<evidence>)`, no merge attempt is spent, and
  the merge lands on the first poll after the release. Per-project `release_freeze`
  sets the patterns, or `false` turns it off, as for a changesets repo like protoContent,
  whose release PR is open most of the time. A check that errors holds the merge.
- New skill **`cross-repo-chain`** and [docs/publish-gates.md](docs/publish-gates.md):
  how to write change → publish → adopt as board cards, with a worked design-system
  example.

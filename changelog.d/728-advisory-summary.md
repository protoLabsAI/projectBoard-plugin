- **Setup advisories lead with a short plain summary and carry a fix button (#728, 1 of 2).**
  A slow pre-PR gate, a slow preflight or an unwinnable coder.solve() oracle used to show
  up as one long line that started with the raw command, so the banner cut it off before
  the fix. Each advisory is now a structured entry (`health.advisory_entries()`): a plain
  summary of at most 120 characters that says what is wrong ("board-plugin's pre-PR check
  takes longer than 10 minutes, so it is skipped."), then the fix, then the detail with the
  command. The preflight setup gap now carries a `plugin_config` button that opens the
  settings named in the first advisory's fix (for a slow gate: `local_gate_timeout_s` and
  `local_gate_cmd`).

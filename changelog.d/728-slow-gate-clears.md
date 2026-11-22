- **The slow-gate warning clears as soon as you fix the gate (#728).** When a project's
  pre-PR gate timed out, the setup warning stayed up until a later gate run finished, which
  an always-slow gate never does. Now the loop checks on every tick and after a settings
  reload: once the project's `local_gate_cmd` or `local_gate_timeout_s` no longer matches
  the one that timed out, the warning goes away without another gate run. This includes
  a restart with the new setting. A gate that is unchanged stays flagged, and the
  merged-state re-verify still skips it.

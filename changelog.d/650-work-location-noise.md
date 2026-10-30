Work-location alerts no longer page the operator about a coder's scratch files (#650). Capture
skips the coder's own session state (Claude Code task output under `<tmp>/claude-*`, `~/.claude`,
`~/.codex`) and any readable file outside every repository, such as a gate log in `/tmp`. The
sweep ignores entries already recorded under those roots. An unreadable location with no recorded
sha, branch or remote ref no longer alerts on "authorization required", because nothing is known
to be at risk there. A location with a recorded commit still alerts. In five days protoEngineer
answered 22 of these alerts, each with "no action needed".

The coder timeout is now activity-aware. When `coder_timeout_s` runs out while the coder is still
streaming events, it gets 5-minute extensions, up to twice the budget. A coder that has been
silent for 10 minutes (the default `provider_stall_idle_s`) is killed on time, as before, and still
reads as a provider stall. The timeout message keeps its `coder timed out after {N}s` prefix and
adds "(extended to Ms while still active)" when it was extended. On 10-29, six cards were killed
at 1800s with their last event 0–42s before the kill.

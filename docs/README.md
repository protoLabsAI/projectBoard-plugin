# Documentation

Start with the [README](../README.md) for the five-minute version. This directory has the rest.

| Doc | What's in it |
|---|---|
| [install.md](install.md) | Requirements, install, enable |
| [annotated-config.md](annotated-config.md) | A commented `project_board:` block and where the board lives |
| [overview.md](overview.md) | How it works: store, loop, escalation, `coder.solve()`, planning layer |
| [usage.md](usage.md) | Tools, skills, the HTTP surface, the console view |
| [lifecycle.md](lifecycle.md) | The lanes, the `in_review` sub-state machine, head-pinned verdicts, blocked-card self-heal |
| [gate.md](gate.md) | The coder's fast slice of CI: tiers, `auto` discovery, preflight, environment |
| [setup-preflight.md](setup-preflight.md) | The setup check, and the `br` first-run fetch |
| [task-lane.md](task-lane.md) | Deliverable cards and self-verification |
| [publish-gates.md](publish-gates.md) | `waits_for` grammar and the release freeze |
| [console-settings.md](console-settings.md) | Settings fields in the console |
| [configuration.md](configuration.md) | Every config key, its default, and whether a change is live / reload / restart |
| [tools.md](tools.md) | Every agent tool, with the lifecycle-changing ones flagged |
| [api.md](api.md) | Every HTTP route: the operator (bearer) and public (HMAC) surfaces |
| [adr/](adr/) | Decision records for **this plugin's** subsystem behavior |
| [development.md](development.md) | Layout, external-seam coverage, scripts, releasing |

`tests/test_docs_reference.py` covers `api.md`, `tools.md`, `configuration.md` and the layout table: it fails
if a route, an agent tool or a config key is added, renamed or removed without its reference changing, and it
recomputes the YAML-only count `configuration.md` states in prose. That is a COVERAGE guarantee — that the lists are
honest and complete. What each entry *means* is still a human's job.

**ADRs cited here that are not in `adr/` are the HOST's**, and live in
[protoAgent's `docs/adr/`](https://github.com/protoLabsAI/protoAgent/tree/main/docs/adr) —
ADR 0008 (sandboxing), 0024/0025 (ACP coding agents + the delegate registry), 0026
(plugin-contributed console surfaces) and 0064 (execution-grounded `coder.solve()`). This
plugin's own decisions are the ones in `adr/`; it does not restate the host's.

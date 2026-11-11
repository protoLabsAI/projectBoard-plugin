# Prospective advisory spec-review pilot

No measured results are claimed. This is explicit opt-in tooling, not an admission gate.
Use ADR 0077's independent-review/synthesis separation as precedent; existing accepted
ADR 0326 merge holds and all Ready/coder/merge controls remain unchanged. Routine coder
choices remain coder choices, and findings require human triage.

Before recruitment, record a fixed enrollment window, target sample size, project mix,
medium/large/hard proportions and coding-card inclusion rules. Enroll consecutive eligible
consenting cards; record exclusions and withdrawals. Compare against a contemporaneous
baseline without this tool, matched by project and difficulty; preassign groups before
seeing outcomes. Record authoring hash and run ID, repeat reviews and spec edits. Count
stale/failed reviews and missing measurements rather than excluding them as successes.

For both groups collect spec blockers found before coding and after dispatch, repair rounds
until accepted delivery, source-grounded defects confirmed by human triage (plus rejected
findings/false positives), elapsed wall time, model cost and human triage minutes. Keep
pre-coding delay separate from delivery time. Define a repair round as a submitted change
followed by another requested correction. Record blocker severity and whether each decision
is a product ambiguity, a routine coder choice or deferred work. Report denominators,
distributions and missingness, not just averages; avoid causal claims from unmatched cohorts.

Token usage, model cost and repository coverage remain null/unknown unless measured by an
actual provider/trace. Never infer cost from elapsed time or claim full coverage from a
bounded search. The local implementation measures wall time only. Before broader adoption,
review blocker yield, repair rounds, grounded defect yield, cost, latency and human burden
with the operator and record a retain/change/drop assessment. Any hard gate requires a
separate explicit policy decision and supporting evidence.

## Trust and evidence boundaries

The comprehension config declares tools=[], inherit_read_tools=False, and extra_tools=[].
Readiness uses a separate registered worker and only injected repository-bound read/search
tools; both calls check the SDK's explicit extra_tools parameter and effective config fields.
These checks prove declaration support, not runtime context isolation or enforcement. Absent
runtime attestation, reports always carry that gap and never claim independent approval.
No conversation history is explicitly passed. The host may still supply hidden context.
Unsupported hosts fail visibly instead of retrying with broader permissions.

The authoring snapshot is at most 64000 bytes. Each stage returns at most 16000 bytes of
JSON, with a 120-second deadline. Reads cap at 16000 bytes; searches cap at 500 files and
80 matches. These are live checkout observations, not a pinned code snapshot. Reports retain
all bounded input and output, policy declarations, elapsed time, status and unknown metrics.
Comments are append-only history; readback verifies the exact run record. Ledger persistence
is recorded as unverified in the record itself; the response supplies observed readback status.
Freshness is checked again after persistence. Always rehash current inputs before reusing a
historical record; concurrent later edits cannot be ruled out by any prior observation.

Automated tests exercise real local filesystem boundary tools and fake SDK/store/registry
seams. They do not establish real host model execution, tool enforcement, isolation, model
cost or real beads persistence. A live host pilot must capture those gaps explicitly.

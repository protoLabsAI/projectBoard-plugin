A no-commit fix round after an EXTERNAL review bounce now blocks once for the operator instead of
climbing the attempt ladder (#653). When a fix round ends on the very head an external QA panel
bounced — the card still carries this card's `ext-review-bounced:<sha>` stamp for that head and
nothing new was committed — the coder has read the finding and DECLINED it, so a stronger model
would decline it the same way. The drive blocks on the FIRST occurrence (owner operator,
`terminal`) with the reason `needs operator: external finding the coder declined — <PR url>`,
followed by the coder's stated reason when its reply has one. No attempt is counted, no tier is
climbed, and no retry is dispatched. Internal-gate and CI-fix no-commit rounds carry no such stamp
for the current head and keep today's escalation ladder unchanged. The incident: an external
bounce drove three fix rounds, each correctly committing nothing and each burning a tier
(bd-jq56.6 / protoAgent#4581).

Unblocking such a card with `board_unblock_feature --override-review` records an `operator ruled
out of scope: external finding at head <sha>` audit comment and LEAVES the `ext-review-bounced:<sha>`
stamp on the head, so reconcile's external-review edge keeps HOLDING that head rather than
dispatching another fix round for it.

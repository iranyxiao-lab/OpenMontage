# OpenMontage v1 Contract Fixtures

These fixtures define the cross-repository OpenMontage control-plane contract.
They are intentionally free of credentials, signed URLs, media bytes, local
paths, shell commands, and complete ObjectKeys. Workers obtain data through
task-scoped grants issued by `biz-module-llm`.

## Redis resources

- Stable command: `mediaworker.openmontage.commands.v1.stable.<pool>`
- Canary command: `mediaworker.openmontage.commands.v1.canary.<pool>`
- Events: `mediaworker.openmontage.events.v1`
- Cancellation: `mediaworker.openmontage.cancel.v1`
- DLQ: `mediaworker.openmontage.dlq.v1`

The `mediaworker.media.*` namespace is reserved for the existing media-worker
recipe contract. Existing `soulx.svc.*` SoulX-SVC resources are unchanged.

## Identity and delivery

`(jobId, attempt, runRevision, stage)` is the execution identity. Events are
deduplicated by `eventId`; command delivery is at-least-once and is ACKed only
after the required event/checkpoint effect is durable. `workerId` and
`consumerName` include pool, namespace, and pod UID in production.

## Fixture inventory

- `valid-command.json`: one immutable stage command.
- `valid-event-approval-required.json`: a stage checkpoint waiting for approval.
- `valid-event-succeeded.json`: a terminal stage result with artifact receipts.
- `valid-grant.json`: a short-lived task-scoped object grant.
- `valid-cancel.json`: an explicit cancellation request.
- `invalid-unknown-field-command.json`: unknown command field rejection.
- `invalid-credential-command.json`: credential-bearing command rejection.

# OpenMontage rollout gates

This is the release runbook for the `openmontage-test` and `openmontage-prod`
overlays. A rendered manifest is not proof of a production rollout; every gate
below requires an auditable command result and an operator approval.

## 1. SHADOW

- Keep `OPENMONTAGE_ENABLED`, publication, recovery, and outbox publication off
  in `biz-module-llm` until contracts and migrations are deployed.
- Route only explicit `ExecutionMode=SHADOW` validation traffic to the canary
  stream. Shadow must not publish business-visible terminal results.
- Verify stable and canary command streams/groups, Redis pending counts, worker
  readiness, and sanitized failure metrics independently.
- Stop the rollout if any unknown-field, credential, unsafe-key, stale-event, or
  receipt-validation case is accepted.

## 2. One-percent canary

- Enable publication and recovery in test first, then production with a 1%
  routing cap enforced by the control plane.
- Canary has its own stream, consumer group, Deployment, PDB, and queue metric;
  never move existing stable attempts between channels.
- Required 30-minute gate: no terminal-state divergence, no duplicate
  `(jobId, attempt, runRevision, stage)` execution, p95 heartbeat age below the
  configured timeout, zero unexpected DLQ entries, and verified output receipts.

## 3. Stable promotion

- Promote the exact immutable image digest and manifest/skill digest that passed
  canary. Scale only the affected pool; planner, render, and GPU remain isolated.
- Require two ready stable replicas in production, `maxUnavailable=0`, PDB and
  topology spread checks, and a successful drain simulation before promotion.
- Keep GPU replicas at zero until node resources, provider configuration, and
  object-store grants are explicitly approved.

## 4. Rollback

- Disable new OpenMontage publication/routing. Do not rewrite terminal history or
  mutate an existing attempt's frozen channel/pool/manifest.
- Let in-flight attempts finish or cancel them through the control plane. Inspect
  SQL outbox, event ledger, attempt, and DLQ state before replaying anything.
- Roll back the Deployment image/config by digest and retain the failed canary
  evidence. Re-enable only after the failed gate has a corrective change.

## 5. Legacy media stream retirement

For the media-worker rename, remove `soulx.media.*` reads only after all of the
following are zero for the full seven-day observation window:

`ready + pending + legacy outbox rows + active legacy attempts`.

Run the drain evidence script, archive its output, then remove legacy consumers
in a separate audited release. `soulx.svc.*` is not part of this migration.

## 6. AGPLv3 release gate

Before exposing a modified OpenMontage service over a network, publish the
corresponding source, license notices, build instructions, and exact image
source revision required by AGPLv3. The gate owner records the source bundle
location and approval alongside the image digest.


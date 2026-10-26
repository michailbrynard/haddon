# 0003 — Collapse self-approval into an audit trail

**Kind:** domain simplification + schema/API replacement  
**Risk:** high  
**Effort:** L

## Problem

Ordinary writes currently create proposal, decision and application records even when the same authenticated
actor submits and immediately accepts the action. Additional rule/authority/opening-verification structures
support a generalized authorization model that the product does not complete or use.

The public generic proposal endpoint is especially misleading: a caller can create an awaiting proposal, but
there is no complete public decision-and-application workflow for arbitrary proposal payloads. Immediate
acceptance records approval metadata; it does not turn an arbitrary payload into a bookkeeping action.

This is ceremony without a real separation of duties. It makes agents reason about persistence mechanics and
makes the audit history harder for a reviewer to read.

## Desired model

Every business command writes its durable result and one intelligible audit action in the same transaction.
The audit action records at least:

- command/action kind;
- entity, actor and timestamp;
- supplied reason;
- idempotency/request identity;
- affected durable record IDs;
- canonical input or its content hash; and
- evidence references where relevant.

The audit log is immutable. Posted entries remain append-only and are corrected through explicit
correction/reversal commands. Auditability therefore survives without pretending each self-approved command
went through an independent authorization process.

## Work

1. Define the minimal audit-action schema and a small helper used by command transactions. Keep request and
   idempotency records separate: they answer retry questions, while audit actions answer who did what and why.
2. Convert one representative command, preferably `post_entry`, to write its domain rows and audit action
   directly. Compare its exported journal and reviewer history with the old path.
3. Convert the remaining immediate write paths. Remove caller-visible `proposal_id`, `decision_id` and
   `application_id` where they are not genuine business identifiers.
4. Delete `POST /proposals` and its read surface when the named commands replace it. Do not add an adapter for
   an API that has never been deployed.
5. Rebuild tests and development fixtures with direct audit actions. Preserve source journal/document
   provenance, but do not preserve synthetic proposal, decision or application identifiers.
6. Remove the proposal/decision/application, rule-version, authority, opening-verification and related tables,
   triggers and code once the direct commands cover their real use cases.

## Optional review queue

If the team needs pre-posting approval, implement it later as a review extension with a complete lifecycle:
submit a typed command, list/get pending work, approve or reject it, apply the exact reviewed content, and
show the outcome in the UI. It should reuse the command handler and verify the reviewed content hash.

Do not retain the current generic proposal machinery merely as a placeholder for that possible feature.

## Acceptance criteria

- A normal command creates no proposal, decision or application row.
- The public surface contains no generic write that can create permanently unactionable work.
- Retrying a command is idempotent and returns the same durable result.
- For every mutation a reviewer can answer who, when, why, what input, what changed and which evidence was
  used without joining a synthetic approval chain.
- Rebuilt fixtures contain the actor, reason, evidence and durable result needed for useful audit history.
- Tests prove that a failed domain write cannot leave an audit action claiming success, and vice versa.

## Pitfalls

1. Do not collapse audit into mutable `updated_by` columns. History must remain append-only.
2. Do not lose the canonical content hash that detects reviewed-content drift.
3. Do not create a compatibility adapter or legacy audit view for undeployed internal structures.
4. A separate actor/reviewer remains meaningful only when two actual principals participate; model that in
   the optional review workflow rather than manufacturing it on every write.

# Operational handover — single-store mock staging

## Scope and evidence
PoPify runs FastAPI, PostgreSQL and three scheduled n8n workflows. Keep customer messaging, refunds, cancellations and other external actions simulated. This runbook is for an approved private staging host or a disposable development stack, not a public service. Never use real customer data in CI or demonstration fixtures.

Before handing over, record the exact revision and CI run, image versions, approved host/network, named administrator and backup-key custodian, escalation contact, restore evidence and any unresolved gates in RELEASE_GATES.md. Do not place passwords, private keys, tokens, customer records or backups in Git or CI artifacts.

## Start and verify
1. Review the PR and require every exact-head CI job to pass. Review migration changes and dependency updates; do not merge automatically.
2. Follow OPERATIONS.md to provision unique secrets and the private TLS profile. Keep API, n8n and PostgreSQL ports private and confirm the separate data, worker and operator networks. Use the trusted CLI to provision separate operator accounts. The staging profile remains mock-only.
3. Verify /live, /ready, signed-intake boundary (disposable environment only), managed login/role permissions, session revocation, and the Operations screen. Confirm actual scheduled worker polls, no unresolved queue failures, and no external provider calls.
4. Configure the approved scheduler and local alert outbox; test opened, reminder and resolved notifications with the approved relay, deduplicating event IDs. Confirm escalation on monitor unavailability or a full outbox. Do not mark alert delivery verified based solely on local output.

## Routine operations
- Daily when staging is in use: inspect readiness, last worker poll/completion/failure, due versus scheduled queue counts, unresolved outcomes and pending human review. Review failed/ambiguous records in Recovery.
- Weekly: review access, outstanding approvals, failed CI checks, dependency updates and log/backup retention. Revoke unused sessions and accounts through approved controls.
- Monthly: record measured queue p95, backlog, HTTP acknowledgement, availability observations, cost and maintenance work. Review security findings and alert noise.
- Quarterly or after material schema/workflow changes: perform isolated encrypted PostgreSQL and n8n restoration, test key recovery and measure RPO/RTO; repeat keyboard, screen-reader and representative workload checks.

## Incident handling
- Database unavailable: stop accepting success assumptions; check /live versus /ready, database connectivity and schema. Signed senders retry the same delivery ID. Do not create new IDs to bypass idempotency.
- n8n stale or failed: inspect private execution logs, database health and configured worker secret; verify durable backlog and restore the existing scheduler. Do not replay an uncertain provider action.
- Provider outcome uncertain or disputed: retain the original attempt, gather evidence and record an authorized reconciliation. A confirmed nonexecution may require a fresh reviewed approval; never modify the original audit trail.
- Authentication incident: revoke affected sessions, disable compromised accounts, preserve at least one authorized administrator, rotate affected secrets under change control, verify access denials and record incident evidence without raw payloads.
- Restore: isolate the target, keep originals intact, verify archive provenance and matching n8n key/image, invalidate restored sessions and confirm migration/sequence integrity. Use the disposable CI drill as a reference, not as a production restore command. Reconcile external state before any separately authorized live action.

## Release / rollback
Take verified encrypted backups before changes. For a compatible code-only rollback, use the previously reviewed image/revision after checking schema compatibility. For incompatible schema changes or data corruption, recover to a separate target and validate before switching traffic; do not run reverse SQL or overwrite the only backup. Record actual rollback/restore outcomes and communicate unresolved risks.

## Conditional work still requiring approval
Approved notification destination, real host and certificate, independent security review, backup custody/offsite/PITR, approved privacy/retention policy, representative merchant traffic and usability evidence, manual accessibility review, Shopify development-store and provider sandbox access. Commercialization, shared multi-store operation and live financial actions remain outside this mock staging scope.

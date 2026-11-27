# Single-store release acceptance gates

PoPify is mock-only. These gates distinguish repository verification from a real operational release. Record evidence URLs and exact commit before marking a gate satisfied; never treat a CI fixture as customer validation.

| Gate | Required evidence |
| --- | --- |
| Code review | All exact-head CI jobs green, independent security/code review, no unresolved critical issue; review migrations and configuration changes |
| Identity | Managed PostgreSQL accounts, deny unauthorized mutations, verified revocation and protected private HTTPS access; MFA/SSO where required; documented break-glass ownership |
| Ingress | Separate public signed-intake and private operator boundaries; trusted certificates/proxy configuration; tune per-IP/global request limits from approved traffic |
| Workflow correctness | Signed intake persisted before acknowledgment; duplicates/out-of-order events, competing workers, human approval, uncertain outcomes and due retries verified; no unintended successful execution in regression suite |
| Operational health | Alert transitions and external delivery/recovery verified; on-call ownership; liveness/readiness and sustained worker health observed |
| Capacity | Representative sustained workload with p95 webhook acknowledgment <=500 ms and normal queue waiting <=120 s; document arrival rate, scheduler interval, queue depth, failure rate and environment. Low-rate synthetic CI is not sufficient |
| Backup/recovery | Encrypted backups, independent key recovery, offsite transfer, PostgreSQL and n8n state restore, tested RPO <=24 h and RTO <=4 h under approved deployment |
| Privacy | Approved inventory, access/retention/deletion and incident procedures; no real customer data before approval. AI processing requires separate documented review |
| Accessibility | Automated scans and manual keyboard, screen reader, zoom, contrast and managed-role journeys against staging; WCAG 2.2 AA target, not a certification |
| Shopify integration | Authorized development store, actual signature, retries, replay/out-of-order and field validation; no real orders or customer information |
| Provider execution | Provider-specific sandbox, idempotency and reconciliation evidence, separate authorization; live financial/customer actions remain disabled |
| Merchant validation | Approved single-store mock/read-only pilot, baseline and observed handling time/errors, agreed goals (proposed >=20% median handling-time reduction); no invented results |
| Multi-store/commercialization | Verified tenant isolation, demonstrated buyer demand, commercial/legal approval and separate authorization |

## Maintenance cadence

Review CI failures and dependency updates before merging. Monthly: review queue trends, alert noise, access, secrets, retention and dependencies; record owners for open risks. Quarterly: rehearse restoration, manual accessibility and representative queue capacity in an isolated environment. Before each deployment: back up, verify migrations and rollback/restore instructions, test with mock-only settings, and preserve human approval history. Deployment, merchant pilot, live provider actions and commercial commitments are not authorized by repository tests.

The repository CI now includes an encrypted n8n state recovery drill on a separate volume. Record its result and measured duration alongside PostgreSQL restoration evidence; host-loss recovery, independent key recovery, actual RPO/RTO and offsite storage remain distinct gates.

The local alert outbox preserves unacknowledged events and supports explicit acknowledgement; actual relay delivery, deduplication and escalation ownership must be exercised on the approved host before deployment.

Use [OPERATIONAL_HANDOVER.md](OPERATIONAL_HANDOVER.md) to record ownership and evidence, run incident procedures and preserve rollback boundaries. A CI-green branch is necessary but not sufficient for a real deployment.

CI exercises a fresh synthetic recovery keyring and rejects truncated encrypted archives before restoration. This does not establish actual offsite custody or authenticity of an archive obtained from an untrusted source; validate provenance separately before an authorized restore.

Expired authentication-artifact cleanup is implemented with a dry-run default and explicit apply flag. Business-data deletion remains blocked on an approved retention policy and evidence-preservation review.

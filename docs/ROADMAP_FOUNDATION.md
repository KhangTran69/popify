# Roadmap foundation: request boundaries and operational readiness

Baseline: main at `1fa9420c68986b6afc272a85c383ad641bb62a14` (25 November 2026). This is an incremental engineering change; PoPify remains a single-store, mock-action portfolio application.

## Request and database boundaries

- Mutation bodies are limited before parsing: 1,000,000 bytes for the Shopify order webhook and 128 KiB for other POST/PUT/PATCH/DELETE routes. Actual streamed bytes are counted even when Content-Length is absent or inaccurate. The original bytes are replayed for HMAC validation.
- Invalid Unicode credentials/signatures fail safely; empty configured internal credentials cannot authenticate workers. The configured demo identity is development-only. Managed production identities use PostgreSQL-backed roles, password hashes and expiring sessions revoked on account changes or logout; see OPERATOR_IDENTITY.md.
- Production startup rejects invalid environment names and known example secret placeholders. The local preview remains isolated.
- Database connection errors return a generic 503, with a correlation ID and a structured event. SQL parameter values are hidden from SQLAlchemy exception formatting. PostgreSQL connections are checked before checkout. This is not a guarantee against all sensitive logging.
- Reverse proxies should independently enforce request size, request timeouts, connection and traffic limits, trusted proxy configuration, HTTPS and private admin access. The application limit is per request, not distributed rate limiting or a total memory bound. Do not expose the local Compose stack publicly.

## Deployment gates and verification

Use the existing `python -m scripts.verify local`, `postgres`, `reviewer` and `demo` commands in their documented disposable environments. The new regression tests cover Unicode authentication, blank integration secrets, oversized and streamed requests, exact byte replay, stale operator identity and database failure responses. Existing signed-webhook and approval/recovery tests remain important. The continuation adds PostgreSQL concurrency and managed-identity verifiers, browser keyboard and axe checks, disposable ingress/outage/restore drills, and index measurements. Check the exact revision's CI results; manual screen-reader review, representative workload and deployed-environment evidence remain required.

| Gate | Evidence needed before proceeding |
| --- | --- |
| Public staging | Verified identity and permissions, traffic limits, TLS, protected admin interfaces, monitoring, secret handling, recovery |
| Real merchant data | Data inventory, access scope, retention and deletion policy, approved processing and tested restore |
| Shopify integration | Authorized development store, signature/replay/out-of-order checks and end-to-end queue measurements |
| Live provider actions | Provider sandbox, least privilege, stable idempotency, reconciliation after ambiguous submission, explicit authorization |
| Shared multi-store | Verified tenant isolation in all routes, jobs, records and audit data; demonstrated demand |

## Remaining roadmap work

Managed identities and authorization, readiness and operational status, a logical backup/restore drill, payload minimization, retention inventory, browser checks and measured indexing are implemented in this branch. Remaining gates include external alerting and deployed ingress verification, approved retention/deletion policy, encrypted offsite backups with an agreed RPO/RTO, manual accessibility review and representative end-to-end queue measurements. See OPERATIONS.md. Preserve current n8n orchestration, PostgreSQL state and evidence-bound human approval. The measured one-minute single-item order schedule exceeded the proposed queue p95 in a low-rate synthetic workload. The order workflow now requests up to three items with independent transactions; the support worker remains single-item. Validate exact-head scheduled results before claiming improvement. In-process API latency does not establish end-to-end capacity.

Keep a monthly maintenance/risk review and quarterly restore/accessibility/capacity checkpoint. A read-only or mock-action pilot can precede a live provider integration. External credentials, infrastructure expenditure, actual customer records and production deployment are separate prerequisites; no live actions are enabled by this change.

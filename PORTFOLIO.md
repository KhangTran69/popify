# CommerceOps — portfolio case study

## Problem and scope

Store operations combine repeatable work with decisions that need human judgment. CommerceOps demonstrates one store's order intake, delayed-order detection, support drafting, approval, and recorded outcome using three n8n workflows, FastAPI, PostgreSQL, and a server-rendered operator console.

## Implemented improvements

- Replaced direct browser execution with durable queues for support triage and approved actions, both consumed by n8n.
- Added operator navigation, searchable/filterable/paginated queues, a redesigned login page, readable supporting facts, related records, and combined order timelines.
- Replaced fixed workflow health labels with actual worker timestamps and event-derived metrics.
- Added an authenticated, CSRF-protected guided walkthrough that follows one synthetic order across the three live n8n workflows, links the related records, refreshes queued work automatically, and lets the operator trigger the next real n8n step.
- Added explicit workflow state labels and a one-click n8n trigger for the approved mock outcome. Conflicting reuse of a support request key now returns a conflict instead of silently reusing an unrelated case.
- Added service-level approval enforcement, scheduled backoff preserved across manual retries, exhausted recovery, and ambiguous-outcome retry blocking.
- Strengthened concurrent delivery ingestion and order updates with unique constraints, row locks, and per-order PostgreSQL advisory locks.
- Preserved mandatory review flags after LLM classification and separated provider adapters from business rules.
- Pinned runtime and test dependency closures, added startup configuration constraints, and added credential-free GitHub Actions checks.

## Measured verification — 24 October 2026, Manila time

- **33 automated behavior tests passed** on 24 October 2026 using isolated SQLite, including guided demo authentication/CSRF, conflicting support request keys, configuration limits, approval gating, retry eligibility, exhaustion, and ambiguous outcomes.
- **PostgreSQL concurrency checks passed** in an isolated temporary schema: concurrent webhook duplicates, duplicate workers, concurrent first-insert/out-of-order deliveries, conflicting support key reuse, repeated approvals, and repeated execution, with six workers per scenario (two concurrent submissions for the conflict race).
- **All three real local n8n workflows executed** in the browser walkthrough and the repeatable CLI demonstration. CLI run `0a56e1fafd` created order 13, case 11, exception approval 15, and reply approval 16. The approved reply completed with exactly one mock execution record.
- The CLI synthetic delivery took **0.253 seconds** from recorded receipt to persistence when its n8n worker was triggered immediately. This is one local observation, not a throughput benchmark or scheduled latency guarantee. The normal n8n schedule runs every minute.
- Browser inspection covered the login and overview at desktop width, plus the dashboard and full guided journey at a 400 px responsive viewport. The walkthrough created order 12, case 10, exception approval 13, and reply approval 14; it showed the linked draft and audit timeline, then completed with one mock execution. Existing overview and approval screenshots are saved in `docs/screenshots/`.
- API was rebuilt from the pinned lock file while the existing PostgreSQL and n8n services and volumes remained running.
- A disposable fresh n8n container imported and published all three exports successfully. Existing n8n and PostgreSQL volumes were preserved.

## Limitations

No live Shopify development store, real LLM request, customer message, cancellation, or refund was used. Shopify HMAC handling is exercised with synthetic signed payloads. The optional OpenAI adapter has mocked HTTP coverage. All action execution and provider failures are simulated. Retry-After behavior is represented by a configurable mock minimum delay, not a verified live provider response. The reference/email match is an operator lookup check, not customer identity verification. There is no live ambiguous-outcome reconciliation endpoint, production authentication system, or measured business impact.

## Resume bullets

- Built three n8n workflows coordinating durable order ingestion, support triage, delayed-order review, and approval-gated mock execution using FastAPI and PostgreSQL.
- Designed an authenticated operator console with searchable queues, CSRF-protected decisions, correlated timelines, event-derived metrics, and failure recovery controls.
- Verified behavior with 33 automated tests, PostgreSQL concurrency scenarios, and a complete local n8n demonstration covering repeat-safe approvals and audited outcomes.

# PoPify — portfolio case study

## Problem and scope

Store operations combine repeatable work with decisions that need human judgment. PoPify demonstrates one store's order intake, delayed-order detection, support drafting, approval, and recorded outcome using three n8n workflows, FastAPI, PostgreSQL, and a server-rendered operator console.

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
- Corrected the guided run so failed triage stays a failure with a recovery link. The overview now separates idle polls, worker failures, and completed business work, and shows intake queue age, due retries, and exhausted items from durable records.
- Verified a queued synthetic delivery across API and n8n restarts without removing either volume; the resumed n8n order worker processed it once.
- Preserved queue search, status, and pagination context across record navigation; grouped approval facts into verified context, triage assessment, draft, and information gaps; surfaced the retry deadline on manual recovery pages.
- Captured customer requests, policy, order state, and delay evidence at approval creation; kept current order state separate; carried validated navigation context through related records and mutations; clarified recovery and rejected-demo states.
- Bound each executable approval to a canonical hash of the action and evidence reviewed by the operator, persisted the same contract on the execution record, and blocked changed evidence or unsupported mock actions before execution.
- Split the overview recovery total into exact action, support, and delivery queue links; bounded receipt-duration aggregation to the latest 500 processed deliveries and removed the support queue's repeated order lookup.
- Added a one-command disposable preview with coherent pending, completed, rejected, retryable, exhausted, and ambiguous synthetic records. It uses a temporary SQLite database and is explicitly separate from live n8n evidence.

## Current verification — 26 October 2026, Manila time

- **45 isolated behavior tests passed**, including reviewed action/evidence binding, changed-evidence worker reporting, unsupported-action blocking, exact mixed recovery queues, approval request snapshots, validated navigation, and ambiguous retry prevention. All three workflow exports validated.
- **Disposable preview verified:** `python -m scripts.preview --port 8765` started with a temporary SQLite database; preview authentication and the pending, completed, rejected, retryable, exhausted, ambiguous, support-recovery, and delivery-recovery views returned successfully. This is synthetic interface evidence, not workflow execution evidence.
- **Rendered inspection:** inspected the overview and support approval at 1280 × 900 and 390 × 844. Recovery links, evidence sections, mock-mode labels, decision controls, and mobile content remained readable. Existing screenshot files were left unchanged because this pass did not capture replacement artifacts.
- Docker Desktop was unavailable because its local engine socket was absent, so PostgreSQL concurrency, live n8n execution, and the CLI end-to-end demo were not rerun for this change. Their results below remain historical.

## Historical verification — 24 October 2026, Manila time

- **36 automated behavior tests passed** on 24 October 2026 using isolated SQLite, including guided demo authentication/CSRF, failed triage presentation, worker outcome metrics, conflicting support request keys, approval gating, retry eligibility, exhaustion, ambiguous outcomes, and queue context preservation.
- **PostgreSQL concurrency checks passed** in an isolated temporary schema: concurrent webhook duplicates, duplicate workers, concurrent first-insert/out-of-order deliveries, conflicting support key reuse, repeated approvals, and repeated execution, with six workers per scenario (two concurrent submissions for the conflict race).
- **All three real local n8n workflows executed** in the browser walkthrough and repeatable CLI demonstration. CLI run `5f247b35ab` created order 15, case 12, exception approval 18, and reply approval 19. The approved reply completed with exactly one mock execution record.
- The CLI synthetic delivery took **0.216 seconds** from recorded receipt to persistence when its n8n worker was triggered immediately. This is one local observation, not a throughput benchmark or scheduled latency guarantee. The normal n8n schedule runs every minute.
- A queued fixture `restart-b6c57714b2` remained in PostgreSQL across an API restart while n8n was stopped. After n8n resumed, workflow 01 processed it in one attempt and created order 14. No persistent volume was deleted.
- Current browser inspection covered login, overview, guided run, approval facts, and audited outcome at desktop width. Chrome's responsive device toolbar also showed the login, overview, and guided run at 366 px; navigation wrapped, cards stayed readable, and controls remained reachable. The walkthrough created order 16, case 13, exception approval 20, and reply approval 21. Those screenshots remain as the original PNGs in `docs/screenshots/`.
- Live n8n storage contains three active workflows and one older inactive duplicate of workflow 03; the duplicate was preserved.
- API was rebuilt from the pinned lock file while the existing PostgreSQL and n8n services and volumes remained running.
- A disposable fresh n8n container imported and published all three exports successfully. Existing n8n and PostgreSQL volumes were preserved.

## Limitations

No live Shopify development store, real LLM request, customer message, cancellation, or refund was used. Shopify HMAC handling is exercised with synthetic signed payloads. The optional OpenAI adapter has mocked HTTP coverage. All action execution and provider failures are simulated. Retry-After behavior is represented by a configurable mock minimum delay, not a verified live provider response. The reference/email match is an operator lookup check, not customer identity verification. There is no live ambiguous-outcome reconciliation endpoint, production authentication system, or measured business impact. The current PostgreSQL migration and concurrency behavior still need confirmation on a running Docker stack.

## Resume bullets

- Built three n8n workflows coordinating durable order ingestion, support triage, delayed-order review, and approval-gated mock execution using FastAPI and PostgreSQL.
- Designed an authenticated operator console with searchable queues, CSRF-protected decisions, correlated timelines, event-derived metrics, and failure recovery controls.
- Verified behavior with 45 automated tests; historical PostgreSQL concurrency, restart-recovery, and local n8n demonstrations cover repeat-safe approvals and audited outcomes.

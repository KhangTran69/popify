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
- Added approval freshness checks against current order, inquiry, matched customer/order email and reference, draft, and policy; stale decisions cannot reach the mock adapter even when the stored snapshot hash still matches. Explicit revisions provide a fresh review path while preserving original approval, decision, actor, attempt, and audit history.
- Made preview settings independent of `.env` integration configuration, disabled n8n/webhook routes in preview, and expanded synthetic states to include evidence-changed and unsupported actions. Preview decisions affect only the disposable SQLite data.
- Added a scan index for delayed orders, bounded scans to 100 eligible orders per worker call without starving later orders, bounded related approval/timeline reads, and surfaced whether delayed orders remain.
- Moved execution-contract mismatch detection before terminal success/ambiguity short-circuiting so a persisted action/hash mismatch is never reported as a successful repeat.

## Current verification — 26 October 2026, Manila time

- **Behavior suite:** `pytest -q` passed: **53 tests**. Coverage includes source-freshness blocking, legacy approval preservation, numbered replacement approvals, terminal execution-contract mismatch, preview isolation, and paginated delayed-order scans.
- **Workflow exports:** all three JSON workflow files passed `python -m scripts.validate_workflows`.
- **Schema upgrade:** a disposable PostgreSQL instance applied migrations 0001–0004, loaded a legacy approved record, then applied 0005–0006. Its revision, evidence, decision, actor, and timestamps remained intact; the delayed-order index was present afterward.
- **PostgreSQL concurrency:** `python -m scripts.verify_postgres` passed against the disposable local PostgreSQL instance, including concurrent webhook deliveries, duplicate workers, out-of-order first inserts, support request-key conflicts, repeated approvals, and repeated execution. The verifier used an isolated schema and removed it afterward.
- **Synthetic preview:** started from `python -m scripts.preview`, signed into the temporary SQLite application, and inspected the overview and approval record. It clearly identified preview mode and displayed pending evidence; its integration routes reject workflow/webhook calls. The preview is not n8n execution evidence.
- **Visual inspection:** inspected the dashboard and approval detail at a 390 × 844 mobile viewport and a 1440 × 900 desktop viewport; approval evidence sections, action copy, recovery links, mock labeling, and mobile wrapping were readable. The checked-in screenshots were not replaced; they remain earlier synthetic preview captures.
- **Not run:** Docker is installed but its daemon is unavailable, so the real local n8n demonstration and n8n workflow execution were not possible in this change. Earlier n8n results below are historical.

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

No live Shopify development store, real LLM request, customer message, cancellation, or refund was used. Shopify HMAC handling is exercised with synthetic signed payloads. The optional OpenAI adapter has mocked HTTP coverage. All action execution and provider failures are simulated. Retry-After behavior is represented by a configurable mock minimum delay, not a verified live provider response. The reference/email match is an operator lookup check, not customer identity verification. There is no live ambiguous-outcome reconciliation endpoint, production authentication system, or measured business impact. PostgreSQL migration upgrade and concurrency behavior passed against an isolated local PostgreSQL cluster; the full Docker stack and real n8n integration were unavailable for this change.

## Resume bullets

- Built three n8n workflows coordinating durable order ingestion, support triage, delayed-order review, and approval-gated mock execution using FastAPI and PostgreSQL.
- Designed an authenticated operator console with searchable queues, CSRF-protected decisions, correlated timelines, event-derived metrics, and failure recovery controls.
- Verified behavior with the current automated suite; ran schema-upgrade and concurrency checks on disposable PostgreSQL. Historical restart-recovery and local n8n demonstrations cover repeat-safe approvals and audited outcomes.

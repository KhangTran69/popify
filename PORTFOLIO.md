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
- Locked the order and support-case rows used by approval decisions and mock execution until commit, closing the PostgreSQL read-check-act window. Freshness comparisons now retain microsecond timestamp precision.
- Excluded replaced revisions from active recovery counts and added dashboard filters for pending decisions, scheduled or eligible retries, exhausted attempts, stale evidence, and uncertain outcomes. Revision pages link to prior and replacement approvals.
- Made preview approvals interactive end to end: approve runs only the local mock adapter and persists its result, while rejection and blocked/retry states retain coherent audit records in the disposable SQLite database.
- Extended the PostgreSQL verifier with source-update contention during execution and duplicate refresh requests against a single stale approval.
- Unified approval, support, and delivery recovery into one searchable, paginated workspace whose dashboard counts use the same query and whose record links preserve their originating context.
- Added subject-level approval lifecycle locking with stable revision ordering and blocked a new revision when another revision has a started, successful, or ambiguous execution.
- Made the isolated preview scenario-driven: reviewers can create fresh outcomes and run a due local retry without mutating global failure, ambiguity, or retry settings.
- Prioritized recovery by urgency before age, included earlier terminal executions across approval types when blocking newer revisions, and surfaced a precise next step for invalid action contracts.
- Added serialized subject-level manual retry queuing. Replaced approvals cannot be requeued or directly executed; queued local preview retries enforce their persisted backoff deadline.
- Added a reviewed/current per-field evidence comparison and complete replacement lineage (decision actor/time, attempt count, replacement reason, and linked revisions) to approval detail pages.
- Made both preview seed paths record exhausted approvals with an attempt count and audit trail matching the configured automatic retry budget; added coverage that an early preview retry changes no execution state.
- Added a durable, authenticated, CSRF-protected ambiguous-outcome reconciliation flow. It records a separate immutable operator assertion, explanation, reference, actor, timestamp, synthetic flag, and correlation ID while preserving the original ambiguous execution and attempt history. Confirmed completion closes recovery; confirmed non-execution allows only a fresh pending approval; unknown outcomes remain blocked.
- Added an interactive guided scenario detail page with linked order, inquiry, approval revisions, persisted execution, and correlated audit events. The scenario progress and next step derive from saved state; retry timing and reconciliation consequences are explicit.
- Added a PostgreSQL migration for immutable execution reconciliations, including a verifier for fresh and populated 0001–0006 schemas. Extended concurrency coverage for repeated identical and competing reconciliation requests. Fixed a progress-card style collision that made the attention stage render horizontally.

## Current assessment and acceptance criteria — 27 October 2026

The branch starts from merged PR #9 (`119bb43`). Inspection of `app/services.py` showed each `ActionExecution` could have only one reconciliation, so an initial `still_unknown` assertion could not be followed after new evidence. `app/templates/detail.html` exposed reconciliation but not a complete revision sequence. `app/preview_scenarios.py` and `app/main.py` made preview examples inspectable, but starting one created pre-resolved records rather than a pending human decision. The scenario page bounded approval/audit histories without pagination, and stale pending scenarios did not surface a replacement action.

Acceptance criteria implemented:

- Reconciliation is append-only and revisioned. The latest definite supported assertion is visible; contradictory claims set `disputed` and block retries/replacements until a later conclusion is appended. An unknown result can be followed when evidence changes.
- Earlier assertions, actors, references, explanations, timestamps, and audit correlation remain preserved. Original ambiguous executions and attempts are not rewritten. A blocking prior approval links directly to its record. When that earlier execution is confirmed not executed, a previously approved blocked action can be requeued only after its own source evidence is rechecked; stale evidence requires a fresh pending approval.
- Each new scenario starts at a pending decision; preview failure injection is scoped to its scenario and replay creates a separate run. Stale, retryable, exhausted, ambiguous, rejected, successful, and blocked-action journeys derive their outcome from saved records.
- Scenario revision, reconciliation, and audit histories are paginated. The current approval and authoritative reconciliation remain visible when browsing older pages.
- Stale pending approvals offer a replacement action only after the operator can inspect reviewed and current evidence.

## Current verification — 27 October 2026, Manila time

- **Behavior suite:** `pytest -q` passed: **79 tests**, including follow-up assertions after unknown outcomes, conflicting evidence and later resolution, unchanged ambiguous execution records, interactive scenario outcomes, stale evidence recovery, scenario history pagination with current-state visibility, and blocker links/requeue after confirmed non-execution.
- **Workflow exports:** all three JSON workflow files passed `python -m scripts.validate_workflows`.
- **Migrations:** `python -m scripts.verify_migrations` passed on a disposable local PostgreSQL 16.15 cluster. It applied migrations 0001–0008 to a fresh schema and upgraded populated 0001–0007 data with an existing reconciliation, preserving its historical approval, execution, actor, and first assertion while accepting an appended follow-up.
- **PostgreSQL concurrency:** `python -m scripts.verify_postgres` passed against a randomly named disposable schema on the same isolated PostgreSQL 16.15 cluster. It covers webhook/worker races, stale source locking, execution/retry and refresh/execution races, concurrent replacement requests, identical reconciliation duplicates, concurrent contradictory claims, a later supported resolution, concurrent blocker resolution/requeue ordering, and preservation of the ambiguous execution. The configured application database was not used.
- **Synthetic preview:** `python -m scripts.preview --port 8002` started the disposable SQLite preview. Browser review exercised a fresh pending ambiguous scenario, approval into a persisted mock timeout, an appended confirmed non-execution assertion, and the fresh-approval recovery action. The stale-evidence page showed the changed source message and replacement guidance. No live workflow, provider, integration credential, or user data was involved.
- **Visual inspection:** inspected the current isolated scenario runner and stale-evidence page at the browser's default desktop viewport, then at 390×844. Checked scenario stage wrapping, long message wrapping, form sizing, keyboard focus, and the current reconciliation in the page summary. The captured browser view is included with this work report; the existing repository screenshots remain historical and are not represented as screenshots of this change.
- **Live n8n:** not run because Docker was unavailable (`docker info` failed and no local n8n endpoint was active). Workflow-export validation is not n8n execution evidence. Earlier real-workflow checks listed below are historical and were not repeated.

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

No live Shopify development store, real LLM request, customer message, cancellation, or refund was used. Shopify HMAC handling is exercised with synthetic signed payloads. The optional OpenAI adapter has mocked HTTP coverage. All action execution and provider failures are simulated. Retry-After behavior is represented by a configurable mock minimum delay, not a verified live provider response. Reconciliation records an operator assertion and reference; it does not query or verify any provider. The reference/email match is an operator lookup check, not customer identity verification. Production authentication and measured business impact remain out of scope. The full Docker stack and real n8n integration were unavailable for this change.

## Resume bullets

- Built three n8n workflows coordinating durable order ingestion, support triage, delayed-order review, and approval-gated mock execution using FastAPI and PostgreSQL.
- Designed an authenticated operator console with searchable queues, CSRF-protected decisions, correlated timelines, event-derived metrics, and failure recovery controls.
- Verified behavior with the current automated suite; ran schema-upgrade and concurrency checks on disposable PostgreSQL. Historical restart-recovery and local n8n demonstrations cover repeat-safe approvals and audited outcomes.

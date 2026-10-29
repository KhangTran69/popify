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

## Reproducible reviewer recovery — 29 October 2026

Inspection found that approved ambiguous approvals linked to `#approval-actions`, while only pending approvals rendered that anchor. The isolated HTTP smoke test stopped after creating a success scenario, so it did not prove the advertised ambiguity recovery. `scripts/verify.py` had no single command for that path.

- The approval action anchor now encloses controls and explanatory outcomes for every approval state, including reconciliation and fresh approval creation. Browser inspection confirmed the guidance link scrolls to the reconciliation form.
- Recovery classification now carries an explicit inconsistency flag instead of searching display text. Replaced revisions with contradictory terminal records remain visible in the blocked queue; a regression checks this case.
- The isolated HTTP smoke test now completes ambiguity, asserted non-execution, fresh approval, persisted mock completion, and conflicting later evidence. `python -m scripts.verify reviewer` runs that path and workflow validation, then prints the commit, Python version, disposable environment, and n8n availability without secrets.
- Current synthetic preview captures show the uncertain approval at desktop and mobile widths in `docs/screenshots/reconciliation-action-desktop.jpg` and `docs/screenshots/reconciliation-action-mobile.jpg`.

Current verification: 100 behavior tests passed; all three workflow exports validated; the new reviewer command passed. PostgreSQL concurrency checks passed on isolated schemas in a disposable local PostgreSQL cluster. The changed approval page was inspected at 1440×900 and 390×844, with no horizontal overflow at 390 px, visible keyboard focus, a working reconciliation anchor, and browser back. No migration changed. `python -m scripts.verify demo` reported that Docker's daemon was unreachable, so the real local n8n demonstration was not run.

## Outcome consistency and recovery reachability — 29 October 2026

The dashboard previously counted every successful execution row, even when its approval was rejected, while the recovery workspace omitted terminal records whose decision and execution disagreed. Record guidance identified some inconsistencies, but lower page copy and scenario stage text could still claim completion. The order journey query already used SQL-linked pagination, yet had no regression beyond its former 100-case limit.

- Added a shared terminal-outcome check for record and scenario interpretation. The dashboard now counts only matching approved, succeeded approval/execution pairs. Contradictory terminal records appear in the blocked recovery queue with an investigation action, and approval detail copy agrees with its guidance.
- Added regression fixtures for missing and contradictory executions and an order with 105 linked cases and approvals. The older records remain reachable through related-approval and journey pages.
- Linked the ambiguity recovery result to the original assertion and fresh saved outcome. Refreshed synthetic recovery workspace captures at desktop and mobile widths.

Verification performed for this change: `python -m scripts.verify local` passed **99 tests** and all three workflow exports validated. The PostgreSQL concurrency script passed on isolated schemas in a disposable local PostgreSQL 16 cluster. Browser inspection covered the blocked recovery page at 1440×900 and 390×844, record navigation and browser back, and visible 2 px keyboard focus; mobile scroll width was 390 px. The new screenshots are synthetic preview evidence. `python -m scripts.verify demo` reported that Docker's daemon was unavailable, so the real n8n run was not repeated. No schema migration was added.

## Operational journey completion — 28 October 2026

Ordinary order, support, approval, and delivery pages previously exposed a raw event-name/JSON timeline while the guided scenario used separate readable labels. The detail route also assembled the cross-record audit query itself and silently capped the result. The recovery walkthrough could create a fresh approval after confirmed non-execution, but it lacked one persisted-state summary connecting the original ambiguity to the successful replacement.

- Added `app/record_journey.py`, a shared presentation/query boundary for readable event copy, bounded cross-record history, pagination, related approvals, and state-derived next-action guidance. `app/main.py` no longer owns that audit assembly, and `app/scenario_view.py` reuses the same event language.
- Replaced raw record timelines with an **Operational journey** that preserves queue search, filters, pagination, and record links. Audit JSON remains expandable evidence, while the primary view explains what happened and what the operator can do next.
- Added a reviewer result derived from persisted approvals, executions, and reconciliation records. A completed recovery explicitly retains the original ambiguous execution and operator assertion, identifies the fresh approval revision, and describes its separate successful local mock execution.

Verification performed for this change:

- **Local behavior and workflow validation (rerun 29 October):** `python -m scripts.verify local` passed **97 tests** and validated all three workflow exports. New regressions cover a missing execution record behind a succeeded status and conflicting later evidence after a fresh approval completes.
- **PostgreSQL checks (29 October):** used a disposable temporary PostgreSQL 16 cluster and isolated schemas. `scripts.verify_migrations` passed for both a fresh schema and an upgrade with existing reconciliation records; `scripts.verify_postgres` passed the concurrent webhook, worker, decision, refresh, retry, reconciliation, and blocker-release scenarios. The repository's configured database and its data were not used.
- **Synthetic browser review (29 October):** inspected the authenticated approval detail at 1440×900 and 390×844. The 390 px document had no horizontal overflow; the skip link received visible 2 px keyboard focus. Current synthetic captures are `docs/screenshots/operational-journey-desktop.jpg` and `docs/screenshots/operational-journey-mobile.jpg`.
- **Real local n8n demonstration:** `python -m scripts.verify demo` reported **unavailable** because the Docker daemon was not reachable. Export validation is not claimed as workflow execution evidence.

## Previous reviewer experience and verification — 27 October 2026

Inspection of `app/main.py` found that the isolated scenario route mixed database recovery interpretation with page assembly. `app/templates/demo_scenario.html` presented raw audit events after several separate record sections; a confirmed non-execution still marked the outcome stage complete even though a new approval was needed. The `/demo` workspace loaded only its latest 50 scenarios and offered no way to reach older runs.

The scenario state and event labels now live in `app/scenario_view.py`. The page shows a chronological, bounded journey with direct links to the saved request, decisions, execution attempts, replacements, and operator conclusions. Confirmed non-execution keeps the outcome stage open until a new approval is created. The preview workspace pages saved scenarios and offers a two-run reviewer path: one successful mock action and one uncertain execution requiring human reconciliation. These changes preserve the existing service transaction and workflow contracts.

Verification performed for this change:

- **Local suite and exports:** `python -m scripts.verify local` passed **95 tests** and validated all three workflow JSON exports.
- **PostgreSQL:** fresh and populated upgrade migration checks and the concurrency verifier passed on a disposable local PostgreSQL cluster. No migration was added in this change.
- **Browser:** exercised the new success and ambiguity paths in an isolated preview, including a saved one-attempt success and an ambiguous attempt followed by an operator conclusion. Inspected the reviewer cards at 1280×813 and 390×844 and checked visible keyboard focus. The new images in `docs/screenshots/reviewer-path-desktop.jpg` and `docs/screenshots/reviewer-path-mobile.jpg` are synthetic preview captures.
- **Live n8n:** `python -m scripts.verify demo` reported that the Docker daemon was unavailable; no real workflow run was claimed.

## Historical assessment and verification — PR #12

After PR #11 merged (`e3fee4e`), review found that `app/services.py` refused new evidence on an original execution once it had a replacement, while `app/templates/detail.html` directed the operator to a replacement that could not reconcile that execution. In `app/preview_scenarios.py`, failure injection was scoped to an approval instead of the journey, and the interactive exhaustion path created attempt records without the usual execution checks. `app/main.py` also ran the preview adapter on a replayed approval when a retry became due. Browser review found an undefined backoff timestamp in `app/templates/demo_scenario.html` and overflow from long reconciliation evidence.

The follow-up implements these acceptance criteria:

- Operators can append evidence to the original ambiguous execution after a replacement exists. Contradictions return the original to the uncertain recovery queue and block further related attempts; an already completed replacement remains recorded. The scenario and its list card point to the unresolved evidence. A previously successful execution directs operators to inspect its record rather than offering unavailable reconciliation.
- A new preview decision invokes its first mock attempt once inside the lifecycle lock. Failure injection is scoped to the journey, and exhaustion takes separate guarded attempts at the stored backoff deadlines before manual recovery. The blocked-action scenario records a decision and demonstrates the adapter guard without creating an execution. Long evidence wraps at mobile widths.
- `python -m scripts.verify` provides explicit local, PostgreSQL, preview, and live-demo targets and reports unavailable Docker separately from a failing check. A shared SQL helper selects related execution keys without loading all revision IDs into Python.

Verification performed for this follow-up:

- **Local behavior and workflow validation:** `python -m scripts.verify local` passed **93 tests** and validated all three workflow exports. Python compilation and `git diff --check` passed.
- **PostgreSQL:** fresh-schema and populated 0007-to-0008 upgrade verification and the concurrency verifier passed on a temporary PostgreSQL 16.15 cluster, which was stopped and removed afterward. The verifier included later evidence racing a replacement execution and concurrent duplicate decision callbacks.
- **Synthetic browser walkthrough:** completed success, rejection, transient retry, exhaustion with real 30- and 60-second backoff, stale-evidence replacement, blocked action, and ambiguity followed by replacement and conflicting later evidence. Direct inspection of the temporary SQLite database confirmed the displayed decisions, attempts, replacement links, and reconciliation history. The exhausted action succeeded on its fourth attempt after an explicit requeue; the blocked and rejected records created no execution.
- **Visual inspection:** captured the later-evidence journey at 1280×813 and 390×844 in `docs/screenshots/scenario-later-evidence-desktop.jpg` and `docs/screenshots/scenario-later-evidence-mobile.jpg`. Browser back, keyboard focus, and a long unbroken evidence string were checked; the mobile document had no horizontal overflow. These are synthetic preview captures.
- **Live n8n:** unavailable because the Docker daemon was not reachable. `python -m scripts.verify demo` and the Compose PostgreSQL target reported the missing prerequisite; the standalone disposable PostgreSQL checks above ran separately. PR #11 CI passed on its merged commit and is historical evidence, not CI for this follow-up.

## Historical assessment and acceptance criteria — PR #10

The branch starts from merged PR #9 (`119bb43`). Inspection of `app/services.py` showed each `ActionExecution` could have only one reconciliation, so an initial `still_unknown` assertion could not be followed after new evidence. `app/templates/detail.html` exposed reconciliation but not a complete revision sequence. `app/preview_scenarios.py` and `app/main.py` made preview examples inspectable, but starting one created pre-resolved records rather than a pending human decision. The scenario page bounded approval/audit histories without pagination, and stale pending scenarios did not surface a replacement action.

Acceptance criteria implemented:

- Reconciliation is append-only and revisioned. The latest definite supported assertion is visible; contradictory claims set `disputed` and block retries/replacements until a later conclusion is appended. An unknown result can be followed when evidence changes.
- Earlier assertions, actors, references, explanations, timestamps, and audit correlation remain preserved. Original ambiguous executions and attempts are not rewritten. A blocking prior approval links directly to its record. When that earlier execution is confirmed not executed, a previously approved blocked action can be requeued only after its own source evidence is rechecked; stale evidence requires a fresh pending approval.
- Each new scenario starts at a pending decision; preview failure injection is scoped to its scenario and replay creates a separate run. Stale, retryable, exhausted, ambiguous, rejected, successful, and blocked-action journeys derive their outcome from saved records.
- Scenario revision, reconciliation, and audit histories are paginated. The current approval and authoritative reconciliation remain visible when browsing older pages.
- Stale pending approvals offer a replacement action only after the operator can inspect reviewed and current evidence.

## Historical verification — PR #10

- **Behavior suite:** `pytest -q` passed: **79 tests**, including follow-up assertions after unknown outcomes, conflicting evidence and later resolution, unchanged ambiguous execution records, interactive scenario outcomes, stale evidence recovery, scenario history pagination with current-state visibility, and blocker links/requeue after confirmed non-execution.
- **Workflow exports:** all three JSON workflow files passed `python -m scripts.validate_workflows`.
- **Migrations:** `python -m scripts.verify_migrations` passed on a disposable local PostgreSQL 16.15 cluster. It applied migrations 0001–0008 to a fresh schema and upgraded populated 0001–0007 data with an existing reconciliation, preserving its historical approval, execution, actor, and first assertion while accepting an appended follow-up.
- **PostgreSQL concurrency:** `python -m scripts.verify_postgres` passed against a randomly named disposable schema on the same isolated PostgreSQL 16.15 cluster. It covers webhook/worker races, stale source locking, execution/retry and refresh/execution races, concurrent replacement requests, identical reconciliation duplicates, concurrent contradictory claims, a later supported resolution, concurrent blocker resolution/requeue ordering, and preservation of the ambiguous execution. The configured application database was not used.
- **Synthetic preview:** `python -m scripts.preview --port 8002` started the disposable SQLite preview. Browser review exercised a fresh pending ambiguous scenario, approval into a persisted mock timeout, an appended confirmed non-execution assertion, and the fresh-approval recovery action. The stale-evidence page showed the changed source message and replacement guidance. No live workflow, provider, integration credential, or user data was involved.
- **Visual inspection:** inspected the current isolated scenario runner and stale-evidence page at the browser's default desktop viewport, then at 390×844. Checked scenario stage wrapping, long message wrapping, form sizing, keyboard focus, and the current reconciliation in the page summary. The captured browser view is included with this work report; the existing repository screenshots remain historical and are not represented as screenshots of this change.
- **Live n8n:** not run because Docker was unavailable (`docker info` failed and no local n8n endpoint was active). Workflow-export validation is not n8n execution evidence. Earlier real-workflow checks listed below are historical and were not repeated.

## Historical follow-up assessment and verification — PR #11

After PR #10 merged (`68857d5`), review found that the scenario revision history in `app/templates/demo_scenario.html` reversed the older/newer pager labels. The approval route in `app/main.py` also attempted preview execution for a replayed approve POST even when `decide_approval` had returned an already rejected approval. That produced an internal error on a stale form submission. The guided stages in `app/main.py` exposed only terse state strings, leaving the trigger, actual human decision, and persisted execution detail hard to follow.

Implemented acceptance criteria:

- Preview approval submissions execute only a currently approved, queued or retryable record. Replaying an approval after rejection redirects safely, leaves the rejection intact, and creates no execution.
- Scenario revision links agree with their page direction, and the current approval remains visible while inspecting older revisions.
- Guided stages show the customer inquiry, linked synthetic order and saved draft, decision action/actor/time, and persisted execution state, attempt count, and detail. Rejected decisions explicitly show that no execution exists. Preview copy states that n8n did not run.

Verification performed for this follow-up:

- **Behavior suite:** `pytest -q --tb=short` passed: **81 tests**, including stale approval replay, both pager directions, and trigger/decision/execution stage detail.
- **Workflow exports:** `python -m scripts.validate_workflows` passed for all three JSON workflow exports.
- **CI on merged baseline:** PR #10's latest unit/workflow and PostgreSQL concurrency checks both passed. This follow-up changes no schema or PostgreSQL lifecycle rules; PostgreSQL concurrency and live n8n were not rerun.
- **Browser inspection:** signed into the disposable preview, created and approved an ambiguous run, and checked the persisted execution and audit history. Captured and visually inspected the current scenario at 1280×813 and 390×844; mobile `scrollWidth` equaled its 390 px viewport. Refreshed `docs/screenshots/scenario-reconciliation-desktop.jpg` and `docs/screenshots/scenario-reconciliation-mobile.jpg`. These images show synthetic preview data only.

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

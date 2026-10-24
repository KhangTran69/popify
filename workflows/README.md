# Workflow exports

The three JSON files have stable workflow IDs and are imported/published by `scripts/n8n-entrypoint.sh` when their content changes. Existing workflows are backed up inside the n8n volume before import. Do not remove IDs on export: importing without an ID can create a duplicate. An inactive legacy workflow may remain in an existing local volume; only the three IDs in these exports are managed.

Each has a one-minute UTC Schedule Trigger and an immediate local Webhook trigger. Both connect to the same HTTP Request worker node. The worker authenticates to FastAPI with `X-N8N-Secret` from the n8n environment. No customer payload is passed through these workflows. Order/support workers process one item per run; the exception worker scans delays and processes at most 20 due approved actions.

- **01:** `POST /api/n8n/process-next` — durable signed order queue, stale-event protection, retries.
- **02:** `POST /api/n8n/triage-next` — durable inquiry queue, matching, grounding, pending approvals.
- **03:** `POST /api/n8n/delayed-exceptions` — idempotent delay tasks plus due mock action execution.

Business failures become explicit database states and visible recovery queues. A successful n8n HTTP execution means the worker endpoint responded; inspect its returned state to distinguish business success, idle polling, and a recorded processing failure. Network/API failures appear in n8n Executions. The application activity card detects missing recent worker calls, not n8n process health.

See the repository README for exact triggers, tests, demonstration, and credential-dependent live checks. To apply changed exports to a running instance, use `docker compose restart n8n`. To edit safely, export changes from the editor to the matching repository JSON before restarting with a changed bundle.

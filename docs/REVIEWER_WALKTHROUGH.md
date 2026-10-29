# PoPify reviewer walkthrough

No Shopify, OpenAI, or outbound-action credentials are needed. The preview and local stack both use synthetic customer data and mock actions. The local operator login is configured in `.env`; follow [README setup](../README.md#setup) before starting Docker.

The provider-neutral action contract is tested with a fake/mock adapter only. Guided preview and ordinary local action outcomes remain simulated; this walkthrough does not call a provider sandbox or external action service.

## Quick review: isolated preview

From the `popify` repository root, install the project dependencies once, then run:

```sh
python -m pip install -r requirements-test.lock && python -m pip install --no-deps -e .
python -m scripts.preview --port 8002
```

Open `http://127.0.0.1:8002` and sign in with `preview` / `preview-only`. Choose **Guided demo**. These scenarios use a disposable SQLite database and do not call n8n or an external classifier.

1. Select **Start success run**. It opens a new run at a pending human decision. Inspect the linked synthetic order and inquiry, including the pre-saved `order_status` category and draft. The preview seeds this triage result; it does not run classification.
2. Open **Review pending decision**, then approve the mock action. Return to the scenario to inspect the saved result and audit history. No external action provider is called.
3. In **Scenario workspace**, start a `rejected` run. That name selects the scenario; it still opens pending. Open **Review pending decision**, then choose **Reject**. The result should say no execution was created.
4. From **Scenario workspace**, start a `retryable` run. Open **Review pending decision** and approve it to create the simulated transient failure. Return to the scenario and note its displayed backoff deadline. Refresh the page after that time; choose **Run one due local mock attempt** when it appears. The page shows the next saved state. **Scenario workspace** also lists a seeded exhausted example for reviewing retry-budget recovery.
5. Choose **Start recovery run** and approve its pending decision to produce a simulated timeout. In the scenario's reconciliation form, select **Confirmed not executed** and enter the demo-only explanation `Synthetic preview; no provider was called` with reference `preview-only`. Append it, choose **Create a fresh pending approval**, open **Review pending decision**, and approve. Follow the links to compare the original ambiguous execution with the new mock result. This synthetic assertion is stored as supplied; it is not provider-verified evidence.

The preview's starting order, inquiry, classification, and draft are generated locally. It demonstrates persisted decisions and recovery states, not inbound webhook handling or live n8n execution. Keep the preview process running while using it; its temporary database is removed when the process exits.

## End-to-end workflow review: local Docker

Complete [README setup](../README.md#setup), start the stack, sign in with the local operator account, and open **Guided demo**. This path runs the actual local n8n workflows over a synthetic order; it does not receive a live Shopify event. The workflow buttons appear only when each step is ready; the page refreshes while work is queued. At each step, use the named button or wait for its scheduled worker.

1. Click **Start guided demo**. The page queues a synthetic Shopify-shaped delivery. In step 01, click **Run order workflow now** or wait for the scheduled worker.
2. After the order appears, step 02 offers **Run exception workflow now**. Click it or wait for the scheduled scan to create the delayed-order review and unlock step 03.
3. Submit the prefilled **Synthetic customer message** with **Queue support inquiry**. Once queued, click **Run triage workflow now** or wait for workflow 02. Open the resulting case to inspect its classification, grounded draft, and pending approval. The default classifier is mock; no OpenAI key is needed.
4. Open the reply approval from step 04. Review its evidence, then choose **Approve mock action** or **Reject**. Rejection records no execution. Approval queues the action.
5. After approval, step 05 offers **Run outcome workflow now** when the action is due. Click it or wait for workflow 03. Inspect the saved mock result and audit timeline. Nothing is sent to a customer and no refund or cancellation is performed.

For a simulated failure and retry, start the isolated preview in a separate terminal and keep it running; use its `retryable` scenario above. Those records are not part of the Docker database. Preview retry deadlines are persisted, so wait for the displayed due time before running an attempt. The Docker route uses real local n8n workflows but mock classification and action behavior; Shopify delivery, OpenAI calls, and external customer or financial actions are not verified by this walkthrough.

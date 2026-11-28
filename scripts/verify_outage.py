"""CI-only local Compose outage and scheduled recovery exercise."""
import argparse
import json
import os
import time
import httpx
from sqlalchemy import select
from app.config import settings
from datetime import timedelta
from app.db import SessionLocal
from app.models import Delivery, Order, Case, Approval, ActionExecution, Audit, utcnow
from app.services import audit, as_utc

KEY = "ci-n8n-outage-recovery"
SUPPORT_MESSAGES = ("Where is my order?", "My item arrived damaged")


def support_keys(key):
    # The separate restored-volume drill already covers its support schedule.
    return [f"{key}-support-{i}" for i in range(len(SUPPORT_MESSAGES))] if key == KEY else []


def enqueue_support(keys):
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
        for key, message in zip(keys, SUPPORT_MESSAGES):
            body = {"message": message, "request_key": key}
            first = client.post("/api/support", json=body, headers=headers)
            assert first.status_code == 202
            replay = client.post("/api/support", json=body, headers=headers)
            assert replay.status_code == 202 and replay.json()["case_id"] == first.json()["case_id"]


def inspect_support(db, keys, *, pending):
    waits = []
    for key in keys:
        rows = list(db.scalars(select(Case).where(Case.request_key == key)))
        assert len(rows) == 1, "Acknowledged support inquiry lost or duplicated"
        case = rows[0]
        approvals = list(db.scalars(select(Approval).where(
            Approval.subject_type == "case", Approval.subject_id == case.id)))
        if pending:
            assert case.status == "queued" and case.attempts == 0 and not approvals
            continue
        if case.status != "needs_review":
            continue
        assert case.attempts == 1 and case.order_id is None
        assert "Verified order reference" in case.missing_info
        assert len(approvals) == 1 and approvals[0].status == "pending"
        assert approvals[0].proposed_action == "send_reply" and approvals[0].execution_status == "not_started"
        assert not db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approvals[0].id}"))
        drafts = list(db.scalars(select(Audit).where(Audit.entity_type == "case",
            Audit.entity_id == case.id, Audit.event == "draft_created")))
        assert len(drafts) == 1
        waits.append(round((as_utc(drafts[0].created_at) - as_utc(case.created_at)).total_seconds(), 3))
    return waits


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("queue", "pending", "recovered"))
    parser.add_argument("--key", default=KEY, choices=(KEY, "ci-n8n-restored-volume"))
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    key = args.key
    keys = support_keys(key)
    if (os.environ.get("POPIFY_ENV", "").lower() != "development" or settings.llm_mode != "mock"
            or settings.shopify_admin_token or settings.openai_api_key):
        parser.error("Only a disposable development stack is supported")
    if args.stage == "queue":
        with SessionLocal() as db:
            if db.scalar(select(Delivery).where(Delivery.webhook_id == key)):
                raise RuntimeError("Fixture already exists; use a fresh disposable stack")
            now = utcnow()
            delivery = Delivery(webhook_id=key, topic="orders/create", shop_domain="demo.myshopify.com",
                payload={"id": key, "name": "#N8N-RESTORE" if key == "ci-n8n-restored-volume" else "#OUTAGE-CHECK",
                         "email": "restore@example.test",
                         "created_at": (now - timedelta(hours=72) if key == "ci-n8n-restored-volume" else now).isoformat(),
                         "updated_at": now.isoformat()}, status="received")
            db.add(delivery)
            db.flush()
            audit(db, "delivery", delivery.id, "synthetic_delivery_received", correlation_id=key)
            db.commit()
        if keys:
            enqueue_support(keys)
        print("PASS: queued durable synthetic records while n8n is stopped")
        return
    deadline = time.monotonic() + 150
    while True:
        with SessionLocal() as db:
            delivery = db.scalar(select(Delivery).where(Delivery.webhook_id == key))
            assert delivery, "Durable delivery was lost"
            orders = list(db.scalars(select(Order).where(Order.external_id == key)))
            if args.stage == "pending":
                assert delivery.status == "received" and not orders
                inspect_support(db, keys, pending=True)
                print("PASS: order and acknowledged support records survived API restart while n8n remained stopped")
                return
            support_waits = inspect_support(db, keys, pending=False)
            if delivery.status == "processed" and len(support_waits) == len(keys):
                assert len(orders) == 1 and delivery.processed_at
                print(json.dumps({"scheduled_recovery": "passed", "orders": len(orders),
                    "support_cases": len(support_waits), "support_receipt_to_draft_seconds_including_outage": support_waits,
                    "receipt_to_persistence_seconds_including_outage":
                    round((as_utc(delivery.processed_at) - as_utc(delivery.received_at)).total_seconds(), 3)}))
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Scheduled n8n worker did not recover the queued record in time")
        time.sleep(2)


if __name__ == "__main__":
    run()

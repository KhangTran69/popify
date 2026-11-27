"""Bounded, low-rate scheduled-worker measurement in disposable Compose only.

Uses real HTTP, signed synthetic intake and the actual n8n schedule. Never
invokes targeted worker endpoints, changes schedules, or approves actions.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import ActionExecution, Approval, Audit, Case, Delivery, Order, utcnow
from app.services import as_utc
from scripts.benchmark import percentile_ms

PROFILES = {"steady": (12, 25, 540), "overload": (18, 10, 540)}


def signed_order(client, key, *, delayed=False):
    now = utcnow()
    payload = {"id": key, "name": "#" + key, "email": "scheduled@example.test",
               "created_at": (now - timedelta(hours=72) if delayed else now).isoformat(),
               "updated_at": now.isoformat(), "financial_status": "paid"}
    raw = json.dumps(payload).encode()
    headers = {"X-Shopify-Hmac-SHA256": base64.b64encode(hmac.new(
        settings.shopify_webhook_secret.encode(), raw, hashlib.sha256).digest()).decode(),
        "X-Shopify-Webhook-Id": key, "X-Shopify-Shop-Domain": "demo.myshopify.com",
        "X-Shopify-Topic": "orders/create"}
    started = time.monotonic()
    response = client.post("/webhooks/shopify/orders", content=raw, headers=headers)
    latency = (time.monotonic() - started) * 1000
    assert response.status_code == 202, response.status_code
    replay = client.post("/webhooks/shopify/orders", content=raw, headers=headers)
    assert replay.status_code == 200 and replay.json()["duplicate"]
    return latency


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    parser.add_argument("--profile", choices=PROFILES, default="steady")
    args = parser.parse_args()
    count, interval, deadline = PROFILES[args.profile]
    assert os.environ.get("POPIFY_ENV") == "development" and settings.llm_mode == "mock"
    assert settings.shopify_webhook_secret and not settings.shopify_admin_token and not settings.openai_api_key
    prefix = "scheduled-" + uuid.uuid4().hex[:12]
    keys = [f"{prefix}-{i}" for i in range(count)]
    start = time.monotonic()
    submitted, inquiries, order_wait, support_wait, ack = set(), set(), {}, {}, []
    exception_seen = False
    peak_orders = peak_support = 0
    end_to_end = {}
    receipts = {}
    last_submission = None
    approval_ids = set()
    backlog_trace = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        while time.monotonic() - start < deadline:
            elapsed = time.monotonic() - start
            for i, key in enumerate(keys):
                if key not in submitted and elapsed >= i * interval:
                    ack.append(signed_order(client, key, delayed=(i == 0)))
                    submitted.add(key)
                    last_submission = time.monotonic()
            ready_for_support = []
            with SessionLocal() as db:
                for key in submitted:
                    delivery = db.scalar(select(Delivery).where(Delivery.webhook_id == key))
                    assert delivery is not None, "Acknowledged delivery missing"
                    receipts[key] = as_utc(delivery.received_at)
                    if delivery.status == "processed":
                        assert delivery.processed_at is not None
                        orders = list(db.scalars(select(Order).where(Order.external_id == key)))
                        assert len(orders) == 1
                        order_wait[key] = (as_utc(delivery.processed_at) - as_utc(delivery.received_at)).total_seconds()
                        if key not in inquiries:
                            ready_for_support.append(key)
                        if key == keys[0]:
                            exceptions = list(db.scalars(select(Approval).where(
                                Approval.subject_type == "order", Approval.subject_id == orders[0].id,
                                Approval.proposed_action == "review_delayed_order")))
                            assert len(exceptions) <= 1
                            if exceptions:
                                assert exceptions[0].status == "pending"
                                exception_seen = True
                                approval_ids.add(exceptions[0].id)
                    if key in inquiries:
                        case = db.scalar(select(Case).where(Case.request_key == key))
                        assert case is not None
                        if case.status == "needs_review":
                            assert case.order_id == orders[0].id, "Support did not match the exact persisted order"
                            event = db.scalar(select(Audit).where(Audit.entity_type == "case",
                                Audit.entity_id == case.id, Audit.event == "draft_created").order_by(Audit.id).limit(1))
                            assert event is not None
                            approvals = list(db.scalars(select(Approval).where(Approval.subject_type == "case",
                                Approval.subject_id == case.id, Approval.proposed_action == "send_reply")))
                            assert len(approvals) == 1 and approvals[0].status == "pending"
                            approval_ids.add(approvals[0].id)
                            support_wait[key] = (as_utc(event.created_at) - as_utc(case.created_at)).total_seconds()
                            end_to_end[key] = (as_utc(event.created_at) - receipts[key]).total_seconds()
            for key in ready_for_support:
                body = {"message": "Where is my order?", "order_ref": "#" + key,
                        "email": "scheduled@example.test", "request_key": key}
                headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
                response = client.post("/api/support", json=body, headers=headers)
                assert response.status_code == 202, response.status_code
                replay = client.post("/api/support", json=body, headers=headers)
                assert replay.status_code == 202 and replay.json()["case_id"] == response.json()["case_id"]
                inquiries.add(key)
            peak_orders = max(peak_orders, len(submitted) - len(order_wait))
            peak_support = max(peak_support, len(inquiries) - len(support_wait))
            backlog_trace.append((round(elapsed, 1), len(submitted)-len(order_wait), len(inquiries)-len(support_wait)))
            if len(order_wait) == count and len(support_wait) == count and exception_seen:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"Scheduled drain exceeded {deadline}s: orders={len(order_wait)}, support={len(support_wait)}, delayed_exception={exception_seen}")
    elapsed = time.monotonic() - start
    with SessionLocal() as db:
        assert len(approval_ids) == count + 1
        assert not list(db.scalars(select(ActionExecution).where(ActionExecution.idempotency_key.in_([f"approval:{i}" for i in approval_ids]))))
        assert all(db.get(Approval, i).status == "pending" for i in approval_ids)
    def timing(values):
        return {"samples": len(values), "p50_seconds": percentile_ms(list(values), .5),
                "p95_seconds": percentile_ms(list(values), .95), "max_seconds": round(max(values), 3)}
    print("SCHEDULED WORKLOAD " + json.dumps({
        "profile": args.profile, "orders": count, "support_cases": count, "arrival_interval_seconds": interval,
        "elapsed_seconds": round(elapsed, 3), "combined_completions_per_minute": round(2*count*60/elapsed, 3),
        "network_ack_ms": {"p50": percentile_ms(ack, .5), "p95": percentile_ms(ack, .95)},
        "order_queue": timing(order_wait.values()), "support_queue": timing(support_wait.values()),
        "end_to_end": timing(end_to_end.values()), "peak_observed_order_backlog": peak_orders,
        "peak_observed_support_backlog": peak_support, "delayed_exception_pending": exception_seen,
        "drain_seconds_after_last_order_submission": round(time.monotonic()-last_submission, 3),
        "final_backlog": {"orders": backlog_trace[-1][1], "support": backlog_trace[-1][2]},
        "backlog_samples": backlog_trace[::15] + [backlog_trace[-1]],
        "scope": "Synthetic paired items, actual HTTP and n8n; not merchant capacity or SLA"
    }))
    if args.profile == "steady":
        assert percentile_ms(list(order_wait.values()), .95) <= 120, "Steady order queue p95 exceeded 120 seconds"
        assert percentile_ms(list(support_wait.values()), .95) <= 120, "Steady support queue p95 exceeded 120 seconds"
    else:
        # Arrival rate exceeds the nominal three-per-minute service rate.
        # Require visible backlog growth and bounded drainage, not a steady-load SLA.
        assert peak_orders > 3, "Overload profile did not exercise backlog growth"
        assert backlog_trace[-1][1:] == (0, 0)
        assert elapsed <= deadline
    assert percentile_ms(ack, .95) <= 500, "Synthetic HTTP acknowledgement p95 exceeded 500 ms"


if __name__ == "__main__":
    run()

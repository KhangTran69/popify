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

# orders, order interval, deadline, independent support count and interval.
PROFILES = {
    "steady": (12, 25, 540, 0, None),
    "overload": (18, 10, 540, 0, None),
    "mixed": (12, 15, 660, 24, 8),
    "sustained": (24, 20, 900, 36, 15),
}


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


def submit_support(client, key, *, paired):
    body = {"message": "Where is my order?" if paired else "My item arrived damaged",
            "order_ref": "#" + key if paired else None,
            "email": "scheduled@example.test", "request_key": key}
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    started = time.monotonic()
    response = client.post("/api/support", json=body, headers=headers)
    latency = (time.monotonic() - started) * 1000
    assert response.status_code == 202, response.status_code
    replay = client.post("/api/support", json=body, headers=headers)
    assert replay.status_code == 202 and replay.json()["case_id"] == response.json()["case_id"]
    if not paired:
        conflict = client.post("/api/support", json={**body, "message": "Changed inquiry"}, headers=headers)
        assert conflict.status_code == 409
    return latency


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    parser.add_argument("--profile", choices=PROFILES, default="steady")
    args = parser.parse_args()
    count, interval, deadline, independent_count, independent_interval = PROFILES[args.profile]
    assert os.environ.get("POPIFY_ENV") == "development" and settings.llm_mode == "mock"
    assert settings.shopify_webhook_secret and not settings.shopify_admin_token and not settings.openai_api_key
    prefix = "scheduled-" + uuid.uuid4().hex[:12]
    keys = [f"{prefix}-{i}" for i in range(count)]
    independent_keys = [f"{prefix}-independent-{i}" for i in range(independent_count)]
    independent_submitted, independent_wait = set(), {}
    support_ack = []
    last_support_submission = None
    start = time.monotonic()
    started_at = utcnow()
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
            for i, key in enumerate(independent_keys):
                if key not in independent_submitted and elapsed >= i * independent_interval:
                    support_ack.append(submit_support(client, key, paired=False))
                    independent_submitted.add(key)
                    last_support_submission = time.monotonic()
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
                for key in independent_submitted:
                    case = db.scalar(select(Case).where(Case.request_key == key))
                    assert case is not None
                    if case.status == "needs_review":
                        assert case.order_id is None and "Verified order reference" in case.missing_info
                        assert case.message == "My item arrived damaged" and case.attempts == 1
                        events = list(db.scalars(select(Audit).where(Audit.entity_type == "case",
                            Audit.entity_id == case.id, Audit.event == "draft_created")))
                        assert len(events) == 1
                        approvals = list(db.scalars(select(Approval).where(Approval.subject_type == "case",
                            Approval.subject_id == case.id)))
                        assert len(approvals) == 1 and approvals[0].proposed_action == "send_reply"
                        assert approvals[0].status == "pending"
                        approval_ids.add(approvals[0].id)
                        independent_wait[key] = (as_utc(events[0].created_at) - as_utc(case.created_at)).total_seconds()
            for key in ready_for_support:
                support_ack.append(submit_support(client, key, paired=True))
                inquiries.add(key)
                last_support_submission = time.monotonic()
            pending_support = len(inquiries) + len(independent_submitted) - len(support_wait) - len(independent_wait)
            peak_orders = max(peak_orders, len(submitted) - len(order_wait))
            peak_support = max(peak_support, pending_support)
            backlog_trace.append((round(elapsed, 1), len(submitted)-len(order_wait), pending_support))
            if len(order_wait) == count and len(support_wait) == count and len(independent_wait) == len(independent_keys) and exception_seen:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"Scheduled drain exceeded {deadline}s: orders={len(order_wait)}, paired_support={len(support_wait)}, independent_support={len(independent_wait)}, delayed_exception={exception_seen}")
    elapsed = time.monotonic() - start
    poll_metrics = {}
    with SessionLocal() as db:
        deliveries = list(db.scalars(select(Delivery).where(Delivery.webhook_id.in_(keys))))
        cases = list(db.scalars(select(Case).where(Case.request_key.in_(keys + independent_keys))))
        assert len(deliveries) == count and all(d.status == "processed" and d.attempts == 1 for d in deliveries)
        assert len(cases) == count + len(independent_keys) and all(c.status == "needs_review" and c.attempts == 1 for c in cases)
        for case in cases:
            events = list(db.scalars(select(Audit.id).where(Audit.entity_type == "case", Audit.entity_id == case.id,
                                                          Audit.event == "draft_created")))
            assert len(events) == 1
        # Aggregate heartbeat rows distinguish one poll from its per-item records.
        # Observe the actual cadence and outcomes, not just configured intervals.
        for number, name in ((1, "orders"), (2, "support")):
            rows = list(db.scalars(select(Audit).where(Audit.entity_type == "workflow",
                Audit.entity_id == number, Audit.created_at >= started_at).order_by(Audit.created_at, Audit.id)))
            polls = [row for row in rows if isinstance(row.details, dict) and "batch_size" in row.details
                     and isinstance(row.details.get("outcomes"), dict)]
            assert polls and all(row.details["batch_size"] == 5 for row in polls)
            gaps = [(as_utc(right.created_at) - as_utc(left.created_at)).total_seconds()
                    for left, right in zip(polls, polls[1:])]
            outcomes = {}
            for row in polls:
                for status, value in row.details["outcomes"].items():
                    outcomes[status] = outcomes.get(status, 0) + value
            poll_metrics[name] = {"observed_polls": len(polls), "max_interpoll_seconds": round(max(gaps), 3) if gaps else None,
                                  "median_interpoll_seconds": percentile_ms(gaps, .5) if gaps else None,
                                  "outcomes": outcomes}
        assert len(approval_ids) == count + len(independent_keys) + 1
        assert not list(db.scalars(select(ActionExecution).where(ActionExecution.idempotency_key.in_([f"approval:{i}" for i in approval_ids]))))
        assert all(db.get(Approval, i).status == "pending" for i in approval_ids)
    def timing(values):
        return {"samples": len(values), "p50_seconds": percentile_ms(list(values), .5),
                "p95_seconds": percentile_ms(list(values), .95), "max_seconds": round(max(values), 3)}
    print("SCHEDULED WORKLOAD " + json.dumps({
        "profile": args.profile, "orders": count, "support_cases": count + len(independent_keys), "arrival_interval_seconds": interval,
        "independent_support_cases": len(independent_keys),
        "independent_support_interval_seconds": independent_interval,
        "elapsed_seconds": round(elapsed, 3), "combined_completions_per_minute": round((2*count+len(independent_keys))*60/elapsed, 3),
        "network_ack_ms": {"p50": percentile_ms(ack, .5), "p95": percentile_ms(ack, .95)},
        "support_ack_ms": {"p50": percentile_ms(support_ack, .5), "p95": percentile_ms(support_ack, .95)},
        "order_queue": timing(order_wait.values()), "support_queue": timing(list(support_wait.values()) + list(independent_wait.values())),
        "paired_support_queue": timing(support_wait.values()),
        "independent_support_queue": timing(independent_wait.values()) if independent_wait else None,
        "end_to_end": timing(end_to_end.values()), "peak_observed_order_backlog": peak_orders,
        "peak_observed_support_backlog": peak_support, "delayed_exception_pending": exception_seen,
        "drain_seconds_after_last_order_submission": round(time.monotonic()-last_submission, 3),
        "drain_seconds_after_last_arrival": round(time.monotonic()-max(last_submission, last_support_submission), 3),
        "final_backlog": {"orders": backlog_trace[-1][1], "support": backlog_trace[-1][2]},
        "backlog_samples": backlog_trace[::15] + [backlog_trace[-1]],
        "worker_polls": poll_metrics,
        "delivery_attempts": {"records": count, "retried": 0},
        "support_attempts": {"records": count + len(independent_keys), "retried": 0},
        "scope": "Synthetic items, actual HTTP and n8n; not merchant capacity or SLA"
    }))
    if args.profile == "steady":
        assert percentile_ms(list(order_wait.values()), .95) <= 120, "Steady order queue p95 exceeded 120 seconds"
        assert percentile_ms(list(support_wait.values()), .95) <= 120, "Steady support queue p95 exceeded 120 seconds"
    elif args.profile == "overload":
        # Arrival rate exceeds the nominal five-per-minute service rate.
        # Require visible backlog growth and bounded drainage, not a steady-load SLA.
        assert peak_orders > 3, "Overload profile did not exercise backlog growth"
        assert backlog_trace[-1][1:] == (0, 0)
        assert elapsed <= deadline
        assert percentile_ms(list(order_wait.values()), .95) <= 180, "Overload order p95 exceeded provisional 180-second regression gate"
    elif args.profile == "mixed":
        assert backlog_trace[-1][1:] == (0, 0)
        assert elapsed <= deadline
        # Measured before the added mock-only poll: support p95 226.038s.
        # Require an improvement, not a minimum backlog (which rewards slowness).
        assert percentile_ms(list(support_wait.values()) + list(independent_wait.values()), .95) <= 180, "Mixed support p95 exceeded provisional regression gate"
    else:
        assert backlog_trace[-1][1:] == (0, 0)
        assert elapsed <= deadline
        assert percentile_ms(list(order_wait.values()), .95) <= 120, "Sustained order p95 exceeded 120 seconds"
        assert percentile_ms(list(support_wait.values()) + list(independent_wait.values()), .95) <= 120, "Sustained support p95 exceeded 120 seconds"
    assert percentile_ms(ack, .95) <= 500, "Synthetic HTTP acknowledgement p95 exceeded 500 ms"
    assert percentile_ms(support_ack, .95) <= 500, "Synthetic support acknowledgement p95 exceeded 500 ms"


if __name__ == "__main__":
    run()

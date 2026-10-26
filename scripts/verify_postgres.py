"""Exercise concurrent behavior in an isolated, temporary PostgreSQL schema."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import hashlib
import hmac
import json
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from app import main
from app.config import settings
from app.db import Base, engine, get_db
from app.models import Order, Delivery, Case, Approval, ActionExecution, utcnow
from app.services import (
    process_delivery,
    create_approval,
    decide_approval,
    execute_approved,
    approval_freshness_differences,
    queue_support,
    SupportRequestKeyConflict,
    refresh_approval,
)
import app.services as services


def run():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Run inside the Compose API container")
    schema = "verify_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = create_engine(
        settings.database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    sessions = sessionmaker(isolated, expire_on_commit=False)
    old_secret = settings.shopify_webhook_secret
    try:
        Base.metadata.create_all(isolated)

        def session_override():
            with sessions() as db:
                yield db

        main.app.dependency_overrides[get_db] = session_override
        settings.shopify_webhook_secret = "isolated-test-secret"
        raw = json.dumps(
            {
                "id": "concurrent",
                "name": "#CONCURRENT",
                "updated_at": utcnow().isoformat(),
            }
        ).encode()
        headers = {
            "X-Shopify-Hmac-SHA256": base64.b64encode(
                hmac.new(
                    settings.shopify_webhook_secret.encode(), raw, hashlib.sha256
                ).digest()
            ).decode(),
            "X-Shopify-Webhook-Id": "same-delivery",
            "X-Shopify-Shop-Domain": settings.shopify_store_domain
            or "demo.myshopify.com",
        }

        def receive(_):
            with TestClient(main.app) as client:
                return client.post(
                    "/webhooks/shopify/orders", content=raw, headers=headers
                ).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(receive, range(6)))
        assert statuses.count(202) == 1 and statuses.count(200) == 5, statuses
        with sessions() as db:
            assert db.scalar(select(func.count(Delivery.id))) == 1
            delivery_id = db.scalar(select(Delivery.id))

        def process(_):
            with sessions() as db:
                return process_delivery(db, delivery_id)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(process, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(Order.id))) == 1
            now = utcnow()
            ids = []
            for i in range(6):
                d = Delivery(
                    webhook_id=f"ordered-{i}",
                    topic="orders/updated",
                    shop_domain="demo.myshopify.com",
                    payload={
                        "id": "first-insert-race",
                        "name": "#RACE",
                        "updated_at": (now + timedelta(seconds=i)).isoformat(),
                        "total_price": str(i),
                    },
                )
                db.add(d)
                db.flush()
                ids.append(d.id)
            db.commit()

        def process_id(delivery):
            with sessions() as db:
                return process_delivery(db, delivery)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(process_id, ids[::-1]))
        with sessions() as db:
            row = db.scalar(
                select(Order).where(Order.external_id == "first-insert-race")
            )
            assert row.total == "5"

        def submit_support(message):
            with sessions() as db:
                try:
                    case = queue_support(
                        db,
                        message,
                        "#CONFLICT",
                        "conflict@example.test",
                        "same-support-key",
                    )
                    return ("queued", case.id)
                except SupportRequestKeyConflict:
                    return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            support_results = list(
                pool.map(submit_support, ["Where is my order?", "Refund this order."])
            )
        assert sorted(result[0] for result in support_results) == [
            "conflict",
            "queued",
        ], support_results
        with sessions() as db:
            assert db.scalar(
                select(func.count(Case.id)).where(
                    Case.request_key == "same-support-key"
                )
            ) == 1
            # Approval decisions validate current order state and delay eligibility.
            delayed_order = db.get(Order, row.id)
            delayed_order.created_at = utcnow() - timedelta(hours=settings.order_delay_hours + 1)
            delayed_order.fulfillment_status = "unfulfilled"
            db.flush()
            a = create_approval(
                db,
                "exception",
                "order",
                delayed_order.id,
                "review_delayed_order",
                {
                    "order_number": delayed_order.order_number,
                    "order_id": delayed_order.id,
                    "hours_threshold": settings.order_delay_hours,
                    "created_at": delayed_order.created_at.isoformat(),
                    "order_fulfillment_status": delayed_order.fulfillment_status,
                    "order_financial_status": delayed_order.financial_status,
                    "order_total": f"{delayed_order.currency} {delayed_order.total}",
                    "order_source_updated_at": delayed_order.source_updated_at.isoformat(),
                },
            )
            differences = approval_freshness_differences(db, a)
            assert not differences, f"PostgreSQL approval fixture is stale: {differences}"
            db.commit()
            aid = a.id

        def approve(_):
            with sessions() as db:
                decide_approval(db, aid, "approved", "test-operator")

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(approve, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(ActionExecution.id))) == 0

        # Conflicting operator clicks serialize on the subject lifecycle; only
        # the first committed decision is authoritative.
        with sessions() as db:
            conflict_order = Order(
                shop_domain="demo.myshopify.com", external_id="decision-conflict",
                order_number="#DECISION-RACE", email="race@example.test",
                currency="USD", total="20.00", fulfillment_status="unfulfilled",
                financial_status="paid",
                created_at=utcnow() - timedelta(hours=settings.order_delay_hours + 2),
                source_updated_at=utcnow(), payload={},
            )
            db.add(conflict_order)
            db.flush()
            conflict = create_approval(
                db, "exception", "order", conflict_order.id, "review_delayed_order",
                {
                    "order_number": conflict_order.order_number, "order_id": conflict_order.id,
                    "hours_threshold": settings.order_delay_hours,
                    "created_at": conflict_order.created_at.isoformat(),
                    "order_fulfillment_status": conflict_order.fulfillment_status,
                    "order_financial_status": conflict_order.financial_status,
                    "order_total": "USD 20.00",
                    "order_source_updated_at": conflict_order.source_updated_at.isoformat(),
                },
            )
            db.commit()
            conflict_id = conflict.id

        def conflicting_decision(decision):
            with sessions() as db:
                return decide_approval(db, conflict_id, decision, decision).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            decision_results = list(pool.map(conflicting_decision, ["approved", "rejected"]))
        assert len(set(decision_results)) == 1, decision_results
        with sessions() as db:
            decided = db.get(Approval, conflict_id)
            assert decided.status in ("approved", "rejected") and decided.actor == decided.status

        # Hold the execution transaction immediately after it reads its locked
        # evidence. A concurrent order update must wait until the mock action
        # commits, then the next attempt must see that update as stale.
        freshness_checked = Event()
        release_execution = Event()
        source_update_started = Event()
        source_update_finished = Event()
        original_freshness = services.approval_freshness_differences

        def pause_after_freshness(db, approval):
            result = original_freshness(db, approval)
            if approval.id == aid:
                freshness_checked.set()
                if not release_execution.wait(10):
                    raise TimeoutError("concurrency verifier timed out waiting to release execution")
            return result

        services.approval_freshness_differences = pause_after_freshness

        def execute_once():
            with sessions() as db:
                execute_approved(db, db.get(Approval, aid))
                db.commit()

        def update_order():
            source_update_started.set()
            with sessions() as db:
                locked_order = db.scalar(
                    select(Order).where(Order.id == row.id).with_for_update()
                )
                locked_order.fulfillment_status = "fulfilled"
                locked_order.source_updated_at = utcnow() + timedelta(seconds=20)
                db.commit()
            source_update_finished.set()

        execution_thread = ThreadPoolExecutor(max_workers=2)
        execution_future = execution_thread.submit(execute_once)
        try:
            assert freshness_checked.wait(5), "execution did not reach freshness validation"
            update_future = execution_thread.submit(update_order)
            assert source_update_started.wait(2)
            assert not source_update_finished.wait(0.2), "source update bypassed the evidence lock"
            release_execution.set()
            execution_future.result(timeout=5)
            update_future.result(timeout=5)
        finally:
            release_execution.set()
            services.approval_freshness_differences = original_freshness
            execution_thread.shutdown(wait=True)
        assert source_update_finished.is_set()

        # Competing refresh clicks on one stale approval return one revision.
        with sessions() as db:
            case = Case(
                message="Where is my order?", category="order_status", confidence=80,
                draft="Old snapshot draft", status="needs_review",
                correlation_id="concurrent-refresh",
            )
            db.add(case)
            db.flush()
            refresh_source = case
            refresh_approval_row = create_approval(
                db,
                "support",
                "case",
                case.id,
                "send_reply",
                {
                    "customer_message": case.message,
                    "customer_email": None,
                    "case_order_ref": None,
                    "order_number": None,
                    "order_id": None,
                    "order_email": None,
                    "policy": settings.store_policies[case.category],
                    "order_fulfillment_status": None,
                    "order_financial_status": None,
                    "order_total": None,
                    "order_source_updated_at": None,
                    "category": case.category,
                    "confidence": case.confidence,
                    "draft": case.draft,
                    "missing_info": "Verified order reference",
                    "case_status": case.status,
                    "correlation_id": case.correlation_id,
                },
            )
            case.draft = "Current response draft"
            db.commit()
            refresh_id = refresh_approval_row.id

        def refresh_once(_):
            with sessions() as db:
                return refresh_approval(db, refresh_id, "test-operator").id

        with ThreadPoolExecutor(max_workers=6) as pool:
            replacement_ids = list(pool.map(refresh_once, range(6)))
        assert len(set(replacement_ids)) == 1, replacement_ids
        with sessions() as db:
            assert db.scalar(
                select(func.count(Approval.id)).where(
                    Approval.subject_id == refresh_source.id,
                    Approval.subject_type == "case",
                )
            ) == 2

        def execute(_):
            with sessions() as db:
                execute_approved(db, db.get(Approval, aid))
                db.commit()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(execute, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(ActionExecution.id))) == 1
            assert db.scalar(select(ActionExecution.attempts)) == 1
        print(
            "PASS: concurrent webhook duplicates, duplicate workers, first-insert/out-of-order races, conflicting support request-key reuse, repeated and conflicting approval decisions, source freshness locking, concurrent replacement requests, and repeated execution (6 workers per repeated scenario)."
        )
    finally:
        settings.shopify_webhook_secret = old_secret
        main.app.dependency_overrides.pop(get_db, None)
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


if __name__ == "__main__":
    run()

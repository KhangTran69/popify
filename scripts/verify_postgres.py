"""Exercise concurrent behavior in an isolated, temporary PostgreSQL schema."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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
)


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
            "PASS: concurrent webhook duplicates, duplicate workers, first-insert/out-of-order races, conflicting support request-key reuse, repeated approval, repeated execution (6 workers per scenario)."
        )
    finally:
        settings.shopify_webhook_secret = old_secret
        main.app.dependency_overrides.pop(get_db, None)
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


if __name__ == "__main__":
    run()

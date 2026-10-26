"""Serve PoPify with disposable synthetic data and no Docker dependency."""

import argparse
import os
import tempfile
from datetime import timedelta


def run():
    parser = argparse.ArgumentParser(
        description="Run an isolated PoPify UI preview with synthetic records."
    )
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    with tempfile.TemporaryDirectory(prefix="popify-preview-") as directory:
        os.environ["DATABASE_URL"] = f"sqlite:///{directory}/preview.db"
        os.environ["OPERATOR_USERNAME"] = "preview"
        os.environ["OPERATOR_PASSWORD"] = "preview-only"
        os.environ["APP_SECRET"] = "preview-session-secret"
        os.environ["MOCK_ACTION_FAILURES"] = "0"
        os.environ["MOCK_ACTION_AMBIGUOUS"] = "false"

        import uvicorn
        from app.db import Base, SessionLocal, engine
        from app.main import app
        from app.config import settings
        from app.models import Case, Delivery, Order, utcnow
        from app.services import create_approval, decide_approval, execute_approved

        Base.metadata.create_all(engine)
        now = utcnow()
        with SessionLocal() as db:
            order = Order(
                shop_domain="demo.myshopify.com",
                external_id="preview-order",
                order_number="#PREVIEW-1001",
                email="synthetic@example.test",
                currency="USD",
                total="84.00",
                fulfillment_status="unfulfilled",
                financial_status="paid",
                created_at=now - timedelta(hours=72),
                source_updated_at=now - timedelta(minutes=5),
                payload={},
            )
            db.add(order)
            db.flush()
            case = Case(
                order_id=order.id,
                order_ref=order.order_number,
                customer_email=order.email,
                message="Where is my order? It has been more than three days.",
                category="order_status",
                confidence=80,
                draft="Thanks for contacting us. The recorded order status is unfulfilled.",
                status="needs_review",
            )
            db.add(case)
            db.flush()
            create_approval(
                db,
                "support",
                "case",
                case.id,
                "send_reply",
                {
                    "order_number": order.order_number,
                    "customer_message": case.message,
                    "policy": "Only share the recorded status; do not infer a delivery date.",
                    "order_fulfillment_status": order.fulfillment_status,
                    "order_total": "USD 84.00",
                    "category": case.category,
                    "confidence": case.confidence,
                    "draft": case.draft,
                    "missing_info": "",
                    "correlation_id": "preview:reply",
                },
            )
            create_approval(
                db,
                "exception",
                "order",
                order.id,
                "review_delayed_order",
                {
                    "order_number": order.order_number,
                    "hours_threshold": 48,
                    "created_at": order.created_at.isoformat(),
                    "order_fulfillment_status": order.fulfillment_status,
                    "order_total": "USD 84.00",
                    "correlation_id": "preview:delay",
                },
            )
            def scenario_case(label):
                record = Case(
                    order_id=order.id,
                    order_ref=order.order_number,
                    customer_email=order.email,
                    message=f"Synthetic {label} support request",
                    category="order_status",
                    confidence=80,
                    draft=f"Synthetic {label} draft response.",
                    status="needs_review",
                    correlation_id=f"preview:{label}",
                )
                db.add(record)
                db.flush()
                return record

            def scenario_approval(label):
                record = scenario_case(label)
                return create_approval(
                    db,
                    "support",
                    "case",
                    record.id,
                    "send_reply",
                    {
                        "order_number": order.order_number,
                        "customer_message": record.message,
                        "policy": "Synthetic preview policy.",
                        "order_fulfillment_status": order.fulfillment_status,
                        "order_total": "USD 84.00",
                        "category": record.category,
                        "confidence": record.confidence,
                        "draft": record.draft,
                        "missing_info": "",
                        "correlation_id": record.correlation_id,
                    },
                )

            completed = scenario_approval("completed")
            decide_approval(db, completed.id, "approved", "preview")
            execute_approved(db, completed)
            db.commit()

            rejected = scenario_approval("rejected")
            decide_approval(db, rejected.id, "rejected", "preview")

            retryable = scenario_approval("retryable")
            decide_approval(db, retryable.id, "approved", "preview")
            settings.mock_action_failures = 1
            execute_approved(db, retryable)
            db.commit()
            settings.mock_action_failures = 0

            ambiguous = scenario_approval("ambiguous")
            decide_approval(db, ambiguous.id, "approved", "preview")
            settings.mock_action_ambiguous = True
            execute_approved(db, ambiguous)
            db.commit()
            settings.mock_action_ambiguous = False

            exhausted = scenario_approval("exhausted")
            decide_approval(db, exhausted.id, "approved", "preview")
            original_max_attempts = settings.max_action_attempts
            settings.max_action_attempts = 1
            settings.mock_action_failures = 1
            execute_approved(db, exhausted)
            db.commit()
            settings.mock_action_failures = 0
            settings.max_action_attempts = original_max_attempts

            db.add(
                Delivery(
                    webhook_id="preview-exhausted-delivery",
                    topic="orders/create",
                    shop_domain="demo.myshopify.com",
                    payload={},
                    status="exhausted",
                    attempts=3,
                    error="Synthetic exhausted delivery for interface preview.",
                    received_at=now - timedelta(minutes=20),
                )
            )
            db.add(
                Case(
                    message="Synthetic support failure",
                    status="failed",
                    attempts=1,
                    failure_detail="Synthetic retryable triage failure.",
                    next_attempt_at=now + timedelta(minutes=5),
                )
            )
            db.commit()

        print("PoPify isolated preview: http://127.0.0.1:%s" % args.port)
        print("Sign in with preview / preview-only")
        print("The temporary database is deleted when this process stops.")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    run()

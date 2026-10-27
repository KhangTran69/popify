"""Serve PoPify with disposable synthetic data and no external integrations."""

import argparse
import os
import tempfile
from datetime import timedelta


def run():
    parser = argparse.ArgumentParser(description="Run an isolated PoPify synthetic preview.")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    with tempfile.TemporaryDirectory(prefix="popify-preview-") as directory:
        os.environ["POPIFY_PREVIEW_MODE"] = "1"
        os.environ["DATABASE_URL"] = f"sqlite:///{directory}/preview.db"
        os.environ["OPERATOR_USERNAME"] = "preview"
        os.environ["OPERATOR_PASSWORD"] = "preview-only"

        import uvicorn
        from app.db import Base, SessionLocal, engine
        from app.main import app
        from app.config import settings
        from app.models import ActionExecution, Case, Delivery, Order, utcnow
        from app.services import audit, create_approval, decide_approval, execute_approved

        Base.metadata.create_all(engine)
        now = utcnow()
        with SessionLocal() as db:
            order = Order(
                shop_domain="demo.myshopify.com", external_id="preview-order",
                order_number="#PREVIEW-1001", email="synthetic@example.test",
                currency="USD", total="84.00", fulfillment_status="unfulfilled",
                financial_status="paid", created_at=now - timedelta(hours=72),
                source_updated_at=now - timedelta(minutes=5), payload={},
            )
            db.add(order)
            db.flush()
            audit(
                db,
                "order",
                order.id,
                "synthetic_order_seeded",
                actor="preview",
                details={"synthetic": True, "age_hours": 72},
                correlation_id="preview:order",
            )
            def scenario_approval(label, action="send_reply"):
                record = Case(
                    order_id=order.id, order_ref=order.order_number,
                    customer_email=order.email, message=f"Synthetic {label} support request",
                    category="order_status", confidence=80,
                    draft=f"Synthetic {label} draft response.", status="needs_review",
                    correlation_id=f"preview:{label}",
                )
                db.add(record)
                db.flush()
                audit(
                    db, "order", order.id, "preview_order_context_attached", "preview",
                    details={"synthetic": True, "scenario": label},
                    correlation_id=record.correlation_id,
                )
                audit(
                    db,
                    "case",
                    record.id,
                    "inquiry_queued",
                    actor="preview",
                    details={"synthetic": True},
                    correlation_id=record.correlation_id,
                )
                audit(
                    db,
                    "case",
                    record.id,
                    "draft_created",
                    details={"category": record.category, "confidence": record.confidence},
                    correlation_id=record.correlation_id,
                )
                return create_approval(db, "support", "case", record.id, action, {
                    "order_number": order.order_number, "customer_message": record.message,
                    "order_id": order.id,
                    "customer_email": record.customer_email,
                    "case_order_ref": record.order_ref,
                    "order_email": order.email,
                    "policy": settings.store_policies[record.category],
                    "order_fulfillment_status": order.fulfillment_status,
                    "order_financial_status": order.financial_status,
                    "order_total": "USD 84.00", "category": record.category,
                    "confidence": record.confidence, "draft": record.draft,
                    "order_source_updated_at": order.source_updated_at.isoformat(),
                    "case_status": record.status, "missing_info": "",
                    "correlation_id": record.correlation_id,
                })

            pending = scenario_approval("pending")
            completed = scenario_approval("completed")
            decide_approval(db, completed.id, "approved", "preview")
            execute_approved(db, completed)
            rejected = scenario_approval("rejected")
            decide_approval(db, rejected.id, "rejected", "preview")

            blocked = scenario_approval("evidence-changed")
            decide_approval(db, blocked.id, "approved", "preview")
            db.get(Case, blocked.subject_id).message = "Synthetic request changed after approval."
            db.flush()
            execute_approved(db, blocked)
            db.commit()

            invalid = scenario_approval("invalid-action", "unsupported_demo_action")
            decide_approval(db, invalid.id, "approved", "preview")
            execute_approved(db, invalid)
            db.commit()

            retryable = scenario_approval("retryable")
            decide_approval(db, retryable.id, "approved", "preview")
            execute_approved(db, retryable, mock_outcome="retryable")

            ambiguous = scenario_approval("ambiguous")
            decide_approval(db, ambiguous.id, "approved", "preview")
            execute_approved(db, ambiguous, mock_outcome="ambiguous")

            exhausted = scenario_approval("exhausted")
            decide_approval(db, exhausted.id, "approved", "preview")
            attempt_budget = settings.max_action_attempts
            failure = "Synthetic automatic retry budget exhausted; no external request was made."
            db.add(ActionExecution(
                idempotency_key=f"approval:{exhausted.id}",
                proposed_action=exhausted.proposed_action,
                evidence_hash=exhausted.evidence_hash,
                state="exhausted",
                attempts=attempt_budget,
                detail=failure,
            ))
            exhausted.attempts = attempt_budget
            exhausted.execution_status = "exhausted"
            exhausted.failure_detail = failure
            for attempt in range(1, attempt_budget + 1):
                audit(
                    db, "approval", exhausted.id, "action_execution_attempt_exhausted", "preview",
                    details={"attempt": attempt, "automatic_attempt_budget": attempt_budget, "synthetic": True},
                    correlation_id=exhausted.supporting_facts.get("correlation_id"),
                )

            db.add(Delivery(
                webhook_id="preview-exhausted-delivery", topic="orders/create",
                shop_domain="demo.myshopify.com", payload={}, status="exhausted",
                attempts=3, error="Synthetic exhausted delivery for preview.",
                received_at=now - timedelta(minutes=20),
            ))
            db.add(Case(
                message="Synthetic support failure", status="failed", attempts=1,
                failure_detail="Synthetic retryable triage failure.",
                next_attempt_at=now + timedelta(minutes=5),
            ))
            db.commit()

        print(f"PoPify isolated preview: http://127.0.0.1:{args.port}")
        print("Sign in with preview / preview-only; integrations are disabled.")
        print("The temporary database is deleted when this process stops.")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    run()

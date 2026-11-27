from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Approval, Audit, Case, Delivery, utcnow
from app.operations import operational_snapshot


def test_due_scheduled_exhausted_and_uncertain_are_distinct():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = utcnow()
    with Session(engine) as db:
        db.add_all([
            Delivery(webhook_id="due", topic="orders/create", shop_domain="demo.myshopify.com", status="failed",
                     attempts=1, received_at=now-timedelta(hours=1), next_attempt_at=now-timedelta(seconds=150)),
            Delivery(webhook_id="future", topic="orders/create", shop_domain="demo.myshopify.com", status="failed",
                     attempts=1, received_at=now-timedelta(hours=1), next_attempt_at=now+timedelta(minutes=5)),
            Case(message="synthetic", status="exhausted"),
            Approval(kind="support", subject_type="case", subject_id=1, proposed_action="send_reply",
                     status="approved", execution_status="ambiguous"),
            Audit(entity_type="workflow", entity_id=1, event="work_failed", correlation_id="synthetic"),
            Audit(entity_type="workflow", entity_id=2, event="poll_completed", correlation_id="synthetic"),
        ])
        db.commit()
        result = operational_snapshot(db)
        orders = result["queues"]["orders"]
        assert orders["queued"] == 2 and orders["due"] == 1 and orders["scheduled"] == 1
        assert 150 <= orders["oldest_due_seconds"] < 160
        assert result["queues"]["support"]["exhausted"] == 1
        assert result["queues"]["uncertain_actions"] == 1
        assert {a["code"] for a in result["alerts"]} == {
            "orders_overdue", "support_exhausted", "uncertain_actions", "worker_1_failed", "worker_3_stale"}
        assert all(w["state"] == "disabled" for w in operational_snapshot(db, preview=True)["workers"])
    engine.dispose()

"""Bounded operational summaries; no customer content or secrets."""
from sqlalchemy import func, select
from app.models import Approval, Case, Delivery, utcnow
from app.services import as_utc


def queue_summary(db):
    now = utcnow()
    result = {}
    for label, model, status, timestamp in [
        ("orders", Delivery, Delivery.status == "received", Delivery.received_at),
        ("support", Case, Case.status == "queued", Case.created_at),
        ("actions", Approval, (Approval.status == "approved") &
         Approval.execution_status.in_(["queued", "retryable"]), Approval.decision_at),
    ]:
        total, oldest = db.execute(select(func.count(), func.min(timestamp)).select_from(model).where(status)).one()
        result[label] = {
            "queued": total,
            "oldest_seconds": max(0, int((now - as_utc(oldest)).total_seconds())) if oldest else None,
        }
    result["uncertain_actions"] = db.scalar(select(func.count()).select_from(Approval).where(
        Approval.execution_status.in_(["ambiguous", "blocked_prior_outcome"]))) or 0
    return result

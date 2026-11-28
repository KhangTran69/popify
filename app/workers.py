"""Queue selection and bounded independent-transaction worker invocations."""
import time
from datetime import timedelta
from sqlalchemy import exists, or_, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from app.config import settings
from app.db import bound_worker_lock_wait
from app.models import Delivery, Case, Order, Approval, utcnow
from app.services import process_delivery, process_support, audit, create_approval_with_status


def process_next_delivery(db):
    bound_worker_lock_wait(db)
    pending = db.scalar(
        select(Delivery)
        .where(
            or_(
                Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= utcnow()
            ),
            (Delivery.status == "received")
            | (
                (Delivery.status == "failed")
                & (Delivery.attempts < settings.max_delivery_attempts)
            ),
        )
        .order_by(Delivery.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    return process_delivery(db, pending.id) if pending else {"status": "idle"}


def process_next_support(db):
    bound_worker_lock_wait(db)
    case = db.scalar(
        select(Case)
        .where(
            Case.status.in_(["queued", "failed"]),
            or_(Case.next_attempt_at.is_(None), Case.next_attempt_at <= utcnow()),
        )
        .order_by(Case.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not case:
        return {"status": "idle"}
    case = process_support(db, case)
    return {"case_id": case.id, "status": case.status}


def heartbeat(db, number, result, completed=False, failed=False):
    audit(db, "workflow", number,
          "work_failed" if failed else "work_completed" if completed else "poll_completed",
          details=result)
    db.commit()
    return result


def run_batch(db, number, process_next, batch_size, success_status):
    """Bound each poll; every item commits separately; never pre-lock a batch."""
    def next_item():
        try:
            return process_next(db)
        except OperationalError as exc:
            # A lock timeout has a known SQLSTATE and a responsive server.
            # Record a sanitized failed poll when possible, without masking the
            # original 503 or spending a business-record attempt.
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                try:
                    db.rollback()
                    bound_worker_lock_wait(db)
                    heartbeat(db, number, {"status": "unavailable", "reason": "database_lock_timeout"}, failed=True)
                except SQLAlchemyError:
                    db.rollback()
            raise

    def record(result):
        status = result["status"]
        return heartbeat(db, number, result, completed=status == success_status,
                         failed=status in ("failed", "exhausted"))
    if batch_size == 1:
        return record(next_item())
    deadline = time.monotonic() + 7
    counts = {status: 0 for status in (success_status, "failed", "exhausted", "duplicate", "deferred")}
    for _ in range(batch_size):
        if time.monotonic() >= deadline:
            break
        result = next_item()
        status = result["status"]
        if status == "idle":
            break
        counts[status] = counts.get(status, 0) + 1
        record(result)
    failed = bool(counts["failed"] or counts["exhausted"])
    processed = counts[success_status]
    result = {"status": "failed" if failed else success_status if processed else "idle",
              "batch_size": batch_size, "outcomes": counts}
    return heartbeat(db, number, result, completed=bool(processed), failed=failed)


def scan_delayed_orders(db):
    """Claim a bounded set of eligible orders and count only our own inserts."""
    bound_worker_lock_wait(db)
    cutoff = utcnow() - timedelta(hours=settings.order_delay_hours)
    existing = exists(select(Approval.id).where(
        Approval.kind == "exception", Approval.subject_type == "order",
        Approval.subject_id == Order.id, Approval.proposed_action == "review_delayed_order",
    ))
    eligible = ((Order.fulfillment_status == "unfulfilled") & (Order.created_at < cutoff) & ~existing)
    orders = list(db.scalars(select(Order).where(eligible).order_by(Order.created_at, Order.id)
                            .with_for_update(of=Order, skip_locked=True).limit(100)))
    inserted = 0
    for order in orders:
        _, created = create_approval_with_status(db, "exception", "order", order.id, "review_delayed_order", {
            "order_number": order.order_number, "order_id": order.id,
            "hours_threshold": settings.order_delay_hours, "created_at": order.created_at.isoformat(),
            "order_fulfillment_status": order.fulfillment_status,
            "order_financial_status": order.financial_status,
            "order_total": f"{order.currency} {order.total}",
            "order_source_updated_at": order.source_updated_at.isoformat(),
        })
        inserted += int(created)
    db.commit()
    return len(orders), inserted, bool(db.scalar(select(exists().where(eligible))))

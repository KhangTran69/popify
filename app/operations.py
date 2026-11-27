"""Bounded operational summaries and actionable alerts without customer content."""
from sqlalchemy import case, func, or_, select
from app.config import settings
from app.models import ActionExecution, Approval, Audit, Case, Delivery, utcnow
from app.services import as_utc

QUEUE_AGE_SECONDS = 120
WORKER_STALE_SECONDS = 180

def workflow_activity(db):
    result = []
    for number, label in [
        (1, "Order intake"),
        (2, "Support triage"),
        (3, "Exceptions & actions"),
    ]:
        last = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event.in_(["work_completed", "work_failed", "poll_completed"]),
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        success = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event == "work_completed",
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        failure = db.scalar(
            select(Audit)
            .where(
                Audit.entity_type == "workflow",
                Audit.entity_id == number,
                Audit.event == "work_failed",
            )
            .order_by(Audit.id.desc())
            .limit(1)
        )
        recent = bool(
            last and (utcnow() - as_utc(last.created_at)).total_seconds() < WORKER_STALE_SECONDS
        )
        result.append(
            {
                "number": number,
                "label": label,
                "last": last,
                "success": success,
                "failure": failure,
                "recent": recent,
            }
        )
    return result



def queue_summary(db):
    now = utcnow()
    result = {}
    for label, model, pending, timestamp, exhausted in [
        ("orders", Delivery, (Delivery.status == "received") | ((Delivery.status == "failed") &
         (Delivery.attempts < settings.max_delivery_attempts)), Delivery.received_at,
         Delivery.status == "exhausted"),
        ("support", Case, Case.status.in_(["queued", "failed"]), Case.created_at,
         Case.status == "exhausted"),
        ("actions", Approval, (Approval.status == "approved") &
         Approval.execution_status.in_(["queued", "retryable"]), Approval.decision_at,
         Approval.execution_status == "exhausted"),
    ]:
        due = or_(model.next_attempt_at.is_(None), model.next_attempt_at <= now)
        eligible_since = case((model.next_attempt_at > timestamp, model.next_attempt_at), else_=timestamp)
        # One statement per queue gives a consistent count/age snapshot under
        # PostgreSQL READ COMMITTED, including concurrent enqueue and drain.
        total, oldest, due_count, oldest_due, exhausted_count = db.execute(select(
            func.count().filter(pending),
            func.min(timestamp).filter(pending),
            func.count().filter(pending, due),
            func.min(eligible_since).filter(pending, due),
            func.count().filter(exhausted),
        ).select_from(model)).one()
        result[label] = {
            "queued": total,
            "oldest_seconds": max(0, int((now - as_utc(oldest)).total_seconds())) if oldest else None,
            "due": due_count,
            "scheduled": total - due_count,
            "oldest_due_seconds": max(0, int((now - as_utc(oldest_due)).total_seconds())) if oldest_due else None,
            "exhausted": exhausted_count,
        }
    result["uncertain_actions"] = db.scalar(select(func.count()).select_from(Approval).where(
        Approval.execution_status.in_(["ambiguous", "disputed", "blocked_prior_outcome"]))) or 0
    result["started_executions"] = db.scalar(select(func.count()).select_from(ActionExecution).where(
        ActionExecution.state == "started")) or 0
    pending_count, oldest_pending = db.execute(select(func.count(), func.min(Approval.created_at)).where(
        Approval.status == "pending", Approval.replacement_approval_id.is_(None))).one()
    result["pending_review"] = {"count": pending_count, "oldest_seconds": max(0, int((now - as_utc(oldest_pending)).total_seconds())) if oldest_pending else None}
    return result


def operational_snapshot(db, *, preview=False):
    queues = queue_summary(db)
    workers = []
    alerts = []
    def alert(code, severity, message, action):
        alerts.append({"code": code, "severity": severity, "message": message, "action": action})
    for item in workflow_activity(db):
        last = item["last"]
        state = "disabled" if preview else "stale" if not item["recent"] else "failed" if last.event == "work_failed" else "healthy"
        workers.append({"number": item["number"], "label": item["label"], "state": state,
                        "recent_poll": item["recent"], "last_poll": as_utc(last.created_at).isoformat() if last else None,
                        "last_success": as_utc(item["success"].created_at).isoformat() if item["success"] else None,
                        "last_failure": as_utc(item["failure"].created_at).isoformat() if item["failure"] else None})
        if state in ("stale", "failed"):
            alert(f"worker_{item['number']}_{state}", "warning", f"{item['label']} worker: {state}.",
                  "Inspect private n8n execution logs and database connectivity before retrying.")
    for name in ("orders", "support", "actions"):
        queue = queues[name]
        if queue["oldest_due_seconds"] is not None and queue["oldest_due_seconds"] >= QUEUE_AGE_SECONDS:
            alert(f"{name}_overdue", "warning", f"{name.title()} has eligible work waiting at least {QUEUE_AGE_SECONDS} seconds.",
                  "Inspect queue and worker activity; preserve existing idempotency keys.")
        if queue["exhausted"]:
            alert(f"{name}_exhausted", "warning", f"{name.title()} has exhausted work.",
                  "Review recorded failures in Recovery before manually retrying.")
    if queues["pending_review"]["oldest_seconds"] is not None and queues["pending_review"]["oldest_seconds"] >= settings.human_review_alert_seconds:
        alert("human_review_overdue", "warning", "A human decision has exceeded the configured review threshold.",
              "Assign an authorized reviewer; do not bypass human approval to clear the queue.")
    if queues["started_executions"]:
        alert("started_execution_unresolved", "critical", "A persisted execution has no recorded terminal outcome.",
              "Inspect the original attempt and reconciliation evidence; never redispatch an unknown outcome.")
    if queues["uncertain_actions"]:
        alert("uncertain_actions", "critical", "Provider outcome requires reconciliation.",
              "Inspect saved evidence; never blindly replay an uncertain action.")
    return {"observed_at": utcnow().isoformat(), "mode": "isolated_preview" if preview else "mock_actions", "queues": queues, "workers": workers,
            "alerts": alerts, "status": "attention" if alerts else "healthy"}

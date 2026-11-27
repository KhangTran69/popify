"""Read-only retention inventory: counts and ages, never customer content."""
import argparse
import json
from datetime import timedelta
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Delivery, Order, Case, Approval, Audit, ActionExecution, ExecutionReconciliation, OperatorAccount, utcnow
from app.identity import purge_expired_identity


def inventory(db, days):
    cutoff = utcnow() - timedelta(days=days)
    result = {"older_than_days": days, "records": {}}
    for name, model, timestamp in [
        ("deliveries", Delivery, Delivery.received_at),
        ("orders", Order, Order.created_at),
        ("support_cases", Case, Case.created_at),
        ("approval_snapshots", Approval, Approval.created_at),
        ("audit_events", Audit, Audit.created_at),
        ("action_executions", ActionExecution, ActionExecution.created_at),
        ("execution_reconciliations", ExecutionReconciliation, ExecutionReconciliation.created_at),
    ]:
        total, old = db.execute(select(func.count(), func.count().filter(timestamp < cutoff)).select_from(model)).one()
        result["records"][name] = {"total": total, "older_than_threshold": old}
    result["orders_with_email"] = db.scalar(select(func.count()).select_from(Order).where(Order.email.is_not(None)))
    result["support_cases_with_email"] = db.scalar(select(func.count()).select_from(Case).where(Case.customer_email.is_not(None)))
    result["authentication_artifacts"] = purge_expired_identity(db)
    result["operator_accounts"] = db.scalar(select(func.count()).select_from(OperatorAccount))
    result["note"] = "Counts are not a deletion recommendation; approval evidence, replay requirements and legal retention must be reviewed."
    return result


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.older_than_days <= 36500:
        parser.error("Threshold must be between 1 and 36500 days.")
    with SessionLocal() as db:
        print(json.dumps(inventory(db, args.older_than_days), indent=2))


if __name__ == "__main__":
    run()

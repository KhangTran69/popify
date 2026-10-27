"""Shared recovery query and presentation rules for dashboard and workspace."""

from math import ceil

from sqlalchemy import String, case, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased

from app.models import ActionExecution, Approval, Case, Delivery, ExecutionReconciliation, utcnow


RECOVERY_CATEGORIES = ("pending", "waiting", "eligible", "exhausted", "stale", "uncertain", "blocked")
RECOVERY_PRIORITY = {
    "uncertain": 0,
    "pending": 1,
    "stale": 2,
    "eligible": 3,
    "exhausted": 4,
    "blocked": 5,
    "waiting": 6,
}


def _approval_category(now, prior_terminal):
    return case(
        (Approval.execution_status.in_(["ambiguous", "disputed"]), "uncertain"),
        (Approval.execution_status.in_(["invalid_action", "blocked_prior_outcome", "superseded", "reconciled_not_executed"]), "blocked"),
        (prior_terminal, "blocked"),
        (Approval.status == "pending", "pending"),
        (Approval.execution_status == "exhausted", "exhausted"),
        (Approval.execution_status == "evidence_changed", "stale"),
        (Approval.next_attempt_at > now, "waiting"),
        else_="eligible",
    )


def _work_category(model, now):
    return case(
        (model.status == "exhausted", "exhausted"),
        (model.next_attempt_at > now, "waiting"),
        else_="eligible",
    )


def recovery_union(now=None):
    now = now or utcnow()
    prior_revision = aliased(Approval)
    latest_reconciliation = aliased(ExecutionReconciliation)
    successor_reconciliation = aliased(ExecutionReconciliation)
    prior_execution_keys = select(
        (literal("approval:") + cast(prior_revision.id, String))
    ).where(
        prior_revision.subject_type == Approval.subject_type,
        prior_revision.subject_id == Approval.subject_id,
        prior_revision.id != Approval.id,
    )
    latest_confirms_nonexecution = exists(
        select(1).select_from(latest_reconciliation).where(
            latest_reconciliation.action_execution_id == ActionExecution.id,
            latest_reconciliation.outcome == "confirmed_not_executed",
            ~exists(select(1).select_from(successor_reconciliation).where(
                successor_reconciliation.previous_reconciliation_id == latest_reconciliation.id
            )),
        )
    )
    has_prior_terminal = exists(
        select(1).select_from(ActionExecution).where(
            ActionExecution.idempotency_key.in_(prior_execution_keys),
            ActionExecution.state.in_(["started", "succeeded", "ambiguous"]),
            ~latest_confirms_nonexecution,
        )
    )
    approvals = select(
        literal("approval").label("source"), Approval.id.label("record_id"),
        (literal("Approval #") + cast(Approval.id, String) + literal(" · ") + Approval.proposed_action).label("label"),
        func.coalesce(
            Approval.failure_detail,
            case((has_prior_terminal, "A prior approval outcome must be reconciled before this revision can act."),
                 else_=literal("Human decision or action recovery required")),
        ).label("reason"),
        Approval.execution_status.label("raw_state"), Approval.attempts.label("attempts"),
        Approval.next_attempt_at.label("next_attempt_at"), Approval.created_at.label("occurred_at"),
        _approval_category(now, has_prior_terminal).label("category"),
        has_prior_terminal.label("prior_terminal"),
    ).where(
        Approval.replacement_approval_id.is_(None),
        or_(
            Approval.status == "pending",
            Approval.execution_status.in_(["failed", "queued", "retryable", "exhausted", "ambiguous", "disputed", "evidence_changed", "invalid_action", "blocked_prior_outcome", "superseded", "reconciled_not_executed"]),
        ),
    )
    cases = select(
        literal("case").label("source"), Case.id.label("record_id"),
        (literal("Support case #") + cast(Case.id, String)).label("label"),
        func.coalesce(Case.failure_detail, literal("Support triage needs another attempt")).label("reason"),
        Case.status.label("raw_state"), Case.attempts.label("attempts"), Case.next_attempt_at.label("next_attempt_at"),
        Case.created_at.label("occurred_at"), _work_category(Case, now).label("category"),
        literal(False).label("prior_terminal"),
    ).where(or_(Case.status.in_(["failed", "exhausted"]), (Case.status == "queued") & (Case.attempts > 0)))
    deliveries = select(
        literal("delivery").label("source"), Delivery.id.label("record_id"),
        (literal("Delivery · ") + Delivery.webhook_id).label("label"),
        func.coalesce(Delivery.error, literal("Order intake needs another attempt")).label("reason"),
        Delivery.status.label("raw_state"), Delivery.attempts.label("attempts"), Delivery.next_attempt_at.label("next_attempt_at"),
        Delivery.received_at.label("occurred_at"), _work_category(Delivery, now).label("category"),
        literal(False).label("prior_terminal"),
    ).where(or_(Delivery.status.in_(["failed", "exhausted"]), (Delivery.status == "received") & (Delivery.attempts > 0)))
    return union_all(approvals, cases, deliveries).subquery()


def recovery_counts(db, now=None):
    view = recovery_union(now)
    rows = db.execute(
        select(view.c.category, func.count()).group_by(view.c.category)
    ).all()
    result = {category: 0 for category in RECOVERY_CATEGORIES}
    result.update(dict(rows))
    result["total"] = sum(result.values())
    return result


def recovery_page(db, category="", q="", page=1, per_page=12, now=None):
    view = recovery_union(now)
    conditions = []
    if category in RECOVERY_CATEGORIES:
        conditions.append(view.c.category == category)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(or_(view.c.label.ilike(pattern, escape="\\"), view.c.reason.ilike(pattern, escape="\\"), view.c.raw_state.ilike(pattern, escape="\\")))
    total = db.scalar(select(func.count()).select_from(view).where(*conditions)) or 0
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)
    rows = db.execute(
        select(view).where(*conditions).order_by(
            case(*[(view.c.category == name, priority) for name, priority in RECOVERY_PRIORITY.items()], else_=99),
            view.c.occurred_at.asc(),
            view.c.source.asc(),
            view.c.record_id.asc(),
        )
        .offset((page - 1) * per_page).limit(per_page)
    ).mappings().all()
    return [recovery_item(dict(row)) for row in rows], total, page, pages


def recovery_item(row):
    source = row["source"]
    row["url"] = f"/{'approvals' if source == 'approval' else 'cases' if source == 'case' else 'deliveries'}/{row['record_id']}"
    row["next_action"] = (
        "Reconcile the prior approval outcome; this revision cannot act"
        if row.get("prior_terminal")
        else {
        "pending": "Review evidence and decide",
        "waiting": "Wait for the recorded retry deadline",
        "eligible": "Open the record and queue or run the retry",
        "exhausted": "Inspect attempts and authorize manual recovery",
        "stale": "Compare current facts and check replacement eligibility",
        "uncertain": "Reconcile the outcome; retry remains blocked",
        "blocked": (
            "Correct the action contract before reprocessing"
            if row["raw_state"] == "invalid_action"
            else "Create a fresh pending approval from current facts"
            if row["raw_state"] == "reconciled_not_executed"
            else "Reconcile the linked revision before taking further action"
        ),
        }[row["category"]]
    )
    return row

"""Shared recovery query and presentation rules for dashboard and workspace."""

from dataclasses import dataclass
from math import ceil

from sqlalchemy import String, case, cast, func, literal, or_, select, union_all

from app.models import Approval, Case, Delivery, utcnow


RECOVERY_CATEGORIES = ("pending", "waiting", "eligible", "exhausted", "stale", "uncertain", "blocked")


def _approval_category(now):
    return case(
        (Approval.status == "pending", "pending"),
        (Approval.execution_status == "exhausted", "exhausted"),
        (Approval.execution_status == "evidence_changed", "stale"),
        (Approval.execution_status == "ambiguous", "uncertain"),
        (Approval.execution_status.in_(["invalid_action", "blocked_prior_outcome"]), "blocked"),
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
    approvals = select(
        literal("approval").label("source"), Approval.id.label("record_id"),
        (literal("Approval #") + cast(Approval.id, String) + literal(" · ") + Approval.proposed_action).label("label"),
        func.coalesce(Approval.failure_detail, literal("Human decision or action recovery required")).label("reason"),
        Approval.execution_status.label("raw_state"), Approval.attempts.label("attempts"),
        Approval.next_attempt_at.label("next_attempt_at"), Approval.created_at.label("occurred_at"),
        _approval_category(now).label("category"),
    ).where(
        Approval.replacement_approval_id.is_(None),
        or_(
            Approval.status == "pending",
            Approval.execution_status.in_(["failed", "queued", "retryable", "exhausted", "ambiguous", "evidence_changed", "invalid_action", "blocked_prior_outcome"]),
        ),
    )
    cases = select(
        literal("case").label("source"), Case.id.label("record_id"),
        (literal("Support case #") + cast(Case.id, String)).label("label"),
        func.coalesce(Case.failure_detail, literal("Support triage needs another attempt")).label("reason"),
        Case.status.label("raw_state"), Case.attempts.label("attempts"), Case.next_attempt_at.label("next_attempt_at"),
        Case.created_at.label("occurred_at"), _work_category(Case, now).label("category"),
    ).where(or_(Case.status.in_(["failed", "exhausted"]), (Case.status == "queued") & (Case.attempts > 0)))
    deliveries = select(
        literal("delivery").label("source"), Delivery.id.label("record_id"),
        (literal("Delivery · ") + Delivery.webhook_id).label("label"),
        func.coalesce(Delivery.error, literal("Order intake needs another attempt")).label("reason"),
        Delivery.status.label("raw_state"), Delivery.attempts.label("attempts"), Delivery.next_attempt_at.label("next_attempt_at"),
        Delivery.received_at.label("occurred_at"), _work_category(Delivery, now).label("category"),
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
        select(view).where(*conditions).order_by(view.c.occurred_at.desc(), view.c.source, view.c.record_id.desc())
        .offset((page - 1) * per_page).limit(per_page)
    ).mappings().all()
    return [recovery_item(dict(row)) for row in rows], total, page, pages


def recovery_item(row):
    source = row["source"]
    row["url"] = f"/{'approvals' if source == 'approval' else 'cases' if source == 'case' else 'deliveries'}/{row['record_id']}"
    row["next_action"] = {
        "pending": "Review evidence and decide",
        "waiting": "Wait for the recorded retry deadline",
        "eligible": "Open the record and queue or run the retry",
        "exhausted": "Inspect attempts and authorize manual recovery",
        "stale": "Compare current facts and create a replacement approval",
        "uncertain": "Reconcile the outcome; retry remains blocked",
        "blocked": "Correct the action contract before reprocessing",
    }[row["category"]]
    return row

import hashlib, logging, uuid
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, text, exists, literal, cast, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from app.config import settings
from app.integrations import verify_shopify, generate_support_content
from app.models import (
    Order, Delivery, Case, Approval, Audit, ActionExecution,
    ExecutionReconciliation, utcnow,
)

log = logging.getLogger("commerceops")


class SupportRequestKeyConflict(ValueError):
    """A caller reused an idempotency key for different inquiry content."""


class ApprovalEvidenceStale(ValueError):
    def __init__(self, differences):
        self.differences = differences
        super().__init__("approval evidence is stale; review the current facts")


ACTION_CONTRACTS = {
    ("support", "case", "send_reply"),
    ("exception", "case", "review_refund_request"),
    ("exception", "case", "review_cancellation_request"),
    ("exception", "order", "review_delayed_order"),
}


def approval_evidence_hash(kind, subject_type, subject_id, action, facts):
    """Hash exactly what the operator is asked to approve."""
    evidence = {
        "kind": kind,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "proposed_action": action,
        "supporting_facts": facts or {},
    }
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def current_approval_evidence_hash(approval):
    return approval_evidence_hash(
        approval.kind,
        approval.subject_type,
        approval.subject_id,
        approval.proposed_action,
        approval.supporting_facts,
    )


def parse_dt(value):
    if not value:
        return utcnow()
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def as_utc(value):
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def normalize_order(payload, shop="demo.myshopify.com"):
    oid = payload.get("id") or payload.get("order_id")
    if oid is None:
        raise ValueError("order payload is missing id")
    source_time = payload.get("updated_at") or payload.get("created_at")
    if not source_time:
        raise ValueError("order payload is missing source timestamp")
    fulfillment = payload.get("fulfillment_status") or "unfulfilled"
    return dict(
        shop_domain=shop,
        external_id=str(oid),
        order_number=str(payload.get("name") or payload.get("order_number") or oid),
        email=payload.get("email"),
        currency=payload.get("currency") or "USD",
        total=str(payload.get("total_price") or payload.get("total") or "0.00"),
        fulfillment_status=str(fulfillment).lower(),
        financial_status=str(payload.get("financial_status") or "unknown").lower(),
        created_at=parse_dt(payload.get("created_at")),
        source_updated_at=parse_dt(
            payload.get("updated_at") or payload.get("created_at")
        ),
        payload=payload,
    )


def audit(
    db, entity_type, entity_id, event, actor="system", details=None, correlation_id=None
):
    db.add(
        Audit(
            correlation_id=correlation_id or f"{entity_type}:{entity_id}",
            entity_type=entity_type,
            entity_id=entity_id,
            event=event,
            actor=actor,
            details=details or {},
        )
    )


def process_delivery(db: Session, delivery_id: int):
    delivery = db.scalar(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    )
    if not delivery or delivery.status == "processed":
        return {"status": "duplicate"}
    if delivery.status == "exhausted":
        return {"status": "exhausted"}
    if delivery.next_attempt_at and as_utc(delivery.next_attempt_at) > utcnow():
        return {"status": "deferred"}
    delivery.attempts += 1
    try:
        data = normalize_order(delivery.payload, delivery.shop_domain)
        # Serialize even the first insert for an order, across different deliveries.
        if db.bind.dialect.name == "postgresql":
            lock_key = int.from_bytes(
                hashlib.sha256(
                    f'{data["shop_domain"]}:{data["external_id"]}'.encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        order = db.scalar(
            select(Order)
            .where(
                Order.shop_domain == data["shop_domain"],
                Order.external_id == data["external_id"],
            )
            .with_for_update()
        )
        if order:
            if data["source_updated_at"] > as_utc(order.source_updated_at):
                for k, v in data.items():
                    if k == "created_at" and not delivery.payload.get("created_at"):
                        continue
                    setattr(order, k, v)
                audit(
                    db,
                    "order",
                    order.id,
                    "order_updated",
                    details={"webhook_id": delivery.webhook_id},
                    correlation_id=delivery.webhook_id,
                )
            else:
                audit(
                    db,
                    "order",
                    order.id,
                    "stale_event_ignored",
                    details={"webhook_id": delivery.webhook_id},
                    correlation_id=delivery.webhook_id,
                )
        else:
            order = Order(**data)
            db.add(order)
            db.flush()
            audit(
                db,
                "order",
                order.id,
                "order_created",
                details={"webhook_id": delivery.webhook_id},
                correlation_id=delivery.webhook_id,
            )
        delivery.status = "processed"
        delivery.processed_at = utcnow()
        delivery.error = None
        delivery.next_attempt_at = None
        audit(
            db,
            "delivery",
            delivery.id,
            "delivery_processed",
            details={"order_id": order.id, "attempt": delivery.attempts},
            correlation_id=delivery.webhook_id,
        )
        db.commit()
        return {"status": "processed", "order_id": order.id}
    except Exception as exc:
        db.rollback()
        delivery = db.scalar(
            select(Delivery).where(Delivery.id == delivery_id).with_for_update()
        )
        if delivery.status == "processed":
            return {"status": "duplicate"}
        delivery.attempts += 1
        delivery.status = (
            "exhausted"
            if delivery.attempts >= settings.max_delivery_attempts
            else "failed"
        )
        delivery.next_attempt_at = (
            utcnow() + timedelta(seconds=retry_delay(delivery.attempts))
            if delivery.status == "failed"
            else None
        )
        delivery.error = type(exc).__name__
        audit(
            db,
            "delivery",
            delivery.id,
            "delivery_processing_failed",
            details={"error_type": type(exc).__name__, "attempt": delivery.attempts},
            correlation_id=delivery.webhook_id,
        )
        db.commit()
        log.error(
            json.dumps(
                {
                    "event": "delivery_processing_failed",
                    "correlation_id": delivery.webhook_id,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return {"status": "failed"}


def create_support_case(
    db: Session, message: str, order_ref: str | None, email: str | None, case=None
):
    order = None
    if order_ref and email:
        order = db.scalar(
            select(Order).where(
                (Order.order_number == order_ref) | (Order.external_id == order_ref),
                func.lower(Order.email) == email.strip().lower(),
                Order.shop_domain
                == (settings.shopify_store_domain or "demo.myshopify.com"),
            )
        )
    category, confidence = classify(message)
    facts = (
        f"Order {order.order_number} is {order.fulfillment_status}; total {order.currency} {order.total}."
        if order
        else "No matching order was found."
    )
    policies = settings.store_policies
    draft = f"Thanks for contacting us. {facts} {policies[category]}"
    missing = ""
    if (
        settings.llm_mode == "openai"
        and settings.openai_api_key
    ):
        try:
            category, confidence, draft, missing = generate_support_content(
                message, facts, policies
            )
        except Exception as exc:
            log.warning(
                json.dumps(
                    {
                        "event": "llm_fallback",
                        "error_type": type(exc).__name__,
                        "correlation_id": case.correlation_id if case else "support",
                    }
                )
            )
    required = []
    if order is None:
        required.append("Verified order reference")
    if category == "damaged_item":
        required.append("Photo or item details")
    if confidence < 60:
        required.append("Uncertain classification — operator review required")
    missing = "; ".join(dict.fromkeys(required + ([missing] if missing else [])))
    if case is None:
        case = Case(message=message, customer_email=email, order_ref=order_ref)
    case.order = order
    case.category = category
    case.confidence = confidence
    case.draft = draft
    case.missing_info = missing
    case.status = "needs_review"
    case.failure_detail = None
    case.next_attempt_at = None
    db.add(case)
    db.flush()
    audit(
        db,
        "case",
        case.id,
        "draft_created",
        details={"category": category, "confidence": confidence},
        correlation_id=case.correlation_id,
    )
    facts = {
        "order_number": order.order_number if order else None,
        "order_id": order.id if order else None,
        "customer_email": case.customer_email,
        "case_order_ref": case.order_ref,
        "order_email": order.email if order else None,
        "customer_message": message,
        "policy": policies[category],
        "order_fulfillment_status": order.fulfillment_status if order else None,
        "order_financial_status": order.financial_status if order else None,
        "order_total": f"{order.currency} {order.total}" if order else None,
        "order_source_updated_at": order.source_updated_at.isoformat() if order and order.source_updated_at else None,
        "category": category,
        "confidence": confidence,
        "draft": draft,
        "missing_info": missing,
        "case_status": case.status,
        "correlation_id": case.correlation_id or f"case:{case.id}",
    }
    create_approval(db, "support", "case", case.id, "send_reply", facts)
    if category in ("refund", "cancellation"):
        create_approval(
            db, "exception", "case", case.id, f"review_{category}_request", facts
        )
    db.commit()
    return case


def classify(text):
    # Deliberately conservative deterministic mock classifier. Message text is never treated as instructions.
    t = text.lower()
    rules = [
        ("damaged_item", ("damaged", "broken", "defective")),
        ("cancellation", ("cancel",)),
        ("refund", ("refund", "money back")),
        ("order_status", ("where is", "status", "tracking", "arrive")),
    ]
    for label, words in rules:
        if any(w in t for w in words):
            return label, 80
    return "other", 45


def create_approval(db, kind, subject_type, subject_id, action, facts, revision=None):
    existing_query = select(Approval).where(
        Approval.kind == kind,
        Approval.subject_type == subject_type,
        Approval.subject_id == subject_id,
        Approval.proposed_action == action,
    )
    if revision is None:
        existing = db.scalar(existing_query.order_by(Approval.revision.desc()).limit(1))
        if existing:
            return existing
        revision = 1
    approval = Approval(
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        proposed_action=action,
        revision=revision,
        supporting_facts=dict(facts),
        evidence_hash=approval_evidence_hash(
            kind, subject_type, subject_id, action, facts
        ),
    )
    try:
        with db.begin_nested():
            db.add(approval)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            existing_query.order_by(Approval.revision.desc()).limit(1)
        )
        if existing:
            return existing
        raise
    audit(
        db,
        "approval",
        approval.id,
        "approval_requested",
        details={"action": action, "revision": revision},
        correlation_id=facts.get("correlation_id"),
    )
    return approval


def current_approval_facts(db, approval):
    """Build the current source facts relevant to this approval's action."""
    facts = approval.supporting_facts or {}
    if approval.subject_type == "case":
        case = db.get(Case, approval.subject_id)
        if not case:
            return None
        order = db.get(Order, case.order_id) if case.order_id else None
        category = case.category
        policy = settings.store_policies.get(category)
        if not policy:
            return None
        return {
            "order_number": order.order_number if order else None,
            "order_id": order.id if order else None,
            "customer_email": case.customer_email,
            "case_order_ref": case.order_ref,
            "order_email": order.email if order else None,
            "customer_message": case.message,
            "policy": policy,
            "order_fulfillment_status": order.fulfillment_status if order else None,
            "order_financial_status": order.financial_status if order else None,
            "order_total": f"{order.currency} {order.total}" if order else None,
            "order_source_updated_at": order.source_updated_at.isoformat()
            if order and order.source_updated_at
            else None,
            "category": category,
            "confidence": case.confidence,
            "draft": case.draft,
            "missing_info": case.missing_info or "",
            "case_status": case.status,
            "correlation_id": case.correlation_id
            or facts.get("correlation_id")
            or f"case:{case.id}",
        }
    if approval.subject_type == "order" and approval.proposed_action == "review_delayed_order":
        order = db.get(Order, approval.subject_id)
        if not order:
            return None
        return {
            "order_number": order.order_number,
            "order_id": order.id,
            "hours_threshold": settings.order_delay_hours,
            "created_at": order.created_at.isoformat(),
            "order_fulfillment_status": order.fulfillment_status,
            "order_financial_status": order.financial_status,
            "order_total": f"{order.currency} {order.total}",
            "order_source_updated_at": order.source_updated_at.isoformat(),
            "correlation_id": facts.get("correlation_id")
            or f"order:{order.id}",
        }
    return None


def lock_approval_sources(db, approval):
    """Lock the facts an approval depends on until its decision/execution commits.

    Order ingestion and support triage use the same row locks when updating these
    records. This closes the read-check-act gap in PostgreSQL transactions.
    """
    if approval.subject_type == "case":
        case = db.scalar(
            select(Case).where(Case.id == approval.subject_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if case and case.order_id:
            db.scalar(
                select(Order).where(Order.id == case.order_id)
                .with_for_update().execution_options(populate_existing=True)
            )
    elif approval.subject_type == "order":
        db.scalar(
            select(Order).where(Order.id == approval.subject_id)
            .with_for_update().execution_options(populate_existing=True)
        )


def lock_approval_lifecycle(db, approval_id):
    """Lock every revision for one subject in a stable order.

    PostgreSQL's transaction advisory lock serializes creation and execution even
    when two callers begin with different approval rows. Flushing first prevents
    a refresh from discarding pending ORM changes via ``populate_existing``.
    """
    db.flush()
    candidate = db.get(Approval, approval_id)
    if not candidate:
        raise LookupError("approval not found")
    # One business subject owns the lifecycle even if an action is revised to a
    # different kind (for example, a reply approval becoming an exception review).
    identity = f"{candidate.subject_type}:{candidate.subject_id}"
    if db.bind.dialect.name == "postgresql":
        lock_key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": lock_key})
    revisions = db.scalars(
        select(Approval)
        .where(
            Approval.subject_type == candidate.subject_type,
            Approval.subject_id == candidate.subject_id,
        )
        .order_by(Approval.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return next(item for item in revisions if item.id == approval_id)


def subject_execution_keys(subject_type, subject_id, excluding=None):
    """Select execution keys in SQL without loading every subject revision."""
    query = select(literal("approval:") + cast(Approval.id, String)).where(
        Approval.subject_type == subject_type, Approval.subject_id == subject_id,
    )
    return query.where(Approval.id != excluding) if excluding is not None else query


def prior_terminal_execution(db, approval):
    latest_reconciliation = aliased(ExecutionReconciliation)
    successor = aliased(ExecutionReconciliation)
    confirmed_nonexecution = exists(
        select(1).select_from(latest_reconciliation).where(
            latest_reconciliation.action_execution_id == ActionExecution.id,
            latest_reconciliation.outcome == "confirmed_not_executed",
            ~exists(select(1).select_from(successor).where(
                successor.previous_reconciliation_id == latest_reconciliation.id
            )),
        )
    )
    return db.scalar(
        select(ActionExecution).where(
            ActionExecution.idempotency_key.in_(subject_execution_keys(
                approval.subject_type, approval.subject_id, excluding=approval.id
            )),
            ActionExecution.state.in_(["started", "succeeded", "ambiguous"]),
            ~confirmed_nonexecution,
        ).order_by(ActionExecution.id.desc())
    )


def execution_reconciliation_history(db, execution_id, page=1):
    """Return a bounded history page and its total for one execution."""
    total = db.scalar(select(func.count()).select_from(ExecutionReconciliation).where(
        ExecutionReconciliation.action_execution_id == execution_id
    )) or 0
    pages = max(1, (total + 19) // 20)
    page = min(max(1, int(page or 1)), pages)
    rows = db.scalars(
        select(ExecutionReconciliation)
        .where(ExecutionReconciliation.action_execution_id == execution_id)
        .order_by(ExecutionReconciliation.revision.desc(), ExecutionReconciliation.id.desc())
        .offset((page - 1) * 20)
        .limit(20)
    ).all()
    rows.reverse()
    return rows, total, page, pages


def approval_freshness_differences(db, approval):
    """Compare the reviewed snapshot with both its hash and current source state."""
    snapshot = approval.supporting_facts or {}
    differences = []
    current_hash = current_approval_evidence_hash(approval)
    if approval.evidence_hash and approval.evidence_hash != current_hash:
        differences.append("The persisted approval snapshot no longer matches its evidence hash.")

    current = current_approval_facts(db, approval)
    if current is None:
        differences.append("The source order or support case is missing or no longer usable.")
        return differences

    if approval.subject_type == "case":
        required = (
            "order_number",
            "order_id",
            "customer_email",
            "case_order_ref",
            "order_email",
            "customer_message",
            "policy",
            "order_fulfillment_status",
            "order_financial_status",
            "order_total",
            "order_source_updated_at",
            "category",
            "confidence",
            "draft",
            "missing_info",
            "case_status",
        )
    else:
        required = (
            "order_number",
            "order_id",
            "hours_threshold",
            "created_at",
            "order_fulfillment_status",
            "order_financial_status",
            "order_total",
            "order_source_updated_at",
        )
    for field in required:
        if field not in snapshot:
            differences.append(f"The reviewed snapshot is missing {field.replace('_', ' ')}.")
        elif snapshot.get(field) != current.get(field):
            same_timestamp = False
            if field.endswith(("_at", "created_at")):
                try:
                    same_timestamp = as_utc(datetime.fromisoformat(snapshot[field])) == as_utc(datetime.fromisoformat(current[field]))
                except (TypeError, ValueError):
                    pass
            if not same_timestamp:
                differences.append(f"{field.replace('_', ' ').capitalize()} changed since review.")

    if approval.subject_type == "case" and approval.proposed_action.startswith("review_"):
        expected_category = approval.proposed_action.removeprefix("review_").removesuffix("_request")
        if current.get("category") != expected_category:
            differences.append("The current inquiry no longer matches the approved exception type.")
    if approval.proposed_action == "review_delayed_order":
        order = db.get(Order, approval.subject_id)
        if order and (
            order.fulfillment_status != "unfulfilled"
            or as_utc(order.created_at) > utcnow() - timedelta(hours=settings.order_delay_hours)
        ):
            differences.append("The order no longer meets the current delayed-order rule.")
    if (
        approval.subject_type == "case"
        and approval.proposed_action == "send_reply"
        and approval.execution_status == "succeeded"
        and current.get("case_status") == "resolved_mock"
    ):
        differences = [difference for difference in differences if not difference.startswith("Case status changed")]
    return list(dict.fromkeys(differences))


def approval_freshness_comparison(db, approval):
    """Return reviewable old/current values alongside the freshness reasons."""
    snapshot = approval.supporting_facts or {}
    current = current_approval_facts(db, approval) or {}
    differences = approval_freshness_differences(db, approval)
    comparisons = []
    fields = (
        (
            "order_number", "order_id", "customer_email", "case_order_ref", "order_email",
            "customer_message", "policy", "order_fulfillment_status", "order_financial_status",
            "order_total", "order_source_updated_at", "category", "confidence", "draft",
            "missing_info", "case_status",
        )
        if approval.subject_type == "case"
        else ("order_number", "order_id", "hours_threshold", "created_at", "order_fulfillment_status", "order_financial_status", "order_total", "order_source_updated_at")
    )
    for field in fields:
        old = snapshot.get(field)
        new = current.get(field)
        same = old == new
        if same and field.endswith(("_at", "created_at")) and old:
            try:
                same = as_utc(datetime.fromisoformat(old)) == as_utc(datetime.fromisoformat(new))
            except (TypeError, ValueError):
                pass
        if field not in snapshot or (current and not same):
            title = field.replace("_", " ").capitalize()
            reason = next((message for message in differences if message.lower().startswith(title.lower())), None)
            comparisons.append({
                "field": title,
                "reviewed": old if field in snapshot else "Not captured",
                "current": new if current else "Source unavailable",
                "reason": reason or ("Missing from the reviewed snapshot" if field not in snapshot else "Changed since review"),
            })
    represented = {item["reason"] for item in comparisons}
    for message in differences:
        # Rule-level and snapshot-integrity changes are not ordinary source fields.
        if message not in represented:
            if message.startswith("The persisted approval snapshot"):
                comparisons.append({"field": "Snapshot integrity", "reviewed": approval.evidence_hash or "No hash", "current": current_approval_evidence_hash(approval), "reason": message})
            elif "delay" in message.lower() or "rule" in message.lower():
                order = db.get(Order, approval.subject_id) if approval.subject_type == "order" else None
                comparisons.append({"field": "Current delay eligibility", "reviewed": "Eligible when reviewed", "current": f"{order.fulfillment_status}; created {order.created_at.isoformat()}" if order else "Not eligible or source unavailable", "reason": message})
            elif not comparisons:
                comparisons.append({"field": "Source record", "reviewed": "Captured approval facts", "current": "Unavailable", "reason": message})
    return comparisons


def replacement_approval_facts(db, approval, actor):
    current = current_approval_facts(db, approval)
    if current is None:
        raise ValueError("The source record is missing; a replacement approval cannot be created.")
    facts = approval.supporting_facts or {}
    if approval.subject_type == "case":
        case = db.get(Case, approval.subject_id)
        order = db.get(Order, case.order_id) if case.order_id else None
        category = case.category
        if approval.kind == "exception":
            if category not in ("refund", "cancellation"):
                raise ValueError("The current inquiry no longer supports a refund or cancellation review.")
            action = f"review_{category}_request"
        else:
            action = "send_reply"
        policy = settings.store_policies[category]
        order_summary = (
            f"Order {order.order_number} is {order.fulfillment_status}; total {order.currency} {order.total}."
            if order
            else "No matching order was found."
        )
        opener = "Thanks for contacting us."
        draft = f"{opener} {order_summary} {policy}"
        required = []
        if not order:
            required.append("Verified order reference")
        if category == "damaged_item":
            required.append("Photo or item details")
        if case.confidence < 60:
            required.append("Uncertain classification — operator review required")
        missing = "; ".join(required)
        case.draft = draft
        case.missing_info = missing
        case.status = "needs_review"
        audit(
            db,
            "case",
            case.id,
            "support_draft_refreshed_for_approval",
            actor,
            details={"previous_approval_id": approval.id},
            correlation_id=case.correlation_id or facts.get("correlation_id"),
        )
        current = current_approval_facts(db, approval)
        current["draft"] = draft
        current["missing_info"] = missing
    else:
        order = db.get(Order, approval.subject_id)
        if (
            approval.proposed_action != "review_delayed_order"
            or order.fulfillment_status != "unfulfilled"
            or as_utc(order.created_at) > utcnow() - timedelta(hours=settings.order_delay_hours)
        ):
            raise ValueError("The order no longer meets the delayed-order rule; no replacement was created.")
        action = approval.proposed_action
    current["correlation_id"] = facts.get("correlation_id") or current.get("correlation_id")
    return action, current


def refresh_approval(db, approval_id, actor):
    """Create a new pending revision while preserving the original approval record."""
    approval = lock_approval_lifecycle(db, approval_id)
    if approval.replacement_approval_id:
        return db.get(Approval, approval.replacement_approval_id)
    reconciled_not_executed = (
        approval.status == "approved"
        and approval.execution_status == "reconciled_not_executed"
    )
    eligible = approval.status == "pending" or (
        approval.status == "approved" and approval.execution_status == "evidence_changed"
    ) or reconciled_not_executed
    if not eligible:
        raise ValueError("Only a pending, stale, or confirmed-not-executed approval can be refreshed.")
    lock_approval_sources(db, approval)
    prior_execution = prior_terminal_execution(db, approval)
    if not reconciled_not_executed:
        prior_execution = prior_execution or db.scalar(
            select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{approval.id}"
            )
        )
    if prior_execution and prior_execution.state in ("started", "succeeded", "ambiguous"):
        raise ValueError(
            "A prior execution is completed, started, or uncertain; inspect its outcome before creating a replacement."
        )
    differences = approval_freshness_differences(db, approval)
    if not differences and not reconciled_not_executed:
        raise ValueError("The approval evidence is current; refresh is not needed.")
    action, facts = replacement_approval_facts(db, approval, actor)
    if (approval.kind, approval.subject_type, action) not in ACTION_CONTRACTS:
        raise ValueError("The current action has no registered mock adapter.")
    latest_revision = db.scalar(
        select(func.max(Approval.revision)).where(
            Approval.kind == approval.kind,
            Approval.subject_type == approval.subject_type,
            Approval.subject_id == approval.subject_id,
        )
    ) or 0
    replacement = create_approval(
        db,
        approval.kind,
        approval.subject_type,
        approval.subject_id,
        action,
        facts,
        revision=latest_revision + 1,
    )
    approval.replacement_approval_id = replacement.id
    if approval.status == "pending":
        approval.status = "superseded"
    audit(
        db,
        "approval",
        approval.id,
        "approval_replaced_with_fresh_evidence",
        actor,
        details={"replacement_approval_id": replacement.id, "differences": differences},
        correlation_id=facts.get("correlation_id"),
    )
    audit(
        db,
        "approval",
        replacement.id,
        "approval_refreshed_from_previous_revision",
        actor,
        details={"previous_approval_id": approval.id},
        correlation_id=facts.get("correlation_id"),
    )
    db.commit()
    return replacement


def decide_approval(db, approval_id, decision, actor, on_approved=None):
    if decision not in ("approved", "rejected"):
        raise ValueError("invalid decision")
    approval = lock_approval_lifecycle(db, approval_id)
    if approval.status != "pending":
        return approval
    lock_approval_sources(db, approval)
    contract = (approval.kind, approval.subject_type, approval.proposed_action)
    differences = approval_freshness_differences(db, approval)
    if differences and decision == "approved":
        audit(
            db,
            "approval",
            approval.id,
            "approval_decision_blocked_stale_evidence",
            actor,
            details={"differences": differences},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.commit()
        raise ApprovalEvidenceStale(differences)
    current_hash = current_approval_evidence_hash(approval)
    if not approval.evidence_hash:
        approval.evidence_hash = current_hash
        audit(
            db,
            "approval",
            approval.id,
            "legacy_evidence_sealed",
            actor,
            details={"evidence_hash": current_hash},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
    approval.status = decision
    approval.actor = actor
    approval.decision_at = utcnow()
    audit(
        db,
        "approval",
        approval.id,
        f"approval_{decision}",
        actor,
        correlation_id=approval.supporting_facts.get("correlation_id"),
    )
    if decision == "approved":
        approval.execution_status = "queued"
        approval.next_attempt_at = utcnow()
        if on_approved:
            on_approved(db, approval, actor)
    db.commit()
    return approval


def retry_delay(attempt, retry_after=None):
    """Retry-After is a lower bound; exponential backoff is capped at 1 hour."""
    return max(
        min(3600, settings.retry_base_seconds * 2 ** min(attempt - 1, 10)),
        retry_after or 0,
    )


def queue_approval_retry(db, approval_id, actor):
    """Queue one operator-authorized retry under the subject lifecycle lock."""
    approval = lock_approval_lifecycle(db, approval_id)
    if approval.replacement_approval_id:
        raise ValueError("This approval was replaced and cannot be retried")
    is_resolved_block = approval.execution_status == "blocked_prior_outcome"
    if approval.status != "approved" or approval.execution_status not in (
        "failed", "retryable", "exhausted", "blocked_prior_outcome",
    ):
        raise ValueError("Action is not retryable; ambiguous outcomes require reconciliation")
    current_execution = db.scalar(
        select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval.id}"
        )
    )
    if current_execution and current_execution.state in ("started", "succeeded", "ambiguous"):
        raise ValueError("The recorded execution may have completed; inspect its outcome before retrying")
    if prior_terminal_execution(db, approval):
        raise ValueError("Another approval revision has a terminal or uncertain outcome; inspect it before retrying")
    if is_resolved_block:
        lock_approval_sources(db, approval)
        differences = approval_freshness_differences(db, approval)
        if differences:
            approval.execution_status = "evidence_changed"
            approval.failure_detail = "The earlier execution was confirmed not executed, but this approval's evidence is stale. Review current facts before creating a fresh approval."
            audit(db, "approval", approval.id, "resolved_block_stale_evidence", actor,
                  details={"differences": differences},
                  correlation_id=(approval.supporting_facts or {}).get("correlation_id"))
            db.commit()
            return approval
        if (approval.kind, approval.subject_type, approval.proposed_action) not in ACTION_CONTRACTS:
            approval.execution_status = "invalid_action"
            approval.failure_detail = "Execution blocked: the approved action has no registered mock adapter."
            audit(db, "approval", approval.id, "action_execution_invalid_action", actor,
                  details={"action": approval.proposed_action},
                  correlation_id=(approval.supporting_facts or {}).get("correlation_id"))
            db.commit()
            return approval
    approval.execution_status = "queued"
    approval.failure_detail = None
    if approval.next_attempt_at is None:
        approval.next_attempt_at = utcnow()
    audit(db, "approval", approval.id,
          "prior_outcome_resolved_requeued" if is_resolved_block else "manual_retry_queued", actor)
    db.commit()
    return approval


def reconcile_approval_execution(
    db, approval_id, outcome, explanation, reference, actor, synthetic=False
):
    """Append an operator assertion without changing the original execution."""
    outcomes = {"confirmed_completed", "confirmed_not_executed", "still_unknown"}
    if outcome not in outcomes:
        raise ValueError("Choose a valid reconciliation outcome")
    explanation = (explanation or "").strip()
    reference = (reference or "").strip() or None
    if len(explanation) < 10 or len(explanation) > 2000:
        raise ValueError("Provide an explanation between 10 and 2000 characters")
    if reference and len(reference) > 240:
        raise ValueError("Supporting reference must be 240 characters or fewer")
    if outcome in ("confirmed_completed", "confirmed_not_executed") and not reference:
        raise ValueError("A supporting reference is required for a confirmed outcome")

    approval = lock_approval_lifecycle(db, approval_id)
    execution = db.scalar(
        select(ActionExecution)
        .where(ActionExecution.idempotency_key == f"approval:{approval.id}")
        .with_for_update()
    )
    if not execution:
        raise ValueError("No persisted execution is available to reconcile")
    latest = db.scalar(
        select(ExecutionReconciliation)
        .where(ExecutionReconciliation.action_execution_id == execution.id)
        .order_by(ExecutionReconciliation.revision.desc(), ExecutionReconciliation.id.desc())
        .limit(1)
    )
    asserted_outcome = outcome
    if latest:
        same_request = (
            (latest.asserted_outcome or latest.outcome) == outcome
            and latest.explanation == explanation
            and latest.reference == reference
            and latest.actor == actor
            and latest.synthetic == bool(synthetic)
        )
        if same_request:
            return latest
    if approval.status != "approved" or execution.state != "ambiguous":
        raise ValueError("Only an approved ambiguous execution can be reconciled")
    if approval.execution_status not in (
        "ambiguous", "disputed", "reconciled_completed", "reconciled_not_executed"
    ):
        raise ValueError("This approval is not in a state that supports reconciliation")

    prior_definitive = db.scalar(
        select(ExecutionReconciliation)
        .where(
            ExecutionReconciliation.action_execution_id == execution.id,
            ExecutionReconciliation.outcome.in_(("confirmed_completed", "confirmed_not_executed")),
        )
        .order_by(ExecutionReconciliation.revision.desc(), ExecutionReconciliation.id.desc())
        .limit(1)
    )
    conflict = bool(
        latest
        and latest.outcome != "conflicting_evidence"
        and prior_definitive
        and prior_definitive.outcome != outcome
    )
    if conflict:
        asserted_outcome = outcome
        outcome = "conflicting_evidence"
    revision = (latest.revision + 1) if latest else 1

    correlation_id = (approval.supporting_facts or {}).get("correlation_id") or f"approval:{approval.id}"
    reconciliation = ExecutionReconciliation(
        action_execution_id=execution.id,
        approval_id=approval.id,
        revision=revision,
        previous_reconciliation_id=latest.id if latest else None,
        outcome=outcome,
        asserted_outcome=asserted_outcome if conflict else None,
        explanation=explanation,
        reference=reference,
        actor=actor,
        correlation_id=correlation_id,
        synthetic=bool(synthetic),
    )
    db.add(reconciliation)
    approval.execution_status = {
        "confirmed_completed": "reconciled_completed",
        "confirmed_not_executed": "reconciled_not_executed",
        "still_unknown": "ambiguous",
        "conflicting_evidence": "disputed",
    }[outcome]
    approval.failure_detail = (
        "Operator recorded the outcome as completed; the original execution remains ambiguous and no provider was queried."
        if outcome == "confirmed_completed"
        else "Operator recorded that no execution occurred; a new human approval is required before another mock action."
        if outcome == "confirmed_not_executed"
        else f"Conflicting evidence: previous conclusion was {prior_definitive.outcome}; the new assertion is {asserted_outcome}. Keep action recovery blocked until a new supported conclusion is recorded."
        if outcome == "conflicting_evidence"
        else "The outcome remains unknown; retries and replacement approvals remain blocked."
    )
    audit(
        db,
        "approval",
        approval.id,
        "execution_outcome_reconciled",
        actor,
        details={
            "execution_id": execution.id,
            "outcome": outcome,
            "asserted_outcome": asserted_outcome if conflict else None,
            "revision": revision,
            "previous_reconciliation_id": latest.id if latest else None,
            "explanation": explanation,
            "reference": reference,
            "original_execution_state": execution.state,
            "synthetic": bool(synthetic),
        },
        correlation_id=correlation_id,
    )
    db.commit()
    return reconciliation


def execute_approved(db, approval, mock_outcome=None):
    """Run an approved mock action; ``mock_outcome`` is preview-only injection."""
    approval = lock_approval_lifecycle(db, approval.id)
    if approval.status != "approved":
        raise PermissionError("operator approval is required")
    if approval.replacement_approval_id:
        if approval.execution_status in (
            "succeeded", "ambiguous", "disputed", "reconciled_completed", "reconciled_not_executed"
        ):
            return
        approval.execution_status = "superseded"
        approval.failure_detail = "Execution blocked: this approval has been replaced by a newer revision."
        audit(
            db, "approval", approval.id, "action_execution_blocked_replaced_approval",
            details={"replacement_approval_id": approval.replacement_approval_id},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.flush()
        return
    lock_approval_sources(db, approval)
    key = f"approval:{approval.id}"
    execution = db.scalar(
        select(ActionExecution)
        .where(ActionExecution.idempotency_key == key)
        .with_for_update()
    )
    if execution and (
        execution.proposed_action not in (None, approval.proposed_action)
        or execution.evidence_hash not in (None, approval.evidence_hash)
    ):
        approval.execution_status = "evidence_changed"
        approval.failure_detail = "Execution blocked: the persisted execution does not match the reviewed action and evidence."
        audit(
            db,
            "approval",
            approval.id,
            "action_execution_contract_mismatch",
            details={"action": approval.proposed_action},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.flush()
        return
    if execution and execution.state in ("succeeded", "ambiguous"):
        if approval.execution_status not in ("disputed", "reconciled_completed", "reconciled_not_executed"):
            approval.execution_status = execution.state
        return
    prior_execution = prior_terminal_execution(db, approval)
    if prior_execution:
        approval.execution_status = "blocked_prior_outcome"
        approval.failure_detail = (
            "Execution blocked: another approval revision has a started, successful, "
            "or ambiguous outcome. Inspect that execution before taking further action."
        )
        audit(
            db, "approval", approval.id, "action_execution_blocked_prior_outcome",
            details={"prior_execution_id": prior_execution.id, "prior_state": prior_execution.state},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.flush()
        return
    contract = (approval.kind, approval.subject_type, approval.proposed_action)
    current_hash = current_approval_evidence_hash(approval)
    if contract not in ACTION_CONTRACTS:
        approval.execution_status = "invalid_action"
        approval.failure_detail = "Execution blocked: the approved action has no registered mock adapter."
        audit(
            db,
            "approval",
            approval.id,
            "action_execution_invalid_action",
            details={"action": approval.proposed_action},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.flush()
        return
    differences = approval_freshness_differences(db, approval)
    if differences:
        approval.execution_status = "evidence_changed"
        approval.failure_detail = "Execution blocked: evidence changed after the operator decision or source facts became stale. Create a fresh approval from current source facts."
        audit(
            db,
            "approval",
            approval.id,
            "action_execution_evidence_changed",
            details={
                "current_evidence_hash": current_hash,
                "action": approval.proposed_action,
                "differences": differences,
            },
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
        db.flush()
        return
    if not approval.evidence_hash:
        approval.evidence_hash = current_hash
        audit(
            db,
            "approval",
            approval.id,
            "legacy_evidence_sealed_at_execution",
            details={"evidence_hash": current_hash},
            correlation_id=approval.supporting_facts.get("correlation_id"),
        )
    if approval.execution_status not in ("queued", "retryable"):
        return
    if approval.next_attempt_at and as_utc(approval.next_attempt_at) > utcnow():
        return
    if not execution:
        execution = ActionExecution(
            idempotency_key=key,
            proposed_action=approval.proposed_action,
            evidence_hash=approval.evidence_hash,
            state="started",
            attempts=0,
        )
        db.add(execution)
        db.flush()
    elif execution:
        execution.proposed_action = approval.proposed_action
        execution.evidence_hash = approval.evidence_hash
    execution.attempts += 1
    approval.attempts = execution.attempts
    if mock_outcome == "ambiguous" or (mock_outcome is None and settings.mock_action_ambiguous):
        execution.state = "ambiguous"
        execution.detail = "Simulated timeout after submission. Confirm provider state before resolving; automatic retry is blocked."
    elif mock_outcome in ("retryable", "exhausted"):
        execution.state = mock_outcome
        execution.detail = "Simulated transient failure; no external request was made."
    elif mock_outcome is None and execution.attempts <= settings.mock_action_failures:
        execution.state = (
            "retryable"
            if execution.attempts < settings.max_action_attempts
            else "exhausted"
        )
        execution.detail = "Simulated transient failure; no external request was made."
    else:
        execution.state = "succeeded"
        execution.detail = "Mock action completed; no external API called."
    execution.updated_at = utcnow()
    approval.execution_status = execution.state
    approval.failure_detail = (
        execution.detail if execution.state != "succeeded" else None
    )
    approval.next_attempt_at = (
        utcnow()
        + timedelta(
            seconds=retry_delay(execution.attempts, settings.mock_retry_after_seconds)
        )
        if execution.state == "retryable"
        else None
    )
    audit(
        db,
        "approval",
        approval.id,
        "action_execution_" + execution.state,
        details={
            "attempt": execution.attempts,
            "action": approval.proposed_action,
            "evidence_hash": approval.evidence_hash,
        },
        correlation_id=approval.supporting_facts.get("correlation_id"),
    )
    if (
        execution.state == "succeeded"
        and approval.subject_type == "case"
        and approval.proposed_action == "send_reply"
    ):
        case = db.get(Case, approval.subject_id)
        if case:
            case.status = "resolved_mock"
    db.flush()


def queue_support(db, message, order_ref, email, request_key=None):
    key = request_key or str(uuid.uuid4())
    existing = db.scalar(select(Case).where(Case.request_key == key))
    if existing:
        if not _same_support_request(existing, message, order_ref, email):
            raise SupportRequestKeyConflict(
                "request_key is already associated with different inquiry details"
            )
        return existing
    case = Case(
        message=message,
        order_ref=order_ref,
        customer_email=email,
        request_key=key,
        correlation_id=key,
        status="queued",
    )
    try:
        with db.begin_nested():
            db.add(case)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Case).where(Case.request_key == key))
        if existing and _same_support_request(existing, message, order_ref, email):
            return existing
        raise SupportRequestKeyConflict(
            "request_key is already associated with different inquiry details"
        )
    audit(db, "case", case.id, "inquiry_queued", correlation_id=key)
    db.commit()
    return case


def _same_support_request(case, message, order_ref, email):
    return (
        case.message == message
        and case.order_ref == (order_ref or None)
        and (case.customer_email or "").strip().casefold()
        == (email or "").strip().casefold()
    )


def process_support(db, case):
    case.attempts += 1
    try:
        return create_support_case(
            db, case.message, case.order_ref, case.customer_email, case=case
        )
    except Exception as exc:
        case_id = case.id
        db.rollback()
        case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
        case.attempts += 1
        case.status = "exhausted" if case.attempts >= 3 else "failed"
        case.failure_detail = type(exc).__name__
        case.next_attempt_at = (
            utcnow() + timedelta(seconds=retry_delay(case.attempts))
            if case.status == "failed"
            else None
        )
        audit(
            db,
            "case",
            case.id,
            "triage_failed",
            details={"error_type": type(exc).__name__},
            correlation_id=case.correlation_id,
        )
        db.commit()
        return case

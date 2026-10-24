import hashlib, logging, uuid
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.config import settings
from app.integrations import verify_shopify, generate_support_content
from app.models import Order, Delivery, Case, Approval, Audit, ActionExecution, utcnow

log = logging.getLogger("commerceops")


class SupportRequestKeyConflict(ValueError):
    """A caller reused an idempotency key for different inquiry content."""


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
    if settings.llm_mode == "openai" and settings.openai_api_key:
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
        "category": category,
        "confidence": confidence,
        "draft": draft,
        "missing_info": missing,
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


def create_approval(db, kind, subject_type, subject_id, action, facts):
    existing = db.scalar(
        select(Approval).where(
            Approval.kind == kind,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
            Approval.proposed_action == action,
        )
    )
    if existing:
        return existing
    approval = Approval(
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        proposed_action=action,
        supporting_facts=facts,
    )
    try:
        with db.begin_nested():
            db.add(approval)
            db.flush()
    except IntegrityError:
        return db.scalar(
            select(Approval).where(
                Approval.kind == kind,
                Approval.subject_type == subject_type,
                Approval.subject_id == subject_id,
                Approval.proposed_action == action,
            )
        )
    audit(
        db,
        "approval",
        approval.id,
        "approval_requested",
        details={"action": action},
        correlation_id=facts.get("correlation_id"),
    )
    return approval


def decide_approval(db, approval_id, decision, actor):
    approval = db.scalar(
        select(Approval)
        .where(Approval.id == approval_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if decision not in ("approved", "rejected"):
        raise ValueError("invalid decision")
    if not approval:
        raise LookupError("approval not found")
    if approval.status != "pending":
        return approval
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
    db.commit()
    return approval


def retry_delay(attempt, retry_after=None):
    """Retry-After is a lower bound; exponential backoff is capped at 1 hour."""
    return max(
        min(3600, settings.retry_base_seconds * 2 ** min(attempt - 1, 10)),
        retry_after or 0,
    )


def execute_approved(db, approval):
    approval = db.scalar(
        select(Approval)
        .where(Approval.id == approval.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval.status != "approved":
        raise PermissionError("operator approval is required")
    key = f"approval:{approval.id}"
    execution = db.scalar(
        select(ActionExecution)
        .where(ActionExecution.idempotency_key == key)
        .with_for_update()
    )
    if execution and execution.state in ("succeeded", "ambiguous"):
        approval.execution_status = execution.state
        return
    if approval.execution_status not in ("queued", "retryable"):
        return
    if approval.next_attempt_at and as_utc(approval.next_attempt_at) > utcnow():
        return
    if not execution:
        execution = ActionExecution(idempotency_key=key, state="started", attempts=0)
        db.add(execution)
        db.flush()
    execution.attempts += 1
    approval.attempts = execution.attempts
    if settings.mock_action_ambiguous:
        execution.state = "ambiguous"
        execution.detail = "Simulated timeout after submission. Confirm provider state before resolving; automatic retry is blocked."
    elif execution.attempts <= settings.mock_action_failures:
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
        details={"attempt": execution.attempts},
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

from datetime import datetime, timezone
from sqlalchemy import (
    String,
    Text,
    DateTime,
    Integer,
    Boolean,
    CheckConstraint,
    ForeignKey,
    UniqueConstraint,
    JSON,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_domain: Mapped[str] = mapped_column(String(255), default="demo.myshopify.com")
    external_id: Mapped[str] = mapped_column(String(100), index=True)
    order_number: Mapped[str] = mapped_column(String(100), index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    total: Mapped[str] = mapped_column(String(32), default="0.00")
    fulfillment_status: Mapped[str] = mapped_column(String(40), default="unfulfilled")
    financial_status: Mapped[str] = mapped_column(String(40), default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("shop_domain", "external_id", name="uq_order_external"),
        Index("ix_orders_delay_scan", "fulfillment_status", "created_at", "id"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    webhook_id: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    event_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    topic: Mapped[str] = mapped_column(String(80))
    shop_domain: Mapped[str] = mapped_column(String(255))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="received")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Case(Base):
    __tablename__ = "support_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    order: Mapped[Order | None] = relationship()
    request_key: Mapped[str | None] = mapped_column(
        String(150), unique=True, nullable=True
    )
    order_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="other")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="needs_review")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[int] = mapped_column(Integer)
    proposed_action: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    replacement_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id"), nullable=True
    )
    supporting_facts: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_status: Mapped[str] = mapped_column(String(30), default="not_started")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "subject_type",
            "subject_id",
            "proposed_action",
            "revision",
            name="uq_approval_revision",
        ),
    )


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(150), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(120), default="system")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ActionExecution(Base):
    __tablename__ = "action_executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    proposed_action: Mapped[str | None] = mapped_column(String(80), nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="started")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExecutionReconciliation(Base):
    __tablename__ = "execution_reconciliations"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_reconciliation_revision"),
        CheckConstraint(
            "outcome IN ('confirmed_completed', 'confirmed_not_executed', 'still_unknown', 'conflicting_evidence')",
            name="ck_reconciliation_outcome",
        ),
        CheckConstraint(
            "outcome IN ('still_unknown', 'conflicting_evidence') OR reference IS NOT NULL",
            name="ck_reconciliation_reference",
        ),
        CheckConstraint(
            "(outcome = 'conflicting_evidence' AND asserted_outcome IN ('confirmed_completed', 'confirmed_not_executed', 'still_unknown')) OR (outcome != 'conflicting_evidence' AND asserted_outcome IS NULL)",
            name="ck_reconciliation_asserted_outcome",
        ),
        UniqueConstraint("action_execution_id", "revision", name="uq_reconciliation_execution_revision"),
        UniqueConstraint("previous_reconciliation_id", name="uq_reconciliation_previous"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    action_execution_id: Mapped[int] = mapped_column(
        ForeignKey("action_executions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    approval_id: Mapped[int] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    previous_reconciliation_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_reconciliations.id", ondelete="RESTRICT"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(32))
    asserted_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    explanation: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(240), nullable=True)
    actor: Mapped[str] = mapped_column(String(120))
    correlation_id: Mapped[str] = mapped_column(String(150), index=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_approval_queue", Approval.status, Approval.execution_status)

"""Regression for a second worker winning the row after a rollback."""
from datetime import timedelta
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Delivery, utcnow
from app import services


def test_rollback_does_not_consume_competing_attempt(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        delivery = Delivery(webhook_id="race", topic="orders/create", shop_domain="demo.myshopify.com", payload={})
        db.add(delivery)
        db.commit()
        key = delivery.id
        rollback = db.rollback
        def competing_rollback():
            rollback()
            # Model another worker acquiring the released lock and persisting
            # a failed attempt before the first worker re-locks the row.
            with Session(engine) as other:
                row = other.get(Delivery, key)
                row.attempts = 1
                row.status = "failed"
                row.next_attempt_at = utcnow()+timedelta(minutes=5)
                other.commit()
        monkeypatch.setattr(db, "rollback", competing_rollback)
        assert services.process_delivery(db, key)["status"] == "deferred"
        db.expire_all()
        row = db.get(Delivery, key)
        assert row.attempts == 1 and row.status == "failed"
        assert services.as_utc(row.next_attempt_at) > utcnow()
    engine.dispose()

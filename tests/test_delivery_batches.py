"""Batch API preserves the existing one-item response and bounded retry semantics."""
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.config import settings
from app.db import Base, get_db
from app.models import Delivery, Order, utcnow


def test_bounded_batch_and_legacy_single_item_contract(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    def override():
        with sessions() as db:
            yield db
    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(settings, "n8n_webhook_secret", "synthetic-batch-secret")
    monkeypatch.setattr(settings, "preview_mode", False)
    now = utcnow()
    with sessions() as db:
        db.add(Delivery(webhook_id="bad", topic="orders/create", shop_domain="demo.myshopify.com", payload={}))
        for i in range(4):
            key = f"batch-{i}"
            db.add(Delivery(webhook_id=key, topic="orders/create", shop_domain="demo.myshopify.com",
                            payload={"id":key,"created_at":now.isoformat(),"updated_at":now.isoformat()}))
        db.add(Delivery(webhook_id="future", topic="orders/create", shop_domain="demo.myshopify.com", payload={},
                        status="failed", attempts=1, next_attempt_at=now+timedelta(hours=1)))
        db.commit()
    client = TestClient(main.app)
    url = "/api/n8n/process-next"
    headers = {"X-N8N-Secret": "synthetic-batch-secret"}
    assert client.post(url+"?batch_size=3").status_code == 401
    assert client.post(url+"?batch_size=6", headers=headers).status_code == 422
    batch = client.post(url+"?batch_size=3", headers=headers).json()
    assert batch["status"] == "failed"
    assert batch["outcomes"]["processed"] == 2 and batch["outcomes"]["failed"] == 1
    single = client.post(url, headers=headers).json()
    assert single["status"] == "processed" and "outcomes" not in single
    assert client.post(url+"?batch_size=3", headers=headers).json()["outcomes"]["processed"] == 1
    with sessions() as db:
        assert len(list(db.scalars(select(Order)))) == 4
        future = db.scalar(select(Delivery).where(Delivery.webhook_id == "future"))
        assert future.attempts == 1 and future.status == "failed"
    engine.dispose()

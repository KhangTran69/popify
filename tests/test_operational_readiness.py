"""Privacy, readiness and operations regressions."""
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.config import settings
from app.db import Base, get_db
from app.models import Delivery, Order
from app.services import process_delivery


def test_minimized_signed_order_and_private_status(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    def override():
        with sessions() as db:
            yield db
    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(settings, "shopify_webhook_secret", "synthetic-secret")
    monkeypatch.setattr(settings, "shopify_store_domain", "demo.myshopify.com")
    client = TestClient(main.app, follow_redirects=False)
    assert client.get("/live").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert client.get("/api/ops/status").status_code == 401
    data = {"id": 1, "name": "#TEST", "email": "synthetic@example.test",
            "updated_at": "2026-11-01T00:00:00Z", "shipping_address": {"street": "private"},
            "note": "unneeded private note"}
    raw = json.dumps(data).encode()
    headers = {"X-Shopify-Hmac-SHA256": base64.b64encode(hmac.new(
        b"synthetic-secret", raw, hashlib.sha256).digest()).decode(),
        "X-Shopify-Webhook-Id": "privacy-check", "X-Shopify-Shop-Domain": "demo.myshopify.com"}
    response = client.post("/webhooks/shopify/orders", content=raw, headers=headers)
    assert response.status_code == 202
    with sessions() as db:
        delivery = db.scalar(select(Delivery))
        assert "shipping_address" not in delivery.payload and "note" not in delivery.payload
        assert process_delivery(db, delivery.id)["status"] == "processed"
        order = db.scalar(select(Order))
        assert order.email == "synthetic@example.test" and "note" not in order.payload
    assert client.post("/webhooks/shopify/orders", content=raw, headers={**headers,
        "X-Shopify-Hmac-SHA256": "invalid", "Accept": "text/html"}).status_code == 401
    engine.dispose()

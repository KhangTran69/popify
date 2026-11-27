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
        from scripts.privacy_inventory import inventory
        result = inventory(db, 90)
        assert result["records"]["orders"]["total"] == 1
        assert result["orders_with_email"] == 1
        assert "synthetic@example.test" not in json.dumps(result)
    assert client.post("/webhooks/shopify/orders", content=raw, headers={**headers,
        "X-Shopify-Hmac-SHA256": "invalid", "Accept": "text/html"}).status_code == 401
    engine.dispose()


def test_database_safe_text_fields():
    from app.privacy import valid_text_fields
    assert valid_text_fields("synthetic café", "", None)
    assert not valid_text_fields("bad\x00text")
    assert not valid_text_fields("\ud800")


def test_canonical_shop_hostname():
    import pytest
    from app.config import Settings, canonical_shop_domain
    assert canonical_shop_domain(" DEMO.MyShopify.com ") == "demo.myshopify.com"
    assert Settings(_env_file=None, shopify_store_domain="DEMO.MyShopify.com").shopify_store_domain == "demo.myshopify.com"
    for domain in ("https://demo.myshopify.com", "nested.demo.myshopify.com", "-demo.myshopify.com",
                   "demo.myshopify.com.evil.test", ".myshopify.com"):
        with pytest.raises(ValueError):
            canonical_shop_domain(domain)


def test_ambiguous_support_reference_does_not_disclose_order(monkeypatch):
    from app.models import Case, utcnow
    from app.services import create_support_case
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "shopify_store_domain", "demo.myshopify.com")
    monkeypatch.setattr(settings, "llm_mode", "mock")
    with sessions() as db:
        for external, display, total in (("one", "collision", "21.00"), ("collision", "two", "72.00")):
            db.add(Order(shop_domain="demo.myshopify.com", external_id=external, order_number=display,
                         email="synthetic@example.test", currency="USD", total=total,
                         fulfillment_status="unfulfilled", financial_status="paid",
                         created_at=utcnow(), source_updated_at=utcnow(), payload={}))
        db.commit()
        case = create_support_case(db, "Where is my order?", "collision", "synthetic@example.test")
        assert case.order_id is None
        assert "No matching order" in case.draft
        assert "21.00" not in case.draft and "72.00" not in case.draft
        assert "Verified order reference" in case.missing_info
    engine.dispose()


def test_readiness_detects_incomplete_schema(monkeypatch):
    from sqlalchemy import text
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    def override():
        with sessions() as db:
            yield db
    monkeypatch.setitem(main.app.dependency_overrides, get_db, override)
    monkeypatch.setattr(settings, "managed_identity", False)
    client = TestClient(main.app)
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json() == {"status": "not_ready"}
    engine.dispose()

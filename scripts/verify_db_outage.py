"""Verify signed intake fails safely during a disposable database outage."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
import httpx
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import Delivery, Order

KEY = "ci-db-interruption-retry"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("unavailable", "recovered"))
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    assert os.environ.get("POPIFY_ENV") == "development" and settings.llm_mode == "mock"
    assert settings.shopify_webhook_secret and not settings.shopify_admin_token
    raw = json.dumps({"id": KEY, "name": "#DB-INTERRUPTION", "financial_status": "paid",
                      "created_at": "2026-11-27T00:00:00Z", "updated_at": "2026-11-27T00:00:00Z"}).encode()
    headers = {"X-Shopify-Hmac-SHA256": base64.b64encode(hmac.new(
        settings.shopify_webhook_secret.encode(), raw, hashlib.sha256).digest()).decode(),
        "X-Shopify-Webhook-Id": KEY, "X-Shopify-Shop-Domain": "demo.myshopify.com",
        "X-Shopify-Topic": "orders/create"}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15) as client:
        def post():
            return client.post("/webhooks/shopify/orders", content=raw, headers=headers)
        if args.stage == "unavailable":
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(post)
                assert client.get("/live", timeout=3).status_code == 200
                response = pending.result(timeout=15)
            assert response.status_code == 503, response.status_code
            assert response.headers.get("X-Correlation-ID")
            assert response.headers.get("Retry-After") == "5"
            assert response.json() == {"detail": "Database temporarily unavailable. Try again later."}
            print("PASS: signed delivery was not acknowledged during database outage; liveness remained responsive")
            return
        response = post()
        assert response.status_code == 202, response.status_code
        delivery_id = response.json()["delivery_id"]
        assert post().status_code == 200
        response = client.post(f"/api/n8n/process-delivery/{delivery_id}", headers={"X-N8N-Secret": settings.n8n_webhook_secret})
        assert response.status_code == 200 and response.json()["status"] == "processed"
        assert client.post(f"/api/n8n/process-delivery/{delivery_id}", headers={"X-N8N-Secret": settings.n8n_webhook_secret}).json()["status"] == "duplicate"
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Delivery).where(Delivery.webhook_id == KEY)))) == 1
        assert len(list(db.scalars(select(Order).where(Order.external_id == KEY)))) == 1
    print("PASS: same signed delivery retried after database recovery; one delivery and one order persisted")


if __name__ == "__main__":
    main()

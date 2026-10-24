"""Send a locally signed synthetic Shopify-shaped webhook to the API."""

import base64, hashlib, hmac, json, uuid
from datetime import datetime, timezone
import httpx
from app.config import settings

if not settings.shopify_webhook_secret:
    raise SystemExit(
        "Set SHOPIFY_WEBHOOK_SECRET in .env first; use a local-only test secret, not a Shopify credential."
    )
now = datetime.now(timezone.utc).isoformat()
payload = {
    "id": f"LOCAL-{uuid.uuid4().hex[:8]}",
    "name": f"#LOCAL-{uuid.uuid4().hex[:5].upper()}",
    "email": "demo@example.test",
    "currency": "USD",
    "total_price": "42.00",
    "financial_status": "paid",
    "fulfillment_status": None,
    "created_at": now,
    "updated_at": now,
}
raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
signature = base64.b64encode(
    hmac.new(settings.shopify_webhook_secret.encode(), raw, hashlib.sha256).digest()
).decode()
response = httpx.post(
    "http://127.0.0.1:8001/webhooks/shopify/orders",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Shopify-Hmac-SHA256": signature,
        "X-Shopify-Webhook-Id": "demo-" + uuid.uuid4().hex,
        "X-Shopify-Shop-Domain": "demo.myshopify.com",
        "X-Shopify-Topic": "orders/create",
    },
    timeout=10,
)
print(response.status_code, response.text)

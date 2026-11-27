"""CI-only local Compose outage and scheduled recovery exercise."""
import argparse
import json
import os
import time
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Delivery, Order, utcnow
from app.services import audit, as_utc

KEY = "ci-n8n-outage-recovery"


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("queue", "pending", "recovered"))
    parser.add_argument("--key", default=KEY, choices=(KEY, "ci-n8n-restored-volume"))
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    key = args.key
    if os.environ.get("POPIFY_ENV", "").lower() != "development":
        parser.error("Only a disposable development stack is supported")
    if args.stage == "queue":
        with SessionLocal() as db:
            if db.scalar(select(Delivery).where(Delivery.webhook_id == key)):
                raise RuntimeError("Fixture already exists; use a fresh disposable stack")
            now = utcnow()
            delivery = Delivery(webhook_id=key, topic="orders/create", shop_domain="demo.myshopify.com",
                payload={"id": key, "name": "#OUTAGE-CHECK", "created_at": now.isoformat(),
                         "updated_at": now.isoformat()}, status="received")
            db.add(delivery)
            db.flush()
            audit(db, "delivery", delivery.id, "synthetic_delivery_received", correlation_id=key)
            db.commit()
        print("PASS: queued durable synthetic record while n8n is stopped")
        return
    deadline = time.monotonic() + 150
    while True:
        with SessionLocal() as db:
            delivery = db.scalar(select(Delivery).where(Delivery.webhook_id == key))
            assert delivery, "Durable delivery was lost"
            orders = list(db.scalars(select(Order).where(Order.external_id == key)))
            if args.stage == "pending":
                assert delivery.status == "received" and not orders
                print("PASS: queued record survived API restart while n8n remained stopped")
                return
            if delivery.status == "processed":
                assert len(orders) == 1 and delivery.processed_at
                print(json.dumps({"scheduled_recovery": "passed", "orders": len(orders),
                    "receipt_to_persistence_seconds_including_outage":
                    round((as_utc(delivery.processed_at) - as_utc(delivery.received_at)).total_seconds(), 3)}))
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Scheduled n8n worker did not recover the queued record in time")
        time.sleep(2)


if __name__ == "__main__":
    run()

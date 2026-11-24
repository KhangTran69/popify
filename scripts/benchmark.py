"""Measure signed intake persistence and delivery worker throughput.

By default this command creates a temporary SQLite database and removes it on
exit. A configured database is used only with two explicit opt-in flags; the
benchmark appends synthetic rows and never deletes records.
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import platform
import secrets
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def percentile_ms(samples: list[float], quantile: float) -> float:
    """Return a nearest-rank percentile from already measured millisecond values."""
    if not samples:
        raise ValueError("sample must not be empty")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return round(ordered[index], 3)


def configure_environment(database_url: str) -> tuple[str, str]:
    """Set isolated mock-mode settings before importing application modules."""
    shopify_secret = secrets.token_urlsafe(32)
    n8n_secret = secrets.token_urlsafe(32)
    app_settings = (
        "DATABASE_URL", "POPIFY_ENV", "APP_SECRET", "SHOPIFY_WEBHOOK_SECRET",
        "SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_API_VERSION",
        "OPERATOR_USERNAME", "OPERATOR_PASSWORD", "LLM_MODE", "OPENAI_API_KEY",
        "OPENAI_MODEL", "N8N_BASE_URL", "N8N_WEBHOOK_SECRET", "ORDER_DELAY_HOURS",
        "MOCK_ACTION_FAILURES", "MOCK_ACTION_AMBIGUOUS", "MAX_ACTION_ATTEMPTS",
        "MAX_DELIVERY_ATTEMPTS", "RETRY_BASE_SECONDS", "MOCK_RETRY_AFTER_SECONDS",
        "STORE_POLICIES", "COOKIE_SECURE",
    )
    for name in app_settings:
        os.environ.pop(name, None)
    os.environ.update(
        {
            "DATABASE_URL": database_url,
            "POPIFY_ENV": "development",
            # Reuse preview's .env-independent settings, then enable only the
            # authenticated local intake/worker routes needed by this runner.
            "POPIFY_PREVIEW_MODE": "1",
            "APP_SECRET": secrets.token_urlsafe(32),
            "OPERATOR_PASSWORD": secrets.token_urlsafe(24),
            "SHOPIFY_WEBHOOK_SECRET": shopify_secret,
            "SHOPIFY_STORE_DOMAIN": "",
            "SHOPIFY_ADMIN_TOKEN": "",
            "LLM_MODE": "mock",
            "OPENAI_API_KEY": "",
            "N8N_BASE_URL": "",
            "N8N_WEBHOOK_SECRET": n8n_secret,
            "MOCK_ACTION_FAILURES": "0",
            "MOCK_ACTION_AMBIGUOUS": "false",
        }
    )
    return shopify_secret, n8n_secret


def run_benchmark(count: int, database_url: str, temporary_database: bool) -> dict:
    shopify_secret, n8n_secret = configure_environment(database_url)

    # Settings and the SQLAlchemy engine must see the benchmark-only environment.
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select, text

    from app.db import Base, SessionLocal, engine
    from app.config import settings

    settings.preview_mode = False
    settings.shopify_webhook_secret = shopify_secret
    settings.n8n_webhook_secret = n8n_secret

    from app.main import app
    from app.models import Delivery

    logging.disable(logging.INFO)
    logging.getLogger("commerceops").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx._client").setLevel(logging.WARNING)
    Base.metadata.create_all(engine)
    run_id = uuid.uuid4().hex[:10]
    shop_domain = f"bench-{run_id}.myshopify.com"
    current = datetime.now(timezone.utc).isoformat()
    ack_samples_ms: list[float] = []
    webhook_ids = [f"bench-{run_id}-{index}" for index in range(count)]
    delivery_ids: list[int] = []

    with TestClient(app) as client:
        for index, webhook_id in enumerate(webhook_ids):
            payload = {
                "id": f"{run_id}-{index}",
                "name": f"#BENCH-{run_id}-{index}",
                "email": "benchmark@example.test",
                "currency": "USD",
                "total_price": "1.00",
                "financial_status": "paid",
                "fulfillment_status": None,
                "created_at": current,
                "updated_at": current,
            }
            raw = json.dumps(payload, separators=(",", ":")).encode()
            signature = base64.b64encode(
                hmac.new(shopify_secret.encode(), raw, hashlib.sha256).digest()
            ).decode()
            started = time.perf_counter()
            response = client.post(
                "/webhooks/shopify/orders",
                content=raw,
                headers={
                    "X-Shopify-Hmac-SHA256": signature,
                    "X-Shopify-Webhook-Id": webhook_id,
                    "X-Shopify-Topic": "orders/create",
                    "X-Shopify-Shop-Domain": shop_domain,
                },
            )
            ack_samples_ms.append((time.perf_counter() - started) * 1000)
            if response.status_code != 202:
                raise RuntimeError(
                    f"intake {index + 1}/{count} returned {response.status_code}: {response.text}"
                )
            delivery_ids.append(response.json()["delivery_id"])

        worker_started = time.perf_counter()
        for index, delivery_id in enumerate(delivery_ids):
            response = client.post(
                f"/api/n8n/process-delivery/{delivery_id}",
                headers={"X-N8N-Secret": n8n_secret},
            )
            if response.status_code != 200 or response.json().get("status") != "processed":
                raise RuntimeError(
                    f"worker {index + 1}/{count} failed: {response.status_code} {response.text}"
                )
        worker_elapsed = time.perf_counter() - worker_started

    with SessionLocal() as db:
        version_query = "SELECT version()" if engine.dialect.name == "postgresql" else "SELECT sqlite_version()"
        database_version = db.execute(text(version_query)).scalar_one()
        persisted = db.scalar(
            select(Delivery.id)
            .where(Delivery.webhook_id.in_(webhook_ids), Delivery.status == "processed")
            .limit(count)
        )
        processed_count = db.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.webhook_id.in_(webhook_ids), Delivery.status == "processed"
            )
        )
        if not persisted or processed_count != count:
            raise RuntimeError(
                f"persisted result mismatch: expected {count}, found {processed_count}"
            )

    ack_total_ms = sum(ack_samples_ms)
    return {
        "benchmark": "signed intake and persisted delivery processing",
        "run_id": run_id,
        "commit": _commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "database": engine.dialect.name,
        "database_version": database_version,
        "database_scope": "temporary database removed at exit"
        if temporary_database
        else "explicit disposable database; synthetic rows are retained",
        "sample_count": count,
        "intake_acknowledgment_ms": {
            "p50": percentile_ms(ack_samples_ms, 0.50),
            "p95": percentile_ms(ack_samples_ms, 0.95),
            "mean": round(ack_total_ms / count, 3),
        },
        "queue_processing": {
            "processed": processed_count,
            "elapsed_seconds": round(worker_elapsed, 6),
            "items_per_second": round(count / worker_elapsed, 2)
            if worker_elapsed
            else None,
        },
        "measurement_scope": (
            "In-process FastAPI TestClient with signed webhook intake and authenticated "
            "targeted delivery worker calls. Intake acknowledgment follows the database commit. "
            "Does not measure network latency, n8n polling/scheduling, or concurrent worker contention."
        ),
    }


def _commit() -> str:
    import subprocess

    configured_commit = os.environ.get("POPIFY_BENCHMARK_COMMIT")
    if configured_commit:
        return configured_commit
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    )
    if result.returncode:
        return "unavailable"
    commit = result.stdout.strip()
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    )
    if changed.returncode == 0 and changed.stdout.strip():
        commit += " + working tree changes"
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100, help="synthetic deliveries (1–5000)")
    parser.add_argument(
        "--use-configured-database",
        action="store_true",
        help="use DATABASE_URL from the environment instead of temporary SQLite",
    )
    parser.add_argument(
        "--confirm-disposable-db",
        action="store_true",
        help="confirm that the configured database is disposable; rows are appended, never deleted",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 5000:
        parser.error("--count must be between 1 and 5000")
    if args.confirm_disposable_db and not args.use_configured_database:
        parser.error("--confirm-disposable-db requires --use-configured-database")
    if args.use_configured_database and not args.confirm_disposable_db:
        parser.error("configured database requires --confirm-disposable-db")

    if args.use_configured_database:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            parser.error("DATABASE_URL is missing")
        return _run_and_print(args.count, database_url, False)

    with tempfile.TemporaryDirectory(prefix="popify-benchmark-") as directory:
        database_url = "sqlite:///" + str(Path(directory) / "benchmark.db")
        return _run_and_print(args.count, database_url, True)


def _run_and_print(count: int, database_url: str, temporary_database: bool) -> int:
    try:
        print(json.dumps(run_benchmark(count, database_url, temporary_database), indent=2))
    except Exception as exc:
        print(f"BENCHMARK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

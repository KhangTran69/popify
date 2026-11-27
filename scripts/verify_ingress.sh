#!/bin/sh
# Exercise the example ingress against the already-running disposable CI Compose network.
set -eu
[ "${POPIFY_CONFIRM_DISPOSABLE:-}" = "1" ] || { echo "Disposable stack confirmation required" >&2; exit 2; }
tmp=$(mktemp -d)
name="popify-gateway-$(basename "$tmp" | tr -cd 'a-zA-Z0-9')"
started=0
cleanup() {
  result=$?
  if [ "$started" = 1 ]; then
    if [ "$result" != 0 ]; then docker logs --tail 50 "$name" >&2 || true; fi
    docker rm -f "$name" >/dev/null
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost -addext subjectAltName=DNS:localhost -keyout "$tmp/tls.key" -out "$tmp/tls.crt" >/dev/null 2>&1
network=$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$(docker compose ps -q api)")
docker run -d --name "$name" --network "$network" -p 127.0.0.1:8443:8443 \
  -v "$PWD/deploy/public-webhook.nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$tmp:/etc/nginx/tls:ro" nginx:1.28-alpine >/dev/null
started=1
docker exec "$name" nginx -t
for i in $(seq 1 20); do
  code=$(curl -s --cacert "$tmp/tls.crt" --resolve localhost:8443:127.0.0.1 --max-time 3 -o /dev/null -w '%{http_code}' https://localhost:8443/login || true)
  [ "$code" = 404 ] && break
  sleep 1
done
[ "$code" = 404 ] || { echo "Gateway readiness expected 404, got $code" >&2; exit 1; }
expect_code() {
  expected=$1; shift
  code=$(curl -sS --cacert "$tmp/tls.crt" --resolve localhost:8443:127.0.0.1 --max-time 10 -o /dev/null -w "%{http_code}" "$@" || true)
  echo "Ingress probe: expected $expected, got $code"
  [ "$code" = "$expected" ]
}
for path in /api/n8n/process-next /api/support / /docs /ready; do
  expect_code 404 "https://localhost:8443$path"
done
base=https://localhost:8443/webhooks/shopify/orders
expect_code 401 -X POST -d '{}' "$base"
# Signed synthetic delivery and duplicate through trusted TLS, not direct ASGI.
printf '%s' '{"id":"ci-tls-ingress","name":"#TLS-INGRESS","created_at":"2026-11-27T00:00:00Z","updated_at":"2026-11-27T00:00:00Z"}' > "$tmp/body"
signature=$(docker compose exec -T api python -c 'import base64,hashlib,hmac,sys; from app.config import settings; assert settings.shopify_webhook_secret and settings.llm_mode=="mock"; print(base64.b64encode(hmac.new(settings.shopify_webhook_secret.encode(),sys.stdin.buffer.read(),hashlib.sha256).digest()).decode())' < "$tmp/body")
for expected in 202 200; do
  expect_code "$expected" -H "X-Shopify-Hmac-SHA256: $signature" -H "X-Shopify-Webhook-Id: ci-tls-ingress" \
    -H "X-Shopify-Shop-Domain: demo.myshopify.com" -H "X-Shopify-Topic: orders/create" --data-binary @"$tmp/body" "$base"
done
docker compose exec -T api python -c 'from sqlalchemy import select; from app.db import SessionLocal; from app.models import Delivery; db=SessionLocal(); rows=list(db.scalars(select(Delivery).where(Delivery.webhook_id=="ci-tls-ingress"))); assert len(rows)==1; db.close()'
echo "PASS: trusted TLS signed intake and duplicate persisted exactly once"

head -c 1000001 /dev/zero > "$tmp/large"
expect_code 413 -H "Expect: 100-continue" --expect100-timeout 5 --data-binary @"$tmp/large" "$base"
# Bounded synthetic burst against the isolated ingress, no valid signatures.
seq 1 40 | xargs -P 8 -I '__INDEX__' sh -c 'curl -s --cacert "$2" --resolve localhost:8443:127.0.0.1 --max-time 10 -o /dev/null -w "%{http_code}\n" -X POST -d "{}" "$1"' _ "$base" "$tmp/tls.crt" '__INDEX__' > "$tmp/status"
sort "$tmp/status" | uniq -c
[ "$(wc -l < "$tmp/status")" -eq 40 ]
if grep -Ev '^(401|429)$' "$tmp/status"; then echo 'Unexpected ingress burst response' >&2; exit 1; fi
grep -q '^429$' "$tmp/status"
echo "PASS: TLS ingress route isolation, body limit, invalid signature and bounded burst rejection"

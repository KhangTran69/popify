#!/bin/sh
# Exercise the example ingress against the already-running disposable CI Compose network.
set -eu
[ "${POPIFY_CONFIRM_DISPOSABLE:-}" = "1" ] || { echo "Disposable stack confirmation required" >&2; exit 2; }
tmp=$(mktemp -d)
name="popify-gateway-$(basename "$tmp" | tr -cd 'a-zA-Z0-9')"
started=0
cleanup() {
  if [ "$started" = 1 ]; then docker rm -f "$name" >/dev/null; fi
  rm -rf "$tmp"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost -keyout "$tmp/tls.key" -out "$tmp/tls.crt" >/dev/null 2>&1
network=$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$(docker compose ps -q api)")
docker run -d --name "$name" --network "$network" -p 127.0.0.1:8443:8443 \
  -v "$PWD/deploy/public-webhook.nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$tmp:/etc/nginx/tls:ro" nginx:1.28-alpine >/dev/null
started=1
docker exec "$name" nginx -t
for i in $(seq 1 20); do
  code=$(curl -ks --max-time 3 -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/login || true)
  [ "$code" = 404 ] && break
  sleep 1
done
[ "$code" = 404 ]
for path in /api/n8n/process-next /api/support / /docs /ready; do
  [ "$(curl -ks -o /dev/null -w '%{http_code}' "https://127.0.0.1:8443$path")" = 404 ]
done
base=https://127.0.0.1:8443/webhooks/shopify/orders
[ "$(curl -ks -o /dev/null -w '%{http_code}' -X POST -d '{}' "$base")" = 401 ]
head -c 1000001 /dev/zero > "$tmp/large"
[ "$(curl -ks -o /dev/null -w '%{http_code}' -H 'Expect:' --data-binary @"$tmp/large" "$base")" = 413 ]
# Bounded synthetic burst against the isolated ingress, no valid signatures.
seq 1 40 | xargs -P 8 -I '%' sh -c 'curl -ks --max-time 10 -o /dev/null -w "%{http_code}\n" -X POST -d "{}" "$1"' _ "$base" '%' > "$tmp/status"
grep -q '^429$' "$tmp/status"
echo "PASS: TLS ingress route isolation, body limit, invalid signature and bounded burst rejection"

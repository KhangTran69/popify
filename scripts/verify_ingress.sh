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
[ "$code" = 404 ] || { echo "Gateway readiness expected 404, got $code" >&2; exit 1; }
expect_code() {
  expected=$1; shift
  code=$(curl -ksS --max-time 10 -o /dev/null -w "%{http_code}" "$@" || true)
  echo "Ingress probe: expected $expected, got $code"
  [ "$code" = "$expected" ]
}
for path in /api/n8n/process-next /api/support / /docs /ready; do
  expect_code 404 "https://127.0.0.1:8443$path"
done
base=https://127.0.0.1:8443/webhooks/shopify/orders
expect_code 401 -X POST -d '{}' "$base"
head -c 1000001 /dev/zero > "$tmp/large"
expect_code 413 -H "Expect: 100-continue" --expect100-timeout 5 --data-binary @"$tmp/large" "$base"
# Bounded synthetic burst against the isolated ingress, no valid signatures.
seq 1 40 | xargs -P 8 -I '__INDEX__' sh -c 'curl -ks --max-time 10 -o /dev/null -w "%{http_code}\n" -X POST -d "{}" "$1"' _ "$base" '__INDEX__' > "$tmp/status"
sort "$tmp/status" | uniq -c
[ "$(wc -l < "$tmp/status")" -eq 40 ]
if grep -Ev '^(401|429)
echo "PASS: TLS ingress route isolation, body limit, invalid signature and bounded burst rejection"
 "$tmp/status"; then echo 'Unexpected ingress burst response' >&2; exit 1; fi
grep -q '^429
echo "PASS: TLS ingress route isolation, body limit, invalid signature and bounded burst rejection"
 "$tmp/status"
echo "PASS: TLS ingress route isolation, body limit, invalid signature and bounded burst rejection"

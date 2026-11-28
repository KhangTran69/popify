"""Verify the actual private staging HTTPS boundary using synthetic accounts."""
import argparse
import http.cookiejar
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://localhost:8444"
PASSWORD = "synthetic-staging-test-password"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def client(cafile):
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))


def call(opener, path, data=None, headers=None):
    request = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode() if data is not None else None,
                                     headers=headers or {})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read().decode()


def csrf(opener):
    status, _, body = call(opener, "/login")
    assert status == 200
    return re.search(r'name="csrf_token" value="([^"]+)', body).group(1)


def login(opener, username):
    status, headers, _ = call(opener, "/login", {"username": username, "password": PASSWORD, "csrf_token": csrf(opener)})
    assert status == 303, status
    cookie = headers.get("Set-Cookie", "").lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    return csrf(opener)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    admin = client(args.ca)
    for _ in range(30):
        try:
            if call(admin, "/live")[0] == 200:
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Private TLS gateway did not become available")
    for path in ("/api/n8n/process-next", "/api/support", "/webhooks/shopify/orders", "/demo", "/docs", "/openapi.json", "/health", "/unknown-future-route",
                 "/approvals/1/preview-run", "/api/unknown", "/webhooks%2Fshopify%2Forders"):
        assert call(admin, path)[0] == 404, path
    assert call(admin, "/api/ops/status")[0] == 401
    assert call(admin, "/ready")[0] == 200
    forged = {"username": "ci-staging-admin", "password": PASSWORD, "csrf_token": csrf(admin)}
    assert call(admin, "/login", forged, {"Origin": "https://other.example.test"})[0] == 403
    assert call(admin, "/login", forged, {"Sec-Fetch-Site": "same-site"})[0] == 403
    admin_token = login(admin, "ci-staging-admin")
    status, headers, body = call(admin, "/operators")
    assert status == 200 and "ci-staging-auditor" in body and "pbkdf2_sha256" not in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    auditor = client(args.ca)
    auditor_token = login(auditor, "ci-staging-auditor")
    assert call(auditor, "/operators")[0] == 403
    assert call(auditor, "/support")[0] == 403
    assert call(auditor, "/operators/ci-staging-admin", {"action": "revoke", "csrf_token": auditor_token})[0] == 403
    status, _, body = call(auditor, "/api/ops/status")
    assert status == 200 and "alerts" in json.loads(body)
    assert call(admin, "/operators/ci-staging-auditor", {"action": "revoke", "csrf_token": admin_token})[0] == 303
    assert call(auditor, "/api/ops/status")[0] == 401
    assert call(admin, "/logout", {"csrf_token": admin_token})[0] == 303
    assert call(admin, "/api/ops/status")[0] == 401
    print("PASS: trusted-test TLS, secure cookies, CSP, private route isolation, role denial and session revocation")


if __name__ == "__main__":
    run()

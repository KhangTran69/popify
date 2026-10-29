"""Exercise the first reviewer path against a real isolated preview process."""

import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def test_isolated_preview_starts_and_saves_scenario():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]

    environment = os.environ.copy()
    environment["POPIFY_ENV"] = "production"
    environment["LLM_MODE"] = "openai"
    environment["OPENAI_API_KEY"] = "must-not-be-used"
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.preview", "--port", str(port)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def page(path):
        with opener.open(base + path, timeout=2) as response:
            return response.read().decode()

    def csrf(html):
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        assert match, "CSRF token missing from reviewer form"
        return match.group(1)

    def post(path, fields):
        fields = {**fields, "csrf_token": csrf(page("/demo"))}
        with opener.open(base + path, data=urllib.parse.urlencode(fields).encode(), timeout=3) as response:
            return response.url, response.read().decode()

    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                login = page("/login")
                break
            except (urllib.error.URLError, TimeoutError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("isolated preview did not start")
                time.sleep(0.1)

        credentials = urllib.parse.urlencode({
            "username": "preview", "password": "preview-only", "csrf_token": csrf(login),
        }).encode()
        with opener.open(base + "/login", data=credentials, timeout=2) as response:
            assert response.url == base + "/"

        demo = page("/demo")
        assert "Start success run" in demo
        scenario = urllib.parse.urlencode({"scenario": "success", "csrf_token": csrf(demo)}).encode()
        with opener.open(base + "/demo/preview/scenarios", data=scenario, timeout=2) as response:
            assert re.fullmatch(base + r"/demo/scenarios/\d+", response.url)
            assert "Synthetic" in response.read().decode()

        recovery_url, recovery_html = post("/demo/preview/scenarios", {"scenario": "ambiguous"})
        original_id = int(recovery_url.rsplit("/", 1)[1])
        assert "pending" in recovery_html.lower()
        recovery_url, recovery_html = post(
            f"/approvals/{original_id}/decision?from_scenario={original_id}",
            {"decision": "approved"},
        )
        assert "Journey still requires attention" in recovery_html
        recovery_url, recovery_html = post(
            f"/approvals/{original_id}/reconcile?from_scenario={original_id}",
            {"scenario_id": original_id, "outcome": "confirmed_not_executed",
             "explanation": "Synthetic ledger confirms the first action did not execute.",
             "reference": "PREVIEW-LEDGER-001"},
        )
        assert "Create a fresh pending approval" in recovery_html
        replacement_url, replacement_html = post(
            f"/approvals/{original_id}/refresh?from_scenario={original_id}", {}
        )
        replacement_id = int(replacement_url.rsplit("/", 1)[1])
        assert replacement_id != original_id and "pending decision" in replacement_html
        _, completed_html = post(
            f"/approvals/{replacement_id}/decision?from_scenario={replacement_id}",
            {"decision": "approved"},
        )
        assert "Recovery completed through a fresh approval" in completed_html
        assert f"approval #{original_id}" in completed_html
        assert f"approval #{replacement_id}" in completed_html
        _, disputed_html = post(
            f"/approvals/{original_id}/reconcile?from_scenario={replacement_id}",
            {"scenario_id": replacement_id, "outcome": "confirmed_completed",
             "explanation": "Later synthetic evidence conflicts with the first assertion.",
             "reference": "PREVIEW-LEDGER-002"},
        )
        assert "Recovery blocked by unresolved earlier evidence" in disputed_html
        assert "separately persisted as succeeded" in disputed_html

        try:
            opener.open(base + "/api/n8n/orders/process", data=b"{}", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError("preview allowed an integration route")
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

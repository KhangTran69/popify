"""Request boundary protections shared by the operator and integration APIs."""

import asyncio
import secrets
import threading

from starlette.responses import JSONResponse

WEBHOOK_BODY_LIMIT = 1_000_000
FORM_API_BODY_LIMIT = 128 * 1024
MAX_CONCURRENT_MUTATIONS = 64
MAX_CONCURRENT_LOGINS = 4
BODY_TIMEOUT_SECONDS = 15


def secure_equal(left: str, right: str) -> bool:
    """Compare UTF-8 text without raising for non-ASCII or invalid Unicode."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeError:
        return False


def valid_integration_secret(candidate: str | None, configured: str) -> bool:
    return bool(configured and candidate and secure_equal(candidate, configured))


class RequestBodyLimitMiddleware:
    """Bound mutation bodies before form or JSON parsing, including streamed bodies."""

    def __init__(self, app):
        self.app = app
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_MUTATIONS)
        self._login_slots = threading.BoundedSemaphore(MAX_CONCURRENT_LOGINS)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {
            "POST", "PUT", "PATCH", "DELETE"
        }:
            return await self.app(scope, receive, send)

        login = scope["path"] == "/login"
        if login and not self._login_slots.acquire(blocking=False):
            return await JSONResponse({"detail": "Sign-in service busy; retry later"}, status_code=503,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        try:
            if not self._slots.acquire(blocking=False):
                return await JSONResponse({"detail": "Service busy; retry later"}, status_code=503,
                                          headers={"Retry-After": "1"})(scope, receive, send)
            try:
                return await self._bounded_request(scope, receive, send)
            finally:
                self._slots.release()
        finally:
            if login:
                self._login_slots.release()

    async def _bounded_request(self, scope, receive, send):
        limit = (
            WEBHOOK_BODY_LIMIT
            if scope["path"] == "/webhooks/shopify/orders"
            else FORM_API_BODY_LIMIT
        )

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        lengths = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 20:
                return await reject(400, "Invalid Content-Length")
            if int(lengths[0]) > limit:
                return await reject(413, "Request body exceeds the allowed size")

        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        return await reject(413, "Request body exceeds the allowed size")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timed out")

        # Replay the exact bytes; signed webhook verification must see the original body.
        buffered = bytes(body)
        del body
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)

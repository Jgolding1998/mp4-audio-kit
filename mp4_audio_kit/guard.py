"""ASGI guard for a loopback-only app: Host, Origin, CSRF token, body caps, headers."""

from __future__ import annotations

import hmac
import json
from typing import Callable

SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
     b"form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class BodyTooLarge(Exception):
    pass


async def send_json(send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": code, "message": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + SECURITY_HEADERS})
    await send({"type": "http.response.body", "body": body})


class LocalGuard:
    """Rejects requests that did not come from this app's own page on loopback."""

    def __init__(self, app, *, port: int, token: str, body_limit: Callable[[str], int]):
        self.app = app
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.hosts = hosts
        self.origins = {f"http://{h}" for h in hosts}
        self.token = token.encode()
        self.body_limit = body_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return  # websockets are not served
        headers = {}
        for key, value in scope["headers"]:
            name = key.decode("latin-1").lower()
            if name in headers:
                headers[name] = None  # duplicates of security-relevant headers are rejected below
            else:
                headers[name] = value.decode("latin-1")

        if headers.get("host") not in self.hosts:
            return await send_json(send, 400, "bad_host", "Open this app at its local address.")
        if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return await send_json(send, 403, "cross_site", "Cross-site requests are not allowed.")

        method = scope["method"]
        if method not in ("GET", "HEAD"):
            if headers.get("origin") not in self.origins:
                return await send_json(send, 403, "bad_origin", "Request origin is not allowed.")
            token = (headers.get("x-csrf-token") or "").encode()
            if not hmac.compare_digest(token, self.token):
                return await send_json(send, 403, "bad_token", "Reload the page and try again.")
        if "transfer-encoding" in headers:
            return await send_json(send, 411, "length_required", "Request must have a known length.")
        length_text = headers.get("content-length")
        limit = self.body_limit(scope["path"]) if method not in ("GET", "HEAD") else 0
        if length_text is None:
            if method not in ("GET", "HEAD"):
                return await send_json(send, 411, "length_required", "Request must have a known length.")
            length = 0
        elif not length_text.isascii() or not length_text.isdecimal() or len(length_text) > 15:
            return await send_json(send, 400, "bad_length", "Bad request length.")
        else:
            length = int(length_text)
        if length > limit:
            return await send_json(send, 413, "too_large", "The request is too large.")

        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > length:
                    raise BodyTooLarge()
            return message

        started = False

        async def secured_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + SECURITY_HEADERS
            await send(message)

        try:
            await self.app(scope, bounded_receive, secured_send)
        except Exception:
            if not started:
                await send_json(send, 500, "failed", "The request failed.")

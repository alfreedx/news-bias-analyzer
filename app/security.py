"""Security middleware: strict headers and a privacy-preserving in-memory rate limiter."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, https_only: bool = False):
        super().__init__(app)
        self.https_only = https_only

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP + ("; upgrade-insecure-requests" if self.https_only else "")
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        # Outlets never learn which search led a reader to them.
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
            "payment=(), usb=()"
        )
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        if self.https_only:
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "public, max-age=86400")
        else:
            h.setdefault("Cache-Control", "no-store")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limit on search endpoints.

    Client addresses are never stored in clear text: they are hashed with a
    random salt that lives only in memory and rotates every day, and entries
    expire after one minute.
    """

    LIMITED_PREFIXES = ("/buscar", "/api/")

    def __init__(self, app, per_minute: int = 30, trust_proxy: bool = False):
        super().__init__(app)
        self.per_minute = per_minute
        self.trust_proxy = trust_proxy
        self.hits: dict[str, deque[float]] = {}
        self._salt = secrets.token_bytes(16)
        self._salt_day = int(time.time() // 86400)
        self._last_sweep = time.monotonic()

    def _client_key(self, request: Request) -> str:
        day = int(time.time() // 86400)
        if day != self._salt_day:
            self._salt, self._salt_day, self.hits = secrets.token_bytes(16), day, {}
        ip = request.client.host if request.client else "unknown"
        if self.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                ip = forwarded.split(",")[0].strip()
        return hashlib.blake2b(ip.encode(), key=self._salt, digest_size=12).hexdigest()

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        for key in [k for k, q in self.hits.items() if not q or now - q[-1] > 60]:
            del self.hits[key]

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(self.LIMITED_PREFIXES):
            return await call_next(request)
        now = time.monotonic()
        self._sweep(now)
        key = self._client_key(request)
        window = self.hits.setdefault(key, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return PlainTextResponse(
                "Demasiadas búsquedas seguidas. Espera un minuto, por favor.",
                status_code=429,
                headers={"Retry-After": "60"},
            )
        window.append(now)
        return await call_next(request)

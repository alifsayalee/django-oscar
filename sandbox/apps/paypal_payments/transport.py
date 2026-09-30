"""
Transport wrapper for the PayPal SDK client.

The SDK has no logging hook, so this wraps its transport: it logs the method,
path and status of every PayPal request (never headers or bodies - they carry
the bearer token and card data) and remembers the last status seen on this
thread. The error boundary uses that status to tell an unreadable *error* body
(a known rejection) from an unreadable *success* body (outcome unknown).
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

from pay_pal_server_sdk.core import HttpClient, HttpRequest, HttpResponse

logger = logging.getLogger("apps.paypal_payments.http")

_state = threading.local()


def reset_last_status() -> None:
    _state.status = None


def last_status() -> int | None:
    status: int | None = getattr(_state, "status", None)
    return status


class LoggingTransport:
    """Satisfies the SDK's ``HttpClient`` protocol by delegating to ``inner``."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        # The head only - the body is still on the socket and belongs to the SDK.
        response = self._inner.send(request)
        _state.status = response.status_code
        logger.info(
            "PayPal %s %s -> %s (%.0f ms)",
            request.method,
            urlsplit(str(request.url)).path,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()

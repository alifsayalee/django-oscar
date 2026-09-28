"""
The one Twilio SDK client this app uses.

The sandbox is a sync WSGI app, so this is the sync ``TwilioSdkClient``. It is
built lazily on first use (after any worker fork), reused for the life of the
process and closed at exit.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import (
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
)

logger = logging.getLogger(__name__)

# Phone numbers travel in lookup paths and list filters; digit runs are masked
# before a URL is logged. ("%2B" is an encoded "+".)
_DIGIT_RUN = re.compile(r"(%2B|\+)?\d{6,}")


def mask_url(url: str) -> str:
    return _DIGIT_RUN.sub("***", url)


def mask_number(number: str) -> str:
    """A number as it may appear in an operator view: country prefix and last two digits."""
    if len(number) <= 5:
        return "***"
    return number[:2] + "*" * (len(number) - 4) + number[-2:]


class LoggingTransport:
    """Logs method, masked URL, status and duration. Never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.warning(
                "twilio %s %s -> %s after %.0f ms",
                request.method,
                mask_url(request.url),
                type(e).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method,
            mask_url(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # Without credentials the SDK would send unauthenticated requests.
        raise ImproperlyConfigured(
            "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set to send SMS."
        )
    server_config: ServerConfigDict | None = None
    if settings.TWILIO_BASE_URL:
        # Messages live on the SDK's `default` server; the override applies to
        # that server only (lookups are served from their own host).
        server_config = {"default": {"base_url": settings.TWILIO_BASE_URL}}
    if transport is None:
        # A supplied transport carries its own timeout; the client's `timeout=`
        # would not reach the wire.
        transport = LoggingTransport(HttpxClient(timeout=settings.TWILIO_TIMEOUT))
    return TwilioSdkClient(
        server_config=server_config,
        custom_http_client=transport,
        account_sid_auth_token=BasicAuthCredentials(
            username=account_sid, password=auth_token
        ),
    )


def get_client() -> TwilioSdkClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = build_client()
            atexit.register(_client.close)
        return _client


@contextmanager
def use_client(client: TwilioSdkClient) -> Iterator[TwilioSdkClient]:
    """Swap in a client (tests: one built on a stub transport)."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    try:
        yield client
    finally:
        with _client_lock:
            _client = previous

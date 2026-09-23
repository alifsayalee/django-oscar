"""
Construction and lifetime of the one Twilio SDK client this app uses.

The sandbox runs under WSGI, so the client is the SDK's *sync* client, built
lazily on first use (never at import, so importing this module needs no
credentials) and shared by every request thread of the process. It is closed at
interpreter exit.
"""

import atexit
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from twilio_sdk import ServerConfigOrDict, TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger("apps.order_notifications.twilio")

_REQUIRED_SETTINGS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_MESSAGING_SERVICE_SID")

_ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")
_MESSAGE_SID = re.compile(r"(SM|MM)[0-9a-fA-F]{32}")
_PHONE_PATH = re.compile(r"/PhoneNumbers/[^/]+")


class TwilioNotConfigured(RuntimeError):
    """A required TWILIO_* setting is empty."""


def redact_url(url: str) -> str:
    """Host and path only: no query string (it carries To/From numbers), no
    account SID, and no phone number from a lookup path."""
    parts = urlsplit(url)
    path = _PHONE_PATH.sub("/PhoneNumbers/***", parts.path)
    path = _ACCOUNT_SID.sub("AC***", path)
    path = _MESSAGE_SID.sub(lambda m: m.group(0)[:2] + "***" + m.group(0)[-4:], path)
    return f"{parts.netloc}{path}"


class RedactingLoggingTransport:
    """Wraps the SDK's own httpx transport and logs method, redacted URL,
    status and latency. Headers (the Basic credential) and bodies (message
    text, phone numbers) are never logged."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "twilio %s %s failed: %s (%.0f ms)",
                request.method, redact_url(request.url), type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method, redact_url(request.url), response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(inner: HttpClient | None = None) -> TwilioSdkClient:
    """``inner`` replaces the httpx transport (tests); logging still wraps it."""
    missing = [name for name in _REQUIRED_SETTINGS if not getattr(settings, name, "")]
    if missing:
        raise TwilioNotConfigured("missing Twilio settings: " + ", ".join(missing))
    server_config: ServerConfigOrDict | None = None
    base_url = getattr(settings, "TWILIO_BASE_URL", "")
    if base_url:
        # The messaging API (Messages resource) is served from the SDK's
        # `default` server. Lookups (`default4`) are not governed by this setting.
        server_config = {"default": {"base_url": base_url}}
    # The timeout lives on the transport: with a custom transport the client's
    # own `timeout=` keyword never reaches the wire.
    transport = RedactingLoggingTransport(inner or HttpxClient(timeout=float(settings.TWILIO_HTTP_TIMEOUT)))
    return TwilioSdkClient(
        account_sid_auth_token=BasicAuthCredentials(
            username=settings.TWILIO_ACCOUNT_SID, password=settings.TWILIO_AUTH_TOKEN
        ),
        custom_http_client=transport,
        server_config=server_config,
    )


_client: TwilioSdkClient | None = None
_lock = threading.Lock()


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client_for_tests(client: TwilioSdkClient | None) -> None:
    """Swap the process-wide client (tests inject one built on a stub transport)."""
    global _client
    with _lock:
        _client = client

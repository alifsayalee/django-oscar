"""
The process-wide Twilio SDK client.

The sandbox is a WSGI Django site, so this is the *sync* client. It is built
lazily on first use - which is after any worker fork - and closed at exit.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

from .errors import TwilioNotConfigured

logger = logging.getLogger(__name__)

_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


class LoggingTransport:
    """
    Logs every provider call: method, host, status and duration.

    The path and query are deliberately left out - lookups and message
    listings carry the shopper's phone number there - and so are headers
    (the credential) and bodies (message text).
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        host = urlsplit(request.url).hostname or '?'
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('twilio %s %s -> %s after %.0f ms', request.method, host,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('twilio %s %s -> %s (%.0f ms)', request.method, host,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # The SDK would otherwise send the request unauthenticated.
        raise TwilioNotConfigured('TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set')
    timeout = float(settings.SMS_NOTIFICATIONS_TIMEOUT)
    server_config: ServerConfigDict | None = None
    if settings.TWILIO_BASE_URL:
        # Governs the messaging API (server "default") only; lookups keep the
        # provider's own host.
        server_config = {'default': {'base_url': settings.TWILIO_BASE_URL}}
    return TwilioSdkClient(
        server_config=server_config,
        timeout=timeout,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )


def get_client() -> TwilioSdkClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = build_client()
            atexit.register(_client.close)
        return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the shared client (tests use this to inject a stub transport)."""
    global _client
    with _client_lock:
        _client = client

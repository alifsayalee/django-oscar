"""
The one Twilio SDK client this process uses.

Sync client (the sandbox is WSGI), built lazily on first use so a forking
server builds it after the fork, and closed at interpreter exit.
"""
import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpClient

# Seconds. The SDK performs no retries, so this bounds each call outright.
TIMEOUT = 10.0

_lock = threading.Lock()
_client: TwilioSdkClient | None = None


def build_client(custom_http_client: HttpClient | None = None) -> TwilioSdkClient:
    account_sid: str = settings.TWILIO_ACCOUNT_SID
    auth_token: str = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # Omitting credentials would silently send unauthenticated requests.
        raise ImproperlyConfigured(
            'TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set')
    server_config: ServerConfigDict | None = None
    if settings.TWILIO_BASE_URL:
        # Governs the messaging API ("default" server) only; Lookup keeps its own host.
        server_config = {'default': {'base_url': settings.TWILIO_BASE_URL}}
    return TwilioSdkClient(
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
        server_config=server_config,
        timeout=TIMEOUT,
        custom_http_client=custom_http_client,
    )


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Swap the process client (tests, credential rotation); closes the old one."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close() -> None:
    if _client is not None:
        _client.close()

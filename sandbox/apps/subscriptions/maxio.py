"""
The Maxio Advanced Billing client, and the one place provider failures and
provider statuses are translated into this site's terms.

The sandbox is a sync WSGI Django site, so the sync client is used. It is built
lazily on first use in each worker process (safe under forking servers), reused
for the life of the process and closed at interpreter exit.
"""
import atexit
import logging
import threading
import time
from typing import Any, NoReturn

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    ApiError, BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.models.enums import SubscriptionState
from maxio_advanced_billing.server import Environment

logger = logging.getLogger('apps.subscriptions.maxio')

# MAXIO_ENVIRONMENT -> the SDK's environment. An unknown value fails loudly
# rather than falling through to the SDK's silent "us" default.
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

# Requests that never left this process: nothing can have happened upstream.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class BillingError(Exception):
    """A failure talking to Maxio, already mapped to what the API answers."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class LoggingTransport:
    """Wraps the SDK transport to log method, URL, status and duration - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('%s %s -> %s (%.0f ms)', request.method, request.url,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('%s %s -> %s (%.0f ms)', request.method, request.url, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    api_key = settings.MAXIO_API_KEY
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    base_url = settings.MAXIO_BASE_URL
    if not api_key:
        # The SDK would otherwise send every request unauthenticated.
        raise ImproperlyConfigured('MAXIO_API_KEY is not set')
    if not settings.MAXIO_DEFAULT_PRODUCT_FAMILY:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY is not set')
    try:
        environment = ENVIRONMENTS[settings.MAXIO_ENVIRONMENT.strip().lower()]
    except KeyError:
        raise ImproperlyConfigured(
            'MAXIO_ENVIRONMENT must be one of %s' % ', '.join(sorted(ENVIRONMENTS))) from None
    if base_url:
        production: dict[str, str] = {'base_url': base_url}
    elif subdomain:
        production = {'site': subdomain}
    else:
        raise ImproperlyConfigured('Set MAXIO_SITE_SUBDOMAIN or MAXIO_BASE_URL')
    if transport is None:
        transport = LoggingTransport(HttpxClient(timeout=settings.MAXIO_TIMEOUT))
    server_config: Any = {'production': {environment: production}}
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=server_config,
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Swap the process-wide client (tests, credential rotation)."""
    global _client
    with _client_lock:
        _client = client


def raise_for_read(exc: Exception, what: str) -> NoReturn:
    """
    Translate a failed *read* into a BillingError. A read changes nothing
    upstream, so outcome_unknown is always False and every provider-side
    rejection is our configuration problem, not the caller's.
    """
    if isinstance(exc, ApiError):
        if exc.status_code == 429:
            raise BillingError(503, 'billing_rate_limited', 'Billing provider is rate limiting us.') from exc
        if exc.status_code in (401, 403):
            raise BillingError(502, 'billing_misconfigured', 'Billing provider refused our credentials.') from exc
        raise BillingError(502, 'billing_unavailable', 'Billing provider could not %s.' % what) from exc
    if isinstance(exc, NEVER_SENT):
        raise BillingError(502, 'billing_unreachable', 'Billing provider is unreachable.') from exc
    if isinstance(exc, httpx.RequestError):
        raise BillingError(504, 'billing_timeout', 'Billing provider did not answer.') from exc
    if isinstance(exc, ValueError):  # pydantic ValidationError or a non-JSON body
        raise BillingError(502, 'billing_unreadable', 'Billing provider sent an unreadable answer.') from exc
    raise exc


def subscription_outcome(state: object) -> str:
    """The ONE place a Maxio subscription state becomes an outcome of ours."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return 'done'
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING
              | SubscriptionState.AWAITING_SIGNUP):
            return 'pending'  # accepted, not finished
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return 'pending'  # exists, but something is outstanding
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED
              | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED):
            return 'failed'  # never in effect, or in effect and later undone
        case _:
            return 'unknown'  # absent, or a value newer than this SDK

"""
The one place the sandbox talks to Maxio Advanced Billing: client lifetime and the error boundary.

Every SDK call goes through ``call()`` (writes) or ``read()`` (idempotent reads, retried on transient
failures), which translate each failure kind into a ``ProviderError`` carrying the HTTP status this API
answers with and whether the provider may have acted anyway (``outcome_unknown``).
"""
import atexit
import logging
import threading
import time
from typing import Callable, Optional, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import ApiError, BasicAuthCredentials, RawError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1
from maxio_advanced_billing.server import Environment
from pydantic import ValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T')

ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

# Failures raised before the request left: nothing can have happened at Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Statuses that are the caller's doing and are passed through (a named list, not a range).
CALLER_STATUSES = (400, 404, 409, 422)

READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = 0.5
RETRYABLE_READ_STATUSES = (429, 502, 503, 504)


class ProviderError(Exception):
    """A Maxio call that did not produce a usable answer."""

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_status: Optional[int] = None, details: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.details = details or []


_client: Optional[MaxioAdvancedBillingClient] = None
_client_lock = threading.Lock()


def build_client() -> MaxioAdvancedBillingClient:
    missing = [name for name in ('MAXIO_API_KEY', 'MAXIO_DEFAULT_PRODUCT_FAMILY')
               if not getattr(settings, name, '')]
    base_url = getattr(settings, 'MAXIO_BASE_URL', '')
    subdomain = getattr(settings, 'MAXIO_SITE_SUBDOMAIN', '')
    if not base_url and not subdomain:
        missing.append('MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)')
    if missing:
        raise ImproperlyConfigured('Maxio billing is not configured; missing: ' + ', '.join(missing))

    env_name = (getattr(settings, 'MAXIO_ENVIRONMENT', '') or 'us').strip().lower()
    try:
        environment = ENVIRONMENTS[env_name]
    except KeyError:
        raise ImproperlyConfigured(
            'MAXIO_ENVIRONMENT must be one of %s' % ', '.join(sorted(ENVIRONMENTS))) from None

    # MAXIO_BASE_URL is used verbatim; otherwise the subdomain fills the environment's URL template.
    server: ServerConfigDict
    if environment == 'eu':
        server = {'production': {'eu': {'base_url': base_url} if base_url else {'site': subdomain}}}
    else:
        server = {'production': {'us': {'base_url': base_url} if base_url else {'site': subdomain}}}
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=float(getattr(settings, 'MAXIO_TIMEOUT_SECONDS', 10.0)),
        server_config=server,
        # Maxio authenticates with the API key as the username and "x" as the password.
        basic_auth=BasicAuthCredentials(username=settings.MAXIO_API_KEY, password='x'),
    )


def get_client() -> MaxioAdvancedBillingClient:
    """The process-wide client, built on first use (so after any worker fork) and closed at exit."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: Optional[MaxioAdvancedBillingClient]) -> None:
    """Replace the process-wide client (tests inject one backed by a stub transport)."""
    global _client
    with _client_lock:
        _client = client


def _error_details(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(e) for e in errors]
        customer = getattr(errors, 'customer', None)
        if isinstance(customer, str):
            return [customer]
    return []


def translate(exc: BaseException, operation: str) -> ProviderError:
    """Map any failure of a Maxio call onto a ProviderError. Order matters: most specific first."""
    if isinstance(exc, ApiError):
        status = exc.status_code
        level = logging.INFO if status in CALLER_STATUSES else logging.WARNING
        if isinstance(exc.error, RawError):
            logger.log(level, 'Maxio %s failed: HTTP %s %s', operation, status, exc.error.text()[:500])
        else:
            logger.log(level, 'Maxio %s failed: HTTP %s %s', operation, status, _error_details(exc.error))
        if status in (401, 403):
            return ProviderError(502, 'Billing provider refused our credentials.', provider_status=status)
        if status == 429:
            return ProviderError(503, 'Billing provider is rate limiting; try again shortly.',
                                 provider_status=status)
        if status in CALLER_STATUSES:
            return ProviderError(status, 'Billing provider rejected the request.', provider_status=status,
                                 details=_error_details(exc.error))
        # 5xx and every unmapped status: ours until proven otherwise. A 5xx on a write may have landed.
        return ProviderError(502, 'Billing provider error.', provider_status=status,
                             outcome_unknown=status >= 500)
    if isinstance(exc, (ValidationError, ValueError)):
        logger.error('Maxio %s returned an unreadable response: %s', operation, type(exc).__name__)
        return ProviderError(502, 'Billing provider returned an unreadable response.', outcome_unknown=True)
    if isinstance(exc, NEVER_SENT):
        logger.warning('Maxio %s not sent: %s', operation, type(exc).__name__)
        return ProviderError(502, 'Billing provider unreachable; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        logger.warning('Maxio %s got no reply: %s', operation, type(exc).__name__)
        return ProviderError(504, 'Billing provider did not answer in time.', outcome_unknown=True)
    raise exc


def call(operation: str, fn: Callable[[MaxioAdvancedBillingClient], T]) -> T:
    """Run one SDK call exactly once (for writes). Raises ProviderError."""
    try:
        return fn(get_client())
    except (ApiError, ValidationError, ValueError, httpx.RequestError) as exc:
        raise translate(exc, operation) from exc


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return exc.status_code in RETRYABLE_READ_STATUSES
    return isinstance(exc, NEVER_SENT + (httpx.ReadTimeout, httpx.RemoteProtocolError))


def read(operation: str, fn: Callable[[MaxioAdvancedBillingClient], T]) -> T:
    """Run an idempotent read, retrying transient failures a bounded number of times."""
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return fn(get_client())
        except (ApiError, ValidationError, ValueError, httpx.RequestError) as exc:
            if attempt < READ_ATTEMPTS and _retryable(exc):
                time.sleep(READ_BACKOFF_SECONDS * attempt)
                continue
            raise translate(exc, operation) from exc
    raise AssertionError('unreachable')  # pragma: no cover

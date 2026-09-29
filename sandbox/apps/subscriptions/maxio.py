"""
Maxio Advanced Billing client lifetime and error boundary.

Everything that talks to the ``maxio_advanced_billing`` SDK goes through this
module: :func:`get_client` hands out one long-lived, process-wide client, and
:func:`call` converts every failure kind the SDK can surface into a single
:class:`MaxioError` carrying the HTTP status our API should answer with and
whether the upstream outcome is unknown.
"""
from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, BasicAuthCredentials, RawError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1
from maxio_advanced_billing.server import (
    Environment,
    ProductionEuConfigDict,
    ProductionUsConfigDict,
    ServerConfigDict,
)
from pydantic import ValidationError

logger = logging.getLogger('apps.subscriptions.maxio')

T = TypeVar('T')

# Maxio's documented Basic-auth convention: the API key is the username and the
# password is the literal "x" (``curl -u <api_key>:x``).
_API_KEY_PASSWORD = 'x'

_ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


class MaxioError(Exception):
    """A Maxio call that did not produce a usable result.

    ``status_code`` is the status our own API answers with (not necessarily
    Maxio's), ``outcome_unknown`` is True when a write may have landed upstream
    even though we could not read the result.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown


def _environment() -> Environment:
    raw = str(getattr(settings, 'MAXIO_ENVIRONMENT', '') or 'us').strip().lower()
    try:
        return _ENVIRONMENTS[raw]
    except KeyError:
        raise ImproperlyConfigured(
            'MAXIO_ENVIRONMENT must be one of %s, got %r' % (sorted(_ENVIRONMENTS), raw)) from None


def server_config() -> ServerConfigDict:
    """Resolve the ``production`` server for the configured environment.

    ``MAXIO_BASE_URL``, when set, is used verbatim as the API base address;
    otherwise the SDK's per-environment template is filled with the site
    subdomain.
    """
    environment = _environment()
    base_url = (getattr(settings, 'MAXIO_BASE_URL', '') or '').strip()
    subdomain = (getattr(settings, 'MAXIO_SITE_SUBDOMAIN', '') or '').strip()
    if not base_url and not subdomain:
        raise ImproperlyConfigured('MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) must be set')
    production: ProductionUsConfigDict | ProductionEuConfigDict = (
        {'base_url': base_url} if base_url else {'site': subdomain})
    if environment == 'eu':
        return {'production': {'eu': production}}
    return {'production': {'us': production}}


def build_client(**overrides: Any) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings. ``overrides`` is for tests (e.g. ``custom_http_client``)."""
    api_key = (getattr(settings, 'MAXIO_API_KEY', '') or '').strip()
    if not api_key:
        # Omitting basic_auth would silently send unauthenticated requests.
        raise ImproperlyConfigured('MAXIO_API_KEY must be set')
    kwargs: dict[str, Any] = {
        'environment': _environment(),
        'server_config': server_config(),
        'timeout': float(getattr(settings, 'MAXIO_TIMEOUT', 10.0)),
        # SDK default retry policy: idempotent methods only. POST is deliberately
        # not retried; subscribe idempotency is handled by the enrollment claim.
        'basic_auth': BasicAuthCredentials(username=api_key, password=_API_KEY_PASSWORD),
    }
    kwargs.update(overrides)
    return MaxioAdvancedBillingClient(**kwargs)


def get_client() -> MaxioAdvancedBillingClient:
    """The process-wide client, built lazily on first use (so after any worker fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_client)


@contextmanager
def use_client(client: MaxioAdvancedBillingClient) -> Iterator[MaxioAdvancedBillingClient]:
    """Temporarily install ``client`` as the process-wide client (tests)."""
    global _client
    previous = _client
    _client = client
    try:
        yield client
    finally:
        _client = previous
        client.close()


def _describe(error: object) -> str:
    """A caller-safe message from a decoded Maxio error body."""
    if isinstance(error, ErrorListResponse1):
        return '; '.join(error.errors) or 'Rejected by billing provider.'
    if isinstance(error, CustomerErrorResponse1):
        # The declared ``errors`` model is narrower than what Maxio sends; the
        # real keys are preserved as extras, so render the whole body.
        errors = error.to_dict(exclude_unset=True).get('errors') or {}
        if isinstance(errors, dict):
            parts = []
            for key, value in errors.items():
                messages = value if isinstance(value, list) else [value]
                parts.append('%s %s' % (key, ', '.join(str(m) for m in messages)))
            if parts:
                return '; '.join(parts)
        return 'Rejected by billing provider.'
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return 'Rejected by billing provider.'
        if isinstance(body, dict):
            errors = body.get('errors')
            if isinstance(errors, list) and errors:
                return '; '.join(str(e) for e in errors)
            if isinstance(body.get('error'), str):
                return str(body['error'])
    return 'Rejected by billing provider.'


def translate_api_error(status: int, error: object, *, write: bool) -> MaxioError:
    """Map a Maxio error status + decoded body onto our boundary's status."""
    if status in (401, 403):
        return MaxioError(502, 'Billing provider refused our credentials.')
    if status == 429:
        return MaxioError(503, 'Billing provider is rate limiting requests; try again shortly.')
    if 400 <= status < 500:
        return MaxioError(status, _describe(error))
    return MaxioError(502, 'Billing provider unavailable.', outcome_unknown=write and status >= 500)


def call(operation: Callable[[], T], *, action: str, write: bool = False) -> T:
    """Run one SDK call and convert every failure kind into :class:`MaxioError`.

    ``write`` marks calls that change state upstream, so a failure after the
    request may have been sent is reported as an unknown outcome.
    """
    try:
        return operation()
    except ApiError as e:
        logger.log(logging.INFO if e.status_code == 404 else logging.WARNING,
                   'Maxio %s failed: HTTP %s (%s)', action, e.status_code, type(e.error).__name__)
        raise translate_api_error(e.status_code, e.error, write=write) from e
    except (ValidationError, ValueError) as e:
        # Undecodable body. On a 2xx the call may well have succeeded.
        logger.error('Maxio %s returned an unreadable response', action, exc_info=True)
        raise MaxioError(502, 'Unreadable response from billing provider.', outcome_unknown=True) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        logger.error('Maxio %s was never sent: %s', action, type(e).__name__)
        raise MaxioError(502, 'Could not reach billing provider.', outcome_unknown=False) from e
    except httpx.RequestError as e:
        logger.error('Maxio %s got no response: %s', action, type(e).__name__)
        raise MaxioError(504, 'Billing provider did not respond.', outcome_unknown=write) from e

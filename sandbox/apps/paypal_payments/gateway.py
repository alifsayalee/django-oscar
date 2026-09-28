"""
The one place this app builds and holds the PayPal SDK client, and turns SDK failures into
:class:`ProviderError`.

Nothing in this module imports Django models, so it type-checks under ``mypy --strict``.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit

import httpx
from paypal import PaypalClient
from paypal.core import (
    ApiError, ClientCredentials, HttpRequest, HttpResponse, HttpxClient,
    OAuthProviderError, RawError)
from paypal.models import Error

logger = logging.getLogger('apps.paypal_payments.gateway')

# The SDK declares exactly one server (sdk-map.md "Servers & auth"). Any other environment needs
# PAYPAL_BASE_URL; an unknown value fails instead of silently falling through to a default host.
KNOWN_BASE_URLS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
}

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    environment: str
    currency: str
    base_url: str | None
    timeout: float = 20.0

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        try:
            return KNOWN_BASE_URLS[self.environment.lower()]
        except KeyError:
            raise ConfigurationError(
                "PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL" % self.environment)

    def validate(self) -> None:
        missing = [name for name, value in (
            ('PAYPAL_CLIENT_ID', self.client_id),
            ('PAYPAL_CLIENT_SECRET', self.client_secret),
            ('PAYPAL_CURRENCY', self.currency),
        ) if not value]
        if missing:
            raise ConfigurationError('Missing PayPal settings: %s' % ', '.join(missing))
        self.resolved_base_url()


def minor_units(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-minor_units(currency)), rounding=ROUND_HALF_UP)


def money_str(value: Decimal, currency: str) -> str:
    """Money as PayPal's string amount, scaled to the currency (never float, never locale)."""
    return str(quantize(value, currency))


# --------------------------------------------------------------------------------------------
# Transport: logging seam + the status of the last response, per thread
# --------------------------------------------------------------------------------------------

_last = threading.local()


def last_response_status() -> int | None:
    """Status of the last response this thread received — lets the error boundary tell an
    unreadable rejection (non-2xx) from an unreadable success (2xx)."""
    status: int | None = getattr(_last, 'status', None)
    return status


class LoggingTransport:
    """Wraps the SDK's own httpx transport; logs method, path and status — never headers or
    bodies (they carry the bearer token and card data)."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        _last.status = None
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('PayPal %s %s failed: %s', request.method, urlsplit(request.url).path,
                           type(exc).__name__)
            raise
        _last.status = response.status_code
        logger.info('PayPal %s %s -> %s (%.0f ms, debug_id=%s)', request.method,
                    urlsplit(request.url).path, response.status_code,
                    (time.monotonic() - started) * 1000, response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


# --------------------------------------------------------------------------------------------
# Client lifetime: one long-lived client per process, built lazily (after any fork)
# --------------------------------------------------------------------------------------------

_client: PaypalClient | None = None
_client_lock = threading.Lock()


def build_client(config: PayPalConfig) -> PaypalClient:
    config.validate()
    return PaypalClient(
        base_url=config.resolved_base_url(),
        timeout=config.timeout,
        custom_http_client=LoggingTransport(HttpxClient(timeout=config.timeout)),
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


def get_client(config: PayPalConfig) -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client(config)
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process client (tests inject a client over a stub transport)."""
    global _client
    with _client_lock:
        _client = client


# --------------------------------------------------------------------------------------------
# Error boundary
# --------------------------------------------------------------------------------------------

class ProviderError(Exception):
    """A PayPal failure, already translated into what this API answers.

    ``status_code`` is the status this API returns; ``outcome_unknown`` says whether anything may
    have happened at PayPal; ``never_sent`` that nothing reached PayPal at all.
    """

    def __init__(self, status_code: int, code: str, message: str, *, outcome_unknown: bool = False,
                 never_sent: bool = False, provider_status: int | None = None,
                 issues: list[str] | None = None, debug_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.never_sent = never_sent
        self.provider_status = provider_status
        self.issues = issues or []
        self.debug_id = debug_id

    @property
    def refused(self) -> bool:
        """PayPal answered with a definite rejection (a 4xx): nothing was done."""
        return self.provider_status is not None and 400 <= self.provider_status < 500 \
            and not self.outcome_unknown

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {'code': self.code, 'message': self.message,
                                   'outcomeUnknown': self.outcome_unknown}
        if self.provider_status is not None:
            body['providerStatus'] = self.provider_status
        if self.issues:
            body['issues'] = self.issues
        if self.debug_id:
            body['paypalDebugId'] = self.debug_id
        return body


def _issues(error: Error) -> list[str]:
    details = error.details
    if isinstance(details, list):
        return [d.issue for d in details]
    return []


def provider_error(status: int, error: object) -> ProviderError:
    """One map from PayPal's answer to ours, used for raised and raw (Failure) results alike."""
    if isinstance(error, OAuthProviderError):
        return ProviderError(502, 'paypal_credentials_rejected',
                             'PayPal refused this application\'s credentials.', never_sent=True,
                             provider_status=status)
    if status in (401, 403):
        return ProviderError(502, 'paypal_credentials_rejected',
                             'PayPal refused this application\'s credentials or permissions.',
                             provider_status=status)
    if status == 429:
        return ProviderError(503, 'paypal_rate_limited', 'PayPal is rate-limiting requests.',
                             provider_status=status)
    if 400 <= status < 500:
        if isinstance(error, Error):
            our_status = 409 if status in (404, 409) else 422
            issues = _issues(error)
            message = error.message
            if issues:
                message = '%s (%s)' % (message, ', '.join(issues))
            return ProviderError(our_status, 'paypal_rejected', message, provider_status=status,
                                 issues=issues, debug_id=error.debug_id)
        return ProviderError(422, 'paypal_rejected', 'PayPal rejected the request (HTTP %s).' % status,
                             provider_status=status)
    return ProviderError(502, 'paypal_unavailable', 'PayPal is unavailable (HTTP %s).' % status,
                         outcome_unknown=status >= 500, provider_status=status)


def translate(exc: BaseException) -> ProviderError:
    """Every failure kind an SDK call can raise, mapped once."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, 'paypal_unreachable', 'Could not reach PayPal; nothing was sent.',
                             never_sent=True)
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, 'paypal_no_response',
                             'PayPal did not answer; the request may have been processed.',
                             outcome_unknown=True)
    if isinstance(exc, ValueError):   # pydantic ValidationError, or a non-JSON body
        status = last_response_status()
        if status is not None and 400 <= status < 500:
            # A rejection whose error body did not decode: the detail is lost, the refusal is not.
            return provider_error(status, RawError(HttpResponse(status_code=status, headers={})))
        return ProviderError(502, 'paypal_unreadable_response',
                             'PayPal\'s response could not be read; the request may have been processed.',
                             outcome_unknown=True, provider_status=status)
    raise exc

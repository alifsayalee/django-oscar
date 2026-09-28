"""
The one place this app talks to the PayPal SDK from: configuration, the
long-lived client, request logging, money formatting and the translation of
every SDK failure into ``PaymentError``.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import (
    ApiError,
    ClientCredentials,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
)
from paypal.models import Error

logger = logging.getLogger("apps.paypal_payments")

# The only server the SDK declares (sdk-map.md, "Servers & auth"). Any other
# environment has to name its host through PAYPAL_BASE_URL.
ENVIRONMENT_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
}

# Transport failures raised before the request left: nothing can have happened.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# ISO 4217 minor units for every currency that does not use two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str
    base_url: str
    currency: str
    timeout: float


def get_config() -> PayPalConfig:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
    currency = (settings.PAYPAL_CURRENCY or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be a three-letter ISO 4217 code.")
    base_url = (settings.PAYPAL_BASE_URL or "").strip()
    if not base_url:
        environment = (settings.PAYPAL_ENVIRONMENT or "").strip().lower()
        try:
            base_url = ENVIRONMENT_BASE_URLS[environment]
        except KeyError:
            raise ImproperlyConfigured(
                f"PAYPAL_ENVIRONMENT={environment!r} has no known API host; set PAYPAL_BASE_URL."
            ) from None
    timeout = float(settings.PAYPAL_TIMEOUT)
    if not timeout > 0:
        raise ImproperlyConfigured("PAYPAL_TIMEOUT must be a positive number of seconds.")
    return PayPalConfig(client_id, client_secret, base_url, currency, timeout)


class LoggingTransport:
    """Wraps the SDK's transport and logs method, URL, status and duration only
    - never headers (the bearer token) or bodies (card data)."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.RequestError as exc:
            logger.warning("PayPal %s %s failed: %s", request.method, _path(request.url), type(exc).__name__)
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, debug id %s)",
            request.method,
            _path(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _path(url: str) -> str:
    # Query strings on this API carry no secrets, but keep the log line short.
    return httpx.URL(url).path


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """The process-wide client, built on first use (so after any fork) and
    closed at interpreter exit."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                config = get_config()
                _client = PaypalClient(
                    base_url=config.base_url,
                    custom_http_client=LoggingTransport(HttpxClient(timeout=config.timeout)),
                    oauth2=ClientCredentials(
                        client_id=config.client_id, client_secret=config.client_secret
                    ),
                )
                atexit.register(_client.close)
    return _client


def reset_client() -> None:
    """Close and forget the client (tests, credential rotation)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def quantize(value: Decimal, currency: str) -> Decimal:
    places = _EXPONENT.get(currency, 2)
    return value.quantize(Decimal(1).scaleb(-places))


def money_str(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


def same_money(expected: Decimal, expected_currency: str, value: object, currency: object) -> bool:
    """Compare an echoed PayPal amount as Decimal: "10.0" and "10.00" are equal."""
    if not isinstance(value, str) or not isinstance(currency, str):
        return False
    try:
        return Decimal(value) == expected and currency == expected_currency
    except ArithmeticError:
        return False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PaymentError(Exception):
    """
    The single failure type the views translate into a response.

    ``status_code`` is what our API answers; ``outcome_unknown`` says whether
    PayPal may have acted anyway.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str = "payment_error",
        outcome_unknown: bool = False,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.details = details or []


def error_details(error: object) -> list[dict[str, str]]:
    """PayPal's issue codes and descriptions. The ``value`` member is dropped:
    it can echo request data such as a card number."""
    if not isinstance(error, Error) or not isinstance(error.details, list):
        return []
    out = []
    for item in error.details:
        entry = {"issue": item.issue}
        if isinstance(item.description, str):
            entry["description"] = item.description
        if isinstance(item.field, str):
            entry["field"] = item.field
        out.append(entry)
    return out


def issues(error: object) -> set[str]:
    return {d["issue"] for d in error_details(error)}


def describe(status: int, error: object) -> str:
    """A log/record line for a PayPal error: name, message, debug id, issues."""
    if isinstance(error, Error):
        found = ", ".join(sorted(issues(error))) or "-"
        return f"HTTP {status} {error.name}: {error.message} (issues: {found}; debug id {error.debug_id})"
    if isinstance(error, OAuthProviderError):
        return f"HTTP {status} token request refused: {error.error}"
    if isinstance(error, RawError):
        return f"HTTP {status} (undocumented error body)"
    return f"HTTP {status}"


def provider_error(status: int, error: object) -> PaymentError:
    """Map PayPal's answer to our API's. 401/403/429 are ours, not the caller's."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return PaymentError(502, "The payment provider refused this site's credentials.", code="provider_auth")
    if status == 429:
        return PaymentError(503, "The payment provider is rate-limiting requests; try again shortly.", code="provider_busy")
    if 400 <= status < 500:
        message = "The payment provider rejected the request."
        if isinstance(error, Error):
            message = error.message
        return PaymentError(
            422 if status in (400, 422) else status,
            message,
            code="provider_rejected",
            details=error_details(error),
        )
    return PaymentError(502, "The payment provider is unavailable.", code="provider_unavailable", outcome_unknown=True)


def call_read(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a read-only SDK call and translate every failure kind."""
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        raise provider_error(exc.status_code, exc.error) from exc
    except ValueError as exc:  # pydantic ValidationError or a non-JSON body
        raise PaymentError(502, "The payment provider sent an unreadable response.", code="provider_unreadable") from exc
    except NEVER_SENT as exc:
        raise PaymentError(502, "Could not reach the payment provider.", code="provider_unreachable") from exc
    except httpx.RequestError as exc:
        raise PaymentError(504, "The payment provider did not answer in time.", code="provider_timeout") from exc

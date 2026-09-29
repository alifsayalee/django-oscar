"""
The one PayPal Server SDK client this process uses, plus the helpers every
call site shares: money formatting and the provider-error mapping.
"""

import atexit
import logging
import threading
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ApiError, ClientCredentials, OAuthProviderError, RawError, UnsetType
from pay_pal_server_sdk.models import DefaultError, Error, Money
from pydantic import ValidationError

logger = logging.getLogger("apps.payments")

# The SDK's only declared server (sdk-map "Servers & auth"). Any other
# environment has to name its host explicitly through PAYPAL_BASE_URL.
_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

CLIENT_TIMEOUT_SECONDS = 20.0

_client: PayPalServerSdkClient | None = None
_client_lock = threading.Lock()

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def resolve_base_url() -> str:
    override = getattr(settings, "PAYPAL_BASE_URL", "") or ""
    if override:
        return str(override)
    environment = getattr(settings, "PAYPAL_ENVIRONMENT", "") or ""
    try:
        return _BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL" % environment
        ) from None


def build_client() -> PayPalServerSdkClient:
    client_id = getattr(settings, "PAYPAL_CLIENT_ID", "") or ""
    client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", "") or ""
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set")
    return PayPalServerSdkClient(
        base_url=resolve_base_url(),
        timeout=CLIENT_TIMEOUT_SECONDS,
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PayPalServerSdkClient:
    """The process-wide client, built lazily so it is created after any worker fork."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PayPalServerSdkClient | None) -> None:
    """Swap the process-wide client (tests inject one built on a stub transport)."""
    global _client
    with _client_lock:
        _client = client


def currency() -> str:
    value = getattr(settings, "PAYPAL_CURRENCY", "") or ""
    if not value:
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be set")
    return str(value).upper()


# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def quantize(value: Decimal, currency_code: str) -> Decimal:
    places = _EXPONENT.get(currency_code, 2)
    return value.quantize(Decimal(1).scaleb(-places))


def format_amount(value: Decimal, currency_code: str) -> str:
    return str(quantize(value, currency_code))


def money(value: Decimal, currency_code: str) -> Money:
    return Money(currency_code=currency_code, value=format_amount(value, currency_code))


class ProviderError(Exception):
    """A PayPal failure translated for our API boundary."""

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False, code: str = "provider_error"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.code = code


def describe_error(error: object) -> str:
    """A card-free, operator-readable summary of a decoded PayPal error body."""
    if isinstance(error, (Error, DefaultError)):
        parts = [f"{error.name}: {error.message}"]
        details = error.details
        if isinstance(details, list):
            for d in details[:3]:
                issue = getattr(d, "issue", "")
                description = unset_to_none(getattr(d, "description", None)) or ""
                parts.append(f"{issue} {description}".strip())
        parts.append(f"(debug_id {error.debug_id})")
        return " ".join(p for p in parts if p)[:500]
    if isinstance(error, OAuthProviderError):
        return f"PayPal refused our credentials: {error.error}"[:500]
    if isinstance(error, RawError):
        return f"HTTP {error.status_code} from PayPal"
    return "PayPal error"


def provider_error(status: int, error: object) -> ProviderError:
    """One map from PayPal's answer to our boundary's answer."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return ProviderError(502, "PayPal refused this application's credentials.", code="provider_auth")
    if status == 429:
        return ProviderError(503, "PayPal is rate-limiting this application; try again shortly.", code="rate_limited")
    if 400 <= status < 500:
        return ProviderError(422 if status in (400, 422) else 409, describe_error(error), code="provider_rejected")
    return ProviderError(502, "PayPal is unavailable.", outcome_unknown=status >= 500, code="provider_unavailable")


def translate(exc: BaseException) -> ProviderError:
    """Translate an exception raised by a PayPal read (or an unguarded write) at the boundary."""
    if isinstance(exc, ApiError):
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, ValidationError):
        return ProviderError(502, "PayPal's response could not be read.", outcome_unknown=True, code="unreadable")
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, "Could not reach PayPal; nothing was sent.", code="never_sent")
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, "No response from PayPal.", outcome_unknown=True, code="no_response")
    if isinstance(exc, ValueError):
        return ProviderError(502, "PayPal's response could not be read.", outcome_unknown=True, code="unreadable")
    raise exc


def unset_to_none(value: Any) -> Any:
    """Resolve the SDK's UNSET sentinel before a value leaves the integration layer."""
    return None if isinstance(value, UnsetType) else value

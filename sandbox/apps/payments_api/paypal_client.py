"""PayPal SDK wiring for the sandbox: client factory, money formatting, status mapping and
the single error boundary that turns every SDK failure kind into one ``ProviderError``.

Contract facts (signatures, wire names, error unions, enum members) come from the plan's
contract sheet / a fresh SDK lookup — never from memory.
"""
from __future__ import annotations

import atexit
import logging
import threading
from decimal import Decimal
from typing import Optional

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import ApiError, ClientCredentials, RawError

log = logging.getLogger("payments_api")

# Currencies PayPal does not accept decimal amounts for get zero fractional digits; a handful
# use three. Everything else is two. The scale belongs to the currency, never the literal 2.
_CURRENCY_EXPONENT = {
    "JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "VND": 0, "CLP": 0,
    "BHD": 3, "KWD": 3, "TND": 3,
}


def currency_exponent(currency: str) -> int:
    return _CURRENCY_EXPONENT.get((currency or "").upper(), 2)


def format_amount(value: Decimal, currency: str) -> str:
    """Format a Decimal money value for the PayPal wire, scaled to the currency."""
    places = currency_exponent(currency)
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum))


def amounts_equal(a, b, currency: str) -> bool:
    places = currency_exponent(currency)
    quantum = Decimal(1).scaleb(-places)
    return Decimal(a).quantize(quantum) == Decimal(b).quantize(quantum)


# ---------------------------------------------------------------------------
# Status mapping — the one place a provider status becomes ours. Members are
# enumerated by name; anything unlisted is "unknown", never "failed".
# ---------------------------------------------------------------------------
def authorization_outcome(status) -> str:
    s = str(status) if status is not None else ""
    if s == "CREATED":
        return "authorized"
    if s in ("CAPTURED", "PARTIALLY_CAPTURED"):
        return "captured"
    if s == "VOIDED":
        return "voided"
    if s == "DENIED":
        return "failed"
    if s == "PENDING":
        return "pending"
    return "unknown"


def capture_outcome(status) -> str:
    s = str(status) if status is not None else ""
    if s == "COMPLETED":
        return "captured"
    if s == "PARTIALLY_REFUNDED":
        return "partially_refunded"
    if s == "REFUNDED":
        return "refunded"
    if s in ("DECLINED", "FAILED"):
        return "failed"
    if s == "PENDING":
        return "pending"
    return "unknown"


# ---------------------------------------------------------------------------
# Client factory — lazily built module global, long-lived, closed at exit.
# ---------------------------------------------------------------------------
_client: Optional[PaypalClient] = None
_client_lock = threading.Lock()


def _resolve_base_url() -> str:
    override = (getattr(settings, "PAYPAL_BASE_URL", "") or "").strip()
    if override:
        return override  # used verbatim for every call, incl. the token fetch
    env = (getattr(settings, "PAYPAL_ENVIRONMENT", "sandbox") or "sandbox").strip().lower()
    hosts = {
        "sandbox": "https://api-m.sandbox.paypal.com",
        "live": "https://api-m.paypal.com",
        "production": "https://api-m.paypal.com",
    }
    try:
        return hosts[env]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={env!r} is not one of {sorted(hosts)}; "
            "set PAYPAL_BASE_URL to override explicitly."
        )


def get_client() -> PaypalClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        client_id = getattr(settings, "PAYPAL_CLIENT_ID", "") or ""
        client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", "") or ""
        if not client_id or not client_secret:
            # The SDK would build happily with no auth and every call would 401 — fail loudly.
            raise ImproperlyConfigured(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured in the environment."
            )
        _client = PaypalClient(
            oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
            base_url=_resolve_base_url(),
            timeout=20.0,
        )
        atexit.register(_close_client)
        return _client


def _close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass
        _client = None


# ---------------------------------------------------------------------------
# Error boundary — one ProviderError type carrying the caller-facing status and
# whether the outcome is unknown (may have landed).
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: Optional[int] = None,
        debug_id: Optional[str] = None,
        name: str = "",
        issues: Optional[list] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.debug_id = debug_id
        self.name = name
        self.issues = issues or []


# httpx failures that happen before the request leaves: nothing can have landed.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# PayPal's typed error name that means a resend under the same idempotency key hit an existing
# resource — an earlier attempt landed. Read from the error union at the call site, not here.
DUPLICATE_ERROR_NAMES = {"DUPLICATE_INVOICE_ID", "DUPLICATE_REQUEST_ID", "IDEMPOTENCY_ERROR"}


def error_name(err) -> str:
    """Best-effort PayPal error 'name' from a typed Error body (else '')."""
    name = getattr(err, "name", None)
    return str(name) if name else ""


def call(operation, *args, write: bool = False, **kwargs):
    """Invoke a parsed SDK operation and translate every failure kind into ProviderError.

    ``write`` marks an operation whose outcome is unknown when the reply is lost (a read that
    times out is safe to treat as failed-to-read; a write that times out may have landed).
    Returns the decoded payload on success.
    """
    try:
        return operation(*args, **kwargs)
    except ApiError as e:
        status = e.status_code
        err = e.error
        name = ""
        message = ""
        debug_id = None
        issues: list = []
        if isinstance(err, RawError):
            body = err.text()[:500]
            message = body or f"HTTP {status}"
        else:
            name = error_name(err)
            message = str(getattr(err, "message", "") or name or f"HTTP {status}")
            debug_id = getattr(err, "debug_id", None)
            details = getattr(err, "details", None)
            if details and not isinstance(details, type(None)):
                for d in details:
                    issue = getattr(d, "issue", None)
                    if issue:
                        issues.append(str(issue))
        if status in (401, 403):
            # Our credentials/scopes, not the caller's fault.
            raise ProviderError(502, "Payment provider rejected our credentials.",
                                provider_status=status, debug_id=debug_id, name=name,
                                issues=issues) from e
        if status == 429:
            raise ProviderError(503, "Payment provider is rate-limiting; try again shortly.",
                                provider_status=status, debug_id=debug_id, name=name,
                                issues=issues) from e
        if status in (400, 404, 409, 422):
            detail = f"{message} ({', '.join(issues)})" if issues else message
            raise ProviderError(status, detail, provider_status=status,
                                debug_id=debug_id, name=name, issues=issues) from e
        raise ProviderError(502, "Payment provider error.", provider_status=status,
                            debug_id=debug_id, name=name, issues=issues) from e
    except ValidationError as e:
        # Decode failure: bypasses both response modes. On a 2xx write the outcome is unknown.
        if write:
            raise ProviderError(504, "Unreadable provider response; outcome unknown.",
                                outcome_unknown=True) from e
        raise ProviderError(502, "Unreadable provider response.") from e
    except _NEVER_SENT as e:
        raise ProviderError(502, "Payment provider unreachable; nothing was sent.") from e
    except httpx.RequestError as e:
        # It may have landed.
        raise ProviderError(504, "No response from payment provider; outcome unknown.",
                            outcome_unknown=bool(write)) from e

"""
The one place the PayPal SDK client is built and every SDK failure is translated.

* One long-lived sync client per process, built lazily on first use (after any
  worker fork) from Django settings, closed at interpreter exit.
* ``call()`` runs an SDK operation and converts every failure kind into a
  :class:`PayPalError` whose ``http_status`` and ``outcome_unknown`` say what the
  API should answer and whether PayPal may have acted.
* Nothing here logs a request or response body or any header - card data and
  the bearer token travel in those.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import (
    UNSET,
    ApiError,
    ClientCredentials,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    UnsetType,
)
from paypal.models import Error, Money
from pydantic import ValidationError

logger = logging.getLogger("apps.payments.gateway")

T = TypeVar("T")

# The only host the SDK declares (sdk-map.md, "Servers & auth"). Passed
# explicitly so the choice is visible rather than an SDK default.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PayPalError(Exception):
    """A PayPal call that did not produce a usable result."""

    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issue: str = "",
        debug_id: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status  # what our API answers
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown  # PayPal may have acted
        self.issue = issue  # PayPal's own issue code, when it gave one
        self.debug_id = debug_id

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"error": self.code, "message": self.message}
        if self.issue:
            body["paypalIssue"] = self.issue
        if self.debug_id:
            body["paypalDebugId"] = self.debug_id
        if self.outcome_unknown:
            body["outcomeUnknown"] = True
        return body


class PayPalRejected(PayPalError):
    """PayPal refused the request: it definitely did not happen."""


class PayPalUnavailable(PayPalError):
    """PayPal could not be reached, or did not answer."""


class PayPalConfigError(PayPalError):
    """Our credentials, scopes or quota - not the caller's to fix."""


# Transport failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's request (python-error-handling).
CALLER_STATUSES = (400, 404, 409, 422)


def _describe(error: Error) -> tuple[str, str]:
    """PayPal's first detail issue and description, falling back to name/message."""
    details = error.details
    if not isinstance(details, UnsetType) and details:
        first = details[0]
        description = first.description if isinstance(first.description, str) else error.message
        return first.issue, description
    return error.name, error.message


def translate(exc: BaseException, operation: str) -> PayPalError:
    """Map any failure out of an SDK call onto a PayPalError (one ladder for every call)."""
    if isinstance(exc, ApiError):
        payload = exc.error
        status = exc.status_code
        if isinstance(payload, OAuthProviderError):
            logger.error("PayPal %s: credentials rejected (%s)", operation, payload.error)
            return PayPalConfigError(502, "paypal_auth_failed", "Payment provider is misconfigured.")
        debug_id = exc.response.headers.get("paypal-debug-id", "")
        if isinstance(payload, Error):
            issue, description = _describe(payload)
            debug_id = payload.debug_id or debug_id
        else:
            issue, description = "", ""
        if status in (401, 403):
            logger.error("PayPal %s: HTTP %s %s (debug id %s)", operation, status, issue, debug_id)
            return PayPalConfigError(502, "paypal_not_permitted", "Payment provider refused our credentials.",
                                     issue=issue, debug_id=debug_id)
        if status == 429:
            logger.warning("PayPal %s: rate limited (debug id %s)", operation, debug_id)
            return PayPalUnavailable(503, "paypal_rate_limited", "Payment provider is busy; try again shortly.",
                                     debug_id=debug_id)
        if status in CALLER_STATUSES:
            logger.info("PayPal %s: rejected HTTP %s %s (debug id %s)", operation, status, issue, debug_id)
            return PayPalRejected(422 if status in (400, 422) else status, "paypal_rejected",
                                  description or "The payment provider rejected the request.",
                                  issue=issue, debug_id=debug_id)
        # 5xx, and every 4xx not named above. A 5xx on a write may still have landed.
        logger.error("PayPal %s: HTTP %s (debug id %s)", operation, status, debug_id)
        return PayPalUnavailable(502, "paypal_error", "Payment provider error.",
                                 outcome_unknown=status >= 500, debug_id=debug_id)
    if isinstance(exc, NEVER_SENT):
        logger.error("PayPal %s: not sent (%s)", operation, type(exc).__name__)
        return PayPalUnavailable(502, "paypal_unreachable", "Payment provider is unreachable; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        logger.error("PayPal %s: no response (%s)", operation, type(exc).__name__)
        return PayPalUnavailable(504, "paypal_no_response", "Payment provider did not answer; outcome unknown.",
                                 outcome_unknown=True)
    if isinstance(exc, (ValidationError, ValueError)):
        # Never log str(exc): pydantic echoes input values.
        logger.error("PayPal %s: unreadable response (%s)", operation, type(exc).__name__)
        return PayPalUnavailable(504, "paypal_unreadable", "Payment provider response was unreadable; outcome unknown.",
                                 outcome_unknown=True)
    raise exc


def call(operation: str, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run one SDK operation, translating every failure kind via :func:`translate`."""
    try:
        return fn(*args, **kwargs)
    except (ApiError, httpx.RequestError, ValidationError, ValueError) as exc:
        raise translate(exc, operation) from exc


def unreadable(operation: str, member: str) -> PayPalUnavailable:
    """A 2xx that lacks a member we depend on: PayPal may have acted, we cannot tell how."""
    logger.error("PayPal %s: response lacked %s", operation, member)
    return PayPalUnavailable(504, "paypal_unreadable",
                             "Payment provider response was incomplete; outcome unknown.", outcome_unknown=True)


def is_not_found(exc: PayPalError) -> bool:
    return exc.http_status == 404


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LoggingTransport:
    """Logs method, path and status of each PayPal call - never headers, query or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("PayPal %s %s -> %s", request.method, path, type(exc).__name__)
            raise
        logger.info("PayPal %s %s -> %s (%.0f ms, debug id %s)", request.method, path, response.status_code,
                    (time.monotonic() - started) * 1000, response.headers.get("paypal-debug-id", "-"))
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    override = str(getattr(settings, "PAYPAL_BASE_URL", "") or "").strip()
    if override:
        return override
    environment = str(getattr(settings, "PAYPAL_ENVIRONMENT", "") or "sandbox").strip().lower()
    if environment == "sandbox":
        return SANDBOX_BASE_URL
    # The SDK declares only the sandbox host; any other host must be configured, not guessed.
    raise ImproperlyConfigured(
        "PAYPAL_ENVIRONMENT=%r has no built-in API host; set PAYPAL_BASE_URL." % environment
    )


def currency() -> str:
    code = str(getattr(settings, "PAYPAL_CURRENCY", "") or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be a three-letter ISO-4217 code.")
    return code


def build_client() -> PaypalClient:
    missing = [name for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET") if not getattr(settings, name, "")]
    if missing:
        raise ImproperlyConfigured("Missing PayPal settings: " + ", ".join(missing))
    timeout = float(getattr(settings, "PAYPAL_TIMEOUT_SECONDS", 20.0))
    return PaypalClient(
        base_url=base_url(),
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Replace the process client (tests inject one built on a stub transport)."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    if previous is not None and previous is not client:
        previous.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


# ---------------------------------------------------------------------------
# Money and time
# ---------------------------------------------------------------------------

# ISO-4217 minor units that are not 2.
_EXPONENTS = {"JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "KWD": 3, "BHD": 3, "TND": 3, "OMR": 3, "JOD": 3}


def exponent(code: str) -> int:
    return _EXPONENTS.get(code, 2)


def quantize(value: Decimal, code: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-exponent(code)), rounding=ROUND_HALF_UP)


def money(value: Decimal, code: str) -> Money:
    return Money(currency_code=code, value=str(quantize(value, code)))


def amount_of(value: Money | UnsetType) -> tuple[Decimal, str] | None:
    """(amount, currency) from a PayPal Money, or None when absent/unreadable."""
    if isinstance(value, UnsetType):
        return None
    try:
        return Decimal(value.value), value.currency_code
    except (InvalidOperation, TypeError):
        return None


def parse_time(value: str | UnsetType) -> datetime | None:
    if isinstance(value, UnsetType) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def text_or_empty(value: object) -> str:
    """A plain string for our columns; UNSET and non-strings become ''."""
    if value is UNSET or value is None:
        return ""
    return str(value)


def raw_error_text(error: RawError) -> str:
    return error.text()[:200]

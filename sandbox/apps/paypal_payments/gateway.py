"""
The one place this app talks to the PayPal SDK.

* ``get_client()`` - one long-lived sync client per process, built on first
  use (never at import), closed at interpreter exit.
* ``call()`` - runs one SDK call and turns every way it can fail into a
  ``PayPalError`` that says what our API should answer and whether anything
  may have happened at PayPal.
"""

import atexit
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import (
    ApiError,
    ClientCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    UnsetType,
)
from paypal.models import Error

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The SDK declares a single server; any other environment must name its host
# explicitly through PAYPAL_BASE_URL.
BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

# Seconds. The SDK performs no retries, so this bounds each attempt; the
# unknown-outcome lookup below makes at most one more.
TIMEOUT = 20.0

# Failures raised before a request leaves this process: nothing can have happened.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# PayPal statuses that are the caller's to fix, passed through by name.
CALLER_STATUSES = (400, 404, 409, 422)


class PayPalError(Exception):
    """
    A PayPal call that did not succeed.

    ``status_code`` is what our API answers; ``outcome_unknown`` is True when
    the request may have taken effect at PayPal (so it must be looked up, not
    assumed failed).
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        paypal_status: int | None = None,
        name: str = "",
        issue: str = "",
        description: str = "",
        debug_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.paypal_status = paypal_status
        self.name = name
        self.issue = issue
        self.description = description
        self.debug_id = debug_id

    def as_dict(self) -> dict[str, object]:
        return {"message": self.message, **self.details()}

    def details(self) -> dict[str, object]:
        """PayPal's diagnostics, safe to return to a caller."""
        data: dict[str, object] = {}
        if self.issue:
            data["paypalIssue"] = self.issue
        if self.description:
            data["paypalDescription"] = self.description
        if self.debug_id:
            data["paypalDebugId"] = self.debug_id
        if self.outcome_unknown:
            data["outcomeUnknown"] = True
        return data


class ProviderConfigError(PayPalError):
    """Our credentials, permissions or configuration - never the caller's fault."""


class ProviderRejected(PayPalError):
    """PayPal answered and refused the request: nothing happened."""


class ProviderUnavailable(PayPalError):
    """PayPal could not be reached, or failed; see ``outcome_unknown``."""


# ---------------------------------------------------------------------------
# Transport: logs method/path/status and remembers the last status per thread
# so an unreadable body can be classified by what PayPal actually answered.
# ---------------------------------------------------------------------------

_last = threading.local()


def _last_status() -> int | None:
    status: int | None = getattr(_last, "status", None)
    return status


class RecordingTransport:
    """Wraps the SDK's httpx transport. Never logs headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = httpx.URL(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            logger.warning(
                "PayPal %s %s failed after %.0f ms: %s",
                request.method,
                path,
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
            )
            raise
        _last.status = response.status_code
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, paypal-debug-id=%s)",
            request.method,
            path,
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


# ---------------------------------------------------------------------------
# Client lifetime
# ---------------------------------------------------------------------------

_client: PaypalClient | None = None
_client_lock = threading.Lock()


def resolve_base_url() -> str:
    override = str(settings.PAYPAL_BASE_URL or "").strip()
    if override:
        return override
    environment = str(settings.PAYPAL_ENVIRONMENT or "").strip().lower()
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; "
            "set PAYPAL_BASE_URL to the API base address for that environment."
        ) from None


def build_client() -> PaypalClient:
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_CURRENCY")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ImproperlyConfigured("Missing PayPal settings: " + ", ".join(missing))
    return PaypalClient(
        base_url=resolve_base_url(),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
        custom_http_client=RecordingTransport(HttpxClient(timeout=TIMEOUT)),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> PaypalClient | None:
    """Swap the process client (tests, credential rotation). Returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------


def _first_detail(error: Error) -> tuple[str, str]:
    if isinstance(error.details, UnsetType) or not error.details:
        return "", ""
    detail = error.details[0]
    description = detail.description if not isinstance(detail.description, UnsetType) else ""
    return detail.issue, description


def _translate_api_error(e: ApiError) -> PayPalError:
    status = e.status_code
    if isinstance(e.error, OAuthProviderError):
        return ProviderConfigError(502, "PayPal rejected the merchant credentials.", paypal_status=status)
    if status in (401, 403):
        return ProviderConfigError(
            502, "PayPal refused the merchant's credentials or permissions.", paypal_status=status
        )
    if status == 429:
        return ProviderUnavailable(503, "PayPal is rate-limiting requests; try again shortly.", paypal_status=status)
    if status in CALLER_STATUSES:
        if isinstance(e.error, Error):
            issue, description = _first_detail(e.error)
            return ProviderRejected(
                status,
                description or e.error.message,
                paypal_status=status,
                name=e.error.name,
                issue=issue,
                description=description,
                debug_id=e.error.debug_id,
            )
        return ProviderRejected(status, "PayPal rejected the request.", paypal_status=status)
    # 5xx and every status we have not mapped: ours to deal with. A 5xx on a
    # write may still have landed.
    detail = e.error.text()[:200] if isinstance(e.error, RawError) else ""
    if detail:
        logger.warning("PayPal HTTP %s body: %s", status, detail)
    return ProviderUnavailable(
        502, "PayPal failed to process the request.", outcome_unknown=status >= 500, paypal_status=status
    )


def call(fn: Callable[[], T]) -> T:
    """
    Run one SDK call. Build request models *before* calling this, so a local
    validation error is never mistaken for an unreadable PayPal response.
    """
    _last.status = None
    try:
        return fn()
    except ApiError as e:
        raise _translate_api_error(e) from e
    except ValueError as e:  # pydantic.ValidationError included: a body that did not decode
        status = _last_status()
        if status is not None and status >= 400:
            # PayPal rejected the request; only the error detail was lost.
            if status in CALLER_STATUSES:
                raise ProviderRejected(
                    status, "PayPal rejected the request.", paypal_status=status
                ) from e
            if status in (401, 403):
                raise ProviderConfigError(
                    502, "PayPal refused the merchant's credentials or permissions.", paypal_status=status
                ) from e
            raise ProviderUnavailable(
                502, "PayPal failed to process the request.", outcome_unknown=status >= 500, paypal_status=status
            ) from e
        raise ProviderUnavailable(
            502, "PayPal's response could not be read; the outcome is unknown.", outcome_unknown=True
        ) from e
    except NEVER_SENT as e:
        raise ProviderUnavailable(
            502, "PayPal could not be reached; nothing was sent.", outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        raise ProviderUnavailable(
            504, "No response from PayPal; the outcome is unknown.", outcome_unknown=True
        ) from e

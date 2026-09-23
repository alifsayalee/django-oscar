"""
The one place this app talks to the PayPal SDK.

* ``get_client()`` builds a single long-lived sync ``PaypalClient`` per process,
  lazily (importing this module never needs credentials) and closes it at exit.
* ``call()`` runs one SDK call and turns every failure kind into a
  ``ProviderError`` whose ``status_code``/``outcome_unknown`` say what the
  caller may conclude. The SDK performs no retries, and neither does this
  module: an unknown outcome is recovered by re-sending under the same
  PayPal-Request-Id on the next request.
"""

from __future__ import annotations

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
)
from paypal.models import Error
from pydantic import ValidationError

logger = logging.getLogger("apps.paypal_payments.gateway")

T = TypeVar("T")

# The SDK declares exactly one server. Any other environment must name its
# host through PAYPAL_BASE_URL; an unknown name never falls through to a default.
_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


class ProviderError(Exception):
    """A PayPal call did not produce a usable result."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        name: str = "",
        issues: list[str] | None = None,
        debug_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code  # what our API answers
        self.message = message
        self.outcome_unknown = outcome_unknown  # may it have happened at PayPal?
        self.provider_status = provider_status
        self.name = name
        self.issues = issues or []
        self.debug_id = debug_id

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "error": self.message,
            "outcomeUnknown": self.outcome_unknown,
        }
        if self.name:
            body["paypalError"] = self.name
        if self.issues:
            body["paypalIssues"] = self.issues
        if self.debug_id:
            body["paypalDebugId"] = self.debug_id
        return body


class ProviderConfigError(ProviderError):
    """Our credentials, scopes or configuration were refused — not the caller's fault."""


class ProviderRejected(ProviderError):
    """PayPal said no to this request (validation, decline, conflict, not found)."""


class ProviderUnavailable(ProviderError):
    """No readable answer: PayPal down, rate-limited, or the reply was lost."""


class LoggingTransport:
    """Logs method, URL, status and latency. Never headers or bodies (tokens, card data)."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "PayPal %s %s failed after %.0f ms: %s",
                request.method,
                request.url.split("?")[0],
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
            )
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, debug_id=%s)",
            request.method,
            request.url.split("?")[0],
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client: PaypalClient | None = None
_lock = threading.Lock()


def resolve_base_url() -> str:
    override = (settings.PAYPAL_BASE_URL or "").strip()
    if override:
        return override
    environment = (settings.PAYPAL_ENVIRONMENT or "").strip().lower()
    if not environment:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT (or PAYPAL_BASE_URL) must be set"
        )
    try:
        return _BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL for it"
            % environment
        ) from None


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
        if not getattr(settings, name)
    ]
    if missing:
        raise ImproperlyConfigured("Missing PayPal settings: " + ", ".join(missing))
    base_url = resolve_base_url()
    if transport is None:
        # We supply the transport, so the timeout must live on it.
        transport = HttpxClient(timeout=float(settings.PAYPAL_TIMEOUT))
    return PaypalClient(
        base_url=base_url,
        custom_http_client=LoggingTransport(transport),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process client (tests, credential rotation). Closes the old one."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


def _error_details(error: object) -> tuple[str, str, list[str], str]:
    """(name, message, issue codes, debug_id) from a typed ``Error``; never echoes ``value``."""
    if isinstance(error, Error):
        issues: list[str] = []
        if isinstance(error.details, list):
            for detail in error.details:
                text = detail.issue
                if isinstance(detail.description, str):
                    text = "%s: %s" % (detail.issue, detail.description)
                issues.append(text)
        return error.name, error.message, issues, error.debug_id
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return "", "", [], ""
        if isinstance(body, dict):
            issues = [
                str(d.get("issue", ""))
                for d in body.get("details", []) or []
                if isinstance(d, dict)
            ]
            return (
                str(body.get("name", "")),
                str(body.get("message", "")),
                issues,
                str(body.get("debug_id", "")),
            )
    return "", "", [], ""


def call(operation: str, fn: Callable[[], T], *, write: bool) -> T:
    """
    Run one SDK call behind the error ladder.

    ``write`` marks calls that move money or create things: a 5xx or an
    unreadable 2xx there may have taken effect, so the outcome is unknown.
    """
    try:
        return fn()
    except ApiError as e:
        if isinstance(e.error, OAuthProviderError):
            logger.error(
                "PayPal token request refused during %s: %s", operation, e.error.error
            )
            raise ProviderConfigError(
                502, "PayPal rejected our API credentials."
            ) from e
        name, message, issues, debug_id = _error_details(e.error)
        status = e.status_code
        logger.warning(
            "PayPal %s -> HTTP %s %s (debug_id=%s, issues=%s)",
            operation,
            status,
            name,
            debug_id,
            issues,
        )
        if status in (401, 403):
            raise ProviderConfigError(
                502,
                "PayPal refused this merchant account's request.",
                provider_status=status,
                name=name,
                debug_id=debug_id,
            ) from e
        if status == 429:
            raise ProviderUnavailable(
                503,
                "PayPal is rate-limiting requests; try again shortly.",
                provider_status=status,
            ) from e
        if status in (400, 404, 409, 422):
            raise ProviderRejected(
                status,
                message or "PayPal rejected the request.",
                provider_status=status,
                name=name,
                issues=issues,
                debug_id=debug_id,
            ) from e
        raise ProviderUnavailable(
            502,
            "PayPal failed to process the request.",
            outcome_unknown=write and status >= 500,
            provider_status=status,
            name=name,
            debug_id=debug_id,
        ) from e
    except ValidationError as e:
        logger.error("PayPal %s returned an unreadable body", operation)
        raise ProviderUnavailable(
            502, "PayPal returned a response we could not read.", outcome_unknown=write
        ) from e
    except NEVER_SENT as e:
        raise ProviderUnavailable(
            502, "Could not reach PayPal; nothing was sent."
        ) from e
    except httpx.RequestError as e:
        raise ProviderUnavailable(
            504,
            "PayPal did not answer in time; the outcome is unknown.",
            outcome_unknown=True,
        ) from e

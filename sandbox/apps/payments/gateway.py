"""
The one place this app talks to the PayPal Server SDK.

* ``get_client()`` builds a single, process-wide synchronous ``PaypalClient``
  on first use (after any worker fork) and reuses it, so the connection pool
  and the OAuth token cache are shared by every request.
* ``call()`` runs one SDK call and turns every way it can fail into a
  ``ProviderError`` carrying the HTTP status this API should answer with and
  whether the PayPal side may have changed (``outcome_unknown``).

The SDK performs no retries; neither does this module. Writes are made safe
against repeats by the PayPal-Request-Id each caller derives from the
operation, not by resending here.
"""

import atexit
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from pydantic import ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Hosts per PAYPAL_ENVIRONMENT. PAYPAL_BASE_URL, when set, replaces the lookup.
BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

# Transport failures raised before the request left this process: nothing
# can have happened at PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's request
CALLER_STATUSES = (400, 404, 409, 422)


class ProviderError(Exception):
    """
    A PayPal call that did not produce a usable answer.

    ``status_code`` is what this API answers with; ``outcome_unknown`` is True
    when PayPal may have acted on the request anyway. ``issue`` is PayPal's
    machine-readable reason (``details[0].issue`` or the error ``name``) when
    PayPal rejected the request.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        issue: str = "",
        provider_status: int | None = None,
        debug_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.provider_status = provider_status
        self.debug_id = debug_id

    @property
    def rejected(self) -> bool:
        """PayPal answered and refused: nothing happened."""
        return self.provider_status is not None and not self.outcome_unknown


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    currency: str


def config() -> GatewayConfig:
    """Resolve and validate the PayPal settings (raises ImproperlyConfigured)."""
    base_url = settings.PAYPAL_BASE_URL
    if not base_url:
        environment = settings.PAYPAL_ENVIRONMENT.strip().lower()
        try:
            base_url = BASE_URLS[environment]
        except KeyError:
            raise ImproperlyConfigured(
                "PAYPAL_ENVIRONMENT must be one of %s (got %r)"
                % (", ".join(sorted(BASE_URLS)), settings.PAYPAL_ENVIRONMENT)
            ) from None
    currency = settings.PAYPAL_CURRENCY.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be a three-letter ISO-4217 code")
    return GatewayConfig(base_url=base_url.rstrip("/"), currency=currency)


def currency() -> str:
    return config().currency


class LoggingTransport:
    """Logs method, path, status and duration of every PayPal request — never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = httpx.URL(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "PayPal %s %s failed after %.0f ms: %s",
                request.method, path, (time.monotonic() - started) * 1000, type(exc).__name__,
            )
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, paypal-debug-id=%s)",
            request.method, path, response.status_code,
            (time.monotonic() - started) * 1000, response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def build_client() -> PaypalClient:
    cfg = config()
    missing = [
        name for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET") if not getattr(settings, name)
    ]
    if missing:
        raise ImproperlyConfigured("Missing PayPal credentials: " + ", ".join(missing))
    timeout = float(settings.PAYPAL_TIMEOUT)
    return PaypalClient(
        base_url=cfg.base_url,
        timeout=timeout,
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET
        ),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: PaypalClient | None) -> PaypalClient | None:
    """Swap the process-wide client (tests, credential rotation). Returns the old one."""
    global _client
    with _client_lock:
        old, _client = _client, client
    return old


@atexit.register
def _close_client() -> None:
    old = set_client(None)
    if old is not None:
        old.close()


def _describe(error: Error) -> tuple[str, str]:
    """PayPal's reason code and a readable description from a typed error body."""
    issue, description = error.name, error.message
    if not isinstance(error.details, UnsetType) and error.details:
        first = error.details[0]
        issue = first.issue
        if not isinstance(first.description, UnsetType) and first.description:
            description = first.description
    return issue, description


def call(operation: str, fn: Callable[[], T]) -> T:
    """
    Run one SDK call; translate every failure into ``ProviderError``.

    ``operation`` is a short label used in logs and messages.
    """
    try:
        return fn()
    except ApiError as e:
        if isinstance(e.error, OAuthProviderError):
            logger.error("PayPal rejected our credentials during %s: %s", operation, e.error.error)
            raise ProviderError(
                502, "The payment provider rejected this shop's credentials.", outcome_unknown=False
            ) from e
        status = e.status_code
        issue, description, debug_id = "", "", ""
        if isinstance(e.error, Error):
            issue, description = _describe(e.error)
            debug_id = e.error.debug_id
        elif isinstance(e.error, RawError):
            issue, description = _describe_raw(e.error)
        logger.warning(
            "PayPal %s answered HTTP %s issue=%s debug_id=%s", operation, status, issue or "-",
            debug_id or e.response.headers.get("paypal-debug-id", "-"),
        )
        if status in (401, 403):
            raise ProviderError(
                502, "The payment provider refused this shop's request (%s)." % (issue or status),
                outcome_unknown=False, issue=issue, provider_status=status, debug_id=debug_id,
            ) from e
        if status == 429:
            raise ProviderError(
                503, "The payment provider is rate-limiting this shop; try again shortly.",
                outcome_unknown=False, issue=issue, provider_status=status, debug_id=debug_id,
            ) from e
        if status in CALLER_STATUSES:
            raise ProviderError(
                422 if status in (400, 422) else status,
                "PayPal declined the %s: %s%s" % (
                    operation, description or "request rejected", " (%s)" % issue if issue else ""),
                outcome_unknown=False, issue=issue, provider_status=status, debug_id=debug_id,
            ) from e
        # 5xx and every unmapped status: a write may still have landed
        raise ProviderError(
            502, "The payment provider failed while processing the %s." % operation,
            outcome_unknown=True, issue=issue, provider_status=status, debug_id=debug_id,
        ) from e
    except ValidationError as e:
        logger.error("PayPal %s returned a body this SDK could not read", operation)
        raise ProviderError(
            502, "The payment provider's answer to the %s could not be read." % operation,
            outcome_unknown=True,
        ) from e
    except NEVER_SENT as e:
        logger.warning("PayPal %s was never sent: %s", operation, type(e).__name__)
        raise ProviderError(
            502, "The payment provider could not be reached; nothing was attempted.",
            outcome_unknown=False,
        ) from e
    except httpx.RequestError as e:
        logger.warning("PayPal %s got no answer: %s", operation, type(e).__name__)
        raise ProviderError(
            504, "The payment provider did not answer the %s in time." % operation,
            outcome_unknown=True,
        ) from e


def _describe_raw(error: RawError) -> tuple[str, str]:
    """Best-effort reason from an undocumented error body (PayPal's usual JSON shape)."""
    try:
        body = error.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    issue = str(body.get("name", ""))
    description = str(body.get("message", ""))
    details = body.get("details")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        issue = str(details[0].get("issue", issue))
        description = str(details[0].get("description", description))
    return issue, description

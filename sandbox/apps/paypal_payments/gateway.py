"""
The PayPal client, and the single place PayPal failures become ``ApiProblem``.

The client is the SDK's synchronous ``PaypalClient`` (the sandbox is a WSGI
Django site). One instance is built lazily on first use - so a forking server
builds it inside each worker - reused for the life of the process, and closed
at interpreter exit. The SDK performs no retries: writes are never retried
blindly here (``claims.safe_write`` settles an unknown outcome by reference),
and reads get a short, bounded retry in ``read_with_retry``.
"""
import atexit
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

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

from .errors import ApiProblem

log = logging.getLogger(__name__)

# The one server the SDK declares (sdk-map.md, "Servers & auth"). Any other
# environment has to name its host through PAYPAL_BASE_URL.
_ENVIRONMENT_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

# Failures raised before the request left this process: nothing can have
# reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def resolve_base_url() -> str:
    override = settings.PAYPAL_BASE_URL
    if override:
        return str(override)
    environment = str(settings.PAYPAL_ENVIRONMENT).strip().lower()
    try:
        return _ENVIRONMENT_BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL." % environment
        ) from None


class LoggingTransport:
    """Wraps the SDK's transport to log method, path, status and timing.

    Headers and bodies are never logged: they carry the bearer token and card
    data. The query string is dropped as well."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            log.warning(
                "PayPal %s %s -> %s after %.0f ms",
                request.method, path, type(exc).__name__, (time.monotonic() - started) * 1000,
            )
            raise
        log.info(
            "PayPal %s %s -> %s (%.0f ms, debug id %s)",
            request.method, path, response.status_code, (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client() -> PaypalClient:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
    timeout = float(settings.PAYPAL_TIMEOUT)
    return PaypalClient(
        base_url=resolve_base_url(),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
        # The client's own timeout= only configures its default transport, so
        # the timeout is set on the transport we hand it.
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_close_client)
    return _client


def install_client(client: PaypalClient | None) -> PaypalClient | None:
    """Replace the process-wide client (test seam). Returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous


def _close_client() -> None:
    client = install_client(None)
    if client is not None:
        client.close()


# ---------------------------------------------------------------------------
# Reading PayPal's errors
# ---------------------------------------------------------------------------


def provider_issue(exc: ApiError) -> str:
    """PayPal's machine-readable ``issue`` code, when the body carries one."""
    error = exc.error
    if isinstance(error, Error):
        if not isinstance(error.details, UnsetType) and error.details:
            return error.details[0].issue
        return error.name
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return ""
        if isinstance(body, dict):
            details = body.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                return str(details[0].get("issue", ""))
            return str(body.get("name", ""))
    return ""


def _provider_message(exc: ApiError) -> str:
    error = exc.error
    if isinstance(error, Error):
        if not isinstance(error.details, UnsetType) and error.details:
            description = error.details[0].description
            if isinstance(description, str) and description:
                return description
        return error.message
    return "PayPal rejected the request."


def translate(exc: BaseException, *, write: bool) -> ApiProblem:
    """Map any failure from a PayPal call onto the caller-facing ``ApiProblem``.

    ``write`` says whether the call could have changed state at PayPal, which
    decides whether an unanswered request is reported as outcome-unknown."""
    if isinstance(exc, ApiProblem):
        return exc
    if isinstance(exc, ImproperlyConfigured):
        return ApiProblem(503, "paypal_not_configured", "PayPal is not configured on this server.")
    if isinstance(exc, ApiError):
        if isinstance(exc.error, OAuthProviderError):
            log.error("PayPal rejected the configured credentials (%s)", exc.error.error)
            return ApiProblem(502, "paypal_auth_failed", "PayPal rejected this application's credentials.")
        status = exc.status_code
        issue = provider_issue(exc)
        if status in (401, 403):
            log.error("PayPal refused the request: HTTP %s %s", status, issue)
            return ApiProblem(502, "paypal_refused", "PayPal refused this application's request.", issue=issue)
        if status == 429:
            return ApiProblem(503, "paypal_rate_limited", "PayPal is rate limiting requests; try again shortly.")
        if status in (400, 422):
            return ApiProblem(422, "paypal_rejected", _provider_message(exc), issue=issue)
        if status == 409:
            return ApiProblem(409, "paypal_conflict", _provider_message(exc), issue=issue)
        # 404 (the ids we send are ours, not the caller's), 5xx, anything unmapped.
        log.error("PayPal answered HTTP %s %s", status, issue)
        return ApiProblem(
            502, "paypal_unavailable", "PayPal could not complete the request.",
            outcome_unknown=write and status >= 500, issue=issue,
        )
    if isinstance(exc, NEVER_SENT):
        return ApiProblem(502, "paypal_unreachable", "PayPal could not be reached; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return ApiProblem(504, "paypal_no_response", "PayPal did not answer in time.", outcome_unknown=write)
    if isinstance(exc, ValueError):
        # pydantic's ValidationError is a ValueError: a body we could not read.
        return ApiProblem(502, "paypal_unreadable", "PayPal's answer could not be read.", outcome_unknown=write)
    raise exc


T = TypeVar("T")


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return not isinstance(exc.error, OAuthProviderError) and exc.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


def read_with_retry(call: Callable[[], T], *, attempts: int = 3) -> T:
    """Run an idempotent PayPal read, retrying transient failures with backoff.
    Writes never go through here."""
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (ApiError, httpx.TransportError) as exc:
            if attempt == attempts or not _transient(exc):
                raise
            wait = delay
            if isinstance(exc, ApiError):
                retry_after = exc.response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    wait = min(float(retry_after), 5.0)
            time.sleep(wait)
            delay *= 2
    raise AssertionError("unreachable")

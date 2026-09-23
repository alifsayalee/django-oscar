"""Maxio SDK client lifetime and the single error-translation boundary.

Sync client (the sandbox is WSGI Django), held as a lazily-built module global so importing this
module never needs credentials — per ``python-client-initialization`` and the "Loading secrets"
section of ``python-authentication``.  Credentials and base URL are read from Django settings at
build time, never at import and never hard-coded.
"""

import contextlib
import threading

import httpx
from django.conf import settings
from pydantic import ValidationError

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, BasicAuthCredentials

from .exceptions import (
    ProviderConfigError,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)

# Transport failures that provably never reached Maxio: the request never left, so nothing
# happened and there is nothing to reconcile (python-error-handling).
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Maxio status codes that are genuinely the *caller's* fault and pass through with their status.
# 401/403 (our credentials) and 429 (our quota) are deliberately NOT here — they are ours.
_CALLER_FAULT = (400, 404, 409, 422)

_client = None
_client_lock = threading.Lock()


def build_client():
    """Construct a fresh sync client from Django settings.

    Raises ``ProviderConfigError`` (not at import) when the mandatory credentials are absent, so a
    missing environment variable stops the request with a clear message rather than sending an
    unauthenticated call that Maxio answers with a confusing 401.
    """
    api_key = getattr(settings, "MAXIO_API_KEY", "") or ""
    subdomain = getattr(settings, "MAXIO_SITE_SUBDOMAIN", "") or ""
    base_url = getattr(settings, "MAXIO_BASE_URL", "") or ""

    if not api_key:
        raise ProviderConfigError("MAXIO_API_KEY is not configured")
    if not base_url and not subdomain:
        raise ProviderConfigError("MAXIO_SITE_SUBDOMAIN or MAXIO_BASE_URL must be configured")

    # production/us base URL template is https://{site}.chargify.com with {site} defaulting to the
    # literal "subdomain"; override the site (or the whole base_url when MAXIO_BASE_URL is given).
    if base_url:
        server_config = {"production": {"us": {"base_url": base_url}}}
    else:
        server_config = {"production": {"us": {"site": subdomain}}}

    return MaxioAdvancedBillingClient(
        # Maxio Basic auth: API key as username, literal "x" as password (api-reference.md).
        basic_auth=BasicAuthCredentials(username=api_key, password="x"),
        environment="us",
        server_config=server_config,
        timeout=getattr(settings, "MAXIO_TIMEOUT", 15.0),
    )


def get_client():
    """Return the long-lived module-scoped client, building it on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def reset_client():
    """Drop and close the cached client (used by tests and credential rotation)."""
    global _client
    with _client_lock:
        if _client is not None:
            with contextlib.suppress(Exception):
                _client.close()
        _client = None


@contextlib.contextmanager
def translate_errors(*, writes=False):
    """Translate every Maxio failure kind into one ``ProviderError`` subclass.

    ``writes=True`` marks the enclosed call as a provider write, so a "may have landed" transport
    failure or decode failure is reported with ``outcome_unknown=True`` (HTTP 504) rather than as a
    definitive failure — the caller (or the claim reconciler) must then look the write up by its
    reference before assuming anything.
    """
    try:
        yield
    except ProviderError:
        raise  # already translated (e.g. a nested guarded call)
    except ApiError as e:
        status = e.status_code
        if status in (401, 403):
            # Our credentials/scopes — not the caller's problem to fix.
            raise ProviderConfigError("Maxio refused our credentials", status_code=502) from e
        if status == 429:
            raise ProviderUnavailable("Maxio rate-limited us", status_code=503) from e
        if status in _CALLER_FAULT:
            raise ProviderRejected(
                "Maxio rejected the request",
                status_code=status,
                detail=_safe_error_detail(e),
            ) from e
        # 5xx and every unmapped 4xx are ours/theirs, not the caller's.
        raise ProviderUnavailable(
            "Maxio is unavailable", status_code=502, outcome_unknown=writes
        ) from e
    except ValidationError as e:
        # A body that would not decode. On a write this is "outcome unknown"; on a read it means we
        # could not read the answer. Never assume the write failed.
        raise ProviderUnreadable(
            "Maxio returned an unreadable response",
            status_code=(504 if writes else 502),
            outcome_unknown=writes,
        ) from e
    except _NEVER_SENT as e:
        raise ProviderUnavailable(
            "Could not reach Maxio", status_code=502, outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        # Sent, but no usable reply — it may have landed.
        raise ProviderUnavailable(
            "No response from Maxio", status_code=504, outcome_unknown=writes
        ) from e


def _safe_error_detail(e):
    """A short, caller-safe detail string from an ``ApiError`` — never ``str(e)`` (leaky/terse)."""
    err = getattr(e, "error", None)
    # Typed Maxio validation bodies expose ``.errors`` (a list of messages).
    messages = getattr(err, "errors", None)
    if messages:
        try:
            return "; ".join(str(m) for m in messages)[:500]
        except Exception:  # pragma: no cover - defensive
            return None
    return None

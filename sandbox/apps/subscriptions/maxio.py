"""
The one place this site builds its Maxio client and turns Maxio failures into
its own error type.

The sandbox runs under WSGI, so the sync client is used. It is built lazily on
first use (so a forking server builds it after the fork), shared by every
request in the process, and closed at interpreter exit.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import Environment, MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import ApiError, HttpClient
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1
from maxio_advanced_billing.models.enums import CollectionMethod

logger = logging.getLogger(__name__)

_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


class MaxioError(Exception):
    """A Maxio call that did not produce a usable answer.

    ``status_code`` is the status this site answers with. ``outcome_unknown``
    is True when the request may have reached Maxio and taken effect.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.details = details or []


def _server_config() -> ServerConfigDict:
    environment = str(settings.MAXIO_ENVIRONMENT).strip().upper()
    base_url = settings.MAXIO_BASE_URL
    site = settings.MAXIO_SITE_SUBDOMAIN
    if not base_url and not site:
        raise ImproperlyConfigured("Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) to reach Maxio.")
    if environment == "US":
        return {"production": {"us": {"base_url": base_url} if base_url else {"site": site}}}
    if environment == "EU":
        return {"production": {"eu": {"base_url": base_url} if base_url else {"site": site}}}
    raise ImproperlyConfigured(f"MAXIO_ENVIRONMENT must be 'US' or 'EU', not {environment!r}.")


def build_client(*, custom_http_client: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """Build a client from settings. Refuses to build one without credentials."""
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        raise ImproperlyConfigured("MAXIO_API_KEY is not set.")
    server_config = _server_config()
    try:
        CollectionMethod(settings.MAXIO_PAYMENT_COLLECTION_METHOD)
    except ValueError:
        raise ImproperlyConfigured(
            f"MAXIO_PAYMENT_COLLECTION_METHOD must be one of {[m.value for m in CollectionMethod]}.") from None
    environment: Environment = "eu" if "eu" in server_config["production"] else "us"
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=server_config,
        timeout=float(settings.MAXIO_TIMEOUT),
        # Maxio takes the API key as the Basic username with the password "x".
        basic_auth={"username": api_key, "password": "x"},
        # The SDK's default policy: idempotent methods only, so a POST is never resent.
        retry_options=None,
        custom_http_client=custom_http_client,
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def install_client(client: MaxioAdvancedBillingClient | None) -> MaxioAdvancedBillingClient | None:
    """Replace the process-wide client (used by tests); returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


def provider_messages(error: object) -> list[str]:
    """The human-readable messages a typed Maxio error body carries."""
    if isinstance(error, ErrorListResponse1):
        return [str(message) for message in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        messages: list[str] = []
        errors = error.to_dict().get("errors")
        if isinstance(errors, dict):
            for field, values in errors.items():
                values = values if isinstance(values, list) else [values]
                messages.extend(f"{field}: {value}" for value in values)
        elif isinstance(errors, list):
            messages.extend(str(value) for value in errors)
        return messages
    if isinstance(error, str):
        return [error]
    return []


def from_api_error(exc: ApiError[Any]) -> MaxioError:
    status = exc.status_code
    if status in (401, 403):
        return MaxioError(502, "The billing provider refused this site's credentials.", provider_status=status)
    if status == 429:
        return MaxioError(503, "The billing provider is rate limiting this site; try again shortly.",
                          provider_status=status)
    if 400 <= status < 500:
        return MaxioError(status, "The billing provider rejected the request.", provider_status=status,
                          details=provider_messages(exc.error))
    return MaxioError(502, "The billing provider is unavailable.", outcome_unknown=status >= 500,
                      provider_status=status)


@contextmanager
def maxio_call(action: str) -> Iterator[None]:
    """Translate every failure kind of the SDK call inside the block into MaxioError.

    Keep the block to the SDK call itself: a ValueError from anything else would
    be misread as an unreadable response.
    """
    try:
        yield
    except ApiError as exc:
        error = from_api_error(exc)
        logger.warning("Maxio %s failed: HTTP %s -> %s %s", action, exc.status_code, error.status_code,
                       error.details)
        raise error from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as exc:
        # The request never left this process: nothing happened at Maxio.
        logger.warning("Maxio %s not sent: %s", action, type(exc).__name__)
        raise MaxioError(502, "The billing provider could not be reached.") from exc
    except httpx.RequestError as exc:
        # Sent, but no answer came back: it may have taken effect.
        logger.warning("Maxio %s got no response: %s", action, type(exc).__name__)
        raise MaxioError(504, "The billing provider did not respond.", outcome_unknown=True) from exc
    except ValueError as exc:
        # pydantic.ValidationError is a ValueError: a body that did not decode.
        logger.error("Maxio %s returned an unreadable body: %s", action, type(exc).__name__)
        raise MaxioError(502, "The billing provider sent an unreadable response.", outcome_unknown=True) from exc

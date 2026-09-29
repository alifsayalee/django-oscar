"""
The single place Maxio SDK failures become this app's own error type.

Every SDK call goes through :func:`maxio_call`, so the four failure kinds the SDK
can produce (an ``ApiError``, a decode failure, a transport failure that never
left the process, and one that may have landed) each map to one outcome.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from maxio_advanced_billing.core import ApiError, RawError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1
from pydantic import ValidationError

logger = logging.getLogger("apps.subscriptions")


class BillingError(Exception):
    """A failure to present to the API caller.

    ``status_code`` is the HTTP status this app answers with; ``outcome_unknown``
    says whether the Maxio write may have happened anyway (so it must be
    reconciled rather than assumed failed).
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        details: list[str] | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []
        self.upstream_status = upstream_status


def _messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return [str(message) for message in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        extra = error.model_extra or {}
        found = extra.get("errors")
        if isinstance(found, list):
            return [str(message) for message in found]
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return []
        found = body.get("errors") if isinstance(body, dict) else None
        if isinstance(found, list):
            return [str(message) for message in found]
        if isinstance(found, str):
            return [found]
    return []


def from_api_error(operation: str, exc: ApiError[Any]) -> BillingError:
    status = exc.status_code
    error: object = exc.error
    details = _messages(error)
    if isinstance(error, RawError):
        logger.warning("Maxio %s -> HTTP %s: %s", operation, status, error.text()[:500])
    else:
        logger.warning("Maxio %s -> HTTP %s: %s", operation, status, details)

    if status in (401, 403):
        # Our credentials, not the caller's: never pass a 401 through.
        return BillingError(502, "billing_unavailable", "The billing provider refused our credentials.",
                            upstream_status=status)
    if status == 429:
        return BillingError(503, "billing_busy", "The billing provider is rate-limiting; try again shortly.",
                            upstream_status=status)
    if status == 404:
        return BillingError(404, "not_found", "The billing provider has no such record.",
                            details=details, upstream_status=status)
    if 400 <= status < 500:
        return BillingError(422, "billing_rejected", "The billing provider rejected the request.",
                            details=details, upstream_status=status)
    return BillingError(502, "billing_unavailable", "The billing provider is unavailable.",
                        outcome_unknown=True, upstream_status=status)


@contextmanager
def maxio_call(operation: str) -> Iterator[None]:
    """Translate every SDK failure kind raised inside the block into a BillingError."""
    try:
        yield
    except ApiError as exc:
        raise from_api_error(operation, exc) from exc
    except ValidationError as exc:
        # Raised while decoding: on a 2xx the call may have succeeded and we cannot read it.
        logger.error("Maxio %s returned an unreadable response: %s", operation, exc.error_count())
        raise BillingError(502, "billing_unreadable", "The billing provider's response could not be read.",
                           outcome_unknown=True) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as exc:
        # Never sent: nothing happened upstream.
        logger.warning("Maxio %s was not sent: %s", operation, type(exc).__name__)
        raise BillingError(502, "billing_unreachable", "The billing provider could not be reached.") from exc
    except httpx.RequestError as exc:
        # Sent, but no (complete) reply: it may have landed.
        logger.warning("Maxio %s got no reply: %s", operation, type(exc).__name__)
        raise BillingError(504, "billing_timeout", "The billing provider did not answer in time.",
                           outcome_unknown=True) from exc

"""One place where every PayPal failure becomes this app's error."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error, ErrorDetails

log = logging.getLogger("apps.payments_api.paypal")

T = TypeVar("T")

# Failures raised before the request left: nothing can have happened at PayPal.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


class ProviderError(Exception):
    """A PayPal failure, already translated into what this app answers."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issues: tuple[str, ...] = (),
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issues = issues
        self.debug_id = debug_id


class OutcomeUnknown(Exception):
    """A write may have landed at PayPal and nothing could confirm it either way."""

    def __init__(self, reference: str) -> None:
        super().__init__(reference)
        self.reference = reference


class AmountMismatch(Exception):
    """PayPal did the thing, but not for the amount that was asked."""

    def __init__(self, reference: str, amount: str | None, currency: str | None) -> None:
        super().__init__(reference)
        self.reference = reference
        self.amount = amount
        self.currency = currency


def issues_of(error: object) -> tuple[str, ...]:
    if isinstance(error, Error):
        details = error.details if isinstance(error.details, list) else []
        return tuple(d.issue for d in details if isinstance(d, ErrorDetails))
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return ()
        if isinstance(body, dict) and isinstance(body.get("details"), list):
            return tuple(
                str(d["issue"]) for d in body["details"] if isinstance(d, dict) and "issue" in d
            )
    return ()


def _description(error: object) -> str | None:
    if isinstance(error, Error) and isinstance(error.details, list):
        for d in error.details:
            if isinstance(d.description, str):
                return d.description
        return error.message
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            details = body.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                desc = details[0].get("description")
                if isinstance(desc, str):
                    return desc
            message = body.get("message")
            if isinstance(message, str):
                return message
    return None


def _debug_id(error: object) -> str | None:
    if isinstance(error, Error):
        return error.debug_id
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("debug_id"), str):
            return str(body["debug_id"])
    return None


def provider_error(status: int, error: object) -> ProviderError:
    """Map PayPal's answer (status + decoded body) onto this app's error."""
    issues = issues_of(error)
    debug_id = _debug_id(error)
    if isinstance(error, OAuthProviderError) or status == 401:
        return ProviderError(502, "paypal_auth_failed", "PayPal refused this application's credentials.",
                             debug_id=debug_id)
    if status == 429:
        return ProviderError(503, "paypal_rate_limited", "PayPal is rate-limiting this application.",
                             debug_id=debug_id)
    if status == 403:
        # Our credentials lack permission for this resource (e.g. a deleted vault token): not the caller's
        # authentication, but still a definite refusal of this request.
        return ProviderError(422, "paypal_permission_denied",
                             _description(error) or "PayPal refused this operation for the payment method.",
                             issues=issues, debug_id=debug_id)
    if 400 <= status < 500:
        return ProviderError(422 if status in (400, 422) else status, "paypal_rejected",
                             _description(error) or "PayPal rejected the request.", issues=issues,
                             debug_id=debug_id)
    return ProviderError(502, "paypal_unavailable", "PayPal is unavailable.", outcome_unknown=status >= 500,
                         debug_id=debug_id)


def guarded_read(call: Callable[[], T], *, attempts: int = 3, backoff: float = 0.5) -> T:
    """Run a read-only SDK call with bounded retries, translating every failure kind.

    Reads are safe to retry; writes never go through here (they go through ``safe_write``).
    """
    import time

    last: ProviderError | None = None
    for attempt in range(attempts):
        try:
            return call()
        except ApiError as e:
            last = provider_error(e.status_code, e.error)
            if not (e.status_code == 429 or e.status_code >= 500) or isinstance(e.error, OAuthProviderError):
                raise last from e
        except NEVER_SENT as e:
            last = ProviderError(502, "paypal_unreachable", "PayPal could not be reached.")
            last.__cause__ = e
        except httpx.RequestError as e:
            last = ProviderError(504, "paypal_timeout", "PayPal did not answer in time.")
            last.__cause__ = e
        except ValueError as e:  # pydantic ValidationError or a non-JSON body: unreadable, not transient
            log.error("paypal returned an unreadable response (%s)", type(e).__name__)
            raise ProviderError(502, "paypal_unreadable", "PayPal returned an unreadable response.") from e
        if attempt + 1 < attempts:
            time.sleep(backoff * (2 ** attempt))
    assert last is not None
    raise last

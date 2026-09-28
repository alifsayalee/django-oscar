"""
The one place PayPal SDK failures become this API's errors.

Every failure reaching a view is an ``ApiProblem`` carrying the HTTP status to
answer with, a stable machine-readable ``code``, a message written here (never
``str()`` of an SDK exception), and whether anything may have happened at PayPal.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error

from .paypal_client import last_status, reset_last_status

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Failures raised before the request left this process: nothing reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or {}


class OutcomeUnknown(ApiProblem):
    """A write may or may not have landed at PayPal and could not be settled yet."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504,
            "outcome_unknown",
            "PayPal did not confirm the outcome yet. Repeat the same request to check again; "
            "it will not be performed twice.",
            outcome_unknown=True,
            details={"reference": reference},
        )
        self.reference = reference


class AmountMismatch(ApiProblem):
    """PayPal acted, but on an amount other than the one sent."""

    def __init__(self, reference: str, sent: str, echoed: str) -> None:
        super().__init__(
            409,
            "amount_mismatch",
            "PayPal reported a different amount than was requested; the payment needs review.",
            details={"reference": reference, "sent": sent, "paypal": echoed},
        )


def paypal_issues(error: object) -> list[str]:
    """The ``details[].issue`` codes of a typed PayPal error body."""
    if isinstance(error, Error) and isinstance(error.details, list):
        return [d.issue for d in error.details]
    return []


def paypal_message(error: object) -> str:
    """A human-readable reason from a typed PayPal error body (never the raw payload)."""
    if isinstance(error, Error):
        if isinstance(error.details, list):
            descriptions = [
                d.description if isinstance(d.description, str) else d.issue
                for d in error.details
            ]
            if descriptions:
                return "; ".join(descriptions)
        return error.message
    return ""


def provider_error(status: int, error: object) -> ApiProblem:
    """Map a PayPal status + decoded error body onto this API's answer."""
    issues = paypal_issues(error)
    details: dict[str, object] = {"paypalStatus": status}
    if issues:
        details["paypalIssues"] = issues
    if isinstance(error, Error):
        details["paypalDebugId"] = error.debug_id

    if isinstance(error, OAuthProviderError) or status == 401:
        # Our credentials: nothing the caller can fix.
        return ApiProblem(502, "paypal_auth_failed", "PayPal refused this shop's credentials.")
    if status == 403:
        # This merchant account may not perform the operation on this resource.
        reason = paypal_message(error) or "PayPal did not permit the operation."
        return ApiProblem(502, "paypal_permission_denied", reason, details=details)
    if status == 429:
        return ApiProblem(503, "paypal_rate_limited", "PayPal is rate-limiting requests; retry later.")
    if 400 <= status < 500:
        message = paypal_message(error) or "PayPal rejected the request."
        # A declined card or other business rule is the caller's to act on.
        answer_status = 402 if status == 422 else (404 if status == 404 else 409 if status == 409 else 400)
        return ApiProblem(answer_status, "paypal_rejected", message, details=details)
    return ApiProblem(
        502, "paypal_unavailable", "PayPal is unavailable.", outcome_unknown=status >= 500, details=details
    )


def describe_raw(error: object) -> str:
    if isinstance(error, RawError):
        return f"HTTP {error.status_code}"
    return type(error).__name__


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ApiError) and exc.status_code in (429, 500, 502, 503, 504)


def read(call: Callable[[], T], *, what: str, attempts: int = 3) -> T:
    """
    Run an idempotent PayPal read, retrying transient failures with backoff.

    Only reads come through here: the SDK retries nothing, and writes are never
    retried blindly (see ``safe_write``).
    """
    for attempt in range(1, attempts + 1):
        reset_last_status()
        try:
            return call()
        except (ApiError, httpx.RequestError) as exc:
            if attempt < attempts and is_transient(exc):
                logger.info("PayPal read %s failed transiently (attempt %s); retrying", what, attempt)
                time.sleep(0.5 * 2 ** (attempt - 1))
                continue
            raise translate(exc, what=what) from exc
        except ValueError as exc:  # pydantic ValidationError or a non-JSON body
            status = last_status()
            if status is not None and status >= 400:
                # An error whose body did not match the SDK's error model.
                raise provider_error(status, None) from exc
            raise ApiProblem(502, "paypal_unreadable", f"PayPal's answer to {what} could not be read.") from exc
    raise AssertionError("unreachable")


def translate(exc: BaseException, *, what: str) -> ApiProblem:
    """Map an exception from a PayPal read onto this API's answer."""
    if isinstance(exc, ApiError):
        logger.warning("PayPal %s failed: HTTP %s (%s)", what, exc.status_code, describe_raw(exc.error))
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ApiProblem(502, "paypal_unreachable", "PayPal could not be reached.")
    if isinstance(exc, httpx.RequestError):
        return ApiProblem(504, "paypal_timeout", "PayPal did not answer in time.")
    return ApiProblem(502, "paypal_error", "PayPal request failed.")

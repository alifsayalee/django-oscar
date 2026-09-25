"""
Failures this app reports, and the one ladder that turns an SDK failure into one.

Every carrier holds the HTTP status we answer with, a stable ``code`` and
whether the provider may have acted (``outcome_unknown``). ``str(e)`` of an SDK
exception is never shown to a caller.
"""

import logging
from typing import Any

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError, UnsetType
from paypal.models import DefaultError, Error

logger = logging.getLogger(__name__)

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


class ApiProblem(Exception):
    """A failure answered to the caller as JSON."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.extra = extra or {}

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.outcome_unknown:
            body["outcomeUnknown"] = True
        body.update(self.extra)
        return body


class OutcomeUnknown(ApiProblem):
    def __init__(self, reference: str) -> None:
        super().__init__(
            504,
            "outcome_unknown",
            "PayPal did not give a readable answer; the operation may have happened. "
            "Repeat the same request to check, or ask an operator to review it.",
            outcome_unknown=True,
        )
        self.reference = reference


class AmountMismatch(ApiProblem):
    def __init__(self, reference: str, expected: str, got: str) -> None:
        super().__init__(
            502,
            "needs_review",
            f"PayPal recorded {got} where {expected} was requested; an operator must review this payment.",
        )
        self.reference = reference


def provider_issues(error: ApiError[Any]) -> list[str]:
    body = error.error
    if isinstance(body, Error) and not isinstance(body.details, UnsetType):
        return [d.issue for d in body.details]
    return []


def provider_message(error: ApiError[Any]) -> str:
    body = error.error
    if isinstance(body, Error):
        details = [] if isinstance(body.details, UnsetType) else body.details
        descriptions = [d.description for d in details if isinstance(d.description, str)]
        return "; ".join(descriptions) or body.message
    if isinstance(body, DefaultError):
        return body.message
    return "PayPal rejected the request."


# PayPal answers these when the card itself is refused (a business outcome for
# the shopper, not a fault in our request).
_DECLINE_ISSUES = {
    "TRANSACTION_REFUSED",
    "INSTRUMENT_DECLINED",
    "CARD_EXPIRED",
    "PAYMENT_DENIED",
    "DECLINED_DUE_TO_RELATED_TXN",
}


def translate(exc: BaseException) -> ApiProblem:
    """The one ladder: an SDK/transport failure becomes an ``ApiProblem``."""
    if isinstance(exc, ApiProblem):
        return exc
    if isinstance(exc, ApiError):
        issues = provider_issues(exc)
        debug_id = exc.error.debug_id if isinstance(exc.error, Error) else None
        extra: dict[str, Any] = {"paypalIssues": issues} if issues else {}
        if debug_id:
            extra["paypalDebugId"] = debug_id
        if isinstance(exc.error, OAuthProviderError) or exc.status_code in (401, 403):
            logger.error("PayPal refused our credentials or permissions (HTTP %s)", exc.status_code)
            return ApiProblem(502, "provider_config", "The payment provider refused this shop's credentials.")
        if exc.status_code == 429:
            return ApiProblem(503, "provider_busy", "The payment provider is rate-limiting; try again shortly.")
        if 400 <= exc.status_code < 500:
            if _DECLINE_ISSUES.intersection(issues):
                return ApiProblem(402, "payment_declined", provider_message(exc), extra=extra)
            status = exc.status_code if exc.status_code in (400, 404, 409, 422) else 422
            return ApiProblem(status, "provider_rejected", provider_message(exc), extra=extra)
        if isinstance(exc.error, RawError):
            logger.error("PayPal HTTP %s", exc.status_code)
        return ApiProblem(502, "provider_error", "The payment provider failed.", outcome_unknown=True, extra=extra)
    if isinstance(exc, NEVER_SENT):
        return ApiProblem(502, "provider_unreachable", "Could not reach the payment provider; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return ApiProblem(504, "provider_timeout", "No answer from the payment provider.", outcome_unknown=True)
    if isinstance(exc, ValueError):
        # Includes pydantic's ValidationError: an unreadable provider answer.
        return ApiProblem(
            502, "provider_unreadable", "Unreadable answer from the payment provider.", outcome_unknown=True
        )
    raise exc

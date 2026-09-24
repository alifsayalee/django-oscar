"""
The one place a Maxio/SDK failure becomes this app's failure.

Every call site funnels through ``from_provider_error`` so the same kind of
failure always produces the same HTTP answer.
"""

import logging

import httpx
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import CustomerError, CustomerErrorResponse1, ErrorListResponse1

logger = logging.getLogger("apps.subscriptions")

# Failures that happen before the request leaves: nothing reached Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's input - a named list,
# never a range: every other 4xx (401, 403, 402, 429, ...) is ours.
CALLER_FAULT_STATUSES = (400, 404, 409, 422)


class BillingError(Exception):
    """A failure with the status this API answers and whether anything may have happened upstream."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        details: list[str] | None = None,
        reference: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []
        self.reference = reference

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "error": self.code,
            "message": self.message,
            "outcomeUnknown": self.outcome_unknown,
        }
        if self.details:
            body["details"] = self.details
        if self.reference:
            body["reference"] = self.reference
        return body


class OutcomeUnknown(BillingError):
    """A write may or may not have landed and a lookup by its reference could not settle it."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504,
            "outcome_unknown",
            "Maxio did not confirm the request. It may still complete; retry the same "
            "request later - it will be checked, never sent twice.",
            outcome_unknown=True,
            reference=reference,
        )


class InProgress(BillingError):
    def __init__(self, reference: str) -> None:
        super().__init__(
            409,
            "request_in_progress",
            "An identical request is already being processed. Retry shortly.",
            reference=reference,
        )


def provider_messages(error: object) -> list[str]:
    """Human-readable messages from a typed Maxio error body (never ``str(e)``)."""
    if isinstance(error, ErrorListResponse1):
        return [str(m) for m in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, UnsetType):
            return []
        if isinstance(errors, CustomerError):
            return [] if isinstance(errors.customer, UnsetType) else [errors.customer]
        return [str(m) for m in errors]
    return []


def from_provider_error(exc: BaseException, *, write: bool = False) -> BillingError:
    """
    Map an SDK/transport failure onto a ``BillingError``.

    ``write`` says whether the failed call could have changed state at Maxio;
    it decides whether a no-reply failure is flagged ``outcome_unknown``.
    """
    if isinstance(exc, BillingError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        if status in (401, 403):
            logger.error("Maxio refused our credentials (HTTP %s)", status)
            return BillingError(502, "provider_auth_failed", "Billing provider unavailable.")
        if status == 429:
            return BillingError(503, "provider_rate_limited", "Billing provider busy; try again shortly.")
        if status in CALLER_FAULT_STATUSES and not isinstance(exc.error, RawError):
            return BillingError(
                status,
                "provider_rejected",
                "The billing provider rejected the request.",
                details=provider_messages(exc.error),
            )
        if status == 404:
            return BillingError(404, "not_found", "Not found at the billing provider.")
        if isinstance(exc.error, RawError):
            logger.error("Maxio HTTP %s: %s", status, exc.error.text()[:500])
        return BillingError(
            502,
            "provider_error",
            "Billing provider error.",
            outcome_unknown=write and status >= 500,
        )
    if isinstance(exc, NEVER_SENT):
        return BillingError(502, "provider_unreachable", "Billing provider unreachable. Nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return BillingError(
            504, "provider_timeout", "Billing provider did not answer.", outcome_unknown=write
        )
    if isinstance(exc, ValueError):  # pydantic.ValidationError, non-JSON body
        logger.error("Unreadable Maxio response: %s", type(exc).__name__)
        return BillingError(
            502, "provider_unreadable", "Unreadable billing provider response.", outcome_unknown=write
        )
    raise exc

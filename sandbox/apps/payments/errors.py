"""
The payments API's own failure types, and the one place a PayPal SDK
failure becomes one of them.

Every ``PaymentError`` carries the HTTP status the API answers with, a stable
``code``, a message written here (never ``str()`` of an SDK exception), and
``outcome_unknown`` - whether PayPal may have acted on a write anyway.
"""

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error
from pydantic import ValidationError


class PaymentError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        outcome_unknown: bool = False,
        issues: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.outcome_unknown = outcome_unknown
        self.issues = issues or []

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"error": self.code, "message": self.message}
        if self.issues:
            body["issues"] = self.issues
        if self.outcome_unknown:
            body["outcomeUnknown"] = True
        return body


class BadRequest(PaymentError):
    status_code = 400
    code = "bad_request"


class NotFound(PaymentError):
    status_code = 404
    code = "not_found"


class Conflict(PaymentError):
    status_code = 409
    code = "conflict"


class InProgress(Conflict):
    code = "in_progress"


class NotRenewable(Conflict):
    """An authorization that is stale and can no longer be renewed."""

    code = "authorization_not_renewable"


class ProviderRejected(PaymentError):
    """PayPal refused the request itself (validation, decline, business rule)."""

    code = "paypal_rejected"


class ProviderConfigError(PaymentError):
    """Our credentials or permissions - never the caller's to fix."""

    status_code = 502
    code = "paypal_configuration"


class ProviderUnavailable(PaymentError):
    status_code = 502
    code = "paypal_unavailable"


class ProviderUnreadable(PaymentError):
    """PayPal answered 2xx but the body could not be read: outcome unknown."""

    status_code = 504
    code = "paypal_unreadable"

    def __init__(self, message: str) -> None:
        super().__init__(message, outcome_unknown=True)


class OutcomeUnknown(PaymentError):
    status_code = 504
    code = "outcome_unknown"

    def __init__(self, reference: str) -> None:
        super().__init__(
            "PayPal did not confirm whether this request took effect. It is recorded "
            "and will be settled by repeating the same request; do not start a new one.",
            outcome_unknown=True,
        )
        self.reference = reference


class AmountMismatch(PaymentError):
    status_code = 502
    code = "amount_mismatch"

    def __init__(self, reference: str, echoed: str) -> None:
        super().__init__(
            f"PayPal processed a different amount ({echoed}) than was requested; "
            "the payment is flagged for operator review.",
        )
        self.reference = reference


NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def paypal_issues(error: object) -> list[str]:
    """PayPal's issue codes and descriptions - never the rejected field values."""
    if not isinstance(error, Error):
        return []
    issues: list[str] = []
    details = error.details if isinstance(error.details, list) else []
    for detail in details:
        text = detail.issue
        if isinstance(detail.description, str) and detail.description:
            text = f"{text}: {detail.description}"
        issues.append(text)
    return issues


def translate_api_error(exc: ApiError, *, payment_write: bool = False) -> PaymentError:
    """Map an ``ApiError`` onto the API's own failure types."""
    status = exc.status_code
    if isinstance(exc.error, OAuthProviderError):
        return ProviderConfigError("PayPal rejected this site's API credentials.")
    if status in (401, 403):
        return ProviderConfigError(
            "PayPal refused the request for this merchant account (HTTP %d)." % status
        )
    if status == 429:
        return ProviderUnavailable(
            "PayPal is rate-limiting this site; try again shortly.", status_code=503
        )
    if isinstance(exc.error, Error) and status in (400, 404, 409, 422):
        issues = paypal_issues(exc.error)
        message = exc.error.message or "PayPal rejected the request."
        if payment_write and status == 422:
            # A payment PayPal would not process (declined card, business rule).
            return ProviderRejected(message, status_code=402, code="payment_declined", issues=issues)
        return ProviderRejected(message, status_code=status, issues=issues)
    if isinstance(exc.error, RawError) and status == 404:
        return ProviderRejected("PayPal does not know this resource.", status_code=404)
    return ProviderUnavailable(
        "PayPal could not process the request (HTTP %d)." % status,
        outcome_unknown=payment_write and status >= 500,
    )


def translate_failure(exc: BaseException, *, payment_write: bool = False) -> PaymentError:
    """The whole ladder: SDK error, decode failure, or transport failure."""
    if isinstance(exc, PaymentError):
        return exc
    if isinstance(exc, ApiError):
        return translate_api_error(exc, payment_write=payment_write)
    if isinstance(exc, (ValidationError, ValueError)):
        return ProviderUnreadable("PayPal's response could not be read.")
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable("PayPal could not be reached; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(
            "PayPal did not answer in time.", status_code=504, outcome_unknown=payment_write
        )
    raise exc

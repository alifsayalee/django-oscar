"""
The error boundary: every failure from a PayPal call becomes a ``ProviderError``
here, and nowhere else.
"""

import logging
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error

from .gateway import NEVER_SENT, last_status

logger = logging.getLogger(__name__)


class ApiProblem(Exception):
    """An error this API answers with: an HTTP status, a stable code and a message."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


class ProviderError(ApiProblem):
    """A PayPal call failed. ``outcome_unknown`` says whether it may still have happened."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issue: str = "",
        debug_id: str = "",
    ) -> None:
        super().__init__(status_code, code, message)
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.debug_id = debug_id


class OutcomeUnknown(ApiProblem):
    """A write may have landed and PayPal could not (yet) tell us."""

    def __init__(self, ref: str) -> None:
        super().__init__(
            504,
            "outcome_unknown",
            "PayPal did not confirm the outcome. Nothing will be sent again under a new "
            "reference; repeat the request later to re-check.",
        )
        self.ref = ref


class AmountMismatch(ApiProblem):
    def __init__(self, ref: str, echoed: Any, currency: Any) -> None:
        super().__init__(
            409,
            "needs_review",
            f"PayPal processed {echoed} {currency}, which is not the amount requested; "
            "the payment needs operator review.",
        )
        self.ref = ref


def issue_of(error: Error) -> tuple[str, str]:
    """PayPal's first detail issue and description, which is what an operator can act on."""
    details = error.details if isinstance(error.details, list) else []
    for detail in details:
        description = detail.description if isinstance(detail.description, str) else ""
        return detail.issue, description
    return "", ""


def provider_error(status: int, error: object) -> ProviderError:
    """Map PayPal's answer (status + decoded body) to what this API tells its caller."""
    if isinstance(error, OAuthProviderError):
        logger.error("PayPal rejected our credentials: %s", error.error)
        return ProviderError(502, "paypal_credentials_rejected", "PayPal refused this site's credentials.")
    if status in (401, 403):
        return ProviderError(502, "paypal_not_permitted", "PayPal refused the request for this merchant account.")
    if status == 429:
        return ProviderError(503, "paypal_rate_limited", "PayPal is rate-limiting requests; try again shortly.")
    if isinstance(error, Error) and 400 <= status < 500:
        issue, description = issue_of(error)
        message = description or error.message
        return ProviderError(
            422 if status == 422 else 409 if status == 409 else 400,
            "paypal_rejected",
            f"PayPal rejected the request: {message}" + (f" ({issue})" if issue else ""),
            issue=issue,
            debug_id=error.debug_id,
        )
    if 400 <= status < 500:
        return ProviderError(400 if status != 404 else 404, "paypal_rejected", "PayPal rejected the request.")
    return ProviderError(502, "paypal_unavailable", "PayPal is unavailable.", outcome_unknown=status >= 500)


T = TypeVar("T")


def call_paypal(call: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into a ``ProviderError``."""
    try:
        return call()
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except ValueError as e:  # pydantic's ValidationError too: a body we could not decode
        status = last_status.get()
        if status is not None and status >= 400:
            # The request was rejected; only the detail was lost.
            raise ProviderError(
                400 if status < 500 else 502,
                "paypal_rejected",
                f"PayPal rejected the request (HTTP {status}).",
                outcome_unknown=status >= 500,
            ) from e
        raise ProviderError(502, "paypal_unreadable", "PayPal's answer could not be read.", outcome_unknown=True) from e
    except NEVER_SENT as e:
        raise ProviderError(502, "paypal_unreachable", "PayPal could not be reached; nothing was sent.") from e
    except httpx.RequestError as e:
        raise ProviderError(504, "paypal_no_response", "PayPal did not answer in time.", outcome_unknown=True) from e


def raw_error_text(error: object) -> str:
    if isinstance(error, RawError):
        return error.text()[:500]
    return ""

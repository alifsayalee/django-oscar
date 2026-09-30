"""
The one error boundary between this app and PayPal.

Every PayPal call goes through :func:`call_paypal`, which turns each way a call
can fail into a :class:`PaymentError` carrying the HTTP status to answer with
and whether the operation may nevertheless have taken effect at PayPal.
"""

import logging
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pay_pal_server_sdk.core import ApiError, OAuthProviderError, RawError
from pay_pal_server_sdk.models import Error
from pydantic import ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class PaymentError(Exception):
    """A failure this API reports to its caller as a JSON error body."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # True when PayPal may have acted even though we could not confirm it.
        self.outcome_unknown = outcome_unknown
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.outcome_unknown:
            body["outcomeUnknown"] = True
        if self.details:
            body["details"] = self.details
        return body


def paypal_issues(error: object) -> list[str]:
    """The ``issue`` codes of a typed PayPal error body, e.g. ``INSTRUMENT_DECLINED``."""
    if isinstance(error, Error) and isinstance(error.details, list):
        return [detail.issue for detail in error.details]
    return []


def _typed_details(error: Error) -> dict[str, Any]:
    details: dict[str, Any] = {"paypalError": error.name, "paypalDebugId": error.debug_id}
    if isinstance(error.details, list):
        details["issues"] = [
            {
                "issue": d.issue,
                **({"description": d.description} if isinstance(d.description, str) else {}),
                **({"field": d.field} if isinstance(d.field, str) else {}),
            }
            for d in error.details
        ]
    return details


def provider_error(operation: str, status: int, error: object) -> PaymentError:
    """Map PayPal's answer (a status and its decoded error body) onto a PaymentError."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        # Our credentials or our account's permissions: nothing the caller can fix.
        return PaymentError(502, "paypal_auth_failed", "The payment provider refused this shop's credentials.")
    if status == 429:
        return PaymentError(
            503, "paypal_rate_limited", "The payment provider is rate-limiting requests; try again shortly."
        )
    if 400 <= status < 500:
        if isinstance(error, Error):
            issues = paypal_issues(error)
            message = error.message
            if issues:
                message = f"{message} ({', '.join(issues)})"
            return PaymentError(
                422 if status == 400 else status,
                "paypal_rejected",
                f"PayPal rejected {operation}: {message}",
                details=_typed_details(error),
            )
        return PaymentError(
            422 if status == 400 else status, "paypal_rejected", f"PayPal rejected {operation}."
        )
    # A PayPal 5xx: a write may or may not have landed.
    return PaymentError(
        502, "paypal_unavailable", f"PayPal failed while processing {operation}.", outcome_unknown=True
    )


def call_paypal(operation: str, call: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into a PaymentError."""
    try:
        return call()
    except ApiError as e:
        # Log what PayPal said, never the request (it may hold card data).
        if isinstance(e.error, Error):
            logger.warning(
                "PayPal %s failed: HTTP %s %s %s debug_id=%s",
                operation, e.status_code, e.error.name, paypal_issues(e.error), e.error.debug_id,
            )
        elif isinstance(e.error, OAuthProviderError):
            logger.error("PayPal token request failed for %s: %s", operation, e.error.error)
        elif isinstance(e.error, RawError):
            logger.warning("PayPal %s failed: HTTP %s (undocumented body)", operation, e.status_code)
        raise provider_error(operation, e.status_code, e.error) from e
    except ValidationError as e:
        # PayPal answered, but the body did not decode. The call may have succeeded.
        logger.error("PayPal %s returned an unreadable response (%s errors)", operation, e.error_count())
        raise PaymentError(
            502, "paypal_unreadable_response",
            f"PayPal's response to {operation} could not be read; its outcome is unknown.",
            outcome_unknown=True,
        ) from e
    except ValueError as e:
        logger.error("PayPal %s returned a non-JSON response", operation)
        raise PaymentError(
            502, "paypal_unreadable_response",
            f"PayPal's response to {operation} could not be read; its outcome is unknown.",
            outcome_unknown=True,
        ) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        # Never sent: nothing happened at PayPal.
        logger.error("PayPal %s not sent: %s", operation, type(e).__name__)
        raise PaymentError(502, "paypal_unreachable", "The payment provider could not be reached.") from e
    except httpx.RequestError as e:
        # Sent, but no answer: it may have landed.
        logger.error("PayPal %s got no response: %s", operation, type(e).__name__)
        raise PaymentError(
            504, "paypal_no_response",
            f"No response from PayPal for {operation}; its outcome is unknown.",
            outcome_unknown=True,
        ) from e


def unknown_outcome(operation: str, missing: str) -> PaymentError:
    """A 2xx response that lacks a member we depend on."""
    logger.error("PayPal %s response lacked %s", operation, missing)
    return PaymentError(
        502, "paypal_incomplete_response",
        f"PayPal's response to {operation} did not include {missing}; its outcome is unknown.",
        outcome_unknown=True,
    )

"""
The single error boundary around every PayPal SDK call.

Every failure kind - an API error, an unreadable body, a transport failure -
becomes a ``ProviderError`` carrying the status this app answers with and
whether the write may have happened at PayPal (``outcome_unknown``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

import httpx
from pay_pal_server_sdk.core import ApiError, OAuthProviderError, RawError
from pay_pal_server_sdk.models import Error

from .transport import last_status, reset_last_status

logger = logging.getLogger("apps.paypal_payments")

T = TypeVar("T")


class ProviderError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        # PayPal's own error name/issue (e.g. "UNPROCESSABLE_ENTITY/INSTRUMENT_DECLINED")
        self.provider_error = provider_error

    @property
    def is_rejection(self) -> bool:
        """PayPal answered and refused: nothing happened, the claim can be released."""
        return not self.outcome_unknown and self.provider_status is not None and 400 <= self.provider_status < 500


def _describe(error: object) -> str | None:
    if isinstance(error, Error):
        parts = [error.name]
        if isinstance(error.details, list) and error.details:
            parts.append(error.details[0].issue)
        return "/".join(parts)
    return None


def provider_error(status: int, error: object) -> ProviderError:
    code = _describe(error)
    if isinstance(error, OAuthProviderError):
        return ProviderError(502, "PayPal refused this application's credentials.", provider_status=status)
    if status in (401, 403):
        return ProviderError(
            502, "PayPal refused this application's credentials or permissions.",
            provider_status=status, provider_error=code,
        )
    if status == 429:
        return ProviderError(503, "PayPal is rate-limiting this application; try again shortly.",
                             provider_status=status, provider_error=code)
    if 400 <= status < 500:
        message = f"PayPal rejected the request ({code})." if code else "PayPal rejected the request."
        return ProviderError(status, message, provider_status=status, provider_error=code)
    return ProviderError(
        502, "PayPal is unavailable; the outcome of this operation is not yet known.",
        outcome_unknown=True, provider_status=status, provider_error=code,
    )


def call(operation: Callable[[], T], *, what: str) -> T:
    """Run one SDK call and translate every failure into ``ProviderError``."""
    reset_last_status()
    try:
        return operation()
    except ApiError as e:
        err = e.error
        if isinstance(err, RawError):
            logger.warning("PayPal %s failed: HTTP %s (undocumented body)", what, e.status_code)
        else:
            logger.warning("PayPal %s failed: HTTP %s %s", what, e.status_code, _describe(err))
        raise provider_error(e.status_code, err) from e
    except ValueError as e:  # pydantic.ValidationError is a ValueError
        status = last_status()
        if status is not None and status >= 400:
            # PayPal rejected the call; only the detail of its error body was lost.
            logger.warning("PayPal %s failed: HTTP %s (unreadable error body)", what, status)
            raise provider_error(status, None) from e
        logger.error("PayPal %s returned an unreadable success body (HTTP %s)", what, status)
        raise ProviderError(
            502, "PayPal's reply could not be read; the outcome of this operation is not yet known.",
            outcome_unknown=True, provider_status=status,
        ) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        logger.warning("PayPal %s never sent: %s", what, type(e).__name__)
        raise ProviderError(502, "PayPal could not be reached; nothing was sent.") from e
    except httpx.RequestError as e:
        logger.warning("PayPal %s got no reply: %s", what, type(e).__name__)
        raise ProviderError(
            504, "PayPal did not answer; the outcome of this operation is not yet known.",
            outcome_unknown=True,
        ) from e


def unknown_from_missing(what: str, member: str) -> ProviderError:
    """A 2xx whose body lacks a member we depend on: the write may have happened."""
    logger.error("PayPal %s returned no %s", what, member)
    return ProviderError(
        502, f"PayPal's reply to {what} carried no {member}; the outcome is not yet known.",
        outcome_unknown=True, provider_status=last_status(),
    )

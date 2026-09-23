"""A single boundary that turns every PayPal SDK failure into one of our own
exception types, so views never branch on SDK internals.

Mapping follows the SDK error-handling contract:
  * a failed OAuth token fetch is *our* misconfiguration, never the caller's;
  * a provider 4xx is the caller's fault only for a named list of statuses;
  * 401/403/429 are our credentials/quota, surfaced as 502/503;
  * a decode failure is "outcome unknown", not a rejection;
  * a transport failure that never left the process is known (nothing happened);
    one that may have landed is unknown.
"""
import logging

import httpx
from pydantic import ValidationError

from paypal.core import ApiError, OAuthProviderError, RawError

logger = logging.getLogger("paypal")

# Provider 4xx statuses that are genuinely the API caller's fault (their input,
# their reference, a real conflict). Everything else — including an unmapped 4xx —
# is treated as ours.
CALLER_FAULT_STATUSES = frozenset({400, 404, 409, 422})


class PayPalError(Exception):
    """Base for every error this integration raises out of a PayPal call."""

    def __init__(self, message, *, status_code=502, issues=None, outcome_unknown=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.issues = issues or []
        self.outcome_unknown = outcome_unknown


class PayPalConfigError(PayPalError):
    """Our credentials/config were rejected. Nothing the caller can fix."""


class PayPalCallerError(PayPalError):
    """The caller's request was rejected by PayPal (validation, not-found, conflict)."""


class PayPalUnavailable(PayPalError):
    """The provider could not be reached or failed. ``outcome_unknown`` says
    whether the request may have taken effect upstream."""


def _issue_codes(error):
    """Extract PayPal's machine-readable issue codes from a typed Error body."""
    codes = []
    details = getattr(error, "details", None)
    if details:
        for d in details:
            issue = getattr(d, "issue", None)
            if issue:
                codes.append(str(issue))
    name = getattr(error, "name", None)
    if name:
        codes.append(str(name))
    return codes


def translate(exc, *, operation):
    """Translate an SDK exception into a PayPalError. Returns the new exception;
    the caller raises it (``raise translate(...) from exc``)."""
    if isinstance(exc, ApiError):
        error = exc.error
        status = exc.status_code
        # Auth/token failures first — nothing was sent, so it is our config fault.
        if isinstance(error, OAuthProviderError):
            logger.error("paypal %s: credentials rejected: %s", operation, error.error)
            return PayPalConfigError("PayPal credentials were rejected.", status_code=502)
        if status in (401, 403):
            logger.error("paypal %s: provider refused us (HTTP %s)", operation, status)
            return PayPalConfigError("PayPal refused this merchant's credentials.", status_code=502)
        if status == 429:
            return PayPalUnavailable("PayPal rate limit reached.", status_code=503)

        issues = _issue_codes(error) if not isinstance(error, RawError) else []
        detail = _human_detail(error)
        if status in CALLER_FAULT_STATUSES and not isinstance(error, RawError):
            return PayPalCallerError(detail, status_code=status, issues=issues)
        # 5xx and every unmapped 4xx are ours to own.
        logger.error("paypal %s: HTTP %s: %s", operation, status, detail)
        return PayPalUnavailable("PayPal could not process this request.", status_code=502, issues=issues)

    if isinstance(exc, (ValidationError, ValueError)):
        # A body that would not decode. For a write the effect is unknown; do not
        # report it as a definite failure.
        logger.error("paypal %s: undecodable response: %s", operation, exc)
        return PayPalUnavailable(
            "PayPal returned an unreadable response; the outcome is unknown.",
            status_code=502,
            outcome_unknown=True,
        )

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
        # Never sent — known outcome: nothing happened.
        return PayPalUnavailable("Could not reach PayPal.", status_code=502, outcome_unknown=False)

    if isinstance(exc, httpx.RequestError):
        # May have landed — unknown outcome.
        return PayPalUnavailable(
            "PayPal did not respond; the outcome is unknown.", status_code=504, outcome_unknown=True
        )

    raise exc  # not ours to translate — a real programming error


def _human_detail(error):
    if isinstance(error, RawError):
        try:
            return error.text()[:300]
        except Exception:  # pragma: no cover - defensive
            return "PayPal error"
    parts = []
    name = getattr(error, "name", None)
    message = getattr(error, "message", None)
    if name:
        parts.append(str(name))
    if message:
        parts.append(str(message))
    for code in _issue_codes(error):
        if code not in parts:
            parts.append(code)
    return ": ".join(parts) if parts else "PayPal error"

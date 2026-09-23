"""Domain exceptions for the Maxio subscriptions integration.

The SDK surfaces failures as a single ``ApiError`` plus a handful of non-``ApiError``
kinds (decode failures, transport failures). ``services._translate`` maps every one of
those onto exactly one of the types below, so the views deal with a single, stable
failure vocabulary and a caller never sees an SDK type or a raw traceback.
"""


class MaxioError(Exception):
    """Base class for every failure this integration raises at its boundary.

    ``status_code`` is the HTTP status the API should answer with (our status, not
    necessarily the provider's). ``outcome_unknown`` says whether a write may have taken
    effect upstream despite the failure -- the caller must reconcile before retrying.
    """

    status_code = 502
    outcome_unknown = False

    def __init__(self, message="Subscription provider error.", *, status_code=None,
                 outcome_unknown=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if outcome_unknown is not None:
            self.outcome_unknown = outcome_unknown


class MaxioConfigError(MaxioError):
    """Our own misconfiguration or rejected credentials (401/403, missing settings).

    Never the caller's fault -- surfaced as 502, never as 401/403 to the caller.
    """

    status_code = 502


class MaxioUnavailable(MaxioError):
    """Provider is unreachable or errored (5xx, 429, transport failure).

    502 when nothing was sent, 504 when a write may have landed.
    """

    status_code = 502


class MaxioUnreadable(MaxioError):
    """The provider answered but the body could not be read/validated."""

    status_code = 502


class MaxioRejected(MaxioError):
    """The provider rejected the request for a reason attributable to the caller.

    Passed through with the provider's own status (400/404/409/422).
    """

    status_code = 422


class PlanNotFound(MaxioError):
    """The requested planHandle is not a product of the configured family."""

    status_code = 404


class OutcomeUnknown(MaxioError):
    """A write may have landed but could not be confirmed even after reconciliation."""

    status_code = 504
    outcome_unknown = True

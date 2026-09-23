"""One failure type at the integration boundary.

Every Maxio failure kind (an ``ApiError`` rejection, a config problem, a decode failure, a
transport failure that never left, a transport failure that may have landed) is translated into
one of these in a single place — the error ladder in ``client.py`` — so views map failure ->
HTTP status uniformly.  The shape follows ``python-error-handling``: a status the boundary
returns, plus ``outcome_unknown`` for a write that may or may not have taken effect.
"""


class ProviderError(Exception):
    """Base for every translated Maxio failure."""

    #: HTTP status this boundary should return to the caller.
    status_code = 502
    #: True only when a write may have landed at Maxio but we could not confirm it.
    outcome_unknown = False
    #: Optional caller-safe detail (never a raw SDK ``str(e)`` or traceback).
    detail = None

    def __init__(self, message, *, status_code=None, outcome_unknown=None, detail=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if outcome_unknown is not None:
            self.outcome_unknown = outcome_unknown
        self.detail = detail


class ProviderConfigError(ProviderError):
    """Our credentials/configuration were rejected — nothing the caller can fix. -> 502."""

    status_code = 502


class ProviderRejected(ProviderError):
    """The caller's input was rejected (400/404/409/422). Passed through with its status."""

    status_code = 400


class ProviderUnavailable(ProviderError):
    """Provider down / rate-limited / transport failure.  ``status_code`` and ``outcome_unknown``
    together say what the caller should do (502 never-sent vs 504 may-have-landed vs 503 quota)."""

    status_code = 502


class ProviderUnreadable(ProviderError):
    """A response body could not be decoded.  On a write the outcome is unknown."""

    status_code = 502

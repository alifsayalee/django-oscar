"""Domain exceptions for the Maxio subscription capability.

The service layer translates every Maxio SDK failure into one of these, so views never see an
``ApiError``/``httpx``/``pydantic`` exception directly and can map each kind onto a clean HTTP
status.
"""


class BillingError(Exception):
    """Base class for every billing failure surfaced to the view layer.

    ``status_code`` is the HTTP status the view should return.
    """

    status_code = 500
    message = 'A billing error occurred.'

    def __init__(self, message=None, *, status_code=None):
        if message is not None:
            self.message = message
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class PlanNotFound(BillingError):
    """The requested plan handle is not one of the available plans."""

    status_code = 400
    message = 'The requested plan was not found.'


class ProviderRejected(BillingError):
    """Maxio rejected the request (a provider 4xx we surface to the caller)."""

    status_code = 422
    message = 'The billing provider rejected the request.'


class ProviderUnavailable(BillingError):
    """The billing provider could not be reached, or returned an unreadable response.

    The outcome of a write in this case is unknown; callers should re-read state rather than
    blindly retrying.
    """

    status_code = 502
    message = 'The billing provider is currently unavailable.'

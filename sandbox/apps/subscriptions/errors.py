"""Translation of Maxio SDK failures into a single boundary exception.

Every failure kind the SDK can raise (``ApiError``, a pydantic decode failure,
and the unwrapped ``httpx`` transport exceptions) is funnelled through here into
one :class:`MaxioError` carrying an HTTP status for our own API and an
``outcome_unknown`` flag that says whether a write may have landed upstream.
See python-error-handling for why each status maps the way it does.
"""

from contextlib import contextmanager

import httpx
from pydantic import ValidationError

from maxio_advanced_billing.core import ApiError, RawError

# Failures that happen before the request leaves the process: nothing landed.
NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Provider 4xx statuses that are genuinely the caller's fault, passed through.
CALLER_FAULT_STATUSES = (400, 404, 409, 422)


class MaxioError(Exception):
    """A failure to surface at our API boundary.

    ``status_code`` is the HTTP status *our* endpoint should answer with;
    ``outcome_unknown`` is True when a write may have taken effect upstream
    even though we could not confirm it.
    """

    def __init__(self, status_code, message, *, outcome_unknown=False):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown


class MaxioConfigurationError(MaxioError):
    """The integration is misconfigured (missing/invalid settings)."""

    def __init__(self, message):
        super().__init__(502, message, outcome_unknown=False)


def _detail(error):
    """Best-effort human-readable detail from a typed error body or RawError.

    These are Maxio's own validation messages (not secrets), so surfacing them
    helps the caller fix their input. Never returns ``str(ApiError)`` (which
    leaks only a type name) and never a raw traceback.
    """
    # Typed arms expose their messages under `.errors` or `.error`.
    errors = getattr(error, 'errors', None)
    if errors:
        if isinstance(errors, dict):
            return '; '.join(f'{k}: {v}' for k, v in errors.items())
        if isinstance(errors, (list, tuple)):
            return '; '.join(str(x) for x in errors)
        return str(errors)
    single = getattr(error, 'error', None)
    if single:
        return str(single)
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return (error.text() or '').strip()[:500] or 'Maxio rejected the request.'
        if isinstance(body, dict):
            body_errors = body.get('errors')
            if isinstance(body_errors, dict):
                return '; '.join(f'{k}: {v}' for k, v in body_errors.items())
            if isinstance(body_errors, (list, tuple)):
                return '; '.join(str(x) for x in body_errors)
            if 'error' in body:
                return str(body['error'])
        return str(body)[:500]
    return 'Maxio rejected the request.'


def translate_api_error(exc):
    """Raise a :class:`MaxioError` for an :class:`ApiError`, per the boundary table."""
    status = exc.status_code
    if status in (401, 403):
        # Our credentials, not the caller's -- never surface 401/403 to them.
        raise MaxioError(502, 'Maxio rejected our credentials.') from exc
    if status == 429:
        raise MaxioError(503, 'Maxio is rate-limiting requests; try again shortly.') from exc
    if status in CALLER_FAULT_STATUSES:
        raise MaxioError(status, _detail(exc.error)) from exc
    # 5xx and every unmapped 4xx are ours to own.
    raise MaxioError(502, 'Maxio request failed.') from exc


@contextmanager
def maxio_call(*, write=False):
    """Run one SDK call, translating every failure kind into :class:`MaxioError`.

    ``write=True`` marks the operation as having a side effect, so an
    unreadable response or a mid-flight transport failure is reported as
    ``outcome_unknown`` rather than a clean failure.
    """
    try:
        yield
    except ApiError as exc:
        translate_api_error(exc)
    except ValidationError as exc:
        # A decode failure bypasses both response modes. On a write the outcome
        # is genuinely unknown; on a read only the detail was lost.
        raise MaxioError(
            502, 'Maxio returned an unreadable response.',
            outcome_unknown=write) from exc
    except NEVER_SENT as exc:
        raise MaxioError(502, 'Could not reach Maxio.', outcome_unknown=False) from exc
    except httpx.RequestError as exc:
        # It may have landed; the reply never came.
        raise MaxioError(504, 'No response from Maxio.', outcome_unknown=write) from exc

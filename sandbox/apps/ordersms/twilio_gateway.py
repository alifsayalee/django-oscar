"""The only module that talks to Twilio.

Everything Twilio-specific lives here: building and holding the SDK client,
translating SDK results into small plain dataclasses, and translating every SDK
failure kind into this app's own exception hierarchy. The rest of the app
depends on those dataclasses and exceptions, never on ``twilio_sdk`` types.

Secrets are read from Django settings at call time and never logged. A shopper's
phone number is never written to a log line from this module.
"""

from __future__ import annotations

import atexit
import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal  # noqa: F401  (kept for parity with money-handling guidance)
from email.utils import parsedate_to_datetime

import httpx
from django.conf import settings
from pydantic import ValidationError

from twilio_sdk import Client, ServerConfigDict
from twilio_sdk.core import UNSET, ApiError, BasicAuthCredentials
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

logger = logging.getLogger("ordersms")

# The messaging API (create/fetch/list/update message) is served from Twilio
# server "default". Lookups live on "default4" and are NOT governed by
# TWILIO_BASE_URL.
_CLIENT_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# Domain exceptions (see python-error-handling: one translation layer, here).
# --------------------------------------------------------------------------- #
class GatewayError(Exception):
    """Base for every failure this gateway raises."""


class ConfigurationError(GatewayError):
    """Credentials/config are missing or the provider rejected our identity.

    Nothing the caller can fix; nothing was accomplished on the provider side.
    """


class InvalidPhoneNumber(GatewayError):
    """The provider does not consider the number a usable destination."""


class ProviderUnavailable(GatewayError):
    """A send/read could not be completed.

    ``outcome_unknown`` is False when we know nothing happened (never sent) and
    True when it may have landed but we could not read the answer.
    """

    def __init__(self, message: str, *, outcome_unknown: bool):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


# --------------------------------------------------------------------------- #
# Plain result types handed back to the rest of the app.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SentMessage:
    sid: str
    status: str
    error_code: int | None
    error_message: str | None
    date_sent: dt.datetime | None


@dataclass(frozen=True)
class ProviderMessage:
    """A message as the provider reports it (used by reconciliation)."""

    sid: str
    status: str
    from_number: str | None
    to_number: str | None
    date_sent: dt.datetime | None
    error_code: int | None


def _val(x):
    """Map the SDK's UNSET sentinel (and only it) to None for our own types."""
    return None if x is UNSET else x


def _status_str(status) -> str:
    status = _val(status)
    return "" if status is None else str(status)


def _parse_rfc2822(value) -> dt.datetime | None:
    value = _val(value)
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Client lifetime — one long-lived, lazily-built, module-global client.
# --------------------------------------------------------------------------- #
_client: Client | None = None
_client_lock = threading.Lock()

_REQUIRED_SETTINGS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")


def _account_sid() -> str:
    return settings.TWILIO_ACCOUNT_SID


def get_client() -> Client:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        missing = [n for n in _REQUIRED_SETTINGS if not getattr(settings, n, "")]
        if missing:
            raise ConfigurationError("missing Twilio settings: " + ", ".join(missing))
        base_url = getattr(settings, "TWILIO_BASE_URL", "")
        # Override ONLY the messaging server ("default") when TWILIO_BASE_URL is
        # set; other hosts (e.g. lookups on "default4") keep their defaults.
        # Omitting server_config (None) falls through to the SDK's own defaults.
        server_config: ServerConfigDict | None = (
            {"default": {"base_url": base_url}} if base_url else None
        )
        _client = Client(
            account_sid_auth_token=BasicAuthCredentials(
                username=settings.TWILIO_ACCOUNT_SID,
                password=settings.TWILIO_AUTH_TOKEN,
            ),
            timeout=_CLIENT_TIMEOUT,
            server_config=server_config,
        )
        return _client


def _reset_client_for_tests() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        _client = None


@atexit.register
def _close_client() -> None:  # pragma: no cover - process teardown
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None


# --------------------------------------------------------------------------- #
# Error translation shared by the send/read paths.
# --------------------------------------------------------------------------- #
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


def _raise_translated(exc: Exception, *, action: str) -> "ProviderUnavailable | ConfigurationError":
    """Translate an SDK/transport failure. Never includes secrets or numbers."""
    if isinstance(exc, ApiError):
        status = exc.status_code
        if status in (401, 403):
            logger.error("Twilio refused our credentials during %s (HTTP %s)", action, status)
            return ConfigurationError("provider refused our credentials")
        if status == 429:
            logger.warning("Twilio rate-limited %s (HTTP 429)", action)
            return ProviderUnavailable("provider rate-limited us", outcome_unknown=False)
        logger.warning("Twilio error during %s (HTTP %s)", action, status)
        # A 5xx on a write may have landed; a 4xx did not.
        return ProviderUnavailable(
            "provider error (HTTP %s)" % status,
            outcome_unknown=status is not None and status >= 500,
        )
    if isinstance(exc, ValidationError):
        logger.warning("Unreadable Twilio response during %s", action)
        return ProviderUnavailable("unreadable provider response", outcome_unknown=True)
    if isinstance(exc, _NEVER_SENT):
        logger.warning("Twilio %s never left the process (%s)", action, type(exc).__name__)
        return ProviderUnavailable("request was never sent", outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        logger.warning("Twilio %s got no readable reply (%s)", action, type(exc).__name__)
        return ProviderUnavailable("no reply from provider", outcome_unknown=True)
    raise exc


# --------------------------------------------------------------------------- #
# Public gateway operations.
# --------------------------------------------------------------------------- #
def canonicalize_number(raw: str) -> str:
    """Validate a number with the provider and return its canonical E.164 form.

    Raises InvalidPhoneNumber for a number the provider will not accept as a
    destination (HTTP 404 from the lookup), ConfigurationError for a credential
    problem, and ProviderUnavailable if the provider could not be reached.
    """
    client = get_client()
    try:
        result = client.lookups_v1_phone_number_api.fetch_phone_number2(raw)
    except ApiError as exc:
        if exc.status_code == 404:
            raise InvalidPhoneNumber("not a usable destination number")
        raise _raise_translated(exc, action="lookup")
    except (ValidationError, httpx.RequestError) as exc:
        raise _raise_translated(exc, action="lookup")
    canonical = _val(result.phone_number)
    if not canonical:
        # 200 but no canonical number: outcome unknown, don't store a bad value.
        raise ProviderUnavailable("lookup returned no canonical number", outcome_unknown=True)
    return canonical


def _send(*, to: str, body: str, scheduled: bool, send_at: dt.datetime | None) -> SentMessage:
    client = get_client()
    account_sid = _account_sid()
    try:
        if scheduled:
            message = client.api20100401_message.create_message(
                account_sid,
                to,
                messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=send_at,
                body=body,
            )
        else:
            message = client.api20100401_message.create_message(
                account_sid,
                to,
                from_=settings.TWILIO_FROM_NUMBER,
                body=body,
            )
    except (ApiError, ValidationError, httpx.RequestError) as exc:
        raise _raise_translated(exc, action="send")
    sid = _val(message.sid)
    if not sid:
        # Accepted with no identifier: we cannot act on it later -> outcome unknown.
        raise ProviderUnavailable("provider returned no message sid", outcome_unknown=True)
    return SentMessage(
        sid=sid,
        status=_status_str(message.status),
        error_code=_val(message.error_code),
        error_message=_val(message.error_message),
        date_sent=_parse_rfc2822(message.date_sent),
    )


def send_sms(to: str, body: str) -> SentMessage:
    """Send an immediate SMS from the configured TWILIO_FROM_NUMBER."""
    return _send(to=to, body=body, scheduled=False, send_at=None)


def schedule_followup(to: str, body: str, send_at: dt.datetime) -> SentMessage:
    """Queue a follow-up message with the provider for ``send_at``.

    Scheduled messages must go through the Messaging Service (Twilio does not
    allow a bare From with scheduling).
    """
    if not settings.TWILIO_MESSAGING_SERVICE_SID:
        raise ConfigurationError("no messaging service configured for scheduling")
    return _send(to=to, body=body, scheduled=True, send_at=send_at)


_CANCEL_RETRY_ATTEMPTS = 4
_CANCEL_RETRY_DELAY = 1.5


def cancel_scheduled(sid: str) -> str:
    """Call off a scheduled message before it goes out. Returns the new status.

    A message scheduled moments earlier can briefly 404 on update while Twilio
    replicates it. Because a follow-up that is *not* cancelled would reach a
    customer whose order was cancelled — the exact incident to prevent — we
    retry a 404 a few times (the SDK itself performs no retries).
    """
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(_CANCEL_RETRY_ATTEMPTS):
        try:
            message = client.api20100401_message.update_message(
                _account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED
            )
            return _status_str(message.status)
        except ApiError as exc:
            last_exc = exc
            if exc.status_code == 404 and attempt < _CANCEL_RETRY_ATTEMPTS - 1:
                time.sleep(_CANCEL_RETRY_DELAY)
                continue
            raise _raise_translated(exc, action="cancel")
        except (ValidationError, httpx.RequestError) as exc:
            raise _raise_translated(exc, action="cancel")
    # Exhausted retries on 404.
    raise _raise_translated(last_exc, action="cancel")  # type: ignore[arg-type]


def redact_content(sid: str) -> str:
    """Dispose of a message's text at the provider (redact the body).

    Keeps the message record and its status; only the text is removed. Returns
    the current status.
    """
    client = get_client()
    try:
        message = client.api20100401_message.update_message(_account_sid(), sid, body="")
    except (ApiError, ValidationError, httpx.RequestError) as exc:
        raise _raise_translated(exc, action="redact")
    return _status_str(message.status)


def fetch_status(sid: str) -> SentMessage:
    """Re-read a message's current delivery outcome from the provider."""
    client = get_client()
    try:
        message = client.api20100401_message.fetch_message(_account_sid(), sid)
    except (ApiError, ValidationError, httpx.RequestError) as exc:
        raise _raise_translated(exc, action="fetch")
    return SentMessage(
        sid=_val(message.sid) or sid,
        status=_status_str(message.status),
        error_code=_val(message.error_code),
        error_message=_val(message.error_message),
        date_sent=_parse_rfc2822(message.date_sent),
    )


_MAX_RECON_PAGES = 200


def list_from_number(date_from: dt.datetime, date_to: dt.datetime) -> list[ProviderMessage]:
    """List the provider's own messages sent from TWILIO_FROM_NUMBER in a window.

    Asks the provider for that number's messages (From filter) over a whole-day
    widened range, then narrows to the exact [date_from, date_to) window in code.
    Paginates with a hard page cap.
    """
    client = get_client()
    account_sid = _account_sid()
    from_number = settings.TWILIO_FROM_NUMBER
    # Twilio's DateSent</> filters are whole-day granular: widen by a day each side.
    lower = date_from - dt.timedelta(days=1)
    upper = date_to + dt.timedelta(days=1)

    collected: list[ProviderMessage] = []
    page = 0
    truncated = False
    while True:
        if page >= _MAX_RECON_PAGES:
            truncated = True
            break
        try:
            response = client.api20100401_message.list_message(
                account_sid,
                from_=from_number,
                date_sent_query=upper,        # DateSent<
                date_sent_query_query=lower,  # DateSent>
                page=page,
                page_size=100,
            )
        except (ApiError, ValidationError, httpx.RequestError) as exc:
            raise _raise_translated(exc, action="reconcile")
        messages = _val(response.messages) or []
        for m in messages:
            sid = _val(m.sid)
            if not sid:
                continue
            collected.append(
                ProviderMessage(
                    sid=sid,
                    status=_status_str(m.status),
                    from_number=_val(m.from_),
                    to_number=_val(m.to),
                    date_sent=_parse_rfc2822(m.date_sent),
                    error_code=_val(m.error_code),
                )
            )
        if _val(response.next_page_uri):
            page += 1
            continue
        break

    # Narrow to the exact window the caller asked about.
    narrowed = [
        pm
        for pm in collected
        if pm.date_sent is not None and date_from <= pm.date_sent < date_to
    ]
    if truncated:
        logger.warning("reconciliation hit the page cap; results may be partial")
    return narrowed

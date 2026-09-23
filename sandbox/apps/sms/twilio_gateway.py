"""The only module in this app that talks to Twilio.

Everything above this layer works with plain ``MessageResult`` objects and a
single boundary exception, ``TwilioError``. The Twilio SDK's several failure
kinds (an ``ApiError`` with a ``RawError`` body, a pydantic decode failure that
bypasses both SDK response modes, an unwrapped ``httpx`` transport error) are
all translated here.

Secrets and PII discipline: the auth token is never logged and never leaves
this module; a shopper's phone number is never written to a log line.
"""

import atexit
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from django.conf import settings
from pydantic import ValidationError

from twilio_sdk import Client
from twilio_sdk.core import ApiError, BasicAuthCredentials, UnsetType
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

logger = logging.getLogger("sms.twilio")

_client: Optional[Client] = None
_client_lock = threading.Lock()


class TwilioError(Exception):
    """A failure talking to Twilio, safe to surface (carries no number or token).

    ``outcome_unknown`` marks the cases where the request may have taken effect
    but we could not read the answer (a decode failure on a 2xx, or a transport
    error) — the caller must not assume the message did or did not go out.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


@dataclass
class MessageResult:
    """The subset of a Twilio message we keep — provider identity and outcome."""

    sid: Optional[str]
    status: Optional[str]
    error_code: Optional[int]
    error_message: Optional[str]
    to: Optional[str]
    from_: Optional[str]
    body: Optional[str]
    date_sent: Optional[datetime]


def _build_client() -> Client:
    """Construct the long-lived SDK client from Django settings.

    HTTP Basic auth (account SID + auth token). When ``TWILIO_BASE_URL`` is set
    it overrides only the *messaging* API host (SDK server key ``default``);
    Lookups (server ``default4``) keeps its own host.
    """
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    base_url = (getattr(settings, "TWILIO_BASE_URL", "") or "").strip()

    kwargs: dict[str, Any] = {
        "account_sid_auth_token": BasicAuthCredentials(
            username=account_sid, password=auth_token
        )
    }
    if base_url:
        # Override the messaging host only; leave every other server default.
        kwargs["server_config"] = {"default": {"base_url": base_url}}
    return Client(**kwargs)


def get_client() -> Client:
    """Return the process-wide client, building it once (thread-safe, lazy)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
                atexit.register(_close_client)
    return _client


def _close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass
        _client = None


def _v(value: Any) -> Any:
    """SDK ``UNSET`` -> ``None``; anything else passes through."""
    if isinstance(value, UnsetType):
        return None
    return value


def _status_str(value: Any) -> Optional[str]:
    """A message status (open enum or plain str) as its wire value, or None."""
    if value is None or isinstance(value, UnsetType):
        return None
    return str(value)  # (str, Enum) __str__ yields the wire value


def _parse_date_sent(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, UnsetType) or not value:
        return None
    try:
        return parsedate_to_datetime(value)  # Twilio sends RFC 1123
    except (TypeError, ValueError, OverflowError):
        return None


def _to_result(msg: Any) -> MessageResult:
    return MessageResult(
        sid=_v(msg.sid),
        status=_status_str(msg.status),
        error_code=_v(msg.error_code),
        error_message=_v(msg.error_message),
        to=_v(msg.to),
        from_=_v(msg.from_),
        body=_v(msg.body),
        date_sent=_parse_date_sent(_v(msg.date_sent)),
    )


# --- Lookups -------------------------------------------------------------

def canonicalize_number(raw_number: str) -> str:
    """Validate a number and return the provider's canonical E.164 form.

    Uses Lookups v1, whose response decodes cleanly (v2's model rejects the
    explicit JSON nulls this account's Lookup responses carry). A number the
    provider cannot parse answers 404 and is rejected here.
    """
    client = get_client()
    try:
        resp = client.lookups_v1_phone_number_api.fetch_phone_number2(raw_number)
    except ApiError as e:
        if e.status_code == 404:
            raise TwilioError(
                "not a usable destination", status_code=404
            ) from e
        raise TwilioError(
            "number lookup was rejected by the provider",
            status_code=e.status_code,
        ) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the lookup response", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during lookup", outcome_unknown=True
        ) from e

    canonical = _v(resp.phone_number)
    if not canonical:
        raise TwilioError("lookup did not return a canonical number")
    return str(canonical)


# --- Messages ------------------------------------------------------------

def _create_message(**kwargs: Any) -> MessageResult:
    client = get_client()
    account_sid = settings.TWILIO_ACCOUNT_SID
    to_number = kwargs.pop("to")
    try:
        msg = client.api20100401_message.create_message(
            account_sid, to_number, **kwargs
        )
    except ApiError as e:
        # Provider rejected the request (e.g. bad number, auth). The request
        # was seen and refused; the outcome is known (not sent).
        raise TwilioError("send rejected", status_code=e.status_code) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the send response", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during send", outcome_unknown=True
        ) from e

    result = _to_result(msg)
    if not result.sid:
        # A 2xx that decoded but named no message id: outcome unknown.
        raise TwilioError(
            "send returned no message id", outcome_unknown=True
        )
    return result


def send_sms(to_number: str, body: str) -> MessageResult:
    """Send an SMS now from the app's configured sending number."""
    return _create_message(
        to=to_number, from_=settings.TWILIO_FROM_NUMBER, body=body
    )


def schedule_followup(to_number: str, body: str, send_at: datetime) -> MessageResult:
    """Queue a message with the provider to be sent at ``send_at``.

    Scheduled sends go through the messaging service (required by Twilio for
    scheduling); ``from_`` must not be combined with it. The provider holds the
    timer — nothing is scheduled inside this application.
    """
    return _create_message(
        to=to_number,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at,
        body=body,
    )


def cancel_scheduled(sid: str) -> MessageResult:
    """Call off a scheduled message before it goes out."""
    client = get_client()
    account_sid = settings.TWILIO_ACCOUNT_SID
    try:
        msg = client.api20100401_message.update_message(
            account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
        )
    except ApiError as e:
        raise TwilioError("cancel rejected", status_code=e.status_code) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the cancel response", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during cancel", outcome_unknown=True
        ) from e
    return _to_result(msg)


def redact_content(sid: str) -> MessageResult:
    """Empty a message's body at the provider (its record and outcome survive)."""
    client = get_client()
    account_sid = settings.TWILIO_ACCOUNT_SID
    try:
        msg = client.api20100401_message.update_message(account_sid, sid, body="")
    except ApiError as e:
        raise TwilioError("redaction rejected", status_code=e.status_code) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the redaction response", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during redaction", outcome_unknown=True
        ) from e
    return _to_result(msg)


def fetch_status(sid: str) -> MessageResult:
    """Read a message's current delivery outcome from the provider."""
    client = get_client()
    account_sid = settings.TWILIO_ACCOUNT_SID
    try:
        msg = client.api20100401_message.fetch_message(account_sid, sid)
    except ApiError as e:
        raise TwilioError("fetch rejected", status_code=e.status_code) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the message", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during fetch", outcome_unknown=True
        ) from e
    return _to_result(msg)


def list_sent_messages(
    date_from: datetime, date_to: datetime
) -> list[MessageResult]:
    """Provider's own record of messages from this app's sending number.

    Filters by ``From = TWILIO_FROM_NUMBER`` at the provider (so traffic on the
    account from other numbers is never counted), narrows the date window at the
    provider (Twilio's DateSent bounds are day-granular, so the window is
    widened a day on each side), paginates the whole range, and finally keeps
    only messages whose ``date_sent`` falls within [date_from, date_to]
    inclusive.
    """
    from datetime import timedelta

    client = get_client()
    account_sid = settings.TWILIO_ACCOUNT_SID
    from_number = settings.TWILIO_FROM_NUMBER

    provider_lower = date_from - timedelta(days=1)
    provider_upper = date_to + timedelta(days=1)

    collected: list[MessageResult] = []
    page = 0
    page_size = 50
    max_pages = 200  # safety bound for a live account
    try:
        while page < max_pages:
            resp = client.api20100401_message.list_message(
                account_sid,
                from_=from_number,
                date_sent_query_query=provider_lower,  # DateSent>
                date_sent_query=provider_upper,  # DateSent<
                page=page,
                page_size=page_size,
            )
            msgs = resp.messages or []
            for m in msgs:
                collected.append(_to_result(m))
            if len(msgs) < page_size:
                break
            page += 1
    except ApiError as e:
        raise TwilioError(
            "reconciliation listing rejected", status_code=e.status_code
        ) from e
    except (ValidationError, ValueError) as e:
        raise TwilioError(
            "could not read the reconciliation listing", outcome_unknown=True
        ) from e
    except httpx.HTTPError as e:
        raise TwilioError(
            "provider unreachable during reconciliation", outcome_unknown=True
        ) from e

    # Precise inclusive filter on the full datetimes.
    def in_range(m: MessageResult) -> bool:
        if m.date_sent is None:
            # No parseable send time: keep it so the report never silently
            # drops a provider record within the widened window.
            return True
        return date_from <= m.date_sent <= date_to

    return [m for m in collected if in_range(m)]

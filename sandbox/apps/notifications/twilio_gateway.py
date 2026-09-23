"""The single point of contact with Twilio, via the APIMatic-generated ``twilio-sdk``.

Everything the app knows about talking to Twilio lives here: one lazily-built, long-lived sync client
(Django/WSGI), the base-URL override for the messaging API, and one error boundary that turns every
Twilio failure mode into this module's own small exception set so the rest of the app never imports
an SDK type or an ``httpx`` type.

Secrets and shopper numbers never enter a log line here.
"""

from __future__ import annotations

import atexit
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx
from django.conf import settings
from pydantic import ValidationError

from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import UNSET, ApiError, BasicAuthCredentials, RawError
from twilio_sdk.models.enums.message_enum_schedule_type import MessageEnumScheduleType
from twilio_sdk.models.enums.message_enum_status import MessageEnumStatus
from twilio_sdk.models.enums.message_enum_update_status import MessageEnumUpdateStatus

log = logging.getLogger("apps.notifications")

# Outcome vocabulary, mirrored on OrderNotification. The one place a provider status becomes ours.
DONE, PENDING, FAILED, PARTIAL, UNKNOWN = "done", "pending", "failed", "partial", "unknown"

_DONE = {MessageEnumStatus.DELIVERED, MessageEnumStatus.SENT, MessageEnumStatus.RECEIVED, MessageEnumStatus.READ}
_PENDING = {
    MessageEnumStatus.QUEUED,
    MessageEnumStatus.SENDING,
    MessageEnumStatus.ACCEPTED,
    MessageEnumStatus.SCHEDULED,
    MessageEnumStatus.RECEIVING,
}
_FAILED = {MessageEnumStatus.FAILED, MessageEnumStatus.UNDELIVERED, MessageEnumStatus.CANCELED}


# --- exceptions this module raises (the app never sees an SDK/httpx type) --------------------------

class TwilioGatewayError(Exception):
    """A Twilio interaction failed. ``outcome`` says what is known about a would-be message."""

    def __init__(self, message: str, *, outcome: str = UNKNOWN, status_code: int | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.status_code = status_code


class NotAUsableDestination(TwilioGatewayError):
    """The provider does not consider the number a usable destination (reject at registration)."""

    def __init__(self, message: str = "Not a usable destination"):
        super().__init__(message, outcome=FAILED, status_code=400)


class TwilioNotConfigured(TwilioGatewayError):
    """Required Twilio settings are missing."""


# --- results handed back to the app (UNSET already resolved to None) -------------------------------

@dataclass
class MessageResult:
    sid: str | None
    provider_status: str | None
    outcome: str
    error_code: int | None
    date_sent: datetime | None


@dataclass
class ProviderMessage:
    sid: str | None
    to: str | None
    from_: str | None
    provider_status: str | None
    date_sent: datetime | None
    error_code: int | None


# --- client lifetime -------------------------------------------------------------------------------

_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def _require(name: str) -> str:
    value = getattr(settings, name, "") or ""
    if not value:
        raise TwilioNotConfigured(f"Missing Twilio setting {name}")
    return value


def get_client() -> TwilioSdkClient:
    """Build the sync client on first use and reuse it for the process lifetime."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                sid = _require("TWILIO_ACCOUNT_SID")
                token = _require("TWILIO_AUTH_TOKEN")
                # TWILIO_BASE_URL overrides ONLY the messaging API (server `default`). Lookup
                # (server `default4`) keeps its own default deliberately.
                base_url = getattr(settings, "TWILIO_BASE_URL", "") or ""
                server_config: ServerConfigDict | None = (
                    {"default": {"base_url": base_url}} if base_url else None
                )
                _client = TwilioSdkClient(
                    account_sid_auth_token=BasicAuthCredentials(username=sid, password=token),
                    timeout=15.0,
                    server_config=server_config,
                )
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


def account_sid() -> str:
    return _require("TWILIO_ACCOUNT_SID")


def from_number() -> str:
    return _require("TWILIO_FROM_NUMBER")


# --- helpers ---------------------------------------------------------------------------------------

def _v(value):
    """Resolve the SDK's UNSET sentinel to None so nothing leaks across the boundary."""
    return None if value is UNSET else value


def _status_str(status) -> str | None:
    status = _v(status)
    return None if status is None else str(status)


def status_to_outcome(status) -> str:
    """Map a provider status to our outcome. Enumerated by name; the default arm is UNKNOWN."""
    status = _v(status)
    if status is None:
        return UNKNOWN
    if status in _DONE:
        return DONE
    if status in _PENDING:
        return PENDING
    if status in _FAILED:
        return FAILED
    if status == MessageEnumStatus.PARTIALLY_DELIVERED:
        return PARTIAL
    # A member we did not list, or a value newer than the SDK: neither done nor failed.
    return UNKNOWN


def _parse_date_sent(value) -> datetime | None:
    value = _v(value)
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _message_result(msg) -> MessageResult:
    return MessageResult(
        sid=_v(msg.sid),
        provider_status=_status_str(msg.status),
        outcome=status_to_outcome(msg.status),
        error_code=_v(msg.error_code),
        date_sent=_parse_date_sent(msg.date_sent),
    )


def _translate(e: Exception, *, action: str) -> TwilioGatewayError:
    """Turn one SDK/httpx/pydantic failure into a TwilioGatewayError with a known outcome.

    Never logs the shopper's number or the auth token, and never surfaces a raw provider body.
    """
    if isinstance(e, ApiError):
        code = e.status_code
        if code in (401, 403):
            log.error("Twilio refused our credentials on %s (HTTP %s)", action, code)
            return TwilioGatewayError("Provider rejected our credentials", outcome=UNKNOWN, status_code=code)
        if code == 429:
            return TwilioGatewayError("Provider rate-limited", outcome=UNKNOWN, status_code=code)
        if code is not None and 400 <= code < 500:
            # Deterministic rejection: the send did not happen.
            detail = e.error.text()[:200] if isinstance(e.error, RawError) else ""
            log.warning("Twilio rejected %s (HTTP %s): %s", action, code, detail)
            return TwilioGatewayError("Provider rejected the request", outcome=FAILED, status_code=code)
        # 5xx or unmapped: on a write it may still have landed.
        log.error("Twilio server error on %s (HTTP %s)", action, code)
        return TwilioGatewayError("Provider unavailable", outcome=UNKNOWN, status_code=code)
    if isinstance(e, ValidationError):
        # Decode failure: bypasses both response modes; outcome genuinely unknown.
        log.error("Twilio response for %s did not decode; outcome unknown", action)
        return TwilioGatewayError("Unreadable provider response", outcome=UNKNOWN)
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
        # Never reached the provider: nothing happened.
        log.error("Could not reach Twilio for %s (never sent)", action)
        return TwilioGatewayError("Provider unreachable", outcome=FAILED, status_code=502)
    if isinstance(e, httpx.RequestError):
        # Sent, no readable answer: may have landed.
        log.error("No response from Twilio for %s (outcome unknown)", action)
        return TwilioGatewayError("No response from provider", outcome=UNKNOWN, status_code=504)
    log.exception("Unexpected error talking to Twilio on %s", action)
    return TwilioGatewayError("Unexpected provider error", outcome=UNKNOWN)


# --- operations ------------------------------------------------------------------------------------

def validate_and_canonicalize(number: str) -> str:
    """Return the provider's canonical E.164 form of ``number``, or reject it.

    Uses Lookup v1: 200 => usable (return canonical ``phone_number``); 404 => not a usable
    destination. v2 Lookup cannot decode live responses (null data-package fields), so v1 is used.
    """
    client = get_client()
    try:
        resp = client.lookups_v1_phone_number_api.fetch_phone_number2(number)
    except ApiError as e:
        if e.status_code == 404:
            raise NotAUsableDestination() from e
        raise _translate(e, action="lookup") from e
    except (ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="lookup") from e
    canonical = _v(resp.phone_number)
    if not canonical:
        # A 200 that did not carry the number: cannot canonicalize -> treat as unusable.
        raise NotAUsableDestination("Provider returned no canonical number")
    return canonical


def send_immediate(to: str, body: str) -> MessageResult:
    """Send an SMS now, from our configured sending number."""
    client = get_client()
    try:
        msg = client.api20100401_message.create_message(
            account_sid(), to, from_=from_number(), body=body
        )
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="send") from e
    result = _message_result(msg)
    if result.sid is None:
        raise TwilioGatewayError("Send accepted no id; outcome unknown", outcome=UNKNOWN)
    return result


def schedule_followup(to: str, body: str, send_at: datetime) -> MessageResult:
    """Queue a message with the provider for ``send_at`` (Messaging Service required for scheduling)."""
    client = get_client()
    try:
        msg = client.api20100401_message.create_message(
            account_sid(),
            to,
            messaging_service_sid=_require("TWILIO_MESSAGING_SERVICE_SID"),
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        )
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="schedule") from e
    result = _message_result(msg)
    if result.sid is None:
        raise TwilioGatewayError("Schedule accepted no id; outcome unknown", outcome=UNKNOWN)
    return result


def cancel_scheduled(sid: str) -> MessageResult:
    """Call off a not-yet-sent scheduled message before it goes out."""
    client = get_client()
    try:
        msg = client.api20100401_message.update_message(
            account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED
        )
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="cancel") from e
    return _message_result(msg)


def redact_content(sid: str) -> None:
    """Dispose of the message text at the provider (record + outcome survive)."""
    client = get_client()
    try:
        client.api20100401_message.update_message(account_sid(), sid, body="")
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="redact") from e


def fetch_status(sid: str) -> MessageResult:
    """Read the provider's current delivery state for a message."""
    client = get_client()
    try:
        msg = client.api20100401_message.fetch_message(account_sid(), sid)
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="fetch") from e
    return _message_result(msg)


def list_from_number(day_from, day_to, *, max_pages: int = 50, page_size: int = 100):
    """List messages the provider recorded as sent from our number over a whole-day window.

    Returns ``(messages, truncated)``. Whole-day granularity is widened by the caller; the caller
    narrows to the exact instant window afterwards. Paging is bounded by ``max_pages``.
    """
    client = get_client()
    messages: list[ProviderMessage] = []
    truncated = False
    page = 0
    try:
        while True:
            if page >= max_pages:
                truncated = True
                break
            resp = client.api20100401_message.list_message(
                account_sid(),
                from_=from_number(),
                date_sent_query_query=day_from,  # DateSent> (lower bound)
                date_sent_query=day_to,          # DateSent< (upper bound)
                page_size=page_size,
                page=page,
            )
            batch = _v(resp.messages) or []
            for m in batch:
                messages.append(
                    ProviderMessage(
                        sid=_v(m.sid),
                        to=_v(m.to),
                        from_=_v(m.from_),
                        provider_status=_status_str(m.status),
                        date_sent=_parse_date_sent(m.date_sent),
                        error_code=_v(m.error_code),
                    )
                )
            if _v(resp.next_page_uri) is None or not batch:
                break
            page += 1
    except (ApiError, ValidationError, httpx.HTTPError) as e:
        raise _translate(e, action="reconcile") from e
    return messages, truncated

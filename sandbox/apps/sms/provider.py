"""The Twilio gateway — the single place this app talks to the provider.

Everything here goes through the APIMatic-generated ``twilio-sdk``. The client is a long-lived,
lazily-built module global (Django/WSGI), closed at process exit.

Contract facts (SDK map + source), never from memory:
- Messaging ops (create/fetch/list/update message) resolve against server ``default``
  (api.twilio.com); ``TWILIO_BASE_URL`` overrides only that server. Number lookup uses server
  ``default4`` (lookups.twilio.com) and is deliberately NOT overridden by ``TWILIO_BASE_URL``.
- Auth is HTTP Basic via ``account_sid_auth_token``.
- The SDK performs no retries; a send is attempted exactly once.
- ``ApiError.error`` is always ``RawError`` for these ops (Case B) — nothing to narrow.
- A decode failure raises ``pydantic.ValidationError`` and bypasses both response modes; httpx
  transport exceptions arrive unwrapped.

Privacy: the destination number is never written to a log line here. Provider error bodies (which
can echo the number) are stored on the Notification row (database, not logs) and never logged.
"""
import atexit
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx
from django.conf import settings
from pydantic import ValidationError

from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import UNSET, ApiError, BasicAuthCredentials, RawError
from twilio_sdk.models.enums import (
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

log = logging.getLogger("apps.sms")

# httpx failures that mean the request never left the machine (nothing happened).
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

_client = None
_lock = threading.Lock()


class ProviderError(Exception):
    """A provider failure the caller should surface (not a per-message send failure)."""

    def __init__(self, status_code=502, message="provider unavailable"):
        super().__init__(message)
        self.status_code = status_code


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                server_config: ServerConfigDict | None = None
                base = str(getattr(settings, "TWILIO_BASE_URL", "") or "").strip()
                if base:
                    # Override the messaging server only; lookups (default4) keep their host.
                    server_config = {"default": {"base_url": base}}
                _client = TwilioSdkClient(
                    account_sid_auth_token=BasicAuthCredentials(
                        username=settings.TWILIO_ACCOUNT_SID,
                        password=settings.TWILIO_AUTH_TOKEN,
                    ),
                    server_config=server_config,
                    timeout=20.0,
                )
                atexit.register(close_client)
    return _client


def close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass
        _client = None


# --- small helpers -------------------------------------------------------------------------

def _val(v):
    """Resolve an SDK UNSET/None member to a plain value or None (never hand UNSET out)."""
    if v is UNSET:
        return None
    return v


def _parse_dt(v):
    """Twilio RFC-2822 timestamp string -> aware datetime (UTC), or None."""
    raw = _val(v)
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def outcome_from_status(status) -> str:
    """The one place a provider message status becomes our own outcome.

    Members enumerated by name; anything unlisted or newer than the SDK -> 'unknown'
    (neither delivered nor failed).
    """
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.RECEIVED | MessageEnumStatus.READ:
            return "delivered"
        case MessageEnumStatus.SENT:
            return "sent"
        case MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.ACCEPTED:
            return "pending"
        case MessageEnumStatus.SCHEDULED:
            return "scheduled"
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return "failed"
        case MessageEnumStatus.CANCELED:
            return "canceled"
        case MessageEnumStatus.PARTIALLY_DELIVERED:
            return "partial"
        case _:
            return "unknown"


@dataclass
class SendResult:
    """A provider interaction's outcome, mapped into our own vocabulary. Never raises out."""

    sid: str | None
    outcome: str
    status: str
    error_code: int | None
    error_message: str
    outcome_unknown: bool
    date_sent: datetime | None = None
    date_created: datetime | None = None


def _result_from_message(msg) -> SendResult:
    sid = _val(msg.sid)
    status = _val(msg.status)
    result = SendResult(
        sid=sid,
        outcome=outcome_from_status(msg.status),
        status=str(status) if status is not None else "",
        error_code=_val(msg.error_code),
        error_message=_val(msg.error_message) or "",
        outcome_unknown=False,
        date_sent=_parse_dt(msg.date_sent),
        date_created=_parse_dt(msg.date_created),
    )
    if result.sid is None:
        # A 2xx with no sid: the call was accepted but we cannot name what it created.
        result.outcome = "unknown"
        result.outcome_unknown = True
        if not result.error_message:
            result.error_message = "provider returned no message sid"
    return result


def _truncate(text, limit=500):
    if not text:
        return ""
    return text[:limit]


def _raw_detail(e: ApiError) -> str:
    # These ops are Case B, so e.error is always RawError; narrow before reading it.
    err = e.error
    if isinstance(err, RawError):
        try:
            return _truncate(err.text())
        except Exception:  # pragma: no cover - RawError.text() should not raise
            return ""
    return _truncate(str(err))


def _guard_write(callable_, *, action):
    """Run a provider write and map every failure kind to a SendResult without raising.

    Used for message sends and message updates that must not fail the caller's operation.
    """
    try:
        msg = callable_()
    except ApiError as e:
        log.warning("Twilio %s failed: HTTP %s", action, e.status_code)
        return SendResult(None, "failed", "", None, _raw_detail(e), False)
    except ValidationError:
        log.warning("Twilio %s: unreadable response; outcome unknown", action)
        return SendResult(None, "unknown", "", None, "provider response unreadable", True)
    except _NEVER_SENT:
        log.warning("Twilio %s: request never sent", action)
        return SendResult(None, "failed", "", None, "request not sent to provider", False)
    except httpx.RequestError:
        log.warning("Twilio %s: no response; outcome unknown", action)
        return SendResult(None, "unknown", "", None, "no response from provider", True)
    return _result_from_message(msg)


# --- public gateway ------------------------------------------------------------------------

def lookup_number(raw: str):
    """Return the provider's canonical E.164 form if it is a usable destination, else None.

    None means the provider rejected the number as not a usable/possible destination (HTTP 404) —
    this is the registration-time reject. Genuine provider/transport failures raise ProviderError.
    """
    client = get_client()
    try:
        result = client.lookups_v1_phone_number_api.fetch_phone_number2(raw)
    except ApiError as e:
        if e.status_code == 404:
            return None
        log.warning("Twilio lookup failed: HTTP %s", e.status_code)
        raise ProviderError(502, "number lookup failed")
    except _NEVER_SENT:
        raise ProviderError(502, "number lookup not sent")
    except (httpx.RequestError, ValidationError):
        raise ProviderError(504, "number lookup: no usable response")
    canonical = _val(result.phone_number)
    return canonical or None


def send_immediate(to: str, body: str) -> SendResult:
    """Send an SMS now from TWILIO_FROM_NUMBER (so it is reconcilable by that From)."""
    client = get_client()
    return _guard_write(
        lambda: client.api20100401_message.create_message(
            settings.TWILIO_ACCOUNT_SID,
            to,
            from_=settings.TWILIO_FROM_NUMBER,
            body=body,
        ),
        action="create_message",
    )


def schedule_followup(to: str, body: str, send_at: datetime) -> SendResult:
    """Queue a message with the provider for `send_at` via the Messaging Service.

    Scheduling requires a Messaging Service and schedule_type=fixed; From cannot be combined.
    """
    client = get_client()
    return _guard_write(
        lambda: client.api20100401_message.create_message(
            settings.TWILIO_ACCOUNT_SID,
            to,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        ),
        action="create_message(schedule)",
    )


# Waits (seconds) between cancel attempts. A scheduled message is briefly (~1-2s) not updatable
# right after it is created (Twilio answers 404), so a dispatch-then-immediately-cancel must retry;
# the follow-up is days away, so a few seconds of bounded retry cannot let it slip out. The SDK
# performs no retries, and cancelling is idempotent, so this bounded retry is ours to add.
_CANCEL_RETRY_WAITS = (1.0, 2.0, 2.0, 3.0)


def cancel_scheduled(sid: str, *, _sleep=time.sleep) -> SendResult:
    """Call off a message still in `scheduled` state (status=canceled), with bounded retry."""
    client = get_client()
    result = SendResult(None, "failed", "", None, "cancellation not attempted", False)
    for attempt in range(len(_CANCEL_RETRY_WAITS) + 1):
        result = _guard_write(
            lambda: client.api20100401_message.update_message(
                settings.TWILIO_ACCOUNT_SID, sid, status=MessageEnumUpdateStatus.CANCELED
            ),
            action="update_message(cancel)",
        )
        if result.sid is not None or result.outcome == "canceled":
            return result
        if attempt < len(_CANCEL_RETRY_WAITS):
            _sleep(_CANCEL_RETRY_WAITS[attempt])
    return result


def redact_message(sid: str) -> SendResult:
    """Dispose of a message's text at the provider (body="") while the record survives."""
    client = get_client()
    return _guard_write(
        lambda: client.api20100401_message.update_message(
            settings.TWILIO_ACCOUNT_SID, sid, body=""
        ),
        action="update_message(redact)",
    )


def fetch_status(sid: str):
    """Re-read a message's current provider state, or None if the provider has no such message."""
    client = get_client()
    try:
        msg = client.api20100401_message.fetch_message(settings.TWILIO_ACCOUNT_SID, sid)
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise ProviderError(502, "message fetch failed")
    except _NEVER_SENT:
        raise ProviderError(502, "message fetch not sent")
    except (httpx.RequestError, ValidationError):
        raise ProviderError(504, "message fetch: no usable response")
    return _result_from_message(msg)


def list_messages_from(from_number, date_after, date_before, *, max_pages=50, page_size=100):
    """Return (messages, truncated) the provider holds for `from_number` in a widened day window.

    `date_after` maps to DateSent> and `date_before` to DateSent< (day-granular at Twilio); the
    caller widens to day boundaries and narrows to instants afterwards. Paging is bounded by
    max_pages; `truncated` is True iff the cap (not the provider) stopped the walk.
    """
    client = get_client()
    collected = []
    truncated = False
    page_token = None
    page = None
    pages = 0
    try:
        while True:
            if pages >= max_pages:
                truncated = True
                break
            kwargs = {
                "from_": from_number,
                "page_size": page_size,
                "date_sent_query_query": date_after,   # DateSent>
                "date_sent_query": date_before,        # DateSent<
            }
            if page_token is not None:
                kwargs["page_token"] = page_token
            if page is not None:
                kwargs["page"] = page
            resp = client.api20100401_message.list_message(
                settings.TWILIO_ACCOUNT_SID, **kwargs
            )
            msgs = _val(resp.messages) or []
            collected.extend(msgs)
            pages += 1
            nxt = _val(resp.next_page_uri)
            if not nxt:
                break
            query = parse_qs(urlparse(nxt).query)
            next_token = query.get("PageToken", [None])[0]
            next_page = query.get("Page", [None])[0]
            if next_token is None and next_page is None:
                break
            page_token = next_token
            page = int(next_page) if next_page is not None else None
    except ApiError as e:
        log.warning("Twilio list_message failed: HTTP %s", e.status_code)
        raise ProviderError(502, "reconciliation query failed")
    except _NEVER_SENT:
        raise ProviderError(502, "reconciliation query not sent")
    except (httpx.RequestError, ValidationError):
        raise ProviderError(504, "reconciliation query: no usable response")
    return collected, truncated

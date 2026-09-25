"""
The one module that talks to the Twilio SDK.

Everything the rest of the app sees is a plain dataclass (``MessageSnapshot``,
``LookupResult``) or one of the ``ProviderError`` classes below - SDK models,
``UNSET`` and ``httpx`` exceptions never leave this module except where the
safe write needs to tell "never sent" from "may have landed"
(see ``safe_write.py``).

Phone numbers are never logged: the logging transport drops query strings and
masks digit runs in paths, and error logs carry exception class names only.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

logger = logging.getLogger("apps.sms_notifications.twilio")

# The Messages API lives on the SDK's ``default`` server. TWILIO_BASE_URL, when
# set, replaces it verbatim; Lookup (server ``default4``) is not governed by it.
DEFAULT_MESSAGING_BASE_URL = "https://api.twilio.com"

# Failures raised before the request left: nothing can have landed.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# How many list pages ``find_by_reference`` scans for a message it may have sent.
FIND_PAGES = 3
FIND_PAGE_SIZE = 100
RECONCILE_PAGE_SIZE = 1000


# --------------------------------------------------------------------------
# Errors the rest of the app handles
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """A provider call failed. ``status_code`` is what our API answers."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        provider_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class ProviderRejected(ProviderError):
    """The provider refused the request itself (a 4xx the caller can act on)."""


class ProviderUnavailable(ProviderError):
    """Our credentials/quota, a provider 5xx, a transport failure or an unreadable answer."""


class NotConfigured(ProviderUnavailable):
    """Twilio settings are missing; nothing was sent."""

    def __init__(self, message: str) -> None:
        super().__init__(503, message, outcome_unknown=False)


def provider_code(error: object) -> int | None:
    """Twilio's numeric error code from a RawError body, if it is readable JSON."""
    if not isinstance(error, RawError):
        return None
    try:
        body = error.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def translate(exc: Exception) -> ProviderError:
    """The one mapping from an SDK/transport failure to our error type."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        code = provider_code(exc.error)
        status = exc.status_code
        if status in (401, 403):
            return ProviderUnavailable(
                502, "The messaging provider refused our credentials.",
                outcome_unknown=False, provider_status=status, provider_code=code)
        if status == 429:
            return ProviderUnavailable(
                503, "The messaging provider is rate-limiting us.",
                outcome_unknown=False, provider_status=status, provider_code=code)
        if 400 <= status < 500:
            return ProviderRejected(
                status, "The messaging provider rejected the request.",
                outcome_unknown=False, provider_status=status, provider_code=code)
        return ProviderUnavailable(
            502, "The messaging provider is unavailable.",
            outcome_unknown=True, provider_status=status, provider_code=code)
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, "The messaging provider could not be reached.",
                                   outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(504, "No response from the messaging provider.",
                                   outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic ValidationError or a non-JSON body
        return ProviderUnavailable(502, "Unreadable answer from the messaging provider.",
                                   outcome_unknown=True)
    raise exc


# --------------------------------------------------------------------------
# Provider status -> our outcome (the only place a provider status is read)
# --------------------------------------------------------------------------

DONE, PENDING, FAILED, UNKNOWN = "done", "pending", "failed", "unknown"


def send_outcome(status: str | None) -> str:
    """Outcome of a message we asked the provider to send."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.SENDING
              | MessageEnumStatus.SENT | MessageEnumStatus.SCHEDULED):
            return PENDING
        case (MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.CANCELED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return FAILED
        case _:  # receiving/received (inbound), a value newer than the SDK, or absent
            return UNKNOWN


def cancel_outcome(status: str | None) -> str:
    """Outcome of asking the provider to call off a scheduled message."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case MessageEnumStatus.SCHEDULED:
            return PENDING
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.SENDING
              | MessageEnumStatus.SENT | MessageEnumStatus.DELIVERED | MessageEnumStatus.READ
              | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.FAILED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return FAILED  # it already left the schedule
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------
# Our own view of provider records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageSnapshot:
    sid: str | None
    status: str | None
    body: str | None
    to: str | None
    from_: str | None
    date_sent: datetime | None
    date_created: datetime | None
    error_code: int | None


@dataclass(frozen=True)
class LookupResult:
    phone_number: str
    country_code: str


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _provider_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parsedate_to_datetime(value)  # RFC 2822, e.g. "Fri, 25 Sep 2026 08:57:51 +0000"
    except (TypeError, ValueError):
        return None


def snapshot(message: ApiV2010AccountMessage) -> MessageSnapshot:
    status = message.status
    return MessageSnapshot(
        sid=_str(message.sid),
        status=str(status) if isinstance(status, str) else None,
        body=_str(message.body),
        to=_str(message.to),
        from_=_str(message.from_),
        date_sent=_provider_time(message.date_sent),
        date_created=_provider_time(message.date_created),
        error_code=message.error_code if isinstance(message.error_code, int) else None,
    )


# --------------------------------------------------------------------------
# Client: one per process, built lazily (after any fork), closed at exit
# --------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"(%2B|\+)?\d{6,}")


def _redacted(url: str) -> str:
    """Host and path only - the query can carry a destination number."""
    parts = urlsplit(url)
    return parts.netloc + _DIGIT_RUN.sub("***", parts.path)


class LoggingTransport:
    """Logs method, redacted path, status and latency - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("twilio %s %s failed: %s", request.method, _redacted(request.url),
                           type(exc).__name__)
            raise
        logger.info("twilio %s %s -> %s (%.0f ms)", request.method, _redacted(request.url),
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


_lock = threading.Lock()
_client: TwilioSdkClient | None = None
_client_pid: int | None = None


def _build_client() -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # The SDK would otherwise send every request unauthenticated.
        raise NotConfigured("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.")
    timeout = float(settings.TWILIO_TIMEOUT_SECONDS)
    return TwilioSdkClient(
        server_config={"default": {"base_url": settings.TWILIO_BASE_URL or DEFAULT_MESSAGING_BASE_URL}},
        timeout=timeout,
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )


def get_client() -> TwilioSdkClient:
    global _client, _client_pid
    pid = os.getpid()
    if _client is None or _client_pid != pid:
        with _lock:
            if _client is None or _client_pid != pid:
                _client = _build_client()
                _client_pid = pid
    return _client


def use_client(client: TwilioSdkClient | None) -> None:
    """Replace the process client (tests inject one built on a stub transport)."""
    global _client, _client_pid
    with _lock:
        _client = client
        _client_pid = os.getpid() if client is not None else None


@atexit.register
def close_client() -> None:
    global _client
    with _lock:
        if _client is not None and _client_pid == os.getpid():
            _client.close()
        _client = None


def _account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


def _from_number() -> str:
    number = settings.TWILIO_FROM_NUMBER
    if not number:
        raise NotConfigured("TWILIO_FROM_NUMBER must be set.")
    return str(number)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def lookup_number(number: str, country_code: str | None = None) -> LookupResult | None:
    """The provider's canonical form of ``number``; None when it is not a usable number.

    Raises ProviderError for anything else. (Lookup v2's response does not decode
    with this SDK version, so v1 is used - see twilio-sdk-plan.md.)
    """
    client = get_client()
    try:
        if country_code:
            result = client.lookups_v1_phone_number_api.fetch_phone_number2(number, country_code=country_code)
        else:
            result = client.lookups_v1_phone_number_api.fetch_phone_number2(number)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise translate(exc) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc
    canonical = _str(result.phone_number)
    if not canonical:
        raise ProviderUnavailable(502, "Lookup returned no phone number.", outcome_unknown=False)
    return LookupResult(phone_number=canonical, country_code=_str(result.country_code) or "")


def create_message(to: str, body: str, send_at: datetime | None = None) -> MessageSnapshot:
    """Send (or schedule) one SMS. Raises the SDK's own exceptions - only the safe write calls this."""
    client = get_client()
    messages = client.api20100401_message
    if send_at is None:
        message = messages.create_message(_account_sid(), to, from_=_from_number(), body=body)
    else:
        service_sid = settings.TWILIO_MESSAGING_SERVICE_SID
        if not service_sid:
            raise NotConfigured("TWILIO_MESSAGING_SERVICE_SID must be set to schedule messages.")
        message = messages.create_message(
            _account_sid(), to,
            from_=_from_number(),
            messaging_service_sid=str(service_sid),
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        )
    return snapshot(message)


def find_by_reference(to: str, ref_token: str) -> MessageSnapshot | None:
    """The message we sent to ``to`` whose body carries ``ref_token``, or None.

    Raises the SDK's own exceptions; the caller treats any failure as "still unknown".
    """
    client = get_client()
    page_token: str | None = None
    page: int | None = None
    for _ in range(FIND_PAGES):
        if page_token is None:
            result = client.api20100401_message.list_message(
                _account_sid(), to=to, from_=_from_number(), page_size=FIND_PAGE_SIZE)
        else:
            result = client.api20100401_message.list_message(
                _account_sid(), to=to, from_=_from_number(), page_size=FIND_PAGE_SIZE,
                page=page, page_token=page_token)
        if not isinstance(result.messages, list):
            raise ProviderUnavailable(502, "Message list had no messages member.", outcome_unknown=True)
        for message in result.messages:
            found = snapshot(message)
            if found.body and ref_token in found.body:
                return found
        next_page = _next_page(result.next_page_uri)
        if next_page is None:
            return None
        page, page_token = next_page
    return None


def fetch_message(sid: str) -> MessageSnapshot:
    try:
        return snapshot(get_client().api20100401_message.fetch_message(_account_sid(), sid))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc


def cancel_message(sid: str) -> MessageSnapshot:
    """Call off a scheduled message. Harmless to repeat."""
    try:
        return snapshot(get_client().api20100401_message.update_message(
            _account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc


def redact_message(sid: str) -> MessageSnapshot:
    """Erase the message text at the provider (body set to ""). Harmless to repeat."""
    try:
        return snapshot(get_client().api20100401_message.update_message(_account_sid(), sid, body=""))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc


def _next_page(next_page_uri: object) -> tuple[int | None, str] | None:
    if not isinstance(next_page_uri, str) or not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    tokens = query.get("PageToken")
    if not tokens:
        raise ProviderUnavailable(502, "Next page link carried no page token.", outcome_unknown=False)
    pages = query.get("Page")
    page = int(pages[0]) if pages and pages[0].isdigit() else None
    return page, tokens[0]


def list_sent_from_our_number(sent_after: datetime, sent_before: datetime) -> Iterator[MessageSnapshot]:
    """Every message the provider holds FROM our sending number with DateSent in the
    (inclusive, possibly day-granular) range - all pages. Raises ProviderError."""
    client = get_client()
    page_token: str | None = None
    page: int | None = None
    try:
        while True:
            if page_token is None:
                result = client.api20100401_message.list_message(
                    _account_sid(), from_=_from_number(),
                    date_sent_query_query=sent_after, date_sent_query=sent_before,
                    page_size=RECONCILE_PAGE_SIZE)
            else:
                result = client.api20100401_message.list_message(
                    _account_sid(), from_=_from_number(),
                    date_sent_query_query=sent_after, date_sent_query=sent_before,
                    page_size=RECONCILE_PAGE_SIZE, page=page, page_token=page_token)
            if not isinstance(result.messages, list):
                raise ProviderUnavailable(502, "Message list had no messages member.", outcome_unknown=False)
            for message in result.messages:
                yield snapshot(message)
            next_page = _next_page(result.next_page_uri)
            if next_page is None:
                return
            page, page_token = next_page
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc

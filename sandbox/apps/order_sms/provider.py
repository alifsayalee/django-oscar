"""
Everything this app says to Twilio goes through this module.

It owns the one long-lived SDK client, the translation of provider failures
into this app's own error types, and the mapping of a provider message status
onto this app's outcomes. Nothing here touches the database; the claim and
bookkeeping around writes live in ``services``.
"""

import atexit
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumDirection,
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


# Outcomes ------------------------------------------------------------------

DONE = "done"
PENDING = "pending"
FAILED = "failed"
UNKNOWN = "unknown"


def status_from_provider(status: object) -> str:
    """
    The one place a provider message status becomes a delivery outcome.

    Only a delivered (or read) message is done. A value this SDK does not
    list, or no value at all, is unknown - never done, never failed.
    """
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (
            MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            # The message was called off: it will never reach the shopper.
            return FAILED
        case _:
            # receiving/received are inbound-only states; anything newer
            # than this SDK arrives as a plain str.
            return UNKNOWN


def cancel_outcome(status: object) -> str:
    """Outcome of *calling off* a scheduled message, read from its status."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED:
            return PENDING
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.DELIVERED
            | MessageEnumStatus.READ
            | MessageEnumStatus.PARTIALLY_DELIVERED
            | MessageEnumStatus.FAILED
            | MessageEnumStatus.UNDELIVERED
        ):
            # Already on its way (or past it): too late to call off.
            return FAILED
        case _:
            return UNKNOWN


# Errors --------------------------------------------------------------------


class ProviderError(Exception):
    """
    A provider call this app could not complete.

    ``status_code`` is what the API boundary answers with; ``outcome_unknown``
    says whether the provider may nevertheless have acted.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool,
                 provider_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_code = provider_code


class ProviderConfigError(ProviderError):
    """Our credentials, account or configuration: not the caller's fault."""


class ProviderRejected(ProviderError):
    """The provider refused the request itself (a definitive 4xx)."""


class ProviderUnavailable(ProviderError):
    """Transport failure, provider 5xx or throttling."""


class ProviderUnreadable(ProviderError):
    """A response arrived that could not be read."""


class NumberRejected(Exception):
    """The provider does not consider the number a usable destination."""


def provider_error_code(error: object) -> int | None:
    """Twilio's own error code from a RawError body, when it has one."""
    if not isinstance(error, RawError):
        return None
    try:
        payload = error.json()
    except ValueError:
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, int) else None


def translate(exc: BaseException) -> ProviderError:
    """Map any failure out of an SDK call onto this app's error types."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        code = provider_error_code(exc.error)
        if exc.status_code in (401, 403):
            return ProviderConfigError(
                502, "The messaging provider refused our credentials.",
                outcome_unknown=False, provider_code=code)
        if exc.status_code == 429:
            return ProviderUnavailable(
                503, "The messaging provider is throttling requests.",
                outcome_unknown=False, provider_code=code)
        if 400 <= exc.status_code < 500:
            return ProviderRejected(
                exc.status_code, "The messaging provider rejected the request.",
                outcome_unknown=False, provider_code=code)
        return ProviderUnavailable(
            502, "The messaging provider failed.", outcome_unknown=True, provider_code=code)
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(
            502, "The messaging provider could not be reached.", outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(
            504, "The messaging provider did not answer.", outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic.ValidationError, or a non-JSON body
        return ProviderUnreadable(
            502, "The messaging provider's answer could not be read.", outcome_unknown=True)
    raise exc


# Client --------------------------------------------------------------------

_PHONE_PATH = re.compile(r"(/PhoneNumbers/)[^/?#]+")


def redact_url(url: str) -> str:
    """Host and path only (queries carry phone numbers), numbers masked."""
    parts = urlsplit(url)
    return parts.netloc + _PHONE_PATH.sub(r"\1***", parts.path)


class RedactingLogTransport:
    """
    Wraps the SDK's transport to log each call: method, host, path, status
    and duration. Never headers (credentials), bodies or query strings
    (phone numbers).
    """

    def __init__(self, inner: HttpxClient):
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "twilio %s %s -> %s after %.0f ms", request.method,
                redact_url(request.url), type(exc).__name__,
                (time.monotonic() - started) * 1000)
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)", request.method, redact_url(request.url),
            response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_pid: int | None = None
_client_lock = threading.Lock()


def build_client() -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # The SDK would otherwise build a client that sends every request
        # unauthenticated.
        raise ProviderConfigError(
            503, "SMS is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).",
            outcome_unknown=False)
    transport = RedactingLogTransport(HttpxClient(timeout=settings.ORDER_SMS_TIMEOUT_SECONDS))
    credentials = BasicAuthCredentials(username=account_sid, password=auth_token)
    if settings.TWILIO_BASE_URL:
        # Governs the messaging API (server "default") only; Lookup keeps
        # its own host.
        return TwilioSdkClient(
            server_config={"default": {"base_url": settings.TWILIO_BASE_URL}},
            custom_http_client=transport,
            account_sid_auth_token=credentials,
        )
    return TwilioSdkClient(custom_http_client=transport, account_sid_auth_token=credentials)


def get_client() -> TwilioSdkClient:
    """
    The process-wide client, built on first use - so after a worker fork -
    and rebuilt if this process is a fork of the one that built it.
    """
    global _client, _client_pid
    pid = os.getpid()
    if _client is None or _client_pid != pid:
        with _client_lock:
            if _client is None or _client_pid != pid:
                _client = build_client()
                _client_pid = pid
                atexit.register(_client.close)
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Install a specific client (tests), or drop it so the next use rebuilds."""
    global _client, _client_pid
    with _client_lock:
        _client = client
        _client_pid = os.getpid() if client is not None else None


def account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


def from_number() -> str:
    number = settings.TWILIO_FROM_NUMBER
    if not number:
        raise ProviderConfigError(
            503, "SMS is not configured (TWILIO_FROM_NUMBER).", outcome_unknown=False)
    return str(number)


# Retries -------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return exc.status_code in TRANSIENT_STATUSES
    return isinstance(exc, httpx.RequestError)


def _retry_delay(exc: BaseException, attempt: int) -> float:
    if isinstance(exc, ApiError):
        retry_after = exc.response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 5.0)
    return 0.5 * float(2 ** attempt)


def repeatable(call: Callable[[], T], attempts: int = 3) -> T:
    """
    Run a call that is safe to repeat (a read, or an update whose repeat
    cannot create anything), retrying transient failures. Never used for a
    create: the SDK sends each create exactly once.
    """
    for attempt in range(attempts):
        try:
            return call()
        except (ApiError, httpx.RequestError) as exc:
            if attempt == attempts - 1 or not _is_transient(exc):
                raise
            time.sleep(_retry_delay(exc, attempt))
    raise AssertionError("unreachable")


# Reading provider records ---------------------------------------------------


@dataclass(frozen=True)
class MessageState:
    """What the provider says about one message, with UNSET resolved."""

    sid: str
    status: str
    body: str | None
    to: str | None
    from_: str | None
    inbound: bool
    error_code: int | None
    error_message: str | None
    date_sent: datetime | None
    date_created: datetime | None

    @property
    def provider_time(self) -> datetime | None:
        """The provider's clock for this message: when sent, else when created."""
        return self.date_sent or self.date_created

    @property
    def outcome(self) -> str:
        return status_from_provider(self.status)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _rfc2822(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_message(message: ApiV2010AccountMessage) -> MessageState:
    """
    Read a message resource. A response without a sid names nothing the
    provider did, so it is unreadable - the caller treats it as unknown.
    """
    sid = message.sid
    if not isinstance(sid, str) or not sid:
        raise ProviderUnreadable(
            502, "The messaging provider's answer carried no message id.", outcome_unknown=True)
    status = message.status
    error_code = message.error_code
    return MessageState(
        sid=sid,
        status=str(status) if isinstance(status, str) else "",
        body=_str_or_none(message.body),
        to=_str_or_none(message.to),
        from_=_str_or_none(message.from_),
        inbound=message.direction == MessageEnumDirection.INBOUND,
        error_code=error_code if isinstance(error_code, int) else None,
        error_message=_str_or_none(message.error_message),
        date_sent=_rfc2822(message.date_sent),
        date_created=_rfc2822(message.date_created),
    )


# Operations ----------------------------------------------------------------


def lookup_number(raw: str) -> tuple[str, str]:
    """
    Ask the provider whether ``raw`` is a usable phone number and return its
    canonical E.164 form and country. Raises ``NumberRejected`` when it is not.

    Lookup v1 is used because Lookup v2's model cannot decode the provider's
    live responses (see twilio-sdk-plan.md).
    """
    client = get_client()
    try:
        result = repeatable(
            lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(raw))
    except ApiError as exc:
        if exc.status_code in (400, 404):
            raise NumberRejected() from exc
        raise translate(exc) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc
    e164 = result.phone_number
    country = result.country_code
    if not isinstance(e164, str) or not e164.startswith("+"):
        raise ProviderUnreadable(
            502, "The number lookup returned no canonical number.", outcome_unknown=False)
    return e164, country if isinstance(country, str) else ""


def reference_token(reference: str) -> str:
    """The short form of a reference carried in the message text."""
    return hashlib.sha256(reference.encode()).hexdigest()[:10].upper()


def create_message(to: str, body: str, *, send_at: datetime | None = None) -> MessageState:
    """
    Send (or, with ``send_at``, schedule) one message. Called only from the
    safe write in ``services``, which holds the claim; it raises the SDK's
    own exceptions so that the caller can tell never-sent from may-have-landed.
    """
    client = get_client()
    if send_at is None:
        message = client.api20100401_message.create_message(
            account_sid(), to, from_=from_number(), body=body)
    else:
        service_sid = settings.TWILIO_MESSAGING_SERVICE_SID
        if not service_sid:
            raise ProviderConfigError(
                503, "SMS scheduling is not configured (TWILIO_MESSAGING_SERVICE_SID).",
                outcome_unknown=False)
        message = client.api20100401_message.create_message(
            account_sid(), to,
            from_=from_number(),
            messaging_service_sid=service_sid,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        )
    return read_message(message)


def find_by_reference(to: str, token: str, since: datetime) -> MessageState | None:
    """
    Look up the message carrying ``Ref <token>`` that this app sent to ``to``,
    scanning the provider's list newest first back to ``since``. None when the
    provider has no such message (yet).
    """
    client = get_client()
    marker = "Ref %s" % token
    page_kwargs: dict[str, Any] = {}
    for _ in range(20):
        kwargs = page_kwargs
        page = repeatable(lambda: client.api20100401_message.list_message(
            account_sid(), to=to, from_=from_number(), page_size=100, **kwargs))
        messages = page.messages if isinstance(page.messages, list) else []
        oldest: datetime | None = None
        for message in messages:
            body = message.body
            if isinstance(body, str) and marker in body:
                return read_message(message)
            created = _rfc2822(message.date_created)
            if created is not None and (oldest is None or created < oldest):
                oldest = created
        next_page = _next_page_kwargs(page.next_page_uri)
        if not messages or next_page is None or (oldest is not None and oldest < since):
            return None
        page_kwargs = next_page
    return None


def fetch_message(sid: str) -> MessageState | None:
    """The provider's current record of a message; None when it has none."""
    client = get_client()
    try:
        message = repeatable(lambda: client.api20100401_message.fetch_message(account_sid(), sid))
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    return read_message(message)


def cancel_message(sid: str) -> MessageState:
    """Call off a scheduled message (repeatable: cancelling twice is harmless)."""
    client = get_client()
    message = repeatable(lambda: client.api20100401_message.update_message(
        account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED))
    return read_message(message)


def redact_message(sid: str) -> MessageState:
    """Erase a message's text at the provider (repeatable)."""
    client = get_client()
    message = repeatable(lambda: client.api20100401_message.update_message(
        account_sid(), sid, body=""))
    return read_message(message)


def _next_page_kwargs(next_page_uri: object) -> dict[str, Any] | None:
    if not isinstance(next_page_uri, str) or not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    kwargs: dict[str, Any] = {}
    if query.get("PageToken"):
        kwargs["page_token"] = query["PageToken"][0]
    if query.get("Page") and query["Page"][0].isdigit():
        kwargs["page"] = int(query["Page"][0])
    return kwargs or None


def list_sent_from_our_number(start: datetime, end: datetime) -> Iterator[MessageState]:
    """
    Every message the provider holds from ``TWILIO_FROM_NUMBER`` with a sent
    date in [start, end). The provider's date filter works in whole UTC days,
    so the request is widened to whole days here and narrowed back by the
    caller on each record's own time.
    """
    client = get_client()
    after = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    before = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    page_kwargs: dict[str, Any] = {}
    for _ in range(500):
        kwargs = page_kwargs
        page = repeatable(lambda: client.api20100401_message.list_message(
            account_sid(),
            from_=from_number(),
            date_sent_query_query=after,   # wire DateSent>
            date_sent_query=before,        # wire DateSent<
            page_size=1000,
            **kwargs,
        ))
        messages = page.messages if isinstance(page.messages, list) else []
        for message in messages:
            yield read_message(message)
        next_page = _next_page_kwargs(page.next_page_uri)
        if not messages or next_page is None:
            return
        page_kwargs = next_page
    raise ProviderUnavailable(
        502, "Too many provider records to reconcile in one report; narrow the range.",
        outcome_unknown=False)

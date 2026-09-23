"""
The only module that talks to Twilio.

Everything the rest of the app needs from the provider goes through
``TwilioGateway``: number lookup, sending, scheduling, cancelling, redacting,
fetching and listing messages. Provider failures leave this module as one of
the ``ProviderError`` classes below - never as an SDK or httpx exception - and
provider messages leave it as ``ProviderMessage`` (no ``UNSET`` escapes).
"""
from __future__ import annotations

import atexit
import datetime as dt
import email.utils
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    UNSET,
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    UnsetType,
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

# Outcomes we derive from a provider message status (see ``outcome_for``).
DELIVERED, PENDING, FAILED, CANCELED, UNKNOWN = "delivered", "pending", "failed", "canceled", "unknown"

LIST_PAGE_SIZE = 200
LIST_MAX_PAGES = 25
FIND_BY_REF_PAGE_SIZE = 50
FIND_BY_REF_MAX_PAGES = 2
CANCEL_RETRY_DELAYS = (1.0, 2.0, 4.0)  # a just-scheduled message answers 404 for a moment
READ_RETRY_DELAY = 0.5

# Twilio error codes observed on the wire.
TWILIO_NOT_FOUND = 20404
TWILIO_NOT_CANCELABLE = 30409


# --------------------------------------------------------------------------
# Failure types
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """
    Base of every provider failure. ``status_code`` is what our own API answers
    when the failure reaches it; ``outcome_unknown`` says whether anything may
    have happened at the provider.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        outcome_unknown: bool,
        provider_status: int | None = None,
        twilio_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.twilio_code = twilio_code


class ProviderConfigError(ProviderError):
    """Our configuration or credentials: nothing the caller can fix."""

    def __init__(self, message: str, *, provider_status: int | None = None, twilio_code: int | None = None):
        super().__init__(
            message,
            status_code=502,
            outcome_unknown=False,
            provider_status=provider_status,
            twilio_code=twilio_code,
        )


class ProviderRejected(ProviderError):
    """The provider answered 400/404/409/422: the request did not take effect."""

    def __init__(self, message: str, *, provider_status: int, twilio_code: int | None):
        super().__init__(
            message,
            status_code=provider_status,
            outcome_unknown=False,
            provider_status=provider_status,
            twilio_code=twilio_code,
        )


class ProviderUnavailable(ProviderError):
    """Rate limited, down, or unreachable."""


class ProviderFailure(ProviderUnavailable):
    """A 5xx or an unmapped status."""


class ProviderUnreadable(ProviderError):
    """A response we could not read; for a write the outcome is unknown."""

    def __init__(self, message: str):
        super().__init__(message, status_code=502, outcome_unknown=True)


_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def _twilio_code(error: Any) -> int | None:
    if not isinstance(error, RawError):
        return None
    try:
        body = error.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def call_provider(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into a ProviderError."""
    try:
        return fn()
    except ApiError as e:
        code = _twilio_code(e.error)
        # Log status and Twilio's numeric code only: error texts embed the
        # account SID and, for lookups, the phone number.
        logger.warning("twilio %s failed: HTTP %s code=%s", operation, e.status_code, code)
        if e.status_code in (401, 403):
            raise ProviderConfigError(
                "The messaging provider refused our credentials.",
                provider_status=e.status_code,
                twilio_code=code,
            ) from e
        if e.status_code == 429:
            raise ProviderUnavailable(
                "The messaging provider is rate limiting us.",
                status_code=503,
                outcome_unknown=False,
                provider_status=429,
                twilio_code=code,
            ) from e
        if e.status_code in (400, 404, 409, 422):
            raise ProviderRejected(
                "The messaging provider rejected the request.",
                provider_status=e.status_code,
                twilio_code=code,
            ) from e
        raise ProviderFailure(
            "The messaging provider failed.",
            status_code=502,
            outcome_unknown=e.status_code >= 500,
            provider_status=e.status_code,
            twilio_code=code,
        ) from e
    except ValidationError as e:  # before ValueError: it is a subclass
        logger.error("twilio %s returned an unreadable response", operation)
        raise ProviderUnreadable("Unreadable response from the messaging provider.") from e
    except ValueError as e:
        logger.error("twilio %s returned a non-JSON response", operation)
        raise ProviderUnreadable("Unreadable response from the messaging provider.") from e
    except _NEVER_SENT as e:
        logger.warning("twilio %s not sent: %s", operation, type(e).__name__)
        raise ProviderUnavailable(
            "The messaging provider could not be reached.", status_code=502, outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        logger.warning("twilio %s got no reply: %s", operation, type(e).__name__)
        raise ProviderUnavailable(
            "No reply from the messaging provider.", status_code=504, outcome_unknown=True
        ) from e


def _is_transient_read_failure(e: ProviderError) -> bool:
    # Reads are safe to repeat; only repeat what can succeed on a second try.
    if isinstance(e, ProviderUnavailable):
        return e.provider_status in (None, 429) or (e.provider_status or 0) >= 500
    return False


# --------------------------------------------------------------------------
# Provider data, free of SDK types
# --------------------------------------------------------------------------


def outcome_for(status: object) -> str:
    """The one place a provider message status becomes one of ours."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DELIVERED
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.SCHEDULED
        ):
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.PARTIALLY_DELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            return CANCELED
        case _:  # RECEIVING, RECEIVED, and any value newer than this SDK
            return UNKNOWN


def _opt(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def parse_provider_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


@dataclass(frozen=True)
class ProviderMessage:
    sid: str
    status: str
    outcome: str
    to: str | None
    from_number: str | None
    body: str | None
    direction: str | None
    error_code: int | None
    date_created: dt.datetime | None
    date_sent: dt.datetime | None

    @classmethod
    def from_sdk(cls, message: ApiV2010AccountMessage) -> ProviderMessage:
        sid = _opt(message.sid)
        if not isinstance(sid, str) or not sid:
            # Accepted or not, we cannot name what was created.
            raise ProviderUnreadable("The messaging provider returned a message without an id.")
        status = _opt(message.status)
        direction = _opt(message.direction)
        return cls(
            sid=sid,
            status=str(status) if status is not None else "",
            outcome=outcome_for(status),
            to=_opt(message.to),
            from_number=_opt(message.from_),
            body=_opt(message.body),
            direction=str(direction) if direction is not None else None,
            error_code=_opt(message.error_code),
            date_created=parse_provider_time(_opt(message.date_created)),
            date_sent=parse_provider_time(_opt(message.date_sent)),
        )


@dataclass(frozen=True)
class LookupResult:
    phone_number: str
    country_code: str


@dataclass(frozen=True)
class SentMessages:
    messages: list[ProviderMessage]
    truncated: bool
    pages: int


# --------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------


class TwilioGateway:
    def __init__(
        self,
        client: TwilioSdkClient,
        *,
        account_sid: str,
        from_number: str,
        messaging_service_sid: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._account_sid = account_sid
        self._from_number = from_number
        self._messaging_service_sid = messaging_service_sid
        self._sleep = sleep

    @property
    def from_number(self) -> str:
        return self._from_number

    def close(self) -> None:
        self._client.close()

    def _read(self, operation: str, fn: Callable[[], T]) -> T:
        try:
            return call_provider(operation, fn)
        except ProviderError as e:
            if not _is_transient_read_failure(e):
                raise
            self._sleep(READ_RETRY_DELAY)
            return call_provider(operation, fn)

    # -- numbers ------------------------------------------------------------

    def lookup(self, number: str, country_code: str | None = None) -> LookupResult | None:
        """
        The provider's canonical form of ``number``, or None when the provider
        does not recognise it as a usable phone number.
        """
        api = self._client.lookups_v1_phone_number_api
        try:
            if country_code:
                result = self._read(
                    "lookup", lambda: api.fetch_phone_number2(number, country_code=country_code)
                )
            else:
                result = self._read("lookup", lambda: api.fetch_phone_number2(number))
        except ProviderRejected as e:
            if e.provider_status == 404:  # the provider's own "not a number"
                return None
            raise
        canonical = _opt(result.phone_number)
        if not isinstance(canonical, str) or not canonical:
            raise ProviderUnreadable("The number lookup returned no phone number.")
        country = _opt(result.country_code)
        return LookupResult(phone_number=canonical, country_code=country if isinstance(country, str) else "")

    # -- messages -----------------------------------------------------------

    def send(self, to: str, body: str) -> ProviderMessage:
        """Send now. Never retried: create_message has no idempotency key."""
        api = self._client.api20100401_message
        message = call_provider(
            "send",
            lambda: api.create_message(self._account_sid, to, from_=self._from_number, body=body),
        )
        return ProviderMessage.from_sdk(message)

    def schedule(self, to: str, body: str, send_at: dt.datetime) -> ProviderMessage:
        """Queue a message with the provider to be sent at ``send_at``."""
        if not self._messaging_service_sid:
            raise ProviderConfigError("TWILIO_MESSAGING_SERVICE_SID is required to schedule messages.")
        api = self._client.api20100401_message
        message = call_provider(
            "schedule",
            lambda: api.create_message(
                self._account_sid,
                to,
                from_=self._from_number,
                messaging_service_sid=self._messaging_service_sid,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=send_at,
                body=body,
            ),
        )
        return ProviderMessage.from_sdk(message)

    def fetch(self, sid: str) -> ProviderMessage | None:
        api = self._client.api20100401_message
        try:
            message = self._read("fetch", lambda: api.fetch_message(self._account_sid, sid))
        except ProviderRejected as e:
            if e.provider_status == 404:
                return None
            raise
        return ProviderMessage.from_sdk(message)

    def cancel(self, sid: str) -> ProviderMessage:
        """
        Cancel a scheduled message. Returns the message as the provider now
        holds it - check ``outcome``: it is CANCELED unless it had already gone
        out (or failed) before the cancel reached it.
        """
        api = self._client.api20100401_message
        for attempt in range(len(CANCEL_RETRY_DELAYS) + 1):
            try:
                message = call_provider(
                    "cancel",
                    lambda: api.update_message(
                        self._account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
                    ),
                )
                return ProviderMessage.from_sdk(message)
            except ProviderRejected as e:
                if e.provider_status == 409:
                    # "Not in a cancelable state": already canceled, or already sent.
                    current = self.fetch(sid)
                    if current is None:
                        raise
                    return current
                if e.provider_status == 404 and attempt < len(CANCEL_RETRY_DELAYS):
                    # Observed: a message scheduled moments ago is briefly not found.
                    self._sleep(CANCEL_RETRY_DELAYS[attempt])
                    continue
                raise
            except ProviderUnavailable as e:
                # Cancelling is naturally idempotent, so a transient failure may be
                # repeated; an unknown outcome is settled by the 409 path above.
                if attempt < len(CANCEL_RETRY_DELAYS) and _is_transient_read_failure(e):
                    self._sleep(CANCEL_RETRY_DELAYS[attempt])
                    continue
                raise
        raise AssertionError("unreachable")

    def redact(self, sid: str) -> ProviderMessage:
        """Erase the message text at the provider and confirm it is gone."""
        api = self._client.api20100401_message
        message = ProviderMessage.from_sdk(
            call_provider("redact", lambda: api.update_message(self._account_sid, sid, body=""))
        )
        if message.body:
            raise ProviderUnreadable("The provider did not confirm the message text was erased.")
        return message

    def list_sent(self, start: dt.datetime, end: dt.datetime) -> SentMessages:
        """
        Every outbound message the provider holds from our sending number with
        a send time in [start, end). The provider filters by whole GMT days, so
        the query is widened to days and narrowed back here.
        """
        api = self._client.api20100401_message
        start_utc = start.astimezone(dt.timezone.utc)
        end_utc = end.astimezone(dt.timezone.utc)
        on_or_after = dt.datetime.combine(start_utc.date(), dt.time(), tzinfo=dt.timezone.utc)
        on_or_before = dt.datetime.combine(
            end_utc.date() + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc
        )
        found: list[ProviderMessage] = []
        page: int | None = None
        page_token: str | None = None
        truncated = False
        pages = 0
        for _ in range(LIST_MAX_PAGES):
            current_page, current_token = page, page_token
            result = self._read(
                "list",
                lambda: api.list_message(
                    self._account_sid,
                    from_=self._from_number,
                    date_sent_query_query=on_or_after,  # wire "DateSent>"
                    date_sent_query=on_or_before,  # wire "DateSent<"
                    page_size=LIST_PAGE_SIZE,
                    page=current_page,
                    page_token=current_token,
                ),
            )
            pages += 1
            for item in _opt(result.messages) or []:
                message = ProviderMessage.from_sdk(item)
                if message.direction == MessageEnumDirection.INBOUND:
                    continue  # a copy received by one of our numbers, not a send
                if message.date_sent is None or not (start_utc <= message.date_sent < end_utc):
                    continue
                found.append(message)
            next_page, next_token = _next_page(_opt(result.next_page_uri))
            if next_token is None:
                break
            if next_token == page_token:
                truncated = True  # the cursor did not advance: stop, and say so
                logger.warning("twilio list: page token did not advance; report truncated")
                break
            page, page_token = next_page, next_token
        else:
            truncated = True
            logger.warning("twilio list: stopped at %s pages; report truncated", LIST_MAX_PAGES)
        return SentMessages(messages=found, truncated=truncated, pages=pages)

    def find_by_ref(self, to: str, ref: str) -> ProviderMessage | None:
        """
        Find a message we may have created without hearing back, by the
        reference embedded in its body. Recent messages to ``to`` only.
        """
        api = self._client.api20100401_message
        page: int | None = None
        page_token: str | None = None
        for _ in range(FIND_BY_REF_MAX_PAGES):
            current_page, current_token = page, page_token
            result = self._read(
                "find_by_ref",
                lambda: api.list_message(
                    self._account_sid,
                    to=to,
                    from_=self._from_number,
                    page_size=FIND_BY_REF_PAGE_SIZE,
                    page=current_page,
                    page_token=current_token,
                ),
            )
            for item in _opt(result.messages) or []:
                body = _opt(item.body)
                if isinstance(body, str) and ref in body:
                    return ProviderMessage.from_sdk(item)
            page, page_token = _next_page(_opt(result.next_page_uri))
            if page_token is None or page_token == current_token:
                break
        return None


def _next_page(next_page_uri: str | None) -> tuple[int | None, str | None]:
    if not next_page_uri:
        return None, None
    query = parse_qs(urlsplit(next_page_uri).query)
    token = query.get("PageToken", [None])[0]
    raw_page = query.get("Page", [None])[0]
    page = int(raw_page) if raw_page is not None and raw_page.isdigit() else None
    return page, token


# --------------------------------------------------------------------------
# Client construction and lifetime
# --------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"\d{4,}")
_ACCOUNT = re.compile(r"/Accounts/AC[0-9a-fA-F]+")


def _mask_digits(text: str) -> str:
    text = _ACCOUNT.sub("/Accounts/AC...", text)
    return _DIGIT_RUN.sub(lambda m: "*" * (len(m.group()) - 2) + m.group()[-2:], text)


class RedactingLoggingTransport:
    """
    Logs method, host, path (phone numbers masked) and status for every call.
    Never the query string, headers or bodies: they carry the credential and
    shoppers' numbers.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        response = self._inner.send(request)
        parts = urlsplit(request.url)
        logger.info(
            "twilio %s %s%s -> %s (%.0f ms)",
            request.method,
            parts.netloc,
            _mask_digits(parts.path),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


REQUIRED_SETTINGS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")


def build_gateway() -> TwilioGateway:
    missing = [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]
    if missing:
        raise ProviderConfigError("Messaging is not configured; missing settings: " + ", ".join(missing))
    base_url = getattr(settings, "TWILIO_BASE_URL", "")
    transport = RedactingLoggingTransport(HttpxClient(timeout=float(settings.TWILIO_TIMEOUT_SECONDS)))
    client = TwilioSdkClient(
        # Only the messaging API's server ("default") is overridable; Lookups
        # keeps its own host.
        server_config={"default": {"base_url": base_url}} if base_url else None,
        custom_http_client=transport,
        account_sid_auth_token=BasicAuthCredentials(
            username=settings.TWILIO_ACCOUNT_SID, password=settings.TWILIO_AUTH_TOKEN
        ),
    )
    return TwilioGateway(
        client,
        account_sid=settings.TWILIO_ACCOUNT_SID,
        from_number=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=getattr(settings, "TWILIO_MESSAGING_SERVICE_SID", ""),
    )


_gateway: TwilioGateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> TwilioGateway:
    """The process-wide gateway, built on first use (after any worker fork)."""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = build_gateway()
                atexit.register(_gateway.close)
    return _gateway


def set_gateway(gateway: TwilioGateway | None) -> None:
    """Replace the process-wide gateway (tests, credential rotation)."""
    global _gateway
    with _gateway_lock:
        previous, _gateway = _gateway, gateway
    if previous is not None and previous is not gateway:
        previous.close()


__all__ = [
    "UNSET",
    "LookupResult",
    "ProviderConfigError",
    "ProviderError",
    "ProviderFailure",
    "ProviderMessage",
    "ProviderRejected",
    "ProviderUnavailable",
    "ProviderUnreadable",
    "SentMessages",
    "TwilioGateway",
    "get_gateway",
    "outcome_for",
    "set_gateway",
]

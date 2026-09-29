"""Twilio gateway: the only module that talks to the Twilio SDK.

The client is synchronous (Django runs under WSGI), built lazily on first use
inside the worker process (so never inherited across a fork), reused for the
life of the process and closed at exit.

Nothing here logs a phone number, a message body or a credential.
"""

import atexit
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar
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
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus
from twilio_sdk.server import ServerConfigDict

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Long runs of digits (optionally '+'-prefixed or URL-encoded) are phone numbers
# as far as logging is concerned.
_PHONE_LIKE = re.compile(r"(?:%2B|\+)?\d{5,}")


_ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")


def mask(text: str) -> str:
    """Provider text with phone numbers and the account id removed."""
    return _PHONE_LIKE.sub("<redacted>", _ACCOUNT_SID.sub("AC<redacted>", text))


# Path segments that are safe to log: API versions, resource names, Twilio SIDs.
_SAFE_SEGMENT = re.compile(r"(?:\d{4}-\d{2}-\d{2}|v\d+|[A-Za-z]+(?:\.json)?|[A-Z]{2}[0-9a-fA-F]{32}(?:\.json)?)")


def safe_path(path: str) -> str:
    """A URL path with every segment that could carry a phone number redacted."""
    return "/".join(
        segment if not segment or _SAFE_SEGMENT.fullmatch(segment) else "<redacted>"
        for segment in path.split("/")
    )


def mask_number(number: str) -> str:
    """Show only the last four digits of a number (for operator-facing output)."""
    return "*" * max(len(number) - 4, 0) + number[-4:]


class NotConfigured(Exception):
    """Twilio settings are missing; no request was made."""


# Failures that happen before the request leaves this process: nothing can have
# reached the provider.
NEVER_SENT: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    NotConfigured,
)


class ProviderError(Exception):
    """A provider failure translated for our HTTP boundary.

    ``status_code`` is the status our API answers with; ``outcome_unknown`` says
    whether something may have happened at the provider.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_code = provider_code


@dataclass(frozen=True)
class ProviderRejection:
    """What a provider error body said, with anything phone-like masked."""

    status_code: int
    code: int | None
    message: str


def describe_rejection(status_code: int, error: RawError) -> ProviderRejection:
    code: int | None = None
    message = ""
    try:
        payload = error.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        code = raw_code if isinstance(raw_code, int) else None
        raw_message = payload.get("message")
        message = mask(raw_message) if isinstance(raw_message, str) else ""
    return ProviderRejection(status_code=status_code, code=code, message=message)


def provider_error(status_code: int, error: RawError) -> ProviderError:
    """The one map from a provider error status to our boundary's answer."""
    rejection = describe_rejection(status_code, error)
    if status_code in (401, 403):
        return ProviderError(502, "The SMS provider refused our credentials.", provider_code=rejection.code)
    if status_code == 429:
        return ProviderError(503, "The SMS provider is rate-limiting us; try again shortly.",
                             provider_code=rejection.code)
    if 400 <= status_code < 500:
        return ProviderError(422, rejection.message or "The SMS provider rejected the request.",
                             provider_code=rejection.code)
    return ProviderError(502, "The SMS provider is unavailable.", outcome_unknown=True,
                         provider_code=rejection.code)


def guarded_read(call: Callable[[], T]) -> T:
    """Run a provider READ, translating every failure kind to ProviderError."""
    try:
        return call()
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except ValueError as e:  # pydantic ValidationError or a non-JSON body
        raise ProviderError(502, "Unreadable response from the SMS provider.") from e
    except NEVER_SENT as e:
        raise ProviderError(502, "Could not reach the SMS provider.") from e
    except httpx.RequestError as e:
        raise ProviderError(504, "The SMS provider did not answer in time.") from e


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class LoggingTransport:
    """Wraps the SDK transport: logs method, host, masked path, status, time.

    Never logs query strings (they carry phone numbers), headers (they carry
    the credential) or bodies. The response body is passed back unread.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        target = "%s%s" % (parts.netloc, safe_path(parts.path))
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.warning("twilio %s %s -> %s", request.method, target, type(e).__name__)
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method, target, response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # Omitting the credential would silently send unauthenticated requests.
        raise NotConfigured("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.")
    server_config: ServerConfigDict = {}
    if settings.TWILIO_BASE_URL:
        server_config["default"] = {"base_url": settings.TWILIO_BASE_URL}
    if settings.TWILIO_LOOKUPS_BASE_URL:
        server_config["default5"] = {"base_url": settings.TWILIO_LOOKUPS_BASE_URL}
    inner = transport if transport is not None else HttpxClient(timeout=settings.TWILIO_TIMEOUT_SECONDS)
    return TwilioSdkClient(
        server_config=server_config,
        timeout=settings.TWILIO_TIMEOUT_SECONDS,
        custom_http_client=LoggingTransport(inner),
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )


_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()  # guards construction only


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                client = build_client()
                atexit.register(client.close)
                _client = client
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the process client (tests, credential rotation). Closes the old one."""
    global _client
    with _client_lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


def _account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------


def text_or_none(value: str | None | UnsetType) -> str | None:
    if isinstance(value, UnsetType):
        return None
    return value


def parse_provider_time(value: str | None | UnsetType) -> datetime | None:
    """Twilio renders message dates as RFC 2822 strings."""
    text = text_or_none(value)
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _next_page(next_page_uri: str | None | UnsetType) -> tuple[str, int | None] | None:
    uri = text_or_none(next_page_uri)
    if not uri:
        return None
    query = parse_qs(urlsplit(uri).query)
    tokens = query.get("PageToken")
    if not tokens:
        return None
    pages = query.get("Page")
    page = int(pages[0]) if pages and pages[0].isdigit() else None
    return tokens[0], page


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberCheck:
    valid: bool
    phone_number: str | None
    country_code: str
    national_format: str
    validation_errors: list[str]


def lookup_number(raw_number: str, country_code: str | None = None) -> NumberCheck:
    """Ask the provider whether a number is a usable destination (read)."""

    def call() -> LookupResponse:
        return get_client().lookups_v2_phone_number.fetch_phone_number2(raw_number, country_code=country_code)

    try:
        result = guarded_read(call)
    except ProviderError as e:
        if e.status_code == 422:  # the provider did not recognise it as a number at all
            return NumberCheck(False, None, "", "", ["NOT_A_NUMBER"])
        raise
    if isinstance(result.valid, UnsetType):
        raise ProviderError(502, "The SMS provider's number check came back without a verdict.")
    errors = [] if isinstance(result.validation_errors, UnsetType) else [str(v) for v in result.validation_errors]
    return NumberCheck(
        valid=result.valid,
        phone_number=text_or_none(result.phone_number),
        country_code=text_or_none(result.country_code) or "",
        national_format=text_or_none(result.national_format) or "",
        validation_errors=errors,
    )


def create_message(to: str, body: str, send_at: datetime | None = None) -> ApiV2010AccountMessage:
    """Send now, or queue with the provider for ``send_at`` (write; raw SDK errors propagate)."""
    client = get_client()
    if send_at is None:
        return client.api20100401_message.create_message(
            _account_sid(), to, from_=settings.TWILIO_FROM_NUMBER, body=body
        )
    if not settings.TWILIO_MESSAGING_SERVICE_SID:
        raise NotConfigured("TWILIO_MESSAGING_SERVICE_SID must be set to schedule messages.")
    return client.api20100401_message.create_message(
        _account_sid(),
        to,
        from_=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at,
        body=body,
    )


def cancel_message(sid: str) -> ApiV2010AccountMessage:
    """Call off a message queued for later (write)."""
    return get_client().api20100401_message.update_message(
        _account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED
    )


def redact_message(sid: str) -> ApiV2010AccountMessage:
    """Erase the text of a message at the provider (write)."""
    return get_client().api20100401_message.update_message(_account_sid(), sid, body="")


def fetch_message(sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.fetch_message(_account_sid(), sid)


FIND_MAX_PAGES = 5


def find_message_by_token(to: str, token: str) -> ApiV2010AccountMessage | None:
    """Look up a message we sent by the reference token carried in its text.

    Twilio has no client reference field or idempotency key on message
    creation, so the token in the body is the reference.
    """
    client = get_client()
    page_token: str | None = None
    page: int | None = None
    for _ in range(FIND_MAX_PAGES):
        result = client.api20100401_message.list_message(
            _account_sid(), to=to, page_size=100, page_token=page_token, page=page
        )
        messages = [] if isinstance(result.messages, UnsetType) else result.messages
        for message in messages:
            body = text_or_none(message.body)
            if body and token in body:
                return message
        following = _next_page(result.next_page_uri)
        if following is None:
            return None
        page_token, page = following
    return None


@dataclass(frozen=True)
class MessageListing:
    messages: list[ApiV2010AccountMessage]
    complete: bool


RECONCILE_MAX_PAGES = 200


def list_messages_sent_from(from_number: str, first_day: date, last_day: date) -> MessageListing:
    """Every message the provider sent from ``from_number`` on the given days (read).

    The provider filters by sender; the date filter is whole-day, so callers
    narrow the result back to their exact window.
    """
    client = get_client()
    after = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc)
    end_day = last_day + timedelta(days=1)
    before = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)
    collected: list[ApiV2010AccountMessage] = []
    page_token: str | None = None
    page: int | None = None
    for _ in range(RECONCILE_MAX_PAGES):
        token, number = page_token, page
        result = guarded_read(
            lambda: client.api20100401_message.list_message(
                _account_sid(),
                from_=from_number,
                date_sent_query_query=after,  # DateSent>
                date_sent_query=before,  # DateSent<
                page_size=1000,
                page_token=token,
                page=number,
            )
        )
        if not isinstance(result.messages, UnsetType):
            collected.extend(result.messages)
        following = _next_page(result.next_page_uri)
        if following is None:
            return MessageListing(collected, complete=True)
        page_token, page = following
    return MessageListing(collected, complete=False)

"""
The only module that talks to Twilio.

Everything here is framework-free so it can be type-checked strictly against the SDK; the Django side
(claims, records, views) lives in ``services`` and ``store``.

Rules this module keeps:

* the auth token and destination numbers never reach a log line or an exception message;
* every provider write goes through :func:`safe_write` (claim -> call -> check -> complete);
* a provider status only becomes an outcome through one of the mappers below, and an outcome only
  becomes an HTTP status through :func:`answer_status`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import ApiError, HttpClient, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

logger = logging.getLogger("apps.sms_notifications")

T = TypeVar("T")

# Outcomes, shared by every provider write this app makes.
SENDING = "sending"  # claimed, no provider answer yet
DONE = "done"
PENDING = "pending"  # provider accepted it and has not finished
FAILED = "failed"  # never sent, refused, or reported failed/undone by the provider
NEEDS_REVIEW = "needs_review"  # happened, but not as asked
UNKNOWN = "unknown"  # may have happened, or a state this app does not map

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


# ---------------------------------------------------------------------------
# Configuration and client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str
    messaging_service_sid: str
    base_url: str | None = None  # messaging API (server "default") only
    lookups_base_url: str | None = None  # Lookups host (server "default4"); None = provider default
    timeout: float = 10.0

    def missing(self) -> list[str]:
        names = {
            "TWILIO_ACCOUNT_SID": self.account_sid,
            "TWILIO_AUTH_TOKEN": self.auth_token,
            "TWILIO_FROM_NUMBER": self.from_number,
            "TWILIO_MESSAGING_SERVICE_SID": self.messaging_service_sid,
        }
        return [name for name, value in names.items() if not value]


class NotConfigured(Exception):
    """The Twilio settings are incomplete; nothing was sent."""


def build_client(config: TwilioConfig, *, transport: HttpClient | None = None) -> TwilioSdkClient:
    missing = config.missing()
    if missing:
        raise NotConfigured("Missing Twilio settings: " + ", ".join(missing))
    servers: ServerConfigDict = {}
    if config.base_url:
        servers["default"] = {"base_url": config.base_url.rstrip("/")}
    if config.lookups_base_url:
        servers["default4"] = {"base_url": config.lookups_base_url.rstrip("/")}
    return TwilioSdkClient(
        account_sid_auth_token={"username": config.account_sid, "password": config.auth_token},
        server_config=servers or None,
        timeout=config.timeout,
        custom_http_client=transport,
    )


# ---------------------------------------------------------------------------
# Errors at this app's boundary
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """A provider call that did not produce a usable answer.

    ``status_code`` is what our own API answers; ``outcome_unknown`` says whether anything may have
    happened upstream. The message is always one we wrote -- never a provider body.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


class OutcomeUnknown(Exception):
    """A write may have landed and the check could not settle it."""

    def __init__(self, reference: str) -> None:
        super().__init__("outcome unknown")
        self.reference = reference


def provider_error(status: int, error: object) -> ProviderError:
    """The one map from a provider answer to our error type."""
    if status in (401, 403):
        return ProviderError(502, "The messaging provider refused our credentials.")
    if status == 429:
        return ProviderError(503, "The messaging provider is rate-limiting us.")
    if 400 <= status < 500:
        return ProviderError(422, "The messaging provider rejected the request.")
    return ProviderError(502, "The messaging provider is unavailable.", outcome_unknown=status >= 500)


def provider_code(error: object) -> int | None:
    """Twilio's numeric error code from a RawError body, when it is readable JSON."""
    if not isinstance(error, RawError):
        return None
    try:
        body = error.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def guarded_read(call: Callable[[], T]) -> T:
    """Run a provider *read*, converting every failure kind to ProviderError."""
    try:
        return call()
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except ValueError as e:  # pydantic ValidationError or a non-JSON body
        raise ProviderError(502, "The messaging provider sent an unreadable response.") from e
    except NEVER_SENT as e:
        raise ProviderError(502, "Could not reach the messaging provider.") from e
    except httpx.RequestError as e:
        raise ProviderError(504, "The messaging provider did not answer in time.") from e


# ---------------------------------------------------------------------------
# Status -> outcome mappers (one per kind of write)
# ---------------------------------------------------------------------------


def status_from_provider(status: object) -> str:
    """A *send*: done only once the provider says it reached the handset."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.CANCELED:
            return FAILED  # canceled: it was called off, so what was asked is not in effect
        case _:
            return UNKNOWN  # RECEIVING/RECEIVED, a value newer than the SDK, or no status at all


def cancel_outcome(status: object) -> str:
    """Calling off a scheduled message: its done is the canceled state."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED:
            return PENDING
        case (
            MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.DELIVERED
            | MessageEnumStatus.READ
            | MessageEnumStatus.PARTIALLY_DELIVERED
            | MessageEnumStatus.FAILED
            | MessageEnumStatus.UNDELIVERED
        ):
            return FAILED  # too late: it already went out (or already ended)
        case _:
            return UNKNOWN


def redact_outcome(body: object) -> str:
    """Redacting a message's text: done only when the provider echoes an empty body."""
    if body == "":
        return DONE
    if isinstance(body, str):
        return NEEDS_REVIEW  # the provider answered, and the text is still there
    return UNKNOWN


def answer_status(outcome: str) -> int:
    """The ONE place an outcome becomes an HTTP status for a write endpoint."""
    match outcome:
        case "done":
            return 200
        case "pending" | "sending":
            return 202
        case "failed" | "needs_review":
            return 409
        case _:
            return 504


# ---------------------------------------------------------------------------
# The safe write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What one write step's response says, read the same way for every step."""

    provider_id: str | None
    status: object
    provider_time: datetime | None
    outcome: str


@dataclass
class ClaimRecord:
    reference: str
    outcome: str
    claimed_at: datetime
    provider_id: str | None = None


class ClaimStore(Protocol):
    def try_claim(self, reference: str) -> bool:
        """Insert-or-fail: True for exactly one caller while the claim is held."""

    def load(self, reference: str) -> ClaimRecord: ...

    def complete(
        self,
        reference: str,
        outcome: str,
        answer: Answer | None = None,
        *,
        error_code: int | None = None,
    ) -> ClaimRecord:
        """Record the outcome. ``failed`` without a provider id releases the claim."""


SEND_WINDOW = timedelta(minutes=2)  # > attempts x timeout; one attempt of <= 10 s here


def safe_write(
    store: ClaimStore,
    reference: str,
    *,
    send: Callable[[], T],
    find: Callable[[], T | None],
    read: Callable[[T], Answer],
    repeat_is_safe: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ClaimRecord:
    """The one path every provider write goes through (a send, a cancel, a redaction).

    ``send`` makes the call; ``find`` looks the write up by the reference it carries (None when
    nothing is found); ``read`` turns either into an :class:`Answer`, outcome included.
    A request that loses the claim never makes a new write: it settles the earlier attempt with the
    lookup -- or, where ``repeat_is_safe`` (the same call twice has the same single effect, e.g.
    blanking a message body by id), with a resend of the identical request.
    """
    checking = False
    if not store.try_claim(reference):
        existing = store.load(reference)
        if existing.outcome == SENDING and existing.claimed_at > now() - SEND_WINDOW:
            return existing  # in flight elsewhere: in progress, no provider call
        if existing.outcome not in (SENDING, UNKNOWN):
            return existing  # settled: answer from the record
        checking = True  # stale sender or unresolved: look, never create
    resending = checking and repeat_is_safe

    result: T | None = None
    if resending or not checking:
        try:
            result = send()
        except NEVER_SENT as e:
            if resending:  # a check that never left says nothing: still unknown
                store.complete(reference, UNKNOWN)
                raise OutcomeUnknown(reference) from e
            store.complete(reference, FAILED)  # a first send that never left: nothing happened
            raise
        except ApiError as e:
            if e.status_code < 500:
                if resending:  # a check never fails the write
                    store.complete(reference, UNKNOWN)
                    raise OutcomeUnknown(reference) from e
                # Refused. Twilio's error body says why; nothing was created.
                store.complete(reference, FAILED, error_code=provider_code(e.error))
                raise
            # a 5xx on a write may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: may have landed

    if result is None:
        try:
            result = find()
        except (ApiError, httpx.RequestError, ValueError) as e:
            store.complete(reference, UNKNOWN)
            raise OutcomeUnknown(reference) from e
        if result is None:
            store.complete(reference, UNKNOWN)  # not found yet: an empty lookup proves nothing
            raise OutcomeUnknown(reference)

    got = read(result)
    if got.provider_id is None:
        store.complete(reference, UNKNOWN)  # a 2xx that names nothing: cannot say what it made
        raise OutcomeUnknown(reference)
    return store.complete(reference, got.outcome, got)


# ---------------------------------------------------------------------------
# Reading provider values
# ---------------------------------------------------------------------------


def _str(value: str | None | UnsetType) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_provider_time(value: str | None | UnsetType) -> datetime | None:
    """Twilio message dates are RFC 2822 strings; accept ISO-8601 too."""
    text = _str(value)
    if text is None:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class MessageState:
    sid: str | None
    status: str | None  # the provider's own word, as a plain string
    raw_status: object
    body: str | None | UnsetType
    date_sent: datetime | None
    date_created: datetime | None
    error_code: int | None
    error_message: str | None
    from_number: str | None

    @property
    def provider_time(self) -> datetime | None:
        return self.date_sent or self.date_created


def message_state(message: ApiV2010AccountMessage) -> MessageState:
    status = message.status
    error_code = message.error_code
    return MessageState(
        sid=_str(message.sid),
        status=None if isinstance(status, UnsetType) else str(status),
        raw_status=status,
        body=message.body,
        date_sent=parse_provider_time(message.date_sent),
        date_created=parse_provider_time(message.date_created),
        error_code=error_code if isinstance(error_code, int) else None,
        error_message=_str(message.error_message),
        from_number=_str(message.from_),
    )


def send_answer(message: ApiV2010AccountMessage) -> Answer:
    state = message_state(message)
    return Answer(state.sid, state.raw_status, state.provider_time, status_from_provider(state.raw_status))


def cancel_answer(message: ApiV2010AccountMessage) -> Answer:
    state = message_state(message)
    return Answer(state.sid, state.raw_status, state.provider_time, cancel_outcome(state.raw_status))


def redact_answer(message: ApiV2010AccountMessage) -> Answer:
    state = message_state(message)
    return Answer(state.sid, state.raw_status, state.provider_time, redact_outcome(state.body))


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def reference_token(reference: str) -> str:
    """The short token a message body carries so the send can be found by its reference later."""
    return hashlib.sha256(reference.encode()).hexdigest()[:10].upper()


def body_with_reference(text: str, reference: str) -> str:
    return f"{text} Ref {reference_token(reference)}"


@dataclass(frozen=True)
class LookupResult:
    valid: bool
    canonical: str | None
    country_code: str | None
    problems: list[str]


class Messaging:
    """Thin, typed wrapper over the SDK operations this app uses."""

    def __init__(self, client: TwilioSdkClient, config: TwilioConfig) -> None:
        self._client = client
        self._config = config

    @property
    def from_number(self) -> str:
        return self._config.from_number

    # -- reads --------------------------------------------------------------

    def lookup(self, number: str) -> LookupResult:
        """Ask the provider whether ``number`` is a usable destination (raises ProviderError)."""
        try:
            response: LookupResponse = self._client.lookups_v2_phone_number.fetch_phone_number3(number)
        except ApiError as e:
            if e.status_code in (400, 404):
                return LookupResult(False, None, None, ["not_a_phone_number"])
            raise provider_error(e.status_code, e.error) from e
        except ValueError as e:
            raise ProviderError(502, "The messaging provider sent an unreadable response.") from e
        except NEVER_SENT as e:
            raise ProviderError(502, "Could not reach the messaging provider.") from e
        except httpx.RequestError as e:
            raise ProviderError(504, "The messaging provider did not answer in time.") from e
        problems = [] if isinstance(response.validation_errors, UnsetType) else [str(p) for p in response.validation_errors]
        canonical = _str(response.phone_number)
        valid = response.valid is True and canonical is not None
        return LookupResult(valid, canonical if valid else None, _str(response.country_code), problems)

    def fetch(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.fetch_message(self._config.account_sid, sid)

    def find_by_reference(self, to: str, reference: str) -> ApiV2010AccountMessage | None:
        """Kind-3 lookup: the newest messages to ``to`` whose body carries the reference token."""
        token = f"Ref {reference_token(reference)}"
        page_token: str | None = None
        page: int | None = None
        for _ in range(2):
            listing = self._client.api20100401_message.list_message(
                self._config.account_sid, to=to, page_size=50, page=page, page_token=page_token
            )
            messages = [] if isinstance(listing.messages, UnsetType) else listing.messages
            for message in messages:
                if isinstance(message.body, str) and token in message.body:
                    return message
            page, page_token = next_page(listing.next_page_uri)
            if page_token is None:
                break
        return None

    def list_sent(self, start: datetime, end: datetime, *, max_pages: int = 200) -> tuple[list[MessageState], bool]:
        """Every message sent FROM our number with a send date in [start, end).

        The provider filters on whole GMT days, so the query is widened to whole days and narrowed back
        here. Returns (messages, complete); ``complete`` is False only if ``max_pages`` was hit.
        """
        first_day = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        last_day = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
        found: list[MessageState] = []
        page_token: str | None = None
        page: int | None = None
        for _ in range(max_pages):
            listing = self._client.api20100401_message.list_message(
                self._config.account_sid,
                from_=self._config.from_number,
                date_sent_query_query=first_day,  # DateSent>  (on or after)
                date_sent_query=last_day,  # DateSent<  (on or before)
                page_size=1000,
                page=page,
                page_token=page_token,
            )
            for message in [] if isinstance(listing.messages, UnsetType) else listing.messages:
                state = message_state(message)
                if state.date_sent is not None and start <= state.date_sent < end:
                    found.append(state)
            page, page_token = next_page(listing.next_page_uri)
            if page_token is None:
                return found, True
        return found, False

    # -- writes (only ever called from inside safe_write) --------------------

    def send_now(self, to: str, body: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.create_message(
            self._config.account_sid, to, from_=self._config.from_number, body=body
        )

    def send_later(self, to: str, body: str, send_at: datetime) -> ApiV2010AccountMessage:
        """Queue the message with the provider (scheduling requires the Messaging Service)."""
        return self._client.api20100401_message.create_message(
            self._config.account_sid,
            to,
            from_=self._config.from_number,
            messaging_service_sid=self._config.messaging_service_sid,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        )

    def cancel(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.update_message(
            self._config.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
        )

    def redact(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.update_message(self._config.account_sid, sid, body="")


def next_page(next_page_uri: str | None | UnsetType) -> tuple[int | None, str | None]:
    uri = _str(next_page_uri)
    if uri is None:
        return None, None
    query = parse_qs(urlsplit(uri).query)
    tokens = query.get("PageToken") or []
    token = tokens[0] if tokens else None
    page_values = query.get("Page") or []
    page = int(page_values[0]) if page_values and page_values[0].isdigit() else None
    return page, token


__all__ = [
    "Answer",
    "ClaimRecord",
    "ClaimStore",
    "DONE",
    "FAILED",
    "LookupResult",
    "MessageState",
    "Messaging",
    "NEEDS_REVIEW",
    "NotConfigured",
    "OutcomeUnknown",
    "PENDING",
    "ProviderError",
    "SENDING",
    "TwilioConfig",
    "UNKNOWN",
    "answer_status",
    "body_with_reference",
    "build_client",
    "cancel_answer",
    "cancel_outcome",
    "guarded_read",
    "message_state",
    "redact_answer",
    "redact_outcome",
    "safe_write",
    "send_answer",
    "status_from_provider",
]

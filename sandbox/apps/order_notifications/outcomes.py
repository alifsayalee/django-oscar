"""Mapping provider states to our outcomes, and outcomes to HTTP answers.

Every member of Twilio's message status enum is listed by name. A value the
enum does not list (a status newer than the SDK) or an absent status is
``unknown`` - never done, never failed.
"""

from twilio_sdk.core import UnsetType
from twilio_sdk.models.enums import MessageEnumStatus

from .models import Outcome

# The provider no longer has the record. For a call-off or a disposal, that is the
# thing asked for.
GONE = object()


def status_from_provider(status: object) -> str:
    """A message's delivery: done only once the provider confirms delivery."""
    if isinstance(status, UnsetType) or status is None:
        return Outcome.UNKNOWN
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return Outcome.DONE
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT  # handed to the carrier; delivery not yet confirmed
            | MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SCHEDULED
        ):
            return Outcome.PENDING
        case (
            MessageEnumStatus.FAILED
            | MessageEnumStatus.UNDELIVERED
            | MessageEnumStatus.CANCELED  # called off: it will never reach the shopper
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return Outcome.FAILED
        case MessageEnumStatus.RECEIVING | MessageEnumStatus.RECEIVED:
            return Outcome.UNKNOWN  # inbound states: never one of ours
        case _:
            return Outcome.UNKNOWN


def cancel_outcome(status: object) -> str:
    """The follow-up call-off: its done is the message being canceled."""
    if status is GONE:
        return Outcome.DONE
    if isinstance(status, UnsetType) or status is None:
        return Outcome.UNKNOWN
    match status:
        case MessageEnumStatus.CANCELED:
            return Outcome.DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED:
            return Outcome.PENDING  # still queued with the provider
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
            return Outcome.FAILED  # too late: it already left the schedule
        case _:
            return Outcome.UNKNOWN


def redact_outcome(body: object) -> str:
    """Content disposal: done once the provider holds no text for the message."""
    if body is GONE:
        return Outcome.DONE
    if isinstance(body, UnsetType):
        return Outcome.UNKNOWN
    if body is None or body == "":
        return Outcome.DONE
    if isinstance(body, str):
        return Outcome.FAILED
    return Outcome.UNKNOWN


def answer_status(outcome: str) -> int:
    """The ONE place an outcome of a provider write becomes an HTTP status."""
    match outcome:
        case Outcome.DONE:
            return 200
        case Outcome.PENDING | Outcome.SENDING:
            return 202
        case Outcome.FAILED | Outcome.NEEDS_REVIEW:
            return 409
        case _:
            return 504


def worst(outcomes: list[str]) -> str:
    """Combine several write outcomes: the least settled one wins."""
    for candidate in (
        Outcome.UNKNOWN,
        Outcome.SENDING,
        Outcome.PENDING,
        Outcome.NEEDS_REVIEW,
        Outcome.FAILED,
    ):
        if candidate in outcomes:
            return candidate
    return Outcome.DONE

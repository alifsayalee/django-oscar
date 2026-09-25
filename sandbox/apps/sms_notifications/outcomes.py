"""
The one place a Twilio message status becomes one of our outcomes.

Outcomes: ``done`` (what we asked for is in effect), ``pending`` (accepted,
not finished), ``failed`` (refused, failed, or done-then-undone) and
``unknown`` (a value we do not map, or none at all - never done).
"""
from twilio_sdk.core import UnsetType
from twilio_sdk.models.enums import MessageEnumStatus

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
UNKNOWN = 'unknown'
SENDING = 'sending'          # our in-flight claim marker, never a provider answer
NEEDS_REVIEW = 'needs_review'

SETTLED = (DONE, FAILED)


def _member(status: object) -> MessageEnumStatus | None:
    if isinstance(status, MessageEnumStatus):
        return status
    if isinstance(status, str):
        try:
            return MessageEnumStatus(status)
        except ValueError:
            return None
    return None


def status_from_provider(status: object) -> str:
    """Outcome of a message *send* from the message's own status."""
    match _member(status):
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING
              | MessageEnumStatus.SENT | MessageEnumStatus.SCHEDULED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            # sent = handed to the carrier, no delivery receipt yet; partially
            # delivered is not settled by the source - both are not-yet.
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            # it was queued and then called off: not in effect -> failed for a send
            return FAILED
        case MessageEnumStatus.RECEIVING | MessageEnumStatus.RECEIVED:
            return UNKNOWN          # inbound values: meaningless for an outbound send
        case _:
            return UNKNOWN


def cancel_outcome_from_provider(status: object) -> str:
    """Outcome of *calling off* a scheduled message (the undoing is what we asked for)."""
    match _member(status):
        case MessageEnumStatus.CANCELED:
            return DONE
        case (MessageEnumStatus.SENDING | MessageEnumStatus.SENT | MessageEnumStatus.DELIVERED
              | MessageEnumStatus.READ | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.FAILED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return FAILED           # it already went out: the call-off did not happen
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED:
            return PENDING
        case _:
            return UNKNOWN


def redaction_outcome(body: object) -> str:
    """Outcome of disposing a message's content: done only when the provider echoes an empty body."""
    if isinstance(body, UnsetType) or body is None:
        return UNKNOWN
    return DONE if body == '' else UNKNOWN

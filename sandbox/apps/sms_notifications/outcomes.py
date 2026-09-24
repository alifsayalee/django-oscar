"""
The one place a provider message status becomes one of this app's outcomes.

Members are listed by name. Anything not listed - including a status newer
than the SDK, or no status at all - is ``unknown``: neither done nor failed.
"""
from __future__ import annotations

from twilio_sdk.models.enums import MessageEnumStatus

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
UNKNOWN = 'unknown'


def status_from_provider(status: object) -> str:
    """Outcome of *sending* a message, from the message's own status."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING
              | MessageEnumStatus.SENT | MessageEnumStatus.ACCEPTED
              | MessageEnumStatus.SCHEDULED):
            # Accepted, not delivered yet. "sent" means a carrier took it, not
            # that it reached the handset.
            return PENDING
        case (MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED
              | MessageEnumStatus.CANCELED):
            # "canceled" is a message that was called off: it never went out.
            return FAILED
        case _:
            # partially_delivered, receiving, received, a value this SDK does
            # not know, or no status at all.
            return UNKNOWN


def cancel_outcome(status: object) -> str:
    """Outcome of *calling off* a scheduled message, from its status after."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case (MessageEnumStatus.SCHEDULED | MessageEnumStatus.QUEUED
              | MessageEnumStatus.ACCEPTED):
            # Not yet sent, but not called off either: try again.
            return PENDING
        case (MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.DELIVERED | MessageEnumStatus.READ
              | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.FAILED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            # It already went out (or was attempted): too late to call off.
            return FAILED
        case _:
            return UNKNOWN


def status_text(status: object) -> str:
    """The status as its wire value, for storage and display."""
    if isinstance(status, str):
        return str(status)
    return ''

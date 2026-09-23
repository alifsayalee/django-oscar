"""
The one place a Twilio message status becomes this app's delivery outcome.
"""

from twilio_sdk.models.enums import MessageEnumStatus

# Outcomes a notification can be in.
SENDING = "sending"  # our row exists; the provider has not answered yet
PENDING = "pending"  # accepted by the provider, not yet handed to the carrier
SCHEDULED = "scheduled"  # queued with the provider for a later send time
SENT = "sent"  # handed to the carrier; no handset receipt yet
DELIVERED = "delivered"
FAILED = "failed"  # did not (and will not) reach the handset
CANCELED = "canceled"  # a scheduled message called off before it went out
UNKNOWN = "unknown"  # may or may not exist / reached; needs a look

OUTCOMES = (SENDING, PENDING, SCHEDULED, SENT, DELIVERED, FAILED, CANCELED, UNKNOWN)

#: Outcomes the provider will not change any more.
TERMINAL = frozenset({DELIVERED, FAILED, CANCELED})


def outcome_from_provider(status: str | None) -> str:
    """Map a MessageEnumStatus (or an unrecognised string) onto our outcome."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DELIVERED
        case MessageEnumStatus.SENT:
            return SENT
        case MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING:
            return PENDING
        case MessageEnumStatus.SCHEDULED:
            return SCHEDULED
        case MessageEnumStatus.CANCELED:
            return CANCELED
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case (
            MessageEnumStatus.PARTIALLY_DELIVERED
            | MessageEnumStatus.RECEIVING
            | MessageEnumStatus.RECEIVED
        ):
            # Multi-part partial delivery, or an inbound status on an outbound
            # message: neither done nor failed.
            return UNKNOWN
        case _:
            return UNKNOWN

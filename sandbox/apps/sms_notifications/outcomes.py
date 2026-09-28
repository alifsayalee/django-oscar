"""
The one place provider statuses become outcomes, and outcomes become HTTP answers.

Every write this app makes to Twilio records one of these outcomes on its claim row:

* ``sending``      claimed, no answer from the provider yet
* ``done``         what was asked for is in effect now
* ``pending``      the provider accepted it and has not finished
* ``failed``       refused, never sent, or done and later undone
* ``needs_review`` it happened, but not as asked
* ``unknown``      it may have happened; only the provider's word settles it
* ``skipped``      deliberately not sent (no number on file, order already cancelled); holds its claim
"""
from twilio_sdk.core import UNSET
from twilio_sdk.models.enums import MessageEnumStatus

SENDING = "sending"
DONE = "done"
PENDING = "pending"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"
UNKNOWN = "unknown"
SKIPPED = "skipped"

OUTCOME_CHOICES = [
    (SENDING, "Sending"),
    (DONE, "Done"),
    (PENDING, "Pending"),
    (FAILED, "Failed"),
    (NEEDS_REVIEW, "Needs review"),
    (UNKNOWN, "Unknown"),
    (SKIPPED, "Skipped"),
]

# Outcomes a later request must still look at: the provider has the last word on them.
UNSETTLED = (SENDING, PENDING, UNKNOWN)


def status_from_provider(status: object) -> str:
    """Outcome of a *send*, from the Message resource's ``status``."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (
            MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            # "sent" means handed to the carrier, not received; a scheduled message has not gone out yet.
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            # Accepted, then called off: it never reached the shopper.
            return FAILED
        case _:
            # receiving/received (inbound), a value newer than the SDK, or no status at all.
            return UNKNOWN


def cancel_outcome(status: object) -> str:
    """Outcome of a *call-off* of a scheduled message: for this step, the undone state is done."""
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
            # Too late: the provider already tried to deliver it.
            return FAILED
        case _:
            return UNKNOWN


def redaction_outcome(body: object) -> str:
    """Outcome of a content redaction, from the body the provider echoes back."""
    if body is UNSET or body is None or not isinstance(body, str):
        return UNKNOWN
    return DONE if body == "" else FAILED


def answer_status(outcome: str) -> int:
    """The HTTP status an outcome is answered with. Success comes from ``done`` alone."""
    match outcome:
        case "done":
            return 200
        case "pending" | "sending":
            return 202
        case "failed" | "needs_review" | "skipped":
            return 409
        case _:
            return 504


def aggregate(outcomes: list[str]) -> str:
    """
    One outcome for removing a contact number, from the call-offs of its queued follow-ups.

    The removal asks that nothing *further* reaches the number. A call-off that came too late (``failed``: the
    message had already gone out before the removal) does not leave anything further outstanding, so it does not
    hold the removal back; a call-off still in flight or unresolved does.
    """
    if any(o not in (DONE, PENDING, SENDING, FAILED, SKIPPED) for o in outcomes):
        return UNKNOWN
    if any(o in (PENDING, SENDING) for o in outcomes):
        return PENDING
    return DONE

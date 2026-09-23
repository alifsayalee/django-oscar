"""The single place a Twilio message status becomes one of ours.

Twilio's ``MessageEnumStatus`` is an *open* enum: a value newer than the SDK
arrives as a plain ``str``. We enumerate every member the SDK lists by its wire
value and leave the default arm meaning ``unknown`` -- neither delivered nor
failed -- exactly as the calling-endpoints guidance requires. A returned SID
means the send was *accepted*, not delivered; the outcome comes from here.
"""

from .models import NotificationStatus

# Twilio MessageEnumStatus wire values -> our NotificationStatus value (a str).
# The right-hand values are exactly the NotificationStatus choice values (see
# models.NotificationStatus); written as literals so this module type-checks
# without django-stubs.
# (verified members: queued, sending, sent, failed, delivered, undelivered,
#  receiving, received, accepted, scheduled, read, partially_delivered, canceled)
_MAP = {
    "delivered": "delivered",
    "received": "delivered",
    "read": "delivered",
    "sent": "sent",
    "queued": "pending",
    "sending": "pending",
    "accepted": "pending",
    "scheduled": "pending",
    "receiving": "pending",
    "failed": "failed",
    "undelivered": "failed",
    "canceled": "canceled",
    "partially_delivered": "partial",
}

_UNKNOWN = "unknown"

# Guard against drift between the literals above and the model's choices.
assert set(_MAP.values()) | {_UNKNOWN} <= set(NotificationStatus.values)


def map_status(raw) -> str:
    """Fold a Twilio status (enum member or open ``str``) into a NotificationStatus.

    Anything not listed -- ``UNSET``, ``None``, or a value newer than the SDK --
    becomes ``UNKNOWN`` rather than being guessed as delivered or failed.
    """
    if raw is None:
        return _UNKNOWN
    # str(member) yields the wire value for a (str, Enum); str() of UNSET/other is harmless here.
    key = str(raw).strip().lower()
    return _MAP.get(key, _UNKNOWN)

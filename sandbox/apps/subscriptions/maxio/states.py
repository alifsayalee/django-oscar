"""
The one place a Maxio subscription state becomes one of ours.

Every ``SubscriptionState`` member is listed by name; anything else (the enum is
open, so the provider may send a value newer than the SDK) is ``unknown``.
"""

from __future__ import annotations

from enum import Enum

from maxio_advanced_billing.models.enums import SubscriptionState


class Bucket(str, Enum):
    ACTIVE = "active"  # in good standing
    PENDING = "pending"  # accepted, not settled yet
    PROBLEM = "problem"  # exists, billing trouble
    INACTIVE = "inactive"  # exists, not currently billing
    ENDED = "ended"  # end of life; the plan may be subscribed to again
    FAILED = "failed"  # signup failed; nothing exists
    UNKNOWN = "unknown"  # a state we do not map; neither done nor failed


def bucket_for(state: object) -> Bucket:
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return Bucket.ACTIVE
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return Bucket.PENDING
        case SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID:
            return Bucket.PROBLEM
        case SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED:
            return Bucket.INACTIVE
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return Bucket.ENDED
        case SubscriptionState.FAILED_TO_CREATE:
            return Bucket.FAILED
        case _:
            return Bucket.UNKNOWN


# Buckets in which the subscription exists at Maxio and still occupies the plan.
LIVE_BUCKETS = frozenset({Bucket.ACTIVE, Bucket.PENDING, Bucket.PROBLEM, Bucket.INACTIVE, Bucket.UNKNOWN})

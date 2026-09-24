"""
The one place a Maxio subscription state becomes this app's outcome.

"done" means what the shopper asked for - a subscription in effect with
nothing outstanding - holds right now. Only the states listed as done can
ever produce a success answer; everything unlisted is "unknown", never done.
"""

from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models.enums import SubscriptionState

from .models import MaxioWrite

DONE = MaxioWrite.DONE
PENDING = MaxioWrite.PENDING
FAILED = MaxioWrite.FAILED
UNKNOWN = MaxioWrite.UNKNOWN


def status_from_provider(state: object) -> str:
    """Map ``Subscription.state`` (an open enum: member, newer str, or UNSET)."""
    if isinstance(state, UnsetType) or state is None:
        return UNKNOWN  # an unreadable status is not-yet, never done
    match state:
        # Live and in effect.
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return DONE
        # Transient: still being created / assessed / signed up.
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return PENDING
        # Exists, but something is outstanding - needs attention, not done.
        case (
            SubscriptionState.SOFT_FAILURE
            | SubscriptionState.PAST_DUE
            | SubscriptionState.UNPAID
            | SubscriptionState.PAUSED
            | SubscriptionState.ON_HOLD
            | SubscriptionState.SUSPENDED
        ):
            return PENDING
        # Never created, or created and since undone/ended: not in effect.
        case (
            SubscriptionState.FAILED_TO_CREATE
            | SubscriptionState.CANCELED
            | SubscriptionState.EXPIRED
            | SubscriptionState.TRIAL_ENDED
        ):
            return FAILED
        case _:
            # A state newer than this SDK, or one we do not map.
            return UNKNOWN


def state_value(state: object) -> str:
    """The wire value of a state, for storage and responses."""
    if isinstance(state, UnsetType) or state is None:
        return ""
    if isinstance(state, SubscriptionState):
        return state.value
    return str(state)

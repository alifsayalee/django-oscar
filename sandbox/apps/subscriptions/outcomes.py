from maxio_advanced_billing.models.enums import SubscriptionState

from .models import Outcome


def status_from_provider(state: object) -> str:
    """The one place a Maxio subscription state becomes this app's outcome."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return Outcome.DONE
        # Accepted, not finished yet.
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return Outcome.PENDING
        # Exists, but something is outstanding: not done.
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return Outcome.PENDING
        # Failed, or happened and was later undone.
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED
              | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED):
            return Outcome.FAILED
        case _:
            # A value newer than this SDK, or no state at all: neither done nor failed.
            return Outcome.UNKNOWN

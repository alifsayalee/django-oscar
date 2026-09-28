"""
How a Maxio status becomes our outcome, and how an outcome becomes the
caller's HTTP status. Nothing else decides either.
"""
from collections.abc import Callable

from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models.enums import SubscriptionState

from .models import MaxioWrite


def status_from_provider(state: object) -> str:
    """Map a subscription's ``state`` onto done / pending / failed / unknown."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return MaxioWrite.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING
              | SubscriptionState.AWAITING_SIGNUP):
            return MaxioWrite.PENDING  # still being created or assessed
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE
              | SubscriptionState.UNPAID | SubscriptionState.PAUSED
              | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return MaxioWrite.PENDING  # exists, but blocked on payment or action
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED
              | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED):
            return MaxioWrite.FAILED  # never came into effect, or was undone
        case _:
            return MaxioWrite.UNKNOWN  # unset, or a state newer than this SDK


def customer_outcome(sent_reference: str) -> Callable[[object], str]:
    """
    Customers carry no status. A customer is created synchronously, so the
    write is done when Maxio echoes back the reference we sent.
    """
    def outcome_of(echoed_reference: object) -> str:
        if isinstance(echoed_reference, UnsetType):
            return MaxioWrite.UNKNOWN
        if echoed_reference == sent_reference:
            return MaxioWrite.DONE
        return MaxioWrite.NEEDS_REVIEW
    return outcome_of


def state_value(state: object) -> str | None:
    """The wire value of a (possibly open-enum) state, for storage and responses."""
    if isinstance(state, SubscriptionState):
        return state.value
    if isinstance(state, str):
        return state
    return None


def answer(outcome: str, *, created: bool = False) -> int:
    """The one place an outcome becomes the caller's HTTP status. Only done is success."""
    match outcome:
        case MaxioWrite.DONE:
            return 201 if created else 200
        case MaxioWrite.PENDING | MaxioWrite.SENDING:
            return 202  # accepted, not in effect yet
        case MaxioWrite.FAILED | MaxioWrite.NEEDS_REVIEW:
            return 409
        case _:
            return 504  # may have happened - never "not created"

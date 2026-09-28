"""
Per-step mapping of PayPal statuses onto this app's outcomes.

``done`` means what the caller asked for is in effect now; ``pending`` means
PayPal accepted it and has not finished; ``failed`` means refused, or done and
since undone; anything the enum does not list (or a missing status) is
``unknown`` — never done.  Enums are open, so a plain string equal to a member's
wire value matches the member.
"""
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CardVerificationStatus,
    OrderStatus,
    RefundStatus,
)

from .models import PaymentWrite

DONE = PaymentWrite.DONE
PENDING = PaymentWrite.PENDING
FAILED = PaymentWrite.FAILED
NEEDS_REVIEW = PaymentWrite.NEEDS_REVIEW
UNKNOWN = PaymentWrite.UNKNOWN


def authorization_outcome(status: object) -> str:
    """Placing (or renewing) a hold: CREATED means the money is held."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            # The hold was taken outside this step; not what "authorize" asked for.
            return NEEDS_REVIEW
        case _:
            return UNKNOWN


def order_without_authorization_outcome(status: object) -> str:
    """A card order that came back without an authorization in it."""
    match status:
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return FAILED
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return PENDING
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            # Taken and since given back: the capture is no longer in effect.
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """Releasing a hold: the undone state is this step's done."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return PENDING  # the hold is still in place
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


# The vault token response has no status member; the synthetic statuses below are
# derived from what it does carry (see services.vault_answer).
VAULTED = "VAULTED"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
INCOMPLETE = "INCOMPLETE"


def vault_outcome(status: object) -> str:
    match status:
        case "VAULTED":
            return DONE
        case "VERIFICATION_FAILED":
            return FAILED
        case _:
            return UNKNOWN


def verification_failed(value: object) -> bool:
    return value == CardVerificationStatus.FAILED


# The delete endpoint returns no body; its HTTP result is its status.
DELETED = "DELETED"


def delete_outcome(status: object) -> str:
    return DONE if status == DELETED else UNKNOWN

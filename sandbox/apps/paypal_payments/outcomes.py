"""
The one place a PayPal status becomes this app's outcome, per write.

Every member of each status enum is listed by name. A value not listed - or
none at all - is ``unknown``: never done, never failed.
"""
from paypal.models.enums import (
    AuthorizationStatus, CaptureStatus, OrderStatus, PaymentTokenStatus, RefundStatus)

from .models import Outcome


def order_create_step(status: object) -> str:
    """
    The PayPal order itself. COMPLETED means the single-step card order was
    processed and its authorization decides; APPROVED means it still needs
    the authorize step (handled by the caller, reported here as pending).
    """
    match status:
        case OrderStatus.COMPLETED:
            return Outcome.DONE
        case OrderStatus.APPROVED | OrderStatus.CREATED | OrderStatus.SAVED:
            return Outcome.PENDING
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def authorization(status: object) -> str:
    """A new hold (create/authorize order, reauthorize)."""
    match status:
        case AuthorizationStatus.CREATED:
            return Outcome.DONE
        case AuthorizationStatus.PENDING:
            return Outcome.PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return Outcome.FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            # Money already taken against a hold we just asked for: unexpected.
            return Outcome.NEEDS_REVIEW
        case _:
            return Outcome.UNKNOWN


def void(status: object) -> str:
    """Releasing a hold: VOIDED is what was asked."""
    match status:
        case AuthorizationStatus.VOIDED:
            return Outcome.DONE
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return Outcome.PENDING
        case (AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED
              | AuthorizationStatus.DENIED):
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def capture(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return Outcome.DONE
        case CaptureStatus.PENDING:
            return Outcome.PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return Outcome.FAILED
        case CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            # Done and then undone: not a capture in effect.
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def refund(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return Outcome.DONE
        case RefundStatus.PENDING:
            return Outcome.PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def setup_token(status: object) -> str:
    match status:
        case PaymentTokenStatus.APPROVED | PaymentTokenStatus.VAULTED | PaymentTokenStatus.TOKENIZED:
            return Outcome.DONE
        case PaymentTokenStatus.CREATED:
            return Outcome.PENDING
        case PaymentTokenStatus.PAYER_ACTION_REQUIRED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def payment_token(status: object) -> str:
    """
    PaymentTokenResponse carries no status; its reader passes True only when
    both the token id and the card came back.
    """
    return Outcome.DONE if status is True else Outcome.UNKNOWN

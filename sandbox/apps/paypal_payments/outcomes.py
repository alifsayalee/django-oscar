"""
The one place a PayPal status becomes one of our outcomes.

Every member of each status enum is listed by name. Anything else (a value
newer than the SDK, or no status at all) is ``unknown``: neither done nor
failed.
"""

from paypal.core import UnsetType
from paypal.models import PaymentTokenResponse
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from .models import Outcome


def order_outcome(status: object) -> str:
    """Outcome of creating a PayPal order with no payment source (an order shell)."""
    match status:
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED | OrderStatus.COMPLETED:
            return Outcome.DONE
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return Outcome.PENDING
        case OrderStatus.VOIDED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def authorization_outcome(status: object) -> str:
    """Outcome of an authorize or reauthorize write."""
    match status:
        case AuthorizationStatus.CREATED:
            return Outcome.DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            # The hold existed and has already moved on to a capture.
            return Outcome.DONE
        case AuthorizationStatus.PENDING:
            return Outcome.PENDING
        case AuthorizationStatus.DENIED:
            return Outcome.FAILED
        case AuthorizationStatus.VOIDED:
            # Held, then released: no longer in effect.
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def void_outcome(status: object) -> str:
    """Outcome of a void write: done means the shopper's hold is released."""
    match status:
        case AuthorizationStatus.VOIDED:
            return Outcome.DONE
        case AuthorizationStatus.DENIED:
            # A denied authorization holds no funds; there is nothing to release.
            return Outcome.DONE
        case AuthorizationStatus.PENDING | AuthorizationStatus.CREATED:
            return Outcome.PENDING
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return Outcome.DONE
        case CaptureStatus.PENDING:
            return Outcome.PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return Outcome.FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            # Captured, then (partly) given back: the capture is no longer whole.
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return Outcome.DONE
        case RefundStatus.PENDING:
            return Outcome.PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def vault_outcome(token: PaymentTokenResponse) -> str:
    """A payment-token response carries no status member. It is done only when
    PayPal returns both the token id and the vaulted card it now holds."""
    if isinstance(token.id, UnsetType) or not token.id:
        return Outcome.UNKNOWN
    source = token.payment_source
    if isinstance(source, UnsetType) or isinstance(source.card, UnsetType):
        return Outcome.UNKNOWN
    return Outcome.DONE


def status_text(status: object) -> str:
    if isinstance(status, UnsetType) or status is None:
        return ""
    return str(status)

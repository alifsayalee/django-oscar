"""
The only place a PayPal status becomes one of this app's outcomes.

Every member of each status enum is named. A value the SDK does not list (the
enums are open), or a missing status, maps to ``unknown``: neither done nor
failed.
"""
from datetime import datetime
from decimal import Decimal

from paypal.core import UnsetType
from paypal.models import Money
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from .models import PaymentOperation

DONE = PaymentOperation.DONE
PENDING = PaymentOperation.PENDING
FAILED = PaymentOperation.FAILED
UNKNOWN = PaymentOperation.UNKNOWN


def order_outcome(status: object) -> str:
    """Status of the PayPal order returned by create_order (before looking at
    the authorization it may carry)."""
    match status:
        case OrderStatus.COMPLETED:
            return DONE  # the authorization's own status decides; see authorization_outcome
        case OrderStatus.APPROVED | OrderStatus.CREATED | OrderStatus.SAVED:
            return PENDING
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def authorization_outcome(status: object) -> str:
    """Outcome of an authorize / reauthorize write."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return DONE  # the hold succeeded and is already being taken
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """Outcome of a void write: its goal is that nothing stays held."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return DONE
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # money already moved; the hold cannot be released
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED:
            return DONE  # a partial refund leaves the capture standing
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED | CaptureStatus.REFUNDED:
            return FAILED  # REFUNDED: taken, then fully undone
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


def status_text(status: object) -> str:
    if isinstance(status, UnsetType) or status is None:
        return ""
    return str(status)


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def parse_time(value: object) -> datetime | None:
    """PayPal times are RFC 3339 with ``Z`` or an offset (both seen)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: object) -> tuple[Decimal | None, str | None]:
    if isinstance(value, Money):
        try:
            return Decimal(value.value), value.currency_code
        except ArithmeticError:
            return None, value.currency_code
    return None, None

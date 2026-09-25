"""
The one place a PayPal status becomes ours: ``done``, ``pending``, ``failed``
or ``unknown``.

Every member of each status enum is listed by name. A value PayPal adds later
(the enums are open, so it arrives as a plain ``str``) or a missing status is
``unknown`` - never done.
"""
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
UNKNOWN = 'unknown'


def order_outcome(status: object) -> str:
    """``orders.create_order``: done means PayPal finished the order in one step."""
    match status:
        case OrderStatus.COMPLETED:
            return DONE
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return PENDING
        case OrderStatus.VOIDED | OrderStatus.PAYER_ACTION_REQUIRED:
            return FAILED
        case _:
            return UNKNOWN


def authorization_outcome(status: object) -> str:
    """Authorize / reauthorize: done means the hold is in place now."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case (AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED
              | AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED):
            # Refused, or no longer a hold that can be taken.
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """``payments.void_payment``: the release of the hold is its done."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.PENDING | AuthorizationStatus.CREATED:
            return PENDING
        case (AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED
              | AuthorizationStatus.DENIED):
            return FAILED
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    """``payments.capture_authorized_payment``: done means the money is taken."""
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            # Taken and then (partly) given back: not a capture in effect.
            return FAILED
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    """``payments.refund_captured_payment``: the money returned is its done."""
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def wire(status: object) -> str:
    """The status as PayPal sent it, for storage; '' when absent."""
    return str(status) if isinstance(status, str) else ''

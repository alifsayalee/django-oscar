"""
The one place a PayPal status becomes one of our outcomes.

Outcomes are ``done``, ``pending`` (PayPal accepted it and has not finished),
``failed`` (refused, or done and later undone) and ``unknown`` (a value this
code does not list, or no value at all). Only ``done`` is ever success.
"""

from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

DONE, PENDING, FAILED, UNKNOWN = "done", "pending", "failed", "unknown"


def authorization_outcome(status: object) -> str:
    """For a write that asks for a hold (authorize, reauthorize)."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return UNKNOWN  # not a state a fresh hold can be in
        case _:
            return UNKNOWN


def order_outcome(order_status: object, authorization_status: object) -> str:
    """For ``create_order`` with intent AUTHORIZE: the order, then its hold."""
    match order_status:
        case OrderStatus.COMPLETED:
            return authorization_outcome(authorization_status)
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return PENDING
        case OrderStatus.VOIDED:
            return FAILED
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return FAILED  # needs a browser approval, which this API does not support
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """For ``void_payment``: releasing the hold is what was asked for."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # money already moved; it can only be refunded
        case AuthorizationStatus.DENIED:
            return UNKNOWN
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
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return FAILED  # taken and then (partly) given back: not "captured" now
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
    """A status as the wire value PayPal sent ('' when absent)."""
    if isinstance(status, str):
        return str.__str__(status)
    return ""

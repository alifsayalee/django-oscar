"""
Each PayPal write step's status, mapped by name onto done / pending / failed /
unknown, and the ONE function that turns an outcome into the caller's HTTP status.
"""

from pay_pal_server_sdk.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from .models import Outcome

DONE, PENDING, FAILED, UNKNOWN = Outcome.DONE, Outcome.PENDING, Outcome.FAILED, Outcome.UNKNOWN


def order_create_outcome(status: object) -> str:
    """``orders.create_order`` — Order.status."""
    match status:
        case OrderStatus.APPROVED | OrderStatus.COMPLETED:
            return DONE
        case OrderStatus.CREATED | OrderStatus.SAVED:
            return PENDING
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return FAILED  # needs a shopper in a browser: not supported here
        case OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def authorization_outcome(status: object) -> str:
    """``orders.authorize_order`` / ``payments.reauthorize_payment`` — the hold's status."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED:
            return FAILED
        case AuthorizationStatus.VOIDED:
            return FAILED  # placed, then undone: no hold is in effect
        case _:
            # CAPTURED / PARTIALLY_CAPTURED (the hold was already converted) and
            # anything newer than this SDK: neither done nor failed.
            return UNKNOWN


def void_outcome(status: object) -> str:
    """``payments.void_payment`` — the undoing step's own mapper: VOIDED is its done."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    """``payments.capture_authorized_payment`` — CapturedPayment.status."""
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return FAILED  # done, then (partly) undone
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    """``payments.refund_captured_payment`` — Refund.status."""
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


# ``vault.create_payment_token`` declares no status member: the vaulted card
# entity in the response is the evidence. The reader reports this marker only
# when both the token id and the card entity are present.
CARD_VAULTED = "CARD_VAULTED"


def vault_outcome(status: object) -> str:
    return DONE if status == CARD_VAULTED else UNKNOWN


# ``vault.delete_payment_token`` returns no body; the raw response's 2xx status
# code is PayPal's word that the token is gone (it also answers 2xx for an
# already-absent token).
def delete_outcome(status: object) -> str:
    if isinstance(status, int) and 200 <= status < 300:
        return DONE
    return UNKNOWN


def http_status(outcome: str) -> int:
    """The ONE place an outcome becomes the caller's HTTP status."""
    match outcome:
        case Outcome.DONE:
            return 200  # the only success
        case Outcome.PENDING | Outcome.SENDING:
            return 202  # accepted, not done
        case Outcome.FAILED | Outcome.NEEDS_REVIEW:
            return 409  # refused, or not as asked
        case _:
            return 504  # may have happened: never "not done"

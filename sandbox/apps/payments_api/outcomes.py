"""
PayPal statuses -> this app's outcomes -> the caller's HTTP answer.

Every step maps its OWN status enum, member by member. A member not listed --
including a value newer than the SDK, which arrives as a plain ``str`` -- is
``unknown``: neither done nor failed.
"""
from typing import Any

from django.http import JsonResponse
from paypal.models.enums import AuthorizationStatus, CaptureStatus, CardVerificationStatus, OrderStatus, RefundStatus

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
UNKNOWN = 'unknown'
SENDING = 'sending'
NEEDS_REVIEW = 'needs_review'


def authorization_outcome(status: object) -> str:
    """Authorize / reauthorize: is a hold on the shopper's money in place?"""
    match status:
        case AuthorizationStatus.CREATED | AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED:
            return FAILED
        case AuthorizationStatus.VOIDED:
            return FAILED  # the hold existed and was released: not in effect
        case _:
            return UNKNOWN


def order_outcome(status: object) -> str:
    """A PayPal order that carries no authorization yet."""
    match status:
        case OrderStatus.APPROVED | OrderStatus.CREATED | OrderStatus.SAVED:
            return PENDING  # needs the authorize-order step
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return FAILED  # a browser challenge; this API does not support it
        case OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN  # includes COMPLETED without an authorization


def capture_outcome(status: object) -> str:
    """Capture: has the money been taken?"""
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED:
            return FAILED  # taken and given back in full: no longer in effect
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """Void (cancel): its done is the released hold."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    """Refund: its done is the money returned."""
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def vault_token_outcome(status: object) -> str:
    """Vault token creation. The token has no status member; ``status`` here is
    the card's ``verification_status`` when PayPal verified it, else the marker
    ``'stored'`` that the reader sets once the token id and card are present."""
    match status:
        case CardVerificationStatus.FAILED:
            return FAILED
        case CardVerificationStatus.VERIFIED | 'stored':
            return DONE
        case _:
            return UNKNOWN


# One place turns an outcome into the caller's HTTP status.
def answer(outcome: str, body: dict[str, Any], *, done_status: int = 200,
           failed_status: int = 409) -> JsonResponse:
    match outcome:
        case 'done':
            status = done_status  # the only success
        case 'pending' | 'sending':
            status = 202  # accepted, not done yet
        case 'failed' | 'needs_review':
            status = failed_status
        case _:
            status = 504  # may have happened: never reported as "not done"
    return JsonResponse({'outcome': outcome, **body}, status=status)

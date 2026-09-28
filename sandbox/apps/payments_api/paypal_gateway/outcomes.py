"""PayPal status members -> this app's outcomes.

Each write step reads its OWN status enum by name. ``done`` is the only success; anything a mapper
does not list is ``unknown`` (neither done nor failed).
"""

from __future__ import annotations

from typing import Literal

from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

Outcome = Literal["sending", "done", "pending", "failed", "needs_review", "unknown"]


def hold_outcome(status: object) -> Outcome:
    """A new or renewed authorization (create-order with intent AUTHORIZE, reauthorize): done = hold in place."""
    match status:
        case AuthorizationStatus.CREATED:
            return "done"
        case AuthorizationStatus.PENDING:
            return "pending"
        case AuthorizationStatus.DENIED:
            return "failed"
        case AuthorizationStatus.VOIDED:
            return "failed"  # held, then released: no longer in effect
        case _:
            # CAPTURED / PARTIALLY_CAPTURED are not what a hold step asked for; unlisted values and UNSET
            # are unknown too.
            return "unknown"


class OrderWithoutHold:
    """The create-order answer carried no authorization; its status is the PayPal ORDER's status.

    Kept as a distinct type because OrderStatus and AuthorizationStatus share wire values ("CREATED",
    "VOIDED") and str-enum members compare equal by value.
    """

    __slots__ = ("status",)

    def __init__(self, status: object) -> None:
        self.status = status


def order_without_hold_outcome(status: object) -> Outcome:
    match status:
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return "pending"  # an order exists but nothing is held yet
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return "failed"  # needs a browser challenge; this integration does not build one
        case OrderStatus.VOIDED:
            return "failed"
        case _:
            return "unknown"  # COMPLETED without an authorization, unlisted values, UNSET


def pay_outcome(status: object) -> Outcome:
    if isinstance(status, OrderWithoutHold):
        return order_without_hold_outcome(status.status)
    return hold_outcome(status)


def capture_outcome(status: object) -> Outcome:
    match status:
        case CaptureStatus.COMPLETED:
            return "done"
        case CaptureStatus.PENDING:
            return "pending"
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return "failed"
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return "failed"  # captured, then (partly) given back: not "taken" any more
        case _:
            return "unknown"


def captured_money_landed(status: object) -> bool:
    """For bookkeeping when re-reading a capture after refunds: did the money get taken at all?"""
    return status in (CaptureStatus.COMPLETED, CaptureStatus.PARTIALLY_REFUNDED, CaptureStatus.REFUNDED)


def void_outcome(status: object) -> Outcome:
    """A cancel's own mapper: its done is the undone state."""
    match status:
        case AuthorizationStatus.VOIDED:
            return "done"
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return "failed"  # too late: the money was taken
        case _:
            return "unknown"


def refund_outcome(status: object) -> Outcome:
    """A refund asks for the undoing itself, so COMPLETED is its done."""
    match status:
        case RefundStatus.COMPLETED:
            return "done"
        case RefundStatus.PENDING:
            return "pending"
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return "failed"
        case _:
            return "unknown"


def vault_outcome(status: object) -> Outcome:
    """PaymentTokenResponse declares no status member; the read() for it passes True when both the token
    id and the card description came back (the token exists), and anything else is unknown."""
    return "done" if status is True else "unknown"


HTTP_FOR_OUTCOME: dict[str, int] = {
    "done": 200,
    "pending": 202,
    "sending": 202,
    "failed": 409,
    "needs_review": 409,
    "unknown": 504,
}


def http_status_for(outcome: str) -> int:
    """The ONE place an outcome becomes the caller's HTTP status. Success comes from ``done`` alone."""
    return HTTP_FOR_OUTCOME.get(outcome, 504)

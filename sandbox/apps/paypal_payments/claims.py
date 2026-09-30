"""
Claims that make a PayPal write happen at most once per operation.

Order of every guarded write: claim (a committed insert the database refuses
twice) -> PayPal call carrying the claim's request id -> record the result.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .errors import PaymentError
from .models import OperationClaim

# An IN_PROGRESS claim older than this belongs to a request that died between
# claiming and recording (a crashed worker). It is then treated like an unknown
# outcome: taken over and re-sent with the same PayPal-Request-Id.
STALE_AFTER = timedelta(minutes=5)


def claim_or_existing(key, action):
    """
    Take the claim ``key`` for ``action``.

    Returns ``(claim, True)`` when the caller now owns the operation and must
    call PayPal with ``claim.request_id`` (a new claim, or a takeover of one
    whose outcome was never learned), or ``(claim, False)`` when the operation
    already succeeded and ``claim.result`` describes it. Raises 409 when the
    same operation is running right now, or when the key is held by a
    different action.
    """
    try:
        with transaction.atomic():
            return OperationClaim.objects.create(key=key, result={"action": action}), True
    except IntegrityError:
        pass

    existing = OperationClaim.objects.get(key=key)
    if existing.result.get("action") != action:
        raise PaymentError(
            409, "conflicting_operation",
            f"This order already has a '{existing.result.get('action')}' operation, so '{action}' is not possible.",
        )
    if existing.state == OperationClaim.SUCCEEDED:
        return existing, False
    if _take_over(existing):
        existing.refresh_from_db()
        return existing, True
    raise PaymentError(
        409, "operation_in_progress",
        "The same operation is already being processed; retry in a moment.",
    )


def _take_over(claim):
    resumable = OperationClaim.objects.filter(pk=claim.pk, state=OperationClaim.OUTCOME_UNKNOWN)
    if not resumable.update(state=OperationClaim.IN_PROGRESS):
        resumable = OperationClaim.objects.filter(
            pk=claim.pk,
            state=OperationClaim.IN_PROGRESS,
            date_updated__lt=timezone.now() - STALE_AFTER,
        )
        if not resumable.update(state=OperationClaim.IN_PROGRESS, date_updated=timezone.now()):
            return False
    return True


def release(claim):
    """PayPal definitively refused the call: free the key so it can be tried again."""
    OperationClaim.objects.filter(pk=claim.pk).delete()


def mark_unknown(claim):
    """PayPal may have acted: keep the key, resumable with the same request id."""
    OperationClaim.objects.filter(pk=claim.pk).update(
        state=OperationClaim.OUTCOME_UNKNOWN, date_updated=timezone.now()
    )


def complete(claim, **result):
    claim.state = OperationClaim.SUCCEEDED
    claim.result = {**claim.result, **result}
    claim.save(update_fields=["state", "result", "date_updated"])


def settle_failure(claim, error):
    """Release or keep ``claim`` according to whether ``error`` may have landed at PayPal."""
    if isinstance(error, PaymentError) and not error.outcome_unknown:
        release(claim)
    else:
        # Includes unexpected exceptions: without proof that nothing reached
        # PayPal, keep the claim so a retry resumes rather than repeats.
        mark_unknown(claim)

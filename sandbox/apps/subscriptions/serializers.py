"""Turn Maxio SDK models into JSON-safe dicts for our API responses.

Two hazards handled here (per ``python-models``):

* ``UNSET`` is not ``None`` and nothing outside the SDK can serialize it — every member is read
  through :func:`_present`, which resolves ``UNSET`` (and ``None``) to a chosen default before it
  crosses our boundary.
* Maxio state is an *open* enum: an unknown wire value arrives as a plain ``str``.  We enumerate
  the members we know by their wire value and default the rest to ``unknown`` — never a blanket
  "failed".
"""

from decimal import Decimal

from maxio_advanced_billing.core import UNSET

# Subscription states grouped by outcome (wire values). Enumerated, not defaulted, so a state Maxio
# adds later reads as "unknown" rather than being silently mis-classified (python-calling-endpoints).
_DONE_STATES = {"active", "trialing"}
_PENDING_STATES = {
    "pending",
    "assessing",
    "awaiting_signup",
    "soft_failure",
    "past_due",
    "on_hold",
    "paused",
}
_NOT_LIVE_STATES = {
    "failed_to_create",
    "canceled",
    "expired",
    "trial_ended",
    "unpaid",
    "suspended",
}


def _present(value, default=None):
    """Resolve an SDK member to a plain Python value, mapping ``UNSET``/``None`` to ``default``."""
    if value is UNSET or value is None:
        return default
    return value


def state_value(state):
    """The wire string for a (possibly open-enum) subscription state, or ``None``."""
    state = _present(state)
    if state is None:
        return None
    # str-enum members stringify to their wire value; a plain str passes through.
    return str(state)


def outcome_from_state(state):
    """Map a subscription state to our outcome vocabulary: done/pending/failed/unknown."""
    s = state_value(state)
    if s in _DONE_STATES:
        return "done"
    if s in _PENDING_STATES:
        return "pending"
    if s in _NOT_LIVE_STATES:
        return "failed"
    return "unknown"


def is_live(state):
    """True when a subscription is not terminal — used by the duplicate-subscription guard."""
    return outcome_from_state(state) in ("done", "pending")


def _iso(dt):
    dt = _present(dt)
    return dt.isoformat() if dt is not None else None


def _money(cents, currency="USD"):
    cents = _present(cents)
    if cents is None:
        return None
    # price_in_cents is a plain int; format as a decimal string. USD/EUR scale (2). The seeded
    # plans are USD; currency is surfaced so a caller is never guessing.
    amount = (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))
    return {
        "amountInCents": cents,
        "amount": str(amount),
        "currency": currency,
        "formatted": f"{amount} {currency}",
    }


def serialize_plan(product):
    """A plan entry for ``GET /api/subscription-plans``. Carries its own ``planHandle``."""
    price = _money(product.price_in_cents)
    return {
        "planHandle": _present(product.handle),
        "productId": _present(product.id),
        "name": _present(product.name),
        "description": _present(product.description),
        "price": price,
        "interval": _present(product.interval),
        "intervalUnit": state_value(product.interval_unit),
    }


def serialize_subscription(subscription, *, claim=None):
    """A subscription detail block for the subscribe response and ``GET /api/my-subscriptions``.

    ``current_period_ends_at`` is Maxio's next-billing date for these plans; both it and
    ``next_assessment_at`` are surfaced so the caller is not guessing which one to read.
    """
    state = subscription.state
    product = _present(subscription.product)
    body = {
        "subscriptionId": _present(subscription.id),
        "state": state_value(state),
        "outcome": outcome_from_state(state),
        "planHandle": _present(product.handle) if product is not None else None,
        "planName": _present(product.name) if product is not None else None,
        "price": _money(subscription.current_billing_amount_in_cents),
        "nextBillingAt": _iso(subscription.current_period_ends_at),
        "nextAssessmentAt": _iso(subscription.next_assessment_at),
        "currentPeriodStartedAt": _iso(subscription.current_period_started_at),
        "createdAt": _iso(subscription.created_at),
    }
    if claim is not None:
        body["claimStatus"] = claim.status
        body["reference"] = claim.reference
    return body

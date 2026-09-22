"""Shape Maxio SDK models into plain JSON-safe dicts for our HTTP responses.

SDK models carry the ``UNSET`` sentinel and enum objects that a JSON encoder
cannot serialize, so nothing from the SDK is handed to the response encoder
directly — every value crosses this boundary first, resolving ``UNSET`` to
``None`` and enums/datetimes to strings.
"""

from maxio_advanced_billing.core import UNSET


def _val(value):
    """Resolve UNSET/None to None, leaving everything else untouched."""
    return None if value is UNSET or value is None else value


def _cents_to_amount(cents):
    """Render an integer cent amount as a fixed 2-dp decimal string, or None."""
    cents = _val(cents)
    if cents is None:
        return None
    sign = '-' if cents < 0 else ''
    cents = abs(int(cents))
    return '{}{}.{:02d}'.format(sign, cents // 100, cents % 100)


def _iso(dt):
    """Render an SDK date-time (a ``datetime``) as an ISO-8601 string, or None."""
    dt = _val(dt)
    if dt is None:
        return None
    isoformat = getattr(dt, 'isoformat', None)
    return isoformat() if callable(isoformat) else str(dt)


def _enum_str(value):
    """Render an open-enum value as its wire string, or None."""
    value = _val(value)
    return str(value) if value is not None else None


def serialize_plan(product):
    """A plan (Maxio product) for GET /api/subscription-plans.

    Carries its own ``planHandle`` so the flow can be driven end to end.
    """
    family = _val(product.product_family)
    price_cents = _val(product.price_in_cents)
    return {
        'planHandle': _val(product.handle),
        'name': _val(product.name),
        'description': _val(product.description),
        'productId': _val(product.id),
        'priceInCents': price_cents,
        'price': _cents_to_amount(price_cents),
        'interval': _val(product.interval),
        'intervalUnit': _enum_str(product.interval_unit),
        'currency': 'USD',
        'productFamilyHandle': _val(family.handle) if family else None,
        'paymentMethodRequired': bool(_val(product.require_credit_card)),
    }


def serialize_subscription(subscription):
    """A subscription for POST /api/subscriptions and GET /api/my-subscriptions."""
    product = _val(subscription.product)
    billing_cents = _val(subscription.current_billing_amount_in_cents)
    if billing_cents is None:
        billing_cents = _val(subscription.product_price_in_cents)
    return {
        'subscriptionId': _val(subscription.id),
        'state': _enum_str(subscription.state),
        'planHandle': _val(product.handle) if product else None,
        'planName': _val(product.name) if product else None,
        'nextBillingAt': _iso(subscription.next_assessment_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'currentBillingAmountInCents': billing_cents,
        'currentBillingAmount': _cents_to_amount(billing_cents),
        'currency': _val(subscription.currency) or 'USD',
        'reference': _val(subscription.reference),
        'createdAt': _iso(subscription.created_at),
        'activatedAt': _iso(subscription.activated_at),
    }

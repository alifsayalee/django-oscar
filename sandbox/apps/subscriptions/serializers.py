"""Map Maxio SDK models onto plain JSON-safe dicts for our API.

Every SDK optional member is either a value, ``UNSET`` or (for nullable fields)
``None``. ``UNSET`` is not JSON-serializable and must never cross our boundary
(python-models), so each accessor resolves it to ``None`` here.
"""

from maxio_advanced_billing.core import UNSET


def _present(value):
    return value is not UNSET and value is not None


def _int(value):
    return int(value) if _present(value) else None


def _str(value):
    return str(value) if _present(value) else None


def _enum(value):
    # Open enums decode as an Enum member or a plain str; str() yields the wire
    # value either way (the SDK sets __str__ = str.__str__ on str-enums).
    return str(value) if _present(value) else None


def _dt(value):
    # Date/time members are datetime subtypes (RFC3339DateTime); ISO-8601 out.
    if not _present(value):
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _format_price(cents):
    # Plans here are USD (2-decimal). Surface both the raw cents and a display
    # string; callers that need exact money use priceInCents.
    if not _present(cents):
        return None
    return f'${int(cents) / 100:.2f}'


def serialize_plan(product):
    """Serialize a Maxio ``Product`` (a subscribable plan)."""
    return {
        'planHandle': _str(product.handle),
        'productId': _int(product.id),
        'name': _str(product.name),
        'description': _str(product.description),
        'priceInCents': _int(product.price_in_cents),
        'priceFormatted': _format_price(product.price_in_cents),
        'interval': _int(product.interval),
        'intervalUnit': _enum(product.interval_unit),
    }


def serialize_subscription(subscription):
    """Serialize a Maxio ``Subscription``; ``subscriptionId`` is top-level."""
    product = subscription.product if _present(subscription.product) else None
    customer = subscription.customer if _present(subscription.customer) else None
    return {
        'subscriptionId': _int(subscription.id),
        'state': _enum(subscription.state),
        'planHandle': _str(product.handle) if product is not None else None,
        'planName': _str(product.name) if product is not None else None,
        'priceInCents': _int(subscription.product_price_in_cents),
        'nextBillingDate': _dt(subscription.current_period_ends_at),
        'currentPeriodEndsAt': _dt(subscription.current_period_ends_at),
        'nextAssessmentAt': _dt(subscription.next_assessment_at),
        'createdAt': _dt(subscription.created_at),
        'customerId': _int(customer.id) if customer is not None else None,
    }

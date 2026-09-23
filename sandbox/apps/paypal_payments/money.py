"""
Currency-aware amount handling.

PayPal carries money as a string scaled to the currency, so amounts are
quantized with the currency's own exponent (never a literal two decimals)
and compared as ``Decimal``.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_EXPONENTS = {"JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "KWD": 3, "BHD": 3, "TND": 3}


def currency():
    code = (settings.PAYPAL_CURRENCY or "").strip().upper()
    if len(code) != 3:
        raise ImproperlyConfigured(
            "PAYPAL_CURRENCY must be set to a 3-letter ISO-4217 code"
        )
    return code


def exponent(code):
    return _EXPONENTS.get(code, 2)


def quantize(value, code):
    return Decimal(value).quantize(
        Decimal(1).scaleb(-exponent(code)), rounding=ROUND_HALF_UP
    )


def to_wire(value, code):
    """Render an amount the way PayPal expects it: ``"10.00"``, ``"1000"``, ``"1.234"``."""
    return str(quantize(value, code))


def parse_amount(raw, code):
    """
    Parse a caller-supplied amount. Returns ``None`` when it is not a positive
    number representable in the currency's scale.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    if value != quantize(value, code):
        return None
    return value


def from_wire(money):
    """Decode an SDK ``Money`` into ``(Decimal, currency)``; ``None`` if absent/unreadable."""
    if money is None or not isinstance(getattr(money, "value", None), str):
        return None
    try:
        return Decimal(money.value), money.currency_code
    except InvalidOperation:
        return None

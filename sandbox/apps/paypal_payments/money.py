"""
Money helpers. PayPal carries amounts as strings scaled to the currency, so
amounts are kept as ``Decimal`` and formatted with the currency's own exponent.
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal.core import UnsetType
from paypal.models import Money

# Currencies whose minor unit is not two digits. Every other currency: 2.
_EXPONENT = {"JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3, "TND": 3}


def configured_currency() -> str:
    currency = str(settings.PAYPAL_CURRENCY or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ImproperlyConfigured(
            "PAYPAL_CURRENCY must be set to a three-letter ISO-4217 code."
        )
    return currency


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def is_exact(value: Decimal, currency: str) -> bool:
    """True when ``value`` needs no rounding to be expressed in ``currency``."""
    return value == value.quantize(quantum(currency))


def to_str(value: Decimal, currency: str) -> str:
    return str(Decimal(value).quantize(quantum(currency)))


def money(value: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=to_str(value, currency))


def parse_amount(raw: object) -> Decimal | None:
    """Parse a caller-supplied amount; ``None`` when it is not a finite number."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def from_money(value: Money | UnsetType) -> tuple[Decimal, str] | None:
    """Read a PayPal ``Money`` back as ``(Decimal, currency)``; ``None`` if absent/unreadable."""
    if isinstance(value, UnsetType):
        return None
    amount = parse_amount(value.value)
    if amount is None:
        return None
    return amount, value.currency_code.upper()

"""
Currency-aware amounts for PayPal.

PayPal takes money as a string scaled to the currency. The scale belongs to
the currency, not to the number two: JPY has no minor unit, KWD has three.
"""
from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENTS = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0,
    'PYG': 0, 'RWF': 0, 'UGX': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
}


class AmountError(ValueError):
    """An amount that cannot be expressed exactly in the currency."""


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def exact(amount: Decimal, currency: str) -> Decimal:
    """Return ``amount`` at the currency's scale, refusing to round it."""
    scaled = amount.quantize(quantum(currency))
    if scaled != amount:
        raise AmountError('%s cannot be expressed exactly in %s' % (amount, currency))
    return scaled


def to_paypal(amount: Decimal, currency: str) -> str:
    """Format an amount the way PayPal's ``value`` fields expect it."""
    return str(exact(amount, currency))


def parse(value: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount (string or number) into an exact Decimal."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AmountError('amount must be a decimal string such as "10.00"')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise AmountError('amount must be a decimal string such as "10.00"') from exc
    if not amount.is_finite() or amount <= 0:
        raise AmountError('amount must be greater than zero')
    return exact(amount, currency)

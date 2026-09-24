"""Currency-aware conversion between Decimal amounts and PayPal's string amounts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0,
    'PYG': 0, 'RWF': 0, 'UGX': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
}


class AmountError(ValueError):
    """An amount that is malformed or cannot be expressed in the currency."""


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def to_paypal(value: Decimal, currency: str) -> str:
    """Format ``value`` as PayPal expects it, refusing to round money silently."""
    q = quantum(currency)
    quantized = value.quantize(q)
    if quantized != value:
        raise AmountError(f'{value} cannot be expressed in {currency} without rounding')
    return str(quantized)


def from_paypal(value: str) -> Decimal:
    """Parse an amount string returned by PayPal."""
    try:
        return Decimal(value)
    except InvalidOperation as e:
        raise AmountError(f'unreadable amount {value!r}') from e


def parse_positive(value: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount; it must be positive and fit the currency."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AmountError('amount must be a string such as "12.50"')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as e:
        raise AmountError('amount must be a decimal number such as "12.50"') from e
    if not amount.is_finite() or amount <= 0:
        raise AmountError('amount must be greater than zero')
    if amount.quantize(quantum(currency)) != amount:
        raise AmountError(f'amount has more decimal places than {currency} allows')
    return amount

"""Money formatting for PayPal: amounts travel as strings scaled to the currency."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    "CLF": 4,
    "UYW": 4,
}


class AmountError(ValueError):
    """An amount that cannot be represented exactly in the currency."""


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def to_wire(value: Decimal, currency: str) -> str:
    """Format ``value`` for PayPal, refusing to round it: the amount held must equal the total."""
    scaled = value.quantize(quantum(currency))
    if scaled != value:
        raise AmountError(f"{value} cannot be expressed exactly in {currency}")
    return str(scaled)


def parse(value: object, currency: str) -> Decimal:
    """Parse a caller- or provider-supplied amount; it must be positive and exact in the currency."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise AmountError(f"{value!r} is not a valid amount") from e
    if not amount.is_finite() or amount <= 0:
        raise AmountError(f"{value!r} must be a positive amount")
    if amount.quantize(quantum(currency)) != amount:
        raise AmountError(f"{value} has more decimal places than {currency} allows")
    return amount

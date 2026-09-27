"""
Money handling for the PayPal API: integer minor units internally, a
currency-scaled decimal string on the wire.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def exponent(currency: str) -> int:
    return EXPONENT.get(currency.upper(), 2)


def to_minor(value: Decimal | str, currency: str) -> int:
    """Convert a decimal amount to integer minor units, rejecting sub-unit precision."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(f"Not a valid amount: {value!r}") from e
    if not amount.is_finite():
        raise ValueError(f"Not a valid amount: {value!r}")
    scaled = amount.scaleb(exponent(currency))
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{value} has more decimal places than {currency} allows ({exponent(currency)})"
        )
    return int(scaled)


def round_to_minor(value: Decimal, currency: str) -> int:
    """Round a computed amount (e.g. a catalogue total) to minor units."""
    return int(value.scaleb(exponent(currency)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    places = exponent(currency)
    return Decimal(minor).scaleb(-places).quantize(Decimal(1).scaleb(-places))


def wire(minor: int, currency: str) -> str:
    """The amount string PayPal expects, e.g. 1234 USD -> "12.34", 1000 JPY -> "1000"."""
    return str(from_minor(minor, currency))

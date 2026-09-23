"""Currency-aware money formatting.

PayPal models money as a string scaled to the currency, and the scale belongs to
the currency, not to the literal two. Format with the currency's own exponent so
a zero- or three-decimal currency is not silently moved by a factor of 100/10.
"""
from decimal import ROUND_HALF_UP, Decimal

# Currencies whose minor-unit exponent is not 2. Everything else uses 2.
_EXPONENT = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "HUF": 0, "TWD": 0,
    "KWD": 3, "BHD": 3, "TND": 3, "OMR": 3, "JOD": 3,
}


def exponent(currency: str) -> int:
    return _EXPONENT.get((currency or "").upper(), 2)


def quantize(value, currency: str) -> Decimal:
    """Quantize a Decimal/str/number to the currency's minor units."""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    q = Decimal(1).scaleb(-exponent(currency))
    return dec.quantize(q, rounding=ROUND_HALF_UP)


def to_wire(value, currency: str) -> str:
    """Format an amount as the string PayPal expects for this currency."""
    return str(quantize(value, currency))

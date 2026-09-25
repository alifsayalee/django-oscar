"""PayPal carries money as decimal strings scaled to the currency."""

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


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-exponent(currency)))


def to_wire(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


def parse(value: object) -> Decimal | None:
    """Parse a provider money string; ``None`` when absent or unreadable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

"""
Money helpers. PayPal carries amounts as strings scaled to the currency's
minor unit, so every amount crossing the boundary goes through here.
"""
from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def is_representable(value: Decimal, currency: str) -> bool:
    """True when ``value`` has no digits below the currency's minor unit."""
    return value == value.quantize(quantum(currency))


def to_wire(value: Decimal, currency: str) -> str:
    """Format an amount for PayPal; refuses to round silently."""
    if not is_representable(value, currency):
        raise ValueError("%s cannot be expressed in %s" % (value, currency))
    return str(value.quantize(quantum(currency)))


def parse_amount(raw: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount; ``ValueError`` when it is not a positive
    amount expressible in ``currency``."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("amount must be a decimal string such as \"10.00\"")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal string such as \"10.00\"") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if not is_representable(value, currency):
        raise ValueError("amount has more decimal places than %s allows" % currency)
    return value

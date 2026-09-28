"""Money as PayPal expects it: a decimal string scaled to the currency's minor units."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not use two.
EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def minor_unit(currency: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(currency.upper(), 2))


def is_exact(value: Decimal, currency: str) -> bool:
    """Whether ``value`` can be charged in ``currency`` without rounding."""
    return value == value.quantize(minor_unit(currency))


def to_paypal(value: Decimal, currency: str) -> str:
    """Format an amount for PayPal. Callers check ``is_exact`` first; this never rounds silently."""
    quantized = value.quantize(minor_unit(currency), rounding=ROUND_HALF_UP)
    if quantized != value:
        raise ValueError(f"{value} cannot be expressed exactly in {currency}")
    return str(quantized)


def parse_amount(raw: object) -> Decimal | None:
    """A positive, finite decimal from caller input, or None."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def same_money(a: str | Decimal | None, b: str | Decimal | None) -> bool:
    """Compare amounts as Decimal: "10.0" and "10.00" are the same money."""
    if a is None or b is None:
        return False
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return False

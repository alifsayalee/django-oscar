"""Money formatting for PayPal's string amounts."""

from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not use two decimal places.
EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(currency.upper(), 2))


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(quantum(currency))


def to_wire(value: Decimal, currency: str) -> str:
    """The amount string PayPal expects, e.g. ``Decimal("10") -> "10.00"`` for USD."""
    return str(quantize(value, currency))


def parse_amount(raw: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount; raises ``ValueError`` when it is not a positive amount
    expressible in the currency's minor units."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError("amount must be a decimal string")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal string") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if value != quantize(value, currency):
        raise ValueError(f"amount has more decimal places than {currency} allows")
    return quantize(value, currency)


def same_money(a_value: Decimal, a_currency: str, b_value: object, b_currency: object) -> bool:
    """Compare as Decimal: "10.0" and "10.00" are the same money."""
    if not isinstance(b_value, str) or not isinstance(b_currency, str):
        return False
    try:
        return (Decimal(b_value), b_currency.upper()) == (a_value, a_currency.upper())
    except InvalidOperation:
        return False

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def currency() -> str:
    code: str = settings.PAYPAL_CURRENCY
    return code.upper()


def quantum(currency_code: str) -> Decimal:
    return Decimal(1).scaleb(-_EXPONENT.get(currency_code.upper(), 2))


def quantize(value: Decimal, currency_code: str) -> Decimal:
    return value.quantize(quantum(currency_code), rounding=ROUND_HALF_UP)


def to_paypal(value: Decimal, currency_code: str) -> str:
    """The amount string PayPal expects, e.g. ``"10.00"`` for USD or ``"1000"`` for JPY."""
    return str(quantize(value, currency_code))


def from_paypal(value: str) -> Decimal:
    return Decimal(value)


def parse_amount(raw: object, currency_code: str) -> Decimal:
    """Parse a caller-supplied amount; raise ValueError unless it is positive and fits the currency."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("amount must be a decimal string such as \"10.00\"")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError("amount must be a decimal string such as \"10.00\"") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if value != quantize(value, currency_code):
        raise ValueError(f"amount has more decimal places than {currency_code} allows")
    return quantize(value, currency_code)

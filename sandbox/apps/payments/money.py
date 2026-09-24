"""Money as PayPal expects it: a string scaled to the currency's minor units."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(quantum(currency), rounding=ROUND_HALF_UP)


def format_amount(value: Decimal, currency: str) -> str:
    """``Decimal("10")`` in USD -> ``"10.00"``; in JPY -> ``"10"``."""
    return str(quantize(value, currency))


def parse_amount(raw: object, currency: str) -> Decimal:
    """
    Parse a caller-supplied amount. It must be positive and must not carry
    more decimal places than the currency has; raises ``ValueError`` otherwise.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("amount must be a string such as \"12.50\"")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValueError("amount is not a number") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if value != value.quantize(quantum(currency)):
        raise ValueError(
            f"amount has more decimal places than {currency.upper()} allows"
        )
    return quantize(value, currency)


def same_money(a_value: object, a_currency: object, b_value: Decimal, b_currency: str) -> bool:
    """Compare an echoed amount with the one sent, as Decimals ("10.0" == "10.00")."""
    if not isinstance(a_value, str) or not isinstance(a_currency, str):
        return False
    try:
        return Decimal(a_value) == b_value and a_currency.upper() == b_currency.upper()
    except InvalidOperation:
        return False

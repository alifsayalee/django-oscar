from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-exponent(currency)))


def to_wire(value: Decimal, currency: str) -> str:
    """The string PayPal expects for ``Money.value``: ``"10.00"``, ``"1000"``."""
    return str(quantize(value, currency))


def parse_amount(raw: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount. Raises ``ValueError`` on anything that is
    not a positive amount expressible in the currency's minor unit."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("amount must be a decimal string such as \"10.00\"")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValueError("amount must be a decimal string such as \"10.00\"") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if quantize(value, currency) != value:
        raise ValueError("amount has more decimal places than %s allows" % currency)
    return quantize(value, currency)


def same_money(a: Decimal | None, a_currency: str | None, b: Decimal, b_currency: str) -> bool:
    return a is not None and a_currency == b_currency and Decimal(a) == Decimal(b)

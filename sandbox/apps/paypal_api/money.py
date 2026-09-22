"""Currency-aware money formatting for the PayPal boundary.

PayPal models money as a *string* scaled to the currency's number of decimal
places (``"10.00"`` for USD, ``"1000"`` for JPY, ``"1.234"`` for KWD). Never
format with ``%f``/``round`` or a locale-dependent conversion — take the scale
from the currency and quantise a ``Decimal``.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ISO-4217 minor-unit exponents that are not the default of 2.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "UGX", "XAF", "XOF", "XPF"}
_THREE_DECIMAL = {"BHD", "KWD", "OMR", "TND", "IQD", "JOD", "LYD"}


def currency_exponent(currency_code: str) -> int:
    code = (currency_code or "").upper()
    if code in _ZERO_DECIMAL:
        return 0
    if code in _THREE_DECIMAL:
        return 3
    return 2


def quantize(amount: Decimal, currency_code: str) -> Decimal:
    exp = currency_exponent(currency_code)
    quant = Decimal(1).scaleb(-exp)  # 10**-exp as a Decimal
    return Decimal(amount).quantize(quant, rounding=ROUND_HALF_UP)


def format_amount(amount: Decimal, currency_code: str) -> str:
    """Return the PayPal wire string for ``amount`` in ``currency_code``."""
    return f"{quantize(amount, currency_code):f}"


def parse_amount(value: str, currency_code: str) -> Decimal:
    """Parse a PayPal money string back into a quantised ``Decimal``."""
    return quantize(Decimal(str(value)), currency_code)

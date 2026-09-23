"""Currency-aware money formatting for PayPal amounts.

PayPal models money as a decimal *string* scaled to the currency's exponent
(``"10.00"`` for USD). Never format money with ``%f``/``round()`` or a
locale-dependent conversion — build and compare with :class:`decimal.Decimal`
and quantize to the exponent the currency actually uses.
"""
from decimal import ROUND_HALF_UP, Decimal

# Currencies whose minor-unit exponent is not 2. Everything else is 2.
# (Per python-models: a hardcoded 2 moves a JPY amount by 100 and rejects KWD.)
_EXPONENT = {
    "JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "CLP": 0, "VND": 0,
    "KWD": 3, "BHD": 3, "TND": 3,
}


def exponent_for(currency: str) -> int:
    return _EXPONENT.get((currency or "").upper(), 2)


def quantize(value, currency: str) -> Decimal:
    """Return ``value`` as a Decimal quantized to the currency's exponent."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    places = exponent_for(currency)
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def format_amount(value, currency: str) -> str:
    """Format ``value`` as the wire string PayPal expects for ``currency``."""
    return str(quantize(value, currency))


def amounts_equal(a, b, currency: str) -> bool:
    """True when two amounts are equal money in ``currency`` (to the cent).

    ``"10.0"`` and ``"10.00"`` are the same money; comparing the strings says
    they differ, so both sides are quantized before comparison.
    """
    return quantize(a, currency) == quantize(b, currency)

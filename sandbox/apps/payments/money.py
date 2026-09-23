"""
Money and time helpers for the PayPal wire format.

PayPal carries amounts as strings scaled to the currency's minor unit; the
scale belongs to the currency, not to the number two.
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ISO-4217 currencies whose minor unit is not two digits
_EXPONENTS = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "HUF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "TWD": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0,
    "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-exponent(currency)), rounding=ROUND_HALF_UP)


def to_wire(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


def parse_amount(raw: object, currency: str) -> Decimal:
    """
    Parse a caller-supplied amount. Raises ``ValueError`` for anything that is
    not a positive number with at most the currency's number of decimals.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("amount must be a decimal string such as \"10.00\"")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValueError("amount must be a decimal string such as \"10.00\"") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    if quantize(value, currency) != value:
        raise ValueError(
            "amount has more decimal places than %s allows (%d)" % (currency, exponent(currency))
        )
    return quantize(value, currency)


def parse_provider_time(raw: str) -> datetime | None:
    """
    Parse a PayPal timestamp (``2026-09-23T11:20:17Z``, ``...-07:00``,
    ``...+0000``, fractional seconds) into an aware UTC datetime.
    """
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_provider_time(value: datetime) -> str:
    """RFC 3339 with seconds, in UTC, as the reporting API requires."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

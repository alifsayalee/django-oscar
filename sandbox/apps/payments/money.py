"""Money as PayPal's wire format: a string scaled to the currency."""
from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(currency.upper(), 2))


def to_wire(value: Decimal, currency: str) -> str:
    """``Decimal('10')`` in USD -> ``'10.00'``; in JPY -> ``'10'``."""
    return str(value.quantize(quantum(currency)))


def parse_amount(raw: object, currency: str) -> Decimal:
    """
    Parse a caller-supplied amount. Raises ``ValueError`` for anything that is
    not a positive amount with no more decimal places than the currency has.
    """
    if isinstance(raw, float) or isinstance(raw, bool):
        raise ValueError('amount must be a decimal string such as "5.00"')
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError('amount must be a decimal string such as "5.00"') from None
    if not value.is_finite() or value <= 0:
        raise ValueError('amount must be greater than zero')
    if value != value.quantize(quantum(currency)):
        raise ValueError('amount has more decimal places than %s allows' % currency)
    return value


def from_wire(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None

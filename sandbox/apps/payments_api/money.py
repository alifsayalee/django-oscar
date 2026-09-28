"""Money as PayPal wants it: a decimal string scaled to the currency."""
from decimal import Decimal, InvalidOperation

# ISO 4217 minor units for every currency that does not have two.
EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0,
    'KRW': 0, 'PYG': 0, 'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0,
    'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


class InvalidAmount(ValueError):
    pass


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(currency, 2))


def is_representable(value: Decimal, currency: str) -> bool:
    """Whether ``value`` can be charged in ``currency`` without rounding."""
    return value == value.quantize(quantum(currency))


def to_wire(value: Decimal, currency: str) -> str:
    """Format an amount for PayPal; refuses to round money silently."""
    if not is_representable(value, currency):
        raise InvalidAmount(
            '%s cannot be expressed exactly in %s' % (value, currency))
    return str(value.quantize(quantum(currency)))


def parse(value: object, currency: str) -> Decimal:
    """Parse a caller-supplied amount string, exact to the currency."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as e:
        raise InvalidAmount('amount must be a decimal string') from e
    if not amount.is_finite() or amount <= 0:
        raise InvalidAmount('amount must be greater than zero')
    if not is_representable(amount, currency):
        raise InvalidAmount(
            'amount has more decimal places than %s allows' % currency)
    return amount.quantize(quantum(currency))

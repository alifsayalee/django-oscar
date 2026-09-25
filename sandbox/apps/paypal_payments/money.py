from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal.core import UnsetType
from paypal.models import Money

# ISO 4217 minor units for every currency that does not have two.
EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def currency() -> str:
    code = (getattr(settings, 'PAYPAL_CURRENCY', '') or '').strip().upper()
    if len(code) != 3:
        raise ImproperlyConfigured('PAYPAL_CURRENCY must be a three-letter ISO 4217 code.')
    return code


def quantum(code: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(code, 2))


def fmt(value: Decimal, code: str) -> str:
    return str(value.quantize(quantum(code)))


def representable(value: Decimal, code: str) -> bool:
    return value == value.quantize(quantum(code))


def parse_amount(raw: object, code: str) -> Decimal:
    """A caller-supplied amount; ValueError when not a positive amount in ``code``."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError('amount must be a decimal number')
    if not value.is_finite() or value <= 0 or not representable(value, code):
        raise ValueError('amount must be positive with at most %d decimal places'
                         % EXPONENT.get(code, 2))
    return value


def money_value(m: object) -> tuple[str | None, str | None]:
    """(value, currency) of an optional SDK Money member."""
    if isinstance(m, Money):
        return m.value, m.currency_code
    return None, None


def decimal_of(m: object) -> Decimal | None:
    if isinstance(m, UnsetType) or not isinstance(m, Money):
        return None
    return Decimal(m.value)

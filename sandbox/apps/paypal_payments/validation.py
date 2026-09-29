"""
Parsing of caller-supplied payment details. Error messages never echo a card
number or security code back.
"""
import re
from datetime import date

from django.views.decorators.debug import sensitive_variables

from .gateway import BillingAddress, CardDetails
from .services import ServiceError

_EXPIRY = re.compile(r'^(\d{4})-(\d{2})$')
_IDEMPOTENCY_KEY = re.compile(r'^[A-Za-z0-9._:-]{1,64}$')


def _invalid(message, field):
    return ServiceError(422, 'invalid_card', message, field=field)


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _optional_text(data, key, max_length):
    value = data.get(key)
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise _invalid('"%s" must be text of at most %d characters.' % (key, max_length), key)
    return value.strip() or None


def _billing_address(data):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise _invalid('"billingAddress" must be an object.', 'billingAddress')
    country = data.get('countryCode')
    if not isinstance(country, str) or not re.fullmatch(r'[A-Za-z]{2}', country):
        raise _invalid('"billingAddress.countryCode" must be a two-letter country code.',
                       'billingAddress.countryCode')
    return BillingAddress(
        country_code=country.upper(),
        address_line_1=_optional_text(data, 'addressLine1', 300),
        address_line_2=_optional_text(data, 'addressLine2', 300),
        admin_area_2=_optional_text(data, 'adminArea2', 120),
        admin_area_1=_optional_text(data, 'adminArea1', 300),
        postal_code=_optional_text(data, 'postalCode', 60),
    )


@sensitive_variables('data', 'number', 'security_code')
def parse_card(data):
    """Validate ``{"number", "expiry": "YYYY-MM", "securityCode", "name"?,
    "billingAddress"?}`` into CardDetails."""
    if not isinstance(data, dict):
        raise _invalid('"card" must be an object.', 'card')
    number = data.get('number')
    if not isinstance(number, str):
        raise _invalid('"card.number" is required.', 'card.number')
    number = re.sub(r'[\s-]', '', number)
    if not re.fullmatch(r'\d{12,19}', number) or not _luhn_ok(number):
        raise _invalid('"card.number" is not a valid card number.', 'card.number')

    expiry = data.get('expiry')
    match = _EXPIRY.match(expiry) if isinstance(expiry, str) else None
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise _invalid('"card.expiry" must be YYYY-MM.', 'card.expiry')
    today = date.today()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise _invalid('The card has expired.', 'card.expiry')

    security_code = data.get('securityCode')
    if not isinstance(security_code, str) or not re.fullmatch(r'\d{3,4}', security_code):
        raise _invalid('"card.securityCode" must be 3 or 4 digits.', 'card.securityCode')

    return CardDetails(
        number=number, expiry=expiry, security_code=security_code,
        name=_optional_text(data, 'name', 300),
        billing_address=_billing_address(data.get('billingAddress')),
    )


def parse_idempotency_key(value, *, required):
    if value is None or value == '':
        if required:
            raise ServiceError(422, 'idempotency_key_required',
                               'An Idempotency-Key header is required.')
        return None
    if not _IDEMPOTENCY_KEY.match(value):
        raise ServiceError(422, 'invalid_idempotency_key',
                           'Idempotency-Key must be 1-64 characters of letters, digits and ._:-')
    return value

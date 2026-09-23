"""
Request validation for the payments API.

Card input is checked here, before any SDK model is built from it, so that a
malformed card never reaches pydantic (whose errors echo input values) and no
card data ever appears in an error message.
"""

import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.utils import timezone


class InvalidRequest(Exception):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


_EXPIRY = re.compile(r"^(\d{4})-(\d{2})$")
_EXPIRY_SHORT = re.compile(r"^(\d{2})/(\d{2}|\d{4})$")


def _luhn_ok(number):
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _text(data, key, *, required=False, max_length=300):
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise InvalidRequest("%s is required." % key, key)
        return ""
    if not isinstance(value, str):
        raise InvalidRequest("%s must be a string." % key, key)
    value = value.strip()
    if len(value) > max_length:
        raise InvalidRequest("%s is too long." % key, key)
    return value


def parse_expiry(value):
    """Accepts ``YYYY-MM`` or ``MM/YY``; returns PayPal's ``YYYY-MM``."""
    if not isinstance(value, str):
        raise InvalidRequest("card.expiry must be YYYY-MM.", "card.expiry")
    value = value.strip()
    match = _EXPIRY.match(value)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        match = _EXPIRY_SHORT.match(value)
        if not match:
            raise InvalidRequest("card.expiry must be YYYY-MM.", "card.expiry")
        month = int(match.group(1))
        year = int(match.group(2))
        year = year + 2000 if year < 100 else year
    if not 1 <= month <= 12:
        raise InvalidRequest("card.expiry has an invalid month.", "card.expiry")
    now = timezone.now()
    if (year, month) < (now.year, now.month):
        raise InvalidRequest("card.expiry is in the past.", "card.expiry")
    return "%04d-%02d" % (year, month)


def parse_billing_address(data):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise InvalidRequest("card.billingAddress must be an object.", "card.billingAddress")
    country = _text(data, "countryCode", required=True, max_length=2).upper()
    if len(country) != 2 or not country.isalpha():
        raise InvalidRequest("card.billingAddress.countryCode must be a 2-letter code.",
                             "card.billingAddress.countryCode")
    address = {"country_code": country}
    for src, dest, limit in (
        ("line1", "address_line_1", 300),
        ("line2", "address_line_2", 300),
        ("city", "admin_area_2", 120),
        ("state", "admin_area_1", 300),
        ("postalCode", "postal_code", 60),
    ):
        value = _text(data, src, max_length=limit)
        if value:
            address[dest] = value
    return address


def parse_card(data):
    """
    Validate a card object from a request body and return the fields PayPal
    needs, in SDK member names. Error messages never contain card data.
    """
    if not isinstance(data, dict):
        raise InvalidRequest("card must be an object.", "card")
    number = data.get("number")
    if not isinstance(number, str):
        raise InvalidRequest("card.number is required.", "card.number")
    number = re.sub(r"[\s-]", "", number)
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise InvalidRequest("card.number is not a valid card number.", "card.number")
    security_code = data.get("securityCode", data.get("cvv"))
    if security_code is not None:
        if not isinstance(security_code, str) or not security_code.isdigit() or not 3 <= len(security_code) <= 4:
            raise InvalidRequest("card.securityCode must be 3 or 4 digits.", "card.securityCode")
    card = {
        "number": number,
        "expiry": parse_expiry(data.get("expiry")),
    }
    if security_code:
        card["security_code"] = security_code
    name = _text(data, "name", max_length=300)
    if name:
        card["name"] = name
    address = parse_billing_address(data.get("billingAddress"))
    if address:
        card["billing_address"] = address
    return card


def parse_amount(value, field="amount"):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InvalidRequest("%s must be a decimal string." % field, field)
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise InvalidRequest("%s must be a decimal string." % field, field)
    if not amount.is_finite() or amount <= 0:
        raise InvalidRequest("%s must be greater than zero." % field, field)
    return amount


def parse_idempotency_key(value, *, required, field="idempotencyKey"):
    if value is None or value == "":
        if required:
            raise InvalidRequest("%s is required." % field, field)
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
        raise InvalidRequest("%s must be a string of at most 128 characters." % field, field)
    return value.strip()


def parse_datetime(value, field):
    if not value:
        raise InvalidRequest("%s is required (ISO-8601 date-time)." % field, field)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequest("%s must be an ISO-8601 date-time." % field, field)
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed

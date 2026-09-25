"""
Card details as they arrive from a caller.

The card number and security code live only in this object for the length of
one request and are passed straight to PayPal. They are never stored, logged or
echoed back; ``repr`` hides them.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from paypal.core import UNSET
from paypal.models import Address, CardRequest, PaymentTokenRequestCard

from .errors import ApiProblem


@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str = ""
    address_line_2: str = ""
    admin_area_2: str = ""
    admin_area_1: str = ""
    postal_code: str = ""

    def to_paypal(self) -> Address:
        return Address(
            country_code=self.country_code,
            address_line_1=self.address_line_1 or UNSET,
            address_line_2=self.address_line_2 or UNSET,
            admin_area_2=self.admin_area_2 or UNSET,
            admin_area_1=self.admin_area_1 or UNSET,
            postal_code=self.postal_code or UNSET,
        )


@dataclass(frozen=True)
class CardInput:
    number: str = field(repr=False)
    security_code: str = field(repr=False)
    expiry: str  # YYYY-MM
    name: str
    billing_address: BillingAddress | None

    @property
    def last_digits(self) -> str:
        return self.number[-4:]

    def to_order_card(self) -> CardRequest:
        card = CardRequest(number=self.number, expiry=self.expiry, security_code=self.security_code, name=self.name)
        if self.billing_address is not None:
            card = card.model_copy(update={"billing_address": self.billing_address.to_paypal()})
        return card

    def to_vault_card(self) -> PaymentTokenRequestCard:
        card = PaymentTokenRequestCard(
            number=self.number, expiry=self.expiry, security_code=self.security_code, name=self.name
        )
        if self.billing_address is not None:
            card = card.model_copy(update={"billing_address": self.billing_address.to_paypal()})
        return card


def _invalid(message: str) -> ApiProblem:
    return ApiProblem(400, "invalid_card", message)


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _text(data: dict[str, Any], key: str, max_len: int) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise _invalid(f"'{key}' must be a string.")
    value = value.strip()
    if len(value) > max_len:
        raise _invalid(f"'{key}' is too long.")
    return value


def parse_card(data: object) -> CardInput:
    if not isinstance(data, dict):
        raise _invalid("'card' must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise _invalid("The card number is not valid.")
    security_code = str(data.get("securityCode", data.get("cvc", ""))).strip()
    if not security_code.isdigit() or not 3 <= len(security_code) <= 4:
        raise _invalid("The security code must be 3 or 4 digits.")
    expiry = _text(data, "expiry", 7)
    match = re.fullmatch(r"(\d{4})-(\d{2})", expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise _invalid("'expiry' must be YYYY-MM.")
    year, month = int(match.group(1)), int(match.group(2))
    today = date.today()
    if (year, month) < (today.year, today.month):
        raise _invalid("The card has expired.")
    name = _text(data, "name", 128)
    if not name:
        raise _invalid("'name' (the cardholder name) is required.")
    billing = data.get("billingAddress")
    address: BillingAddress | None = None
    if billing is not None:
        if not isinstance(billing, dict):
            raise _invalid("'billingAddress' must be an object.")
        country = _text(billing, "countryCode", 2).upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise _invalid("'billingAddress.countryCode' must be a two-letter country code.")
        address = BillingAddress(
            country_code=country,
            address_line_1=_text(billing, "addressLine1", 300),
            address_line_2=_text(billing, "addressLine2", 300),
            admin_area_2=_text(billing, "city", 120),
            admin_area_1=_text(billing, "state", 300),
            postal_code=_text(billing, "postalCode", 60),
        )
    return CardInput(number=number, security_code=security_code, expiry=expiry, name=name, billing_address=address)

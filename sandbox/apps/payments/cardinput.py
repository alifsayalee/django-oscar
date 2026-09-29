"""
Validation of card details arriving in a request.

Card data only ever lives in memory for the duration of the request that
carries it: it is handed to PayPal and dropped. ``repr`` never shows it.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pay_pal_server_sdk.core import UNSET
from pay_pal_server_sdk.models import Address, CardRequest, PaymentTokenRequestCard

from .common import ClientError

_ADDRESS_FIELDS = {
    "addressLine1": "address_line_1",
    "addressLine2": "address_line_2",
    "city": "admin_area_2",
    "state": "admin_area_1",
    "postalCode": "postal_code",
}


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


def _normalise_expiry(value: str) -> str:
    """Accept ``YYYY-MM`` or ``MM/YY``/``MM/YYYY``; return PayPal's ``YYYY-MM``."""
    value = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{2})\s*/\s*(\d{2}|\d{4})", value)
        if not m:
            raise ClientError(422, "invalid_card", "card.expiry must be YYYY-MM or MM/YY")
        month, year = int(m.group(1)), int(m.group(2))
        if year < 100:
            year += 2000
    if not 1 <= month <= 12:
        raise ClientError(422, "invalid_card", "card.expiry month must be 01-12")
    today = date.today()
    if (year, month) < (today.year, today.month):
        raise ClientError(422, "invalid_card", "card.expiry is in the past")
    return f"{year:04d}-{month:02d}"


@dataclass(frozen=True)
class CardInput:
    number: str = field(repr=False)
    expiry: str
    security_code: str = field(repr=False)
    name: str
    billing_address: dict[str, str] | None = None

    @property
    def last_digits(self) -> str:
        return self.number[-4:]

    @classmethod
    def parse(cls, data: Any) -> "CardInput":
        if not isinstance(data, dict):
            raise ClientError(422, "invalid_card", "card must be an object")
        number = re.sub(r"[\s-]", "", str(data.get("number", "")))
        if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
            raise ClientError(422, "invalid_card", "card.number is not a valid card number")
        security_code = str(data.get("securityCode", data.get("cvc", ""))).strip()
        if not re.fullmatch(r"\d{3,4}", security_code):
            raise ClientError(422, "invalid_card", "card.securityCode must be 3 or 4 digits")
        expiry = _normalise_expiry(str(data.get("expiry", "")))
        name = str(data.get("name", "")).strip()[:300]
        if not name:
            raise ClientError(422, "invalid_card", "card.name is required")
        address = data.get("billingAddress")
        billing: dict[str, str] | None = None
        if address is not None:
            if not isinstance(address, dict):
                raise ClientError(422, "invalid_card", "card.billingAddress must be an object")
            country = str(address.get("countryCode", "")).strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise ClientError(422, "invalid_card", "card.billingAddress.countryCode must be a 2-letter code")
            billing = {"country_code": country}
            for wire, attr in _ADDRESS_FIELDS.items():
                value = str(address.get(wire, "")).strip()
                if value:
                    billing[attr] = value[:300]
        return cls(number=number, expiry=expiry, security_code=security_code, name=name, billing_address=billing)

    def _address(self) -> Address | None:
        if self.billing_address is None:
            return None
        return Address.model_validate(self.billing_address)

    def to_card_request(self) -> CardRequest:
        address = self._address()
        return CardRequest(
            name=self.name,
            number=self.number,
            expiry=self.expiry,
            security_code=self.security_code,
            billing_address=address if address is not None else UNSET,
        )

    def to_token_card(self) -> PaymentTokenRequestCard:
        address = self._address()
        return PaymentTokenRequestCard(
            name=self.name,
            number=self.number,
            expiry=self.expiry,
            security_code=self.security_code,
            billing_address=address if address is not None else UNSET,
        )

"""
Saved cards: PayPal's vault holds the card; Oscar's ``Bankcard`` holds only what
the shopper needs to recognise it (brand, last four digits, expiry month) and the
vault token id in ``partner_reference``.  Full card details are passed straight
through to PayPal and never stored or logged.
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.utils import timezone
from oscar.apps.payment import bankcards
from oscar.core.loading import get_model
from paypal.core import UNSET, Failure, UnsetType
from paypal.models import (
    Address,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
)

from . import errors, outcomes
from .errors import ApiProblem
from .models import PaymentWrite
from .paypal_client import get_client
from .safe_write import Answer, answer_for, deterministic_ref, provider_time, safe_write

Bankcard = get_model("payment", "Bankcard")

EXPIRY = re.compile(r"^(\d{4})-(\d{2})$")
BRANDS = {
    bankcards.VISA: "VISA",
    bankcards.VISA_ELECTRON: "VISA",
    bankcards.MASTERCARD: "MASTERCARD",
    bankcards.AMEX: "AMEX",
    bankcards.DISCOVER: "DISCOVER",
    bankcards.DINERS_CLUB: "DINERS",
    bankcards.JCB: "JCB",
    bankcards.MAESTRO: "MAESTRO",
}


@dataclass(repr=False)
class CardInput:
    """Card details from a request. ``repr`` is disabled so they never reach a log line."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str = ""
    billing: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"CardInput(last4={self.number[-4:]})"

    @property
    def brand(self) -> str:
        return BRANDS.get(bankcards.bankcard_type(self.number) or "", "CARD")

    @classmethod
    def parse(cls, raw: object) -> CardInput:
        if not isinstance(raw, dict):
            raise ApiProblem(400, "invalid_card", "card must be an object.")
        number = re.sub(r"[\s-]", "", str(raw.get("number", "")))
        if not number.isdigit() or not 12 <= len(number) <= 19 or not bankcards.luhn(number):
            raise ApiProblem(400, "invalid_card", "card.number is not a valid card number.")
        expiry = str(raw.get("expiry", ""))
        match = EXPIRY.match(expiry)
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise ApiProblem(400, "invalid_card", "card.expiry must be YYYY-MM.")
        year, month = int(match.group(1)), int(match.group(2))
        today = timezone.now().date()
        if (year, month) < (today.year, today.month):
            raise ApiProblem(400, "invalid_card", "card.expiry is in the past.")
        code = str(raw.get("securityCode", ""))
        if not code.isdigit() or not 3 <= len(code) <= 4:
            raise ApiProblem(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
        name = raw.get("name", "")
        billing = raw.get("billingAddress") or {}
        if not isinstance(name, str) or not isinstance(billing, dict):
            raise ApiProblem(400, "invalid_card", "card.name must be a string and card.billingAddress an object.")
        return cls(number=number, expiry=expiry, security_code=code, name=name.strip()[:300], billing=billing)


def billing_address(raw: dict[str, Any]) -> Address | None:
    if not raw:
        return None
    country = raw.get("countryCode")
    if not isinstance(country, str) or len(country.strip()) != 2:
        raise ApiProblem(400, "invalid_card", "card.billingAddress.countryCode must be a 2-letter country code.")

    def opt(key: str) -> str | UnsetType:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else UNSET

    return Address(
        address_line_1=opt("line1"),
        address_line_2=opt("line2"),
        admin_area_2=opt("city"),
        admin_area_1=opt("state"),
        postal_code=opt("postcode"),
        country_code=country.strip().upper(),
    )


def usable_cards(user: Any) -> Any:
    """The shopper's saved cards, minus any whose removal has been requested."""
    removing = PaymentWrite.objects.filter(kind=PaymentWrite.VAULT_DELETE).values("provider_id")
    return Bankcard.objects.filter(user=user).exclude(partner_reference="").exclude(partner_reference__in=removing)


def serialize_card(card: Any) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.pk),
        "brand": card.card_type,
        "last4": card.number[-4:],
        "expiry": card.expiry_date.strftime("%Y-%m"),
        "label": f"{card.card_type} ending {card.number[-4:]} (expires {card.expiry_date.strftime('%m/%y')})",
    }


# --- save -----------------------------------------------------------------------------------


def read_token(token: PaymentTokenResponse) -> Answer:
    """The vault response has no status member: derive one from what it carries."""
    token_id = token.id if isinstance(token.id, str) else ""
    source = token.payment_source
    card = UNSET if isinstance(source, UnsetType) else source.card
    if isinstance(card, UnsetType) or not isinstance(card.last_digits, str):
        status = outcomes.INCOMPLETE
    elif outcomes.verification_failed(card.verification_status):
        status = outcomes.VERIFICATION_FAILED
    else:
        status = outcomes.VAULTED
    return Answer(token_id, status, provider_time((token.model_extra or {}).get("create_time")))


def save_card(user: Any, payload: dict[str, Any], idempotency_key: str | None) -> tuple[Any, bool]:
    """Vault a card for ``user``. Returns the Bankcard and whether this request created it."""
    card = CardInput.parse(payload.get("card"))
    address = billing_address(card.billing)
    # Without a caller key every request is a new save by definition; with one, a
    # repeat is answered from the first.
    key = idempotency_key or secrets.token_hex(16)
    reference = deterministic_ref(f"u{user.pk}", "card", hashlib.sha256(key.encode()).hexdigest()[:24])

    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                name=card.name or UNSET,
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                brand=card.brand if card.brand != "CARD" else UNSET,
                billing_address=address or UNSET,
            )
        )
    )

    def send(ref: str) -> PaymentTokenResponse:
        return get_client().vault.create_payment_token(body, pay_pal_request_id=ref)

    result = safe_write(
        reference=reference,
        kind=PaymentWrite.VAULT_CREATE,
        send=send,
        read=read_token,
        outcome_of=outcomes.vault_outcome,
        release_on_refusal=False,
        claim_fields={"user": user},
    )
    write = result.write
    if write.outcome != PaymentWrite.DONE:
        if write.outcome == PaymentWrite.FAILED:
            raise ApiProblem(402, "card_not_saved",
                             write.error_message or "PayPal could not verify the card, so it was not saved.")
        answer_for(write, what="Saving the card")

    token = result.response
    existing = Bankcard.objects.filter(user=user, partner_reference=write.provider_id).first()
    if existing is not None:
        return existing, False
    if token is None:
        # Settled by an earlier request that did not get to record it: read the token back.
        token = errors.read(lambda: get_client().vault.get_payment_token(write.provider_id), what="saved card lookup")
    return _record_card(user, token, fallback=card), True


def _record_card(user: Any, token: PaymentTokenResponse, *, fallback: CardInput) -> Any:
    source = token.payment_source
    vaulted = UNSET if isinstance(source, UnsetType) else source.card
    last4 = fallback.number[-4:]
    brand = fallback.brand
    expiry = fallback.expiry
    if not isinstance(vaulted, UnsetType):
        last4 = vaulted.last_digits if isinstance(vaulted.last_digits, str) else last4
        brand = vaulted.brand if isinstance(vaulted.brand, str) else brand
        expiry = vaulted.expiry if isinstance(vaulted.expiry, str) and EXPIRY.match(vaulted.expiry) else expiry
    year, month = (int(p) for p in expiry.split("-"))
    token_id = token.id if isinstance(token.id, str) else ""
    with transaction.atomic():
        bankcard = Bankcard(
            user=user,
            number=f"XXXX-XXXX-XXXX-{last4}",
            expiry_date=datetime.date(year, month, calendar.monthrange(year, month)[1]),
            partner_reference=token_id,
        )
        bankcard.card_type = str(brand)
        bankcard.save()
    return bankcard


# --- delete ---------------------------------------------------------------------------------


def delete_card(user: Any, card_id: str) -> None:
    bankcard = Bankcard.objects.filter(user=user, pk=card_id).first() if card_id.isdigit() else None
    if bankcard is None:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    token_id = bankcard.partner_reference

    def send(_: str) -> str:
        result = get_client().vault.with_raw_response.delete_payment_token(token_id)
        if isinstance(result, Failure) and result.response.status_code != 404:
            result.unwrap()  # raises ApiError with the operation's error union
        return outcomes.DELETED  # 2xx, or 404: the token is gone either way

    # The claim itself hides the card and makes it unusable from this moment on.
    result = safe_write(
        reference=deterministic_ref("card", bankcard.pk, "delete"),
        kind=PaymentWrite.VAULT_DELETE,
        send=send,
        read=lambda status: Answer(token_id, status, timezone.now()),
        outcome_of=outcomes.delete_outcome,
        claim_fields={"user": user, "provider_id": token_id},
    )
    write = result.write
    if write.outcome == PaymentWrite.DONE:
        Bankcard.objects.filter(pk=bankcard.pk).delete()
        return
    answer_for(write, what="Removing the card")

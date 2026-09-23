"""Saved cards (PayPal vault), scoped to the signed-in shopper.

Full card details are never stored in this app's database: only PayPal's vault
token id (in ``Bankcard.partner_reference``) and a safe display (brand + last 4 +
expiry) are kept. A card belongs to the shopper who saved it.
"""

import calendar
import uuid
from datetime import date

from oscar.core.loading import get_model

from .. import errors
from . import gateway

Bankcard = get_model("payment", "Bankcard")


def _expiry_date(expiry):
    """Turn PayPal's ``YYYY-MM`` expiry into the last day of that month."""
    try:
        year, month = (int(part) for part in expiry.split("-"))
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, last_day)
    except (ValueError, AttributeError):
        raise errors.ApiValidationError("Card expiry must be in YYYY-MM format.")


def save_card(user, card):
    """Vault a card for ``user`` and record a safe reference to it.

    ``card`` is the raw one-off card payload (number/expiry/security_code/...).
    """
    number = (card or {}).get("number")
    expiry = (card or {}).get("expiry")
    if not number or not expiry:
        raise errors.ApiValidationError("Card number and expiry are required.")

    # Validate expiry format up front (also used for the local record).
    expiry_date = _expiry_date(expiry)

    vault_card = {"number": number, "expiry": expiry}
    for key in ("security_code", "name", "billing_address"):
        if card.get(key):
            vault_card[key] = card[key]

    result = gateway.create_vault_token(vault_card, request_id=uuid.uuid4().hex)
    display = result.get("card") or {}
    last_digits = display.get("last_digits") or str(number)[-4:]
    brand = display.get("brand") or "Card"

    bankcard = Bankcard(
        user=user,
        number="XXXX-XXXX-XXXX-%s" % last_digits,
        expiry_date=expiry_date,
        name=card.get("name", "") or display.get("name", ""),
        partner_reference=result["token_id"],
    )
    bankcard.card_type = brand
    bankcard.save()
    return bankcard


def list_cards(user):
    return list(
        Bankcard.objects.filter(user=user).exclude(partner_reference="").order_by("-id")
    )


def delete_card(user, payment_method_id):
    try:
        bankcard = Bankcard.objects.get(id=payment_method_id, user=user)
    except (Bankcard.DoesNotExist, ValueError):
        raise errors.NotFound("No saved card with that id.")

    if bankcard.partner_reference:
        try:
            gateway.delete_vault_token(bankcard.partner_reference)
        except errors.PayPalRejected as e:
            # If PayPal already has no such token (404) the card is already gone
            # upstream; still remove it locally so it can no longer be used.
            if e.status_code != 404:
                raise
    bankcard.delete()

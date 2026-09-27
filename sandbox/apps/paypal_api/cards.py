"""
Saved cards: vaulted at PayPal, recorded locally as Oscar ``Bankcard`` rows that
hold only the masked number, brand, expiry month and PayPal's token id.
"""

import calendar
import datetime
from typing import Any

from django.db import transaction
from oscar.core.loading import get_model
from paypal.core import UNSET, ApiError, UnsetType
from paypal.models import (
    Customer,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
)

from . import gateway
from .errors import ApiProblem
from .models import PayPalCustomer
from .models import PayPalOperation as Op
from .payments import key_digest, parse_card, ref
from .safe_write import Answer, delete_outcome, safe_write, vault_outcome

Bankcard = get_model("payment", "Bankcard")


def v(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def _delete_ref(user: Any, token: str) -> str:
    return ref("user", user.pk, "vault-delete", token)


def _deleting_tokens(user: Any) -> set[str]:
    """Tokens with a delete claimed (in flight, unknown or done): hidden and unusable."""
    prefix = ref("user", user.pk, "vault-delete", "")
    refs = Op.objects.filter(kind=Op.VAULT_DELETE, user=user).exclude(outcome=Op.FAILED).values_list("ref", flat=True)
    return {r[len(prefix):] for r in refs if r.startswith(prefix)}


def list_cards(user: Any) -> list[Any]:
    hidden = _deleting_tokens(user)
    cards = Bankcard.objects.filter(user=user).exclude(partner_reference="").order_by("pk")
    return [c for c in cards if c.partner_reference not in hidden]


def get_card(user: Any, payment_method_id: Any) -> Any:
    try:
        pk = int(payment_method_id)
    except (TypeError, ValueError):
        raise ApiProblem(404, "not_found", "Saved card not found.") from None
    for card in list_cards(user):
        if card.pk == pk:
            return card
    raise ApiProblem(404, "not_found", "Saved card not found.")


def usable_card(user: Any, payment_method_id: Any) -> Any:
    try:
        return get_card(user, payment_method_id)
    except ApiProblem:
        raise ApiProblem(400, "unknown_payment_method", "paymentMethodId is not one of your saved cards.") from None


def describe_card(card: Any) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.pk),
        "brand": card.card_type,
        "lastDigits": card.number[-4:],
        "expiry": card.expiry_date.strftime("%Y-%m"),
        "label": f"{card.card_type} ending {card.number[-4:]}, expires {card.expiry_date.strftime('%m/%Y')}",
    }


def _read_token(result: PaymentTokenResponse) -> Answer:
    source = v(result.payment_source)
    card = v(source.card) if source else None
    customer = v(result.customer)
    token_id = v(result.id)
    verification = v(card.verification_status) if card else None
    return Answer(
        provider_id=token_id or "",
        status=("TOKENIZED" if token_id and card else "INCOMPLETE", verification),
        provider_time=None,
        detail={
            "customer_id": v(customer.id) if customer else None,
            "brand": str(v(card.brand) or "Card") if card else "",
            "last_digits": (v(card.last_digits) or "") if card else "",
            "expiry": (v(card.expiry) or "") if card else "",
        },
    )


def _expiry_date(expiry: str) -> datetime.date:
    year, month = (int(p) for p in expiry.split("-")[:2])
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def save_card(user: Any, payload: dict[str, Any], key: str) -> tuple[Op, Any]:
    card = parse_card(payload.get("card"))
    fingerprint = f"{card['number'][-4:]}:{card['expiry']}"
    op_ref = ref("user", user.pk, "vault", key_digest(key))
    existing = Op.objects.filter(ref=op_ref).first()
    if existing is not None and existing.fingerprint != fingerprint:
        raise ApiProblem(409, "idempotency_key_reused", "This Idempotency-Key was already used for a different card.")

    customer = PayPalCustomer.objects.filter(user=user).first()
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**card)),
        customer=Customer(id=customer.customer_id) if customer else UNSET,
    )
    client = gateway.get_client()

    def send(k: str) -> PaymentTokenResponse:
        return client.vault.create_payment_token(body, pay_pal_request_id=k)

    op, _ = safe_write(
        op_ref,
        send=send,
        find=send,  # smoke: a same-key repeat returns the same token
        read=_read_token,
        outcome_of=vault_outcome,
        repeat_is_safe=True,
        claim={"kind": Op.VAULT_CREATE, "user": user, "fingerprint": fingerprint},
    )
    saved = None
    if op.outcome == Op.DONE:
        d = op.detail
        with transaction.atomic():
            if d.get("customer_id") and customer is None:
                PayPalCustomer.objects.get_or_create(user=user, defaults={"customer_id": d["customer_id"]})
            saved = Bankcard.objects.filter(user=user, partner_reference=op.provider_id).first()
            if saved is None and Op.objects.filter(pk=op.pk, applied=False).update(applied=True) == 1:
                saved = Bankcard(
                    user=user,
                    number="XXXX-XXXX-XXXX-" + (d.get("last_digits") or card["number"][-4:]),
                    expiry_date=_expiry_date(d.get("expiry") or card["expiry"]),
                    partner_reference=op.provider_id,
                )
                saved.card_type = (d.get("brand") or "Card")[:128]
                saved.save()
            elif saved is None:
                saved = Bankcard.objects.filter(user=user, partner_reference=op.provider_id).first()
    return op, saved


def delete_card(user: Any, payment_method_id: Any) -> Op:
    card = get_card(user, payment_method_id)
    token = card.partner_reference
    client = gateway.get_client()

    def send(_key: str) -> str:
        try:
            client.vault.with_raw_response.delete_payment_token(token).unwrap()
        except ApiError as e:
            if e.status_code == 404:
                return "deleted"  # PayPal no longer has it
            raise
        return "deleted"

    op, _ = safe_write(
        _delete_ref(user, token),
        send=send,
        find=send,  # smoke: deleting again answers 204
        read=lambda status: Answer(provider_id=token, status=status, provider_time=None),
        outcome_of=delete_outcome,
        repeat_is_safe=True,
        claim={"kind": Op.VAULT_DELETE, "user": user},
    )
    if op.outcome == Op.DONE:
        card.delete()
    return op

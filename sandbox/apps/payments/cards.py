"""
Saved cards: vaulted at PayPal, remembered locally only as PayPal's token id
plus what the shopper needs to recognise the card (brand, last digits, expiry).
"""

from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from pay_pal_server_sdk.core import UNSET, ApiError, Failure, Success
from pay_pal_server_sdk.models import (
    Customer,
    PaymentTokenRequest,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
)

from . import outcomes
from .cardinput import CardInput
from .common import ClientError, ServiceResult, digest, reference_prefix
from .models import Outcome, PayPalCustomer, ProviderWrite, SavedCard
from .paypal import describe_error, get_client, translate, unset_to_none
from .safe_write import Answer, OutcomeUnknown, safe_write


def serialize_card(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand or None,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "name": card.name or None,
        "label": f"{card.brand or 'Card'} ending {card.last_digits}",
        "status": card.outcome,
        "createdAt": card.created_at.isoformat(),
    }


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, deleted_at__isnull=True, outcome=Outcome.DONE))


def usable_card(user: Any, payment_method_id: str) -> SavedCard:
    """The caller's own, live saved card — anyone else's is indistinguishable from a missing one."""
    card = SavedCard.objects.filter(user=user, public_id=payment_method_id).first()
    if card is None or not card.is_usable:
        raise ClientError(404, "payment_method_not_found", "No such saved card")
    return card


def save_card(user: Any, card: CardInput, idempotency_key: str | None) -> ServiceResult:
    customer, _ = PayPalCustomer.objects.get_or_create(user=user)
    # Derived from what identifies the request, never random: the caller's key
    # if sent, else a keyed digest of the card itself (no card data in the ref).
    identity = digest("key", idempotency_key) if idempotency_key else digest("card", card.number, card.expiry)
    ref = f"{reference_prefix()}:u{user.pk}:card:{identity}:d{customer.times_deleted}"
    try:
        with transaction.atomic():
            saved, _ = SavedCard.objects.get_or_create(
                ref=ref,
                defaults={"user": user, "last_digits": card.last_digits, "expiry": card.expiry, "name": card.name},
            )
    except IntegrityError:
        saved = SavedCard.objects.get(ref=ref)
    if saved.user_id != user.pk:  # pragma: no cover - refs embed the user id
        raise ClientError(409, "conflict", "Reference conflict")
    if saved.outcome == Outcome.DONE and saved.deleted_at is None:
        return ServiceResult(200, serialize_card(saved))

    body = PaymentTokenRequest(
        customer=Customer(id=customer.paypal_customer_id) if customer.paypal_customer_id else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(card=card.to_token_card()),
    )

    def send(key: str) -> PaymentTokenResponse:
        return get_client().vault.create_payment_token(body, pay_pal_request_id=key)

    def read(r: PaymentTokenResponse) -> Answer:
        source = unset_to_none(r.payment_source)
        vaulted = unset_to_none(source.card) if source is not None else None
        token_id = unset_to_none(r.id)
        return Answer(token_id, outcomes.CARD_VAULTED if token_id and vaulted is not None else None, None)

    def on_complete(record: ProviderWrite, r: PaymentTokenResponse) -> None:
        row = SavedCard.objects.select_for_update().get(pk=saved.pk)
        row.outcome = record.outcome
        row.paypal_token_id = unset_to_none(r.id) or row.paypal_token_id
        source = unset_to_none(r.payment_source)
        vaulted = unset_to_none(source.card) if source is not None else None
        if vaulted is not None:
            row.brand = str(unset_to_none(vaulted.brand) or row.brand)
            row.last_digits = unset_to_none(vaulted.last_digits) or row.last_digits
            row.expiry = unset_to_none(vaulted.expiry) or row.expiry
            row.name = unset_to_none(vaulted.name) or row.name
        row.save()
        paypal_customer = unset_to_none(r.customer)
        customer_id = unset_to_none(paypal_customer.id) if paypal_customer is not None else None
        if customer_id:
            PayPalCustomer.objects.filter(pk=customer.pk, paypal_customer_id="").update(paypal_customer_id=customer_id)

    try:
        result = safe_write(
            ref,
            kind="vault_card",
            send=send,
            read=read,
            outcome_of=outcomes.vault_outcome,
            repeat_is_safe=True,  # PayPal-Request-Id returns the original token (3 h)
            on_complete=on_complete,
        )
    except OutcomeUnknown as e:
        SavedCard.objects.filter(pk=saved.pk).update(outcome=Outcome.UNKNOWN)
        return ServiceResult(504, {
            "error": "outcome_unknown", "outcomeUnknown": True, "reference": e.record.ref,
            "message": "PayPal's answer was lost; repeat the request to settle it",
        })
    except ApiError as e:
        SavedCard.objects.filter(pk=saved.pk).delete()  # nothing was vaulted
        err = translate(e)
        return ServiceResult(err.status_code, {"error": "card_refused", "message": describe_error(e.error)})
    saved.refresh_from_db()
    status = outcomes.http_status(result.outcome)
    return ServiceResult(status, serialize_card(saved))


def delete_card(user: Any, payment_method_id: str) -> ServiceResult:
    with transaction.atomic():
        card = SavedCard.objects.select_for_update().filter(user=user, public_id=payment_method_id).first()
        if card is None:
            raise ClientError(404, "payment_method_not_found", "No such saved card")
        if card.delete_outcome == Outcome.DONE:
            return ServiceResult(200, {"paymentMethodId": str(card.public_id), "deleted": True})
        if card.deleted_at is None:
            # From this moment the card is neither listed nor usable, whatever PayPal answers.
            card.deleted_at = timezone.now()
            card.delete_outcome = Outcome.SENDING
            card.save(update_fields=["deleted_at", "delete_outcome"])

    if not card.paypal_token_id:
        SavedCard.objects.filter(pk=card.pk).update(delete_outcome=Outcome.DONE)
        return ServiceResult(200, {"paymentMethodId": str(card.public_id), "deleted": True})

    token_id = card.paypal_token_id

    def send(_key: str) -> Success[None]:
        raw = get_client().vault.with_raw_response.delete_payment_token(token_id)
        if isinstance(raw, Failure):
            raw.unwrap()  # raises ApiError carrying PayPal's error body
        assert isinstance(raw, Success)
        return raw

    def read(r: Success[None]) -> Answer:
        return Answer(token_id, r.status_code, timezone.now())

    def on_complete(record: ProviderWrite, _r: Success[None]) -> None:
        SavedCard.objects.filter(pk=card.pk).update(delete_outcome=record.outcome)
        if record.outcome == Outcome.DONE:
            customer, _ = PayPalCustomer.objects.select_for_update().get_or_create(user=user)
            customer.times_deleted += 1
            customer.save(update_fields=["times_deleted"])

    body = {"paymentMethodId": str(card.public_id)}
    try:
        result = safe_write(
            f"{reference_prefix()}:pm{card.pk}:delete",
            kind="vault_delete",
            send=send,
            read=read,
            outcome_of=outcomes.delete_outcome,
            repeat_is_safe=True,  # DELETE by id: PayPal answers 204 even when already gone
            on_complete=on_complete,
        )
    except OutcomeUnknown as e:
        SavedCard.objects.filter(pk=card.pk).update(delete_outcome=Outcome.UNKNOWN)
        return ServiceResult(504, body | {
            "deleted": False, "error": "outcome_unknown", "outcomeUnknown": True, "reference": e.record.ref,
            "message": "The card is no longer usable here; PayPal's answer was lost — repeat the DELETE to settle it",
        })
    except ApiError as e:
        SavedCard.objects.filter(pk=card.pk).update(delete_outcome=Outcome.FAILED)
        err = translate(e)
        return ServiceResult(err.status_code, body | {
            "deleted": False, "error": "delete_refused",
            "message": f"The card is no longer usable here, but PayPal refused to delete it: {describe_error(e.error)}",
        })
    return ServiceResult(outcomes.http_status(result.outcome), body | {"deleted": result.outcome == Outcome.DONE})

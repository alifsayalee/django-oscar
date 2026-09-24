"""
Saved cards: a PayPal vault payment token per card, owned by one shopper.

Only the token id and what PayPal echoes for display (brand, last digits,
expiry) are stored. The card number exists in this process only for the
duration of the request that saves it, and only in memory.
"""
import hashlib
import hmac
import logging
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from paypal.core import ApiError, Failure, Success, UnsetType
from paypal.models import (
    Address,
    Customer,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
)

from . import statuses
from .claims import Answer, safe_write, try_claim
from .errors import not_found
from .gateway import get_client
from .models import PaymentOperation, SavedCard
from .orders import reference_prefix
from .payments import CardInput

log = logging.getLogger(__name__)


def read_token(result: PaymentTokenResponse) -> Answer:
    """A payment token has no status: it is done only when PayPal returned both
    its id and the card it vaulted."""
    provider_id = statuses.text(result.id)
    source = result.payment_source
    has_card = not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType)
    created = (result.model_extra or {}).get("create_time")
    return Answer(
        provider_id=provider_id,
        status="VAULTED" if provider_id and has_card else "",
        outcome=statuses.DONE if provider_id and has_card else statuses.UNKNOWN,
        provider_time=statuses.parse_time(created) or timezone.now(),
    )


@sensitive_variables()
def _fingerprint(user: Any, card: CardInput) -> str:
    """Keyed hash identifying (shopper, card number, expiry), so a repeated
    save of the same card maps to the same claim. Never reversible."""
    message = "%s:%s:%s" % (user.pk, card.number, card.expiry)
    return hmac.new(settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()


def _customer_id(user: Any) -> str:
    return (
        SavedCard.objects.filter(user=user).exclude(paypal_customer_id="")
        .order_by("created_at").values_list("paypal_customer_id", flat=True).first()
        or ""
    )


@sensitive_variables()
def save_card(user: Any, card: CardInput) -> tuple[SavedCard | None, PaymentOperation, bool]:
    """Vault a card for ``user``. Returns ``(card, operation, created)``."""
    fingerprint = _fingerprint(user, card)
    latest = (
        PaymentOperation.objects.filter(kind=PaymentOperation.VAULT, user=user, request_key=fingerprint)
        .order_by("-attempt", "-pk").first()
    )
    if latest is not None and latest.outcome == PaymentOperation.DONE:
        existing = latest.saved_card
        if existing is not None and existing.is_active:
            return existing, latest, False  # the same card saved again: one saved card
    if latest is not None and latest.outcome not in (PaymentOperation.DONE, PaymentOperation.FAILED):
        op, won = latest, False
    else:
        attempt = latest.attempt + 1 if latest is not None else 1
        op, won = try_claim(
            reference="%s-u%s-vault-%s-%s" % (reference_prefix(), user.pk, fingerprint[:20], attempt),
            kind=PaymentOperation.VAULT, attempt=attempt, user=user, request_key=fingerprint,
        )

    card_kwargs: dict[str, Any] = {"number": card.number, "expiry": card.expiry}
    if card.security_code:
        card_kwargs["security_code"] = card.security_code
    if card.name:
        card_kwargs["name"] = card.name
    if card.billing_address:
        card_kwargs["billing_address"] = Address(**card.billing_address)
    customer_id = _customer_id(user)
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**card_kwargs)),
        **({"customer": Customer(id=customer_id)} if customer_id else {}),
    )
    client = get_client()

    def send(key: str) -> PaymentTokenResponse:
        return client.vault.create_payment_token(body, pay_pal_request_id=key)

    op, result = safe_write(
        op, won, send=send, find=send, read=read_token,
        repeat_is_safe=True,  # PayPal returns the original token for a repeated request id (kept 3 hours)
        sent=None,
    )
    if op.outcome != PaymentOperation.DONE or result is None:
        return op.saved_card, op, False
    return _record_card(user, op, result), op, True


def _record_card(user: Any, op: PaymentOperation, result: PaymentTokenResponse) -> SavedCard:
    brand = last_digits = expiry = ""
    source = result.payment_source
    if not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType):
        brand = statuses.status_text(source.card.brand)
        last_digits = statuses.text(source.card.last_digits)
        expiry = statuses.text(source.card.expiry)
    customer = result.customer
    customer_id = "" if isinstance(customer, UnsetType) else statuses.text(customer.id)
    saved, _ = SavedCard.objects.get_or_create(
        paypal_token_id=op.provider_id,
        defaults={
            "user": user, "paypal_customer_id": customer_id, "brand": brand[:32],
            "last_digits": last_digits[:4], "expiry": expiry[:7],
        },
    )
    op.saved_card = saved
    op.save(update_fields=["saved_card"])
    return saved


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, deleted_at__isnull=True))


def delete_card(user: Any, public_id: str) -> bool:
    """Remove a saved card. It is hidden and unusable from the first step;
    returns whether PayPal has also deleted the token (a repeat retries)."""
    saved = SavedCard.objects.filter(public_id=public_id, user=user).first()
    if saved is None:
        raise not_found("Payment method")
    if saved.deleted_at is None:
        SavedCard.objects.filter(pk=saved.pk, deleted_at__isnull=True).update(deleted_at=timezone.now())
    if saved.provider_deleted_at is not None:
        return True
    try:
        result = get_client().vault.with_raw_response.delete_payment_token(saved.paypal_token_id)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        log.warning("Vault delete for saved card %s not confirmed: %s", saved.public_id, type(exc).__name__)
        return False
    match result:
        case Success():
            pass
        case Failure(response=response) if response.status_code == 404:
            pass  # already gone at PayPal
        case Failure(response=response):
            log.warning("Vault delete for saved card %s refused: HTTP %s", saved.public_id, response.status_code)
            return False
    SavedCard.objects.filter(pk=saved.pk).update(provider_deleted_at=timezone.now())
    return True

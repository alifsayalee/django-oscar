"""
Saved cards: vaulted at PayPal, described locally only by brand, last digits
and expiry. The card number goes to PayPal in the one request that vaults it
and is never stored or logged here.
"""
import logging
import uuid

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, Failure, Success
from paypal.models import (
    Customer, PaymentTokenRequest, PaymentTokenRequestCard, PaymentTokenRequestPaymentSource)

from . import outcomes
from .client import get_client
from .errors import ApiProblem, translate
from .fulfilment import validate_key
from .models import CardState, Outcome, PayPalCustomer, ProviderWrite, SavedCard
from .payflow import _time, _v, parse_card
from .safe_write import VAULT_KEY_RETENTION, Answer, deterministic_ref, safe_write

logger = logging.getLogger('apps.payments')


def list_cards(user):
    return SavedCard.objects.filter(user=user, state=CardState.ACTIVE).order_by('created_at')


def save_card(user, payload: dict, idempotency_key: str | None) -> tuple[int, SavedCard]:
    card_details = parse_card(payload.get('card', payload))
    key = validate_key(idempotency_key) if idempotency_key else uuid.uuid4().hex
    ref = deterministic_ref('usr', user.pk, 'card', key)
    try:
        with transaction.atomic():
            card = SavedCard.objects.create(user=user, request_ref=ref)
    except IntegrityError:
        card = SavedCard.objects.get(request_ref=ref)  # the ref carries the user id
        if card.state in (CardState.ACTIVE, CardState.DELETED):
            return (200 if card.state == CardState.ACTIVE else 409), card
        if card.state == CardState.FAILED:
            SavedCard.objects.filter(pk=card.pk, state=CardState.FAILED).update(
                state=CardState.SENDING)
            card.refresh_from_db()

    customer = PayPalCustomer.objects.filter(user=user).first()
    source = PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard.model_validate(card_details))
    body = (PaymentTokenRequest(payment_source=source, customer=Customer(id=customer.vault_customer_id))
            if customer else PaymentTokenRequest(payment_source=source))
    client = get_client()
    try:
        written = safe_write(
            ref, operation='vault_card', retention=VAULT_KEY_RETENTION,
            send=lambda k: client.vault.create_payment_token(body, pay_pal_request_id=k),
            read=_read_token, apply=lambda record, result, answer: _finish(card, record, result))
    except ApiProblem:
        SavedCard.objects.filter(pk=card.pk, state=CardState.SENDING).update(state=CardState.UNKNOWN)
        raise
    except (ApiError, httpx.RequestError, ValueError) as e:
        problem = translate(e, action='save card')
        SavedCard.objects.filter(pk=card.pk, state=CardState.SENDING).update(
            state=CardState.UNKNOWN if problem.outcome_unknown else CardState.FAILED)
        raise problem from e
    card.refresh_from_db()
    if written.in_flight:
        return 202, card
    if written.result is None and card.state == CardState.SENDING:
        # Settled by an earlier request with the same key but not booked here.
        new_state = CardState.FAILED if written.record.outcome == Outcome.FAILED else CardState.UNKNOWN
        SavedCard.objects.filter(pk=card.pk).update(state=new_state)
        card.refresh_from_db()
    statuses: dict[str, int] = {CardState.ACTIVE: 201, CardState.FAILED: 422}
    return statuses.get(card.state, 504), card


def _read_token(result) -> Answer:
    """A vault token carries no status: an id plus the card description is done."""
    token_id = _v(result.id)
    if not token_id:
        raise ValueError('payment token id')
    source = _v(result.payment_source)
    card = _v(source.card) if source is not None else None
    outcome = outcomes.DONE if card is not None else outcomes.UNKNOWN
    return Answer(token_id, 'VAULTED' if card is not None else '', outcome,
                  _time((result.model_extra or {}).get('create_time')))


def _finish(card: SavedCard, record: ProviderWrite, result):
    if record.outcome != Outcome.DONE:
        SavedCard.objects.filter(pk=card.pk).update(state=CardState.UNKNOWN)
        return
    described = result.payment_source.card
    SavedCard.objects.filter(pk=card.pk).update(
        state=CardState.ACTIVE, paypal_token_id=record.provider_id,
        brand=outcomes.wire(_v(described.brand)), last_digits=_v(described.last_digits) or '',
        expiry=_v(described.expiry) or '', cardholder_name=(_v(described.name) or '')[:128])
    customer = _v(result.customer)
    customer_id = _v(customer.id) if customer is not None else None
    if customer_id:
        try:
            with transaction.atomic():
                PayPalCustomer.objects.get_or_create(
                    user_id=card.user_id, defaults={'vault_customer_id': customer_id})
        except IntegrityError:
            pass  # a concurrent first save recorded the customer already


def delete_card(user, public_id) -> SavedCard:
    try:
        public_id = uuid.UUID(str(public_id))
    except ValueError:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
    card = SavedCard.objects.filter(public_id=public_id, user=user).exclude(
        state=CardState.FAILED).first()
    if card is None or (card.state == CardState.DELETED and card.deleted_at is not None):
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    if card.state in (CardState.SENDING, CardState.UNKNOWN) and not card.paypal_token_id:
        raise ApiProblem(409, 'payment_method_not_settled',
                         'This card is still being saved; repeat the save request first.')
    # Unusable from this moment on, whatever PayPal answers.
    SavedCard.objects.filter(pk=card.pk).update(state=CardState.DELETED)
    try:
        result = get_client().vault.with_raw_response.delete_payment_token(card.paypal_token_id)
    except (ApiError, httpx.RequestError, ValueError) as e:
        raise translate(e, action='delete card') from e
    match result:
        case Success():
            pass
        case Failure(response=response) if response.status_code == 404:
            pass  # already gone at PayPal
        case Failure(response=response):
            logger.warning('PayPal refused to delete vault token for card %s (HTTP %s)',
                           card.public_id, response.status_code)
            raise ApiProblem(502, 'payment_provider_error',
                             'The card was removed from your account, but PayPal did not confirm '
                             'deleting it; repeat the request to retry.')
    SavedCard.objects.filter(pk=card.pk).update(deleted_at=timezone.now())
    card.refresh_from_db()
    return card

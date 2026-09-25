"""
Saved cards: the card goes to PayPal's vault (setup token -> payment token);
this app keeps only an Oscar Bankcard with the masked number, brand and expiry
PayPal reports, plus the PayPal token id that can charge it.
"""
import calendar
import datetime
import logging
import re
from typing import Any

import httpx
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model
from paypal.core import ApiError, Failure, Success, UnsetType
from paypal.models import (
    Address, CardPaymentTokenEntity, Customer, PaymentTokenRequest, PaymentTokenRequestPaymentSource,
    PaymentTokenResponse, SetupTokenRequest, SetupTokenRequestCard, SetupTokenRequestPaymentSource,
    SetupTokenResponse, VaultTokenRequest)
from paypal.models.enums import VaultTokenRequestType

from . import outcomes
from .client import get_client
from .errors import PaymentAPIError, translate
from .models import Outcome, PayPalCustomer, PayPalOperation, PayPalSavedCard
from .writes import Answer, OutcomeUnknown, make_ref, safe_write, try_claim

Bankcard = get_model('payment', 'Bankcard')
logger = logging.getLogger('apps.paypal_payments')

EXPIRY_RE = re.compile(r'^(\d{4})-(\d{2})$')
ADDRESS_FIELDS = {
    'addressLine1': 'address_line_1', 'addressLine2': 'address_line_2',
    'city': 'admin_area_2', 'state': 'admin_area_1', 'postalCode': 'postal_code',
    'countryCode': 'country_code',
}


@sensitive_variables('card', 'number', 'cvc', 'raw')
def parse_card(raw: object) -> dict:
    """
    Validate caller-supplied card details into the SDK's card members. The
    values live only in memory for the one request that sends them to PayPal.
    """
    if not isinstance(raw, dict):
        raise PaymentAPIError(400, 'invalid_card', 'card must be an object.')
    number = re.sub(r'[\s-]', '', str(raw.get('number', '')))
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise PaymentAPIError(400, 'invalid_card', 'card.number must be 12 to 19 digits.')
    expiry = str(raw.get('expiry', ''))
    match = EXPIRY_RE.match(expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise PaymentAPIError(400, 'invalid_card', 'card.expiry must be YYYY-MM.')
    cvc = str(raw.get('securityCode', ''))
    if not cvc.isdigit() or not 3 <= len(cvc) <= 4:
        raise PaymentAPIError(400, 'invalid_card', 'card.securityCode must be 3 or 4 digits.')
    card: dict[str, Any] = {'number': number, 'expiry': expiry, 'security_code': cvc}
    name = raw.get('name')
    if name is not None:
        if not isinstance(name, str) or not 0 < len(name) <= 300:
            raise PaymentAPIError(400, 'invalid_card', 'card.name must be a string.')
        card['name'] = name
    billing = raw.get('billingAddress')
    if billing is not None:
        card['billing_address'] = parse_address(billing)
    return card


def parse_address(raw: object) -> Address:
    if not isinstance(raw, dict):
        raise PaymentAPIError(400, 'invalid_card', 'card.billingAddress must be an object.')
    unknown = set(raw) - set(ADDRESS_FIELDS)
    if unknown:
        raise PaymentAPIError(400, 'invalid_card', 'Unknown billingAddress fields: %s' % ', '.join(sorted(unknown)))
    fields = {}
    for key, member in ADDRESS_FIELDS.items():
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise PaymentAPIError(400, 'invalid_card', 'billingAddress.%s must be a string.' % key)
            fields[member] = value
    if not re.fullmatch(r'[A-Z]{2}', fields.get('country_code', '')):
        raise PaymentAPIError(400, 'invalid_card', 'billingAddress.countryCode must be a 2-letter ISO code.')
    return Address(**fields)


def owned_card(user, payment_method_id) -> PayPalSavedCard:
    """A saved card of ``user`` that can still pay; 404 for anyone else's."""
    if not isinstance(payment_method_id, int) or isinstance(payment_method_id, bool):
        raise PaymentAPIError(400, 'invalid_request', 'paymentMethodId must be an integer.')
    card = (PayPalSavedCard.objects.select_related('bankcard')
            .filter(bankcard__pk=payment_method_id, bankcard__user=user, removed_at__isnull=True)
            .first())
    if card is None:
        raise PaymentAPIError(404, 'payment_method_not_found', 'No such saved card.')
    return card


def serialize(card: PayPalSavedCard) -> dict:
    bankcard = card.bankcard
    return {
        'paymentMethodId': bankcard.pk,
        'type': 'card',
        'brand': bankcard.card_type,
        'last4': bankcard.number[-4:],
        'expiry': bankcard.expiry_date.strftime('%Y-%m'),
        'name': bankcard.name,
        'createdAt': card.created.isoformat(),
    }


def list_cards(user) -> list[dict]:
    cards = (PayPalSavedCard.objects.select_related('bankcard')
             .filter(bankcard__user=user, removed_at__isnull=True).order_by('created'))
    return [serialize(c) for c in cards]


def _read_setup_token(r: SetupTokenResponse) -> Answer:
    return Answer(provider_id=r.id if isinstance(r.id, str) else None, status=r.status, provider_time=None)


def _card_entity(r: PaymentTokenResponse) -> CardPaymentTokenEntity | None:
    source = r.payment_source
    if isinstance(source, UnsetType) or isinstance(source.card, UnsetType):
        return None
    return source.card


def _read_payment_token(r: PaymentTokenResponse) -> Answer:
    token_id = r.id if isinstance(r.id, str) else None
    # No status member: done only when the token id AND the card came back.
    return Answer(provider_id=token_id, status=bool(token_id and _card_entity(r) is not None),
                  provider_time=None)


def _expiry_date(expiry: str) -> datetime.date:
    year, month = int(expiry[:4]), int(expiry[5:7])
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def _customer_id(r: SetupTokenResponse | PaymentTokenResponse) -> str | None:
    customer = r.customer
    if isinstance(customer, UnsetType) or not isinstance(customer.id, str):
        return None
    return customer.id


@sensitive_variables('card', 'payload')
def save_card(user, idempotency_key: str, payload: object) -> tuple[PayPalSavedCard, bool]:
    """Returns (saved card, created-by-this-request)."""
    if not isinstance(payload, dict):
        raise PaymentAPIError(400, 'invalid_request', 'Body must be a JSON object.')
    base = make_ref('card', user.pk, idempotency_key)
    client = get_client()
    known = PayPalCustomer.objects.filter(user=user).first()

    setup_op, won = try_claim(base + ':setup', PayPalOperation.Kind.SETUP_TOKEN,
                              user=user, idempotency_key=idempotency_key)
    token_op = PayPalOperation.objects.filter(ref=base + ':token').first()
    if token_op is not None and token_op.outcome == Outcome.DONE:
        existing = PayPalSavedCard.objects.filter(payment_token_id=token_op.provider_id).first()
        if existing is not None:
            return existing, False
        raise PaymentAPIError(410, 'payment_method_removed', 'The card saved under this Idempotency-Key was removed.')

    if setup_op.outcome != Outcome.DONE:
        card = parse_card(payload.get('card'))
        setup_body = SetupTokenRequest(
            payment_source=SetupTokenRequestPaymentSource(card=SetupTokenRequestCard(**card)),
            **({'customer': Customer(id=known.paypal_customer_id)} if known else {}))
        setup_op, _ = safe_write(
            setup_op, won,
            send=lambda key: client.vault.create_setup_token(setup_body, pay_pal_request_id=key),
            read=_read_setup_token, outcome_of=outcomes.setup_token, action='card setup')
        del card, setup_body
    if setup_op.outcome == Outcome.FAILED:
        if setup_op.provider_status == 'PAYER_ACTION_REQUIRED':
            raise PaymentAPIError(
                422, 'card_verification_required',
                'PayPal requires the cardholder to complete a verification challenge in a browser; '
                'saving this card through the API is not supported.')
        raise PaymentAPIError(422, 'card_not_saved', 'PayPal did not accept the card: %s' % setup_op.detail)
    if setup_op.outcome in (Outcome.PENDING, Outcome.UNKNOWN, Outcome.SENDING):
        raise OutcomeUnknown(setup_op, 'PayPal has not finished validating the card; repeat the request '
                                       'with the same Idempotency-Key to continue.')

    setup_token_id = setup_op.provider_id
    token_op, won = try_claim(base + ':token', PayPalOperation.Kind.PAYMENT_TOKEN, user=user,
                              idempotency_key=idempotency_key, inputs={'setupTokenId': setup_token_id})
    token_body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(
            token=VaultTokenRequest(id=token_op.inputs['setupTokenId'], type=VaultTokenRequestType.SETUP_TOKEN)),
        **({'customer': Customer(id=known.paypal_customer_id)} if known else {}))

    def apply(op: PayPalOperation, result: PaymentTokenResponse, answer: Answer) -> None:
        if op.outcome != Outcome.DONE:
            return
        entity = _card_entity(result)
        assert entity is not None
        customer_id = _customer_id(result) or (known.paypal_customer_id if known else '')
        if customer_id:
            PayPalCustomer.objects.get_or_create(user=user, defaults={'paypal_customer_id': customer_id})
        expiry = entity.expiry if isinstance(entity.expiry, str) and EXPIRY_RE.match(entity.expiry) else None
        last4 = entity.last_digits if isinstance(entity.last_digits, str) else ''
        bankcard = Bankcard(
            user=user,
            name=entity.name if isinstance(entity.name, str) else '',
            number='XXXX-XXXX-XXXX-%s' % last4[-4:],
            expiry_date=_expiry_date(expiry) if expiry else datetime.date(1970, 1, 31))
        bankcard.card_type = str(entity.brand) if not isinstance(entity.brand, UnsetType) else 'Card'
        bankcard.save()
        PayPalSavedCard.objects.create(
            bankcard=bankcard, payment_token_id=op.provider_id, paypal_customer_id=customer_id)

    token_op, _ = safe_write(
        token_op, won,
        send=lambda key: client.vault.create_payment_token(token_body, pay_pal_request_id=key),
        read=_read_payment_token, outcome_of=outcomes.payment_token, apply=apply, action='card save')
    if token_op.outcome != Outcome.DONE:
        raise OutcomeUnknown(token_op, 'PayPal did not confirm the saved card; repeat the request with the '
                                       'same Idempotency-Key to check.')
    return PayPalSavedCard.objects.select_related('bankcard').get(payment_token_id=token_op.provider_id), won


def delete_card(user, payment_method_id: int) -> bool:
    """
    Hide and disable the card first, then delete it from PayPal's vault.
    Returns True when PayPal confirmed the deletion; False when it will be
    retried by the next DELETE (the card stays hidden and unusable).
    """
    with transaction.atomic():
        card = (PayPalSavedCard.objects.select_for_update().select_related('bankcard')
                .filter(bankcard__pk=payment_method_id, bankcard__user=user).first())
        if card is None:
            raise PaymentAPIError(404, 'payment_method_not_found', 'No such saved card.')
        if card.removed_at is None:
            card.removed_at = timezone.now()
            card.save(update_fields=['removed_at'])
    try:
        result = get_client().vault.with_raw_response.delete_payment_token(card.payment_token_id)
    except (ApiError, httpx.RequestError, ValueError) as e:
        # A failed token fetch or transport failure: the card is already
        # unusable here; the vault delete is retried by the next DELETE.
        logger.warning('PayPal vault delete for saved card %s failed: %s',
                       payment_method_id, translate(e, action='card removal').message)
        return False
    match result:
        case Success():
            pass
        case Failure(response=response) if response.status_code == 404:
            pass  # already gone at PayPal
        case Failure(response=response):
            logger.warning('PayPal vault delete for saved card %s answered HTTP %s',
                           payment_method_id, response.status_code)
            return False
    card.bankcard.delete()
    return True

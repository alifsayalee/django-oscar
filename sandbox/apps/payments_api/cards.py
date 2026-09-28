"""
Saved cards: the card itself lives in PayPal's vault; this app keeps only
Oscar's ``Bankcard`` row -- brand, last four digits, expiry month and the vault
token id -- so a shopper can recognise the card and pay with it again.
"""
import calendar
import re
import secrets
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model
from paypal.core import UNSET, Failure, UnsetType
from paypal.models import (
    Address, PaymentTokenRequest, PaymentTokenRequestCard, PaymentTokenRequestPaymentSource, PaymentTokenResponse)

from .errors import NEVER_SENT, ApiProblem, OutcomeUnknown, ProviderError
from .models import ProviderWrite
from .outcomes import DONE, vault_token_outcome
from .paypal_client import get_client
from .references import key_digest, reference
from .safe_write import Answer, provider_time, safe_write, try_claim

Bankcard = get_model('payment', 'Bankcard')

ADDRESS_FIELDS = {
    'line1': 'address_line_1', 'line2': 'address_line_2', 'city': 'admin_area_2',
    'state': 'admin_area_1', 'postalCode': 'postal_code', 'countryCode': 'country_code',
}


@dataclass(repr=False)
class CardInput:
    """Card details for one request. Never stored, never logged (no repr)."""
    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str = ''
    billing_address: dict[str, str] = field(default_factory=dict)


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(number)):
        d = int(digit)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


@sensitive_variables('data', 'number', 'cvc')
def parse_card(data: object) -> CardInput:
    """Validate card details. Error messages never echo what was sent."""
    if not isinstance(data, dict):
        raise ApiProblem(422, 'invalid_card', '"card" must be an object.')
    number = re.sub(r'[\s-]', '', str(data.get('number', '')))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiProblem(422, 'invalid_card', 'The card number is not valid.')
    expiry = str(data.get('expiry', '')).strip()
    match = re.fullmatch(r'(\d{4})-(\d{2})', expiry) or re.fullmatch(r'(\d{2})/(\d{2}|\d{4})', expiry)
    if not match:
        raise ApiProblem(422, 'invalid_card', '"expiry" must be YYYY-MM (or MM/YY).')
    if '-' in expiry:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        month, year = int(match.group(1)), int(match.group(2))
        year = year + 2000 if year < 100 else year
    today = date.today()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        raise ApiProblem(422, 'invalid_card', 'The card has expired or the expiry is not valid.')
    cvc = str(data.get('securityCode', '')).strip()
    if not re.fullmatch(r'\d{3,4}', cvc):
        raise ApiProblem(422, 'invalid_card', '"securityCode" must be 3 or 4 digits.')
    name = str(data.get('name', '')).strip()[:300]
    address: dict[str, str] = {}
    raw_address = data.get('billingAddress')
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise ApiProblem(422, 'invalid_card', '"billingAddress" must be an object.')
        for key, target in ADDRESS_FIELDS.items():
            value = raw_address.get(key)
            if value not in (None, ''):
                address[target] = str(value).strip()[:300]
        if not re.fullmatch(r'[A-Za-z]{2}', address.get('country_code', '')):
            raise ApiProblem(422, 'invalid_card', '"billingAddress.countryCode" must be a 2-letter country code.')
        address['country_code'] = address['country_code'].upper()
    return CardInput(number=number, expiry='%04d-%02d' % (year, month), security_code=cvc,
                     name=name, billing_address=address)


def card_label(brand: str, last4: str) -> str:
    return ('%s ending %s' % (brand.title(), last4)).strip() if brand else 'Card ending %s' % last4


def _expiry_date(expiry: str) -> date:
    year, month = (int(p) for p in expiry.split('-'))
    return date(year, month, calendar.monthrange(year, month)[1])


def card_body(card: Any) -> dict[str, Any]:
    last4 = card.number[-4:]
    return {
        'paymentMethodId': card.pk,
        'brand': card.card_type,
        'last4': last4,
        'expiry': card.expiry_date.strftime('%Y-%m'),
        'label': card_label(card.card_type, last4),
    }


def list_cards(user: Any) -> list[dict[str, Any]]:
    return [card_body(c) for c in Bankcard.objects.filter(user=user).order_by('pk')]


def _token_answer(token: PaymentTokenResponse) -> Answer:
    token_id = token.id if isinstance(token.id, str) else ''
    source = token.payment_source if not isinstance(token.payment_source, UnsetType) else None
    card = source.card if source is not None and not isinstance(source.card, UnsetType) else None
    status: object = None
    if card is not None and not isinstance(card.verification_status, UnsetType):
        status = card.verification_status
    elif token_id and card is not None and isinstance(card.last_digits, str):
        status = 'stored'  # the token has no status member; its card is what proves it exists
    created = (token.model_extra or {}).get('create_time')
    return Answer(provider_id=token_id, status=status, provider_time=provider_time(created))


@sensitive_variables('card', 'payload', 'body')
def save_card(user: Any, payload: dict[str, Any], idempotency_key: str | None) -> tuple[str, Any]:
    """Vault a card for ``user``. Returns (outcome, Bankcard or None)."""
    card = parse_card(payload.get('card'))
    # The same key is a repeat of the same save; without a key every request is a new save.
    key = idempotency_key or secrets.token_hex(16)
    ref = reference('user', user.pk, 'vault', key_digest(user.pk, key))
    body = PaymentTokenRequest(payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
        number=card.number, expiry=card.expiry, security_code=card.security_code,
        name=card.name or UNSET,
        billing_address=Address(**card.billing_address) if card.billing_address else UNSET)))
    client = get_client()
    tokens: dict[str, PaymentTokenResponse] = {}

    def send(k: str) -> PaymentTokenResponse:
        return client.vault.create_payment_token(body, pay_pal_request_id=k)

    def read(result: PaymentTokenResponse) -> Answer:
        tokens['token'] = result
        return _token_answer(result)

    write = safe_write(ref, send=send, find=send, read=read, outcome_of=vault_token_outcome,
                       repeat_is_safe=True, claim={'kind': ProviderWrite.VAULT_CREATE, 'user': user})
    if write.outcome != DONE:
        return write.outcome, None
    saved = Bankcard.objects.filter(user=user, partner_reference=write.provider_id).first()
    if saved is None:
        token = tokens.get('token')
        if token is None:  # answered from an earlier request whose card row is gone (deleted since)
            raise ApiProblem(410, 'payment_method_deleted', 'This saved card has since been deleted.')
        vaulted = token.payment_source.card  # type: ignore[union-attr]  # present: outcome is done
        brand = str(vaulted.brand) if not isinstance(vaulted.brand, UnsetType) else 'CARD'  # type: ignore[union-attr]
        expiry = vaulted.expiry if isinstance(vaulted.expiry, str) else card.expiry  # type: ignore[union-attr]
        saved = Bankcard(user=user, number='XXXX-XXXX-XXXX-%s' % vaulted.last_digits,  # type: ignore[union-attr]
                         expiry_date=_expiry_date(expiry), partner_reference=write.provider_id)
        saved.card_type = brand
        saved.save()
    return DONE, saved


def delete_card(user: Any, payment_method_id: str) -> str:
    """Remove a saved card: unusable here at once, then deleted from the vault.

    Returns the vault deletion outcome."""
    with transaction.atomic():
        try:
            card = Bankcard.objects.select_for_update().get(pk=int(payment_method_id), user=user)
        except (Bankcard.DoesNotExist, ValueError):
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
        token = card.partner_reference
        card.delete()
        ref = reference('vault-delete', token)
        try:
            claimed = try_claim(ref, kind=ProviderWrite.VAULT_DELETE, user=user, target_id=token)
        except IntegrityError:
            claimed = False
    return delete_vault_token(ref, token, claimed=claimed)


def delete_vault_token(ref: str, token: str, *, claimed: bool) -> str:
    client = get_client()

    def send(k: str) -> str:
        result = client.vault.with_raw_response.delete_payment_token(token)
        if isinstance(result, Failure):
            if result.response.status_code == 404:
                return 'gone'
            result.unwrap()  # raises ApiError with the decoded error body
        return 'deleted'

    try:
        write = safe_write(
            ref, send=send, find=send,  # deleting by id is idempotent: the resend is the check
            read=lambda r: Answer(provider_id=token, status=r, provider_time=timezone.now()),
            outcome_of=lambda s: DONE if s in ('deleted', 'gone') else 'unknown',
            repeat_is_safe=True, claimed=claimed,
            claim={'kind': ProviderWrite.VAULT_DELETE, 'target_id': token})
    except OutcomeUnknown:
        return 'unknown'
    except (ProviderError, *NEVER_SENT):
        return 'failed'  # the card is already unusable here; `paypal_retry_writes` retries the vault deletion
    return str(write.outcome)

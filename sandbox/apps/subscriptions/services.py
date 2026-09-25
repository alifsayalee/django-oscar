"""
Subscription billing use-cases, with Maxio Advanced Billing as the system of record.
"""
import datetime
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, CustomerResponse,
    Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState
from oscar.core.loading import get_model

from . import maxio
from .errors import (
    NEVER_SENT, BillingNotConfigured, InProgress, InvalidRequest, NotFound, ProviderRejected, provider_errors)
from .models import BillingCustomer, BillingSubscription, Outcome
from .safe_write import Answer, WriteResult, safe_write, send_window

logger = logging.getLogger('apps.subscriptions')

UserAddress = get_model('address', 'UserAddress')

T = TypeVar('T')

PLANS_CACHE_SECONDS = 60
RETRYABLE_READ_STATUSES = {429, 502, 503, 504}


# --------------------------------------------------------------------------
# Provider status -> our outcome (the ONE place this mapping lives)
# --------------------------------------------------------------------------

def status_from_provider(state: object) -> str:
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return Outcome.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP):
            return Outcome.PENDING      # Maxio has not finished
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.ON_HOLD | SubscriptionState.PAUSED | SubscriptionState.SUSPENDED):
            return Outcome.PENDING      # exists, but something is outstanding: not done
        case SubscriptionState.FAILED_TO_CREATE:
            return Outcome.FAILED
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return Outcome.FAILED       # happened, then undone: no longer in effect
        case _:
            return Outcome.UNKNOWN      # UNSET, or a state newer than this SDK


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _val(value: T | UnsetType | None) -> T | None:
    """An optional SDK member as a plain value (UNSET -> None)."""
    if value is None or isinstance(value, UnsetType):
        return None
    return value


def _money(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


def _iso(value: Any) -> str | None:
    value = _val(value)
    return value.isoformat() if value is not None else None


def _read(call: Callable[[], T]) -> T:
    """A provider read: retried once when it provably never left, or on a transient status."""
    with provider_errors():
        try:
            return call()
        except NEVER_SENT:
            pass
        except ApiError as e:
            if e.status_code not in RETRYABLE_READ_STATUSES:
                raise
        time.sleep(0.5)
        return call()


def _find_or_none(call: Callable[[], T]) -> T | None:
    """A lookup whose 404 means 'not found'. Other failures propagate to the safe write."""
    try:
        return call()
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def _plan_dict(product: Product, family: str) -> dict[str, Any] | None:
    handle = _val(product.handle)
    if not handle or _val(product.archived_at) is not None:
        return None
    cents = _val(product.price_in_cents)
    interval_unit = _val(product.interval_unit)
    return {
        'planHandle': handle,
        'productId': _val(product.id),
        'name': _val(product.name),
        'description': _val(product.description) or '',
        'price': _money(cents),
        'priceInCents': cents,
        'interval': _val(product.interval),
        'intervalUnit': str(interval_unit) if interval_unit is not None else None,
        'productFamily': family,
    }


def list_plans() -> list[dict[str, Any]]:
    family = _family_or_config_error()
    cache_key = f'maxio:plans:{family}'
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)
    client = _client()
    plans: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _read(lambda: client.product_families.list_products_for_product_family(
            f'handle:{family}', page=page, per_page=200))
        for item in batch:
            plan = _plan_dict(item.product, family)
            if plan is not None:
                plans.append(plan)
        if len(batch) < 200:
            break
        page += 1
    plans.sort(key=lambda p: (p['priceInCents'] or 0, p['planHandle']))
    cache.set(cache_key, plans, PLANS_CACHE_SECONDS)
    return plans


def _family_or_config_error() -> str:
    with provider_errors():
        return maxio.product_family()


def _client() -> MaxioAdvancedBillingClient:
    with provider_errors():
        return maxio.get_client()


def get_plan(plan_handle: str) -> dict[str, Any]:
    for plan in list_plans():
        if plan['planHandle'] == plan_handle:
            return plan
    raise NotFound(f'Unknown plan {plan_handle!r}.', code='unknown_plan')


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------

def _customer_payload(user: Any, ref: str) -> CreateCustomer:
    email = (user.email or '').strip()
    if not email:
        raise InvalidRequest('Your account needs an email address before you can subscribe.',
                             code='email_required')
    address = (UserAddress.objects.filter(user=user, is_default_for_billing=True)
               .select_related('country').first())
    first_name = (user.first_name or (address.first_name if address else '') or email.split('@')[0]).strip()
    last_name = (user.last_name or (address.last_name if address else '') or 'Customer').strip()
    fields: dict[str, Any] = {}
    if address is not None:
        fields = {
            'address': address.line1, 'address_2': address.line2, 'city': address.line4,
            'state': address.state, 'zip': address.postcode,
            'country': address.country.iso_3166_1_a2 if address.country_id else '',
            'phone': str(address.phone_number) if address.phone_number else '',
        }
        fields = {k: v for k, v in fields.items() if v}       # omit, never send empty/None
    return CreateCustomer(first_name=first_name, last_name=last_name, email=email, reference=ref, **fields)


def _customer_answer(response: CustomerResponse) -> Answer:
    customer = response.customer
    customer_id = _val(customer.id)
    return Answer(provider_id=customer_id,
                  outcome=Outcome.DONE if customer_id is not None else Outcome.UNKNOWN,
                  provider_time=_val(customer.created_at))


def ensure_customer(user: Any) -> tuple[BillingCustomer, bool]:
    """
    Make sure a Maxio customer exists for ``user``. Idempotent: returns (record, created_now).
    """
    existing = BillingCustomer.objects.filter(user=user).first()
    if existing is not None and existing.outcome == Outcome.DONE:
        return existing, False
    ref = existing.reference if existing is not None else f'{maxio.reference_prefix()}:customer:{user.pk}'
    payload = _customer_payload(user, ref)
    client = _client()

    with provider_errors(write=True):
        written: WriteResult[BillingCustomer, CustomerResponse] = safe_write(
            BillingCustomer, ref,
            claim_fields={'user': user},
            send=lambda key: client.customers.create_customer(
                body=CreateCustomerRequest(customer=payload)),
            find=lambda key: _find_or_none(lambda: client.customers.read_customer_by_reference(key)),
            read=_customer_answer,
            # Maxio requires customer references to be unique, so a same-reference resend cannot
            # create a second customer; its 422 means "maybe already there" -> look it up.
            repeat_is_safe=True,
            may_be_duplicate=lambda e: e.status_code == 422,
        )
    record = written.record
    if record.outcome == Outcome.DONE:
        return record, written.sent
    if record.outcome == Outcome.SENDING:
        raise InProgress('Your billing account is being set up; retry in a few seconds.')
    raise InProgress('Your billing account could not be confirmed yet; retry shortly.',
                     outcome_unknown=record.outcome == Outcome.UNKNOWN)


def customer_dict(record: BillingCustomer) -> dict[str, Any]:
    return {
        'customerId': record.provider_id,
        'reference': record.reference,
        'outcome': record.outcome,
    }


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------

def _subscription_answer(response: SubscriptionResponse) -> Answer:
    sub = _val(response.subscription)
    if sub is None:
        return Answer(provider_id=None, outcome=Outcome.UNKNOWN)
    state = _val(sub.state)
    return Answer(provider_id=_val(sub.id), outcome=status_from_provider(state),
                  provider_time=_val(sub.created_at), provider_state=str(state) if state is not None else '')


def subscription_dict(sub: Subscription) -> dict[str, Any]:
    product = _val(sub.product)
    state = _val(sub.state)
    plan: dict[str, Any] = {}
    if product is not None:
        interval_unit = _val(product.interval_unit)
        plan = {
            'planHandle': _val(product.handle),
            'planName': _val(product.name),
            'interval': _val(product.interval),
            'intervalUnit': str(interval_unit) if interval_unit is not None else None,
        }
    cents = _val(sub.product_price_in_cents)
    if cents is None and product is not None:
        cents = _val(product.price_in_cents)
    next_billing = _val(sub.next_assessment_at) or _val(sub.current_period_ends_at)
    return {
        'subscriptionId': _val(sub.id),
        'reference': _val(sub.reference),
        'planHandle': plan.get('planHandle'),
        'planName': plan.get('planName'),
        'price': _money(cents),
        'priceInCents': cents,
        'interval': plan.get('interval'),
        'intervalUnit': plan.get('intervalUnit'),
        'state': str(state) if state is not None else None,
        'outcome': status_from_provider(state),
        'nextBillingAt': next_billing.isoformat() if next_billing else None,
        'currentPeriodEndsAt': _iso(sub.current_period_ends_at),
        'activatedAt': _iso(sub.activated_at),
        'createdAt': _iso(sub.created_at),
    }


def _refresh_record(sub: Subscription) -> None:
    """Record the latest state Maxio reports for a subscription we created."""
    sub_id = _val(sub.id)
    if sub_id is None:
        return
    state = _val(sub.state)
    BillingSubscription.objects.filter(provider_id=sub_id).update(
        outcome=status_from_provider(state), provider_state=str(state) if state is not None else '')


def _pending_dict(record: BillingSubscription) -> dict[str, Any]:
    return {
        'subscriptionId': record.provider_id,
        'reference': record.reference,
        'planHandle': record.plan_handle,
        'state': record.provider_state or None,
        'outcome': record.outcome,
        'requestedAt': record.created_at.isoformat(),
    }


def subscribe(user: Any, plan_handle: str) -> tuple[dict[str, Any], str, bool]:
    """
    Subscribe ``user`` to ``plan_handle``. Idempotent per (user, plan) until that
    subscription ends. Returns (body, outcome, created_now).
    """
    plan = get_plan(plan_handle)
    customer, _ = ensure_customer(user)
    client = _client()
    generation = BillingSubscription.objects.filter(
        user=user, plan_handle=plan_handle, outcome=Outcome.FAILED, provider_id__isnull=False).count()
    ref = f'{maxio.reference_prefix()}:subscription:{user.pk}:{plan_handle}:{generation}'
    customer_id = customer.provider_id
    assert customer_id is not None
    collection_method = _collection_method()

    with provider_errors(write=True):
        written: WriteResult[BillingSubscription, SubscriptionResponse] = safe_write(
            BillingSubscription, ref,
            claim_fields={'user': user, 'plan_handle': plan_handle},
            send=lambda key: client.subscriptions.create_subscription(body=CreateSubscriptionRequest(
                subscription=CreateSubscription(product_handle=plan_handle, customer_id=customer_id,
                                                payment_collection_method=collection_method,
                                                reference=key))),
            find=lambda key: _find_or_none(lambda: client.subscriptions.find_subscription(reference=key)),
            read=_subscription_answer,
            # Maxio does not document subscription references as unique: only a lookup is safe.
            repeat_is_safe=False,
        )
    record = written.record

    sub: Subscription | None = None
    if written.result is not None:
        sub = _val(written.result.subscription)
    elif record.provider_id is not None:
        # A repeat of an earlier request: answer from Maxio's current view of it.
        sub = _val(_read(lambda: client.subscriptions.read_subscription(record.provider_id)).subscription)
        if sub is not None:
            _refresh_record(sub)
            record.refresh_from_db()

    if sub is not None:
        body = subscription_dict(sub)
    else:
        body = _pending_dict(record) | {'planName': plan['name'], 'price': plan['price'],
                                        'priceInCents': plan['priceInCents']}
    body['outcome'] = record.outcome
    body['customerId'] = customer_id
    if record.outcome == Outcome.FAILED and record.provider_id is None:
        raise ProviderRejected('The subscription could not be created.')
    return body, record.outcome, written.sent


def _collection_method() -> CollectionMethod:
    """No card is captured in this flow, so subscriptions are invoiced (remittance) by default."""
    value = str(getattr(settings, 'MAXIO_PAYMENT_COLLECTION_METHOD', '') or 'remittance').lower()
    try:
        return CollectionMethod(value)
    except ValueError:
        raise BillingNotConfigured(f'Unsupported MAXIO_PAYMENT_COLLECTION_METHOD {value!r}') from None


def _settle_unresolved(client: MaxioAdvancedBillingClient, record: BillingSubscription) -> None:
    """Ask Maxio about a create whose outcome we never learned (never creates anything)."""
    stale = record.outcome == Outcome.SENDING and record.claimed_at < _stale_before()
    if record.outcome != Outcome.UNKNOWN and not stale:
        return
    try:
        with provider_errors():
            found = _find_or_none(lambda: client.subscriptions.find_subscription(reference=record.reference))
    except Exception:
        logger.warning('Could not reconcile subscription claim %s yet', record.reference)
        return
    if found is None:
        if stale:
            BillingSubscription.objects.filter(pk=record.pk, outcome=Outcome.SENDING).update(
                outcome=Outcome.UNKNOWN)
        return
    answer = _subscription_answer(found)
    if answer.provider_id is not None:
        BillingSubscription.objects.filter(pk=record.pk).update(
            provider_id=answer.provider_id, outcome=answer.outcome, provider_state=answer.provider_state,
            provider_time=answer.provider_time)


def _stale_before() -> datetime.datetime:
    now: datetime.datetime = timezone.now()
    return now - send_window()


def my_subscriptions(user: Any) -> dict[str, Any]:
    customer = BillingCustomer.objects.filter(user=user, outcome=Outcome.DONE).first()
    client = _client()

    for record in BillingSubscription.objects.filter(
            user=user, provider_id__isnull=True, outcome__in=[Outcome.SENDING, Outcome.UNKNOWN]):
        _settle_unresolved(client, record)

    subscriptions: list[dict[str, Any]] = []
    if customer is not None and customer.provider_id is not None:
        customer_id = customer.provider_id
        for item in _read(lambda: client.customers.list_customer_subscriptions(customer_id)):
            sub = _val(item.subscription)
            if sub is None:
                continue
            _refresh_record(sub)
            subscriptions.append(subscription_dict(sub))
        subscriptions.sort(key=lambda s: s['createdAt'] or '', reverse=True)

    unresolved = [
        _pending_dict(r) for r in BillingSubscription.objects.filter(
            user=user, provider_id__isnull=True, outcome__in=[Outcome.SENDING, Outcome.UNKNOWN])
    ]
    return {
        'customerId': customer.provider_id if customer is not None else None,
        'subscriptions': subscriptions,
        'unresolvedRequests': unresolved,
    }


def get_subscription(user: Any, subscription_id: int) -> dict[str, Any]:
    customer = BillingCustomer.objects.filter(user=user, outcome=Outcome.DONE).first()
    if customer is None:
        raise NotFound('Subscription not found.')
    client = _client()
    try:
        response = _read(lambda: client.subscriptions.read_subscription(subscription_id))
    except ProviderRejected as e:
        if e.status_code == 404:
            raise NotFound('Subscription not found.') from e
        raise
    sub = _val(response.subscription)
    owner = _val(sub.customer) if sub is not None else None
    if sub is None or owner is None or _val(owner.id) != customer.provider_id:
        raise NotFound('Subscription not found.')     # never reveal other customers' subscriptions
    _refresh_record(sub)
    return subscription_dict(sub)


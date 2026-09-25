"""
Subscription billing flows, with Maxio Advanced Billing as the system of record.

Every provider write that creates something (a customer, a subscription) goes
through ``safe_write``: claim a reference in the database first, send the write
carrying that reference, and if the answer is lost look it up by the same
reference instead of sending a second create.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CustomerError, CreateSubscription, CreateSubscriptionRequest, CustomerErrorResponse1,
    CustomerResponse, ErrorListResponse1, Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod

from .maxio import NEVER_SENT, BillingError, get_client, raise_for_read, subscription_outcome
from .models import ProviderWrite

logger = logging.getLogger('apps.subscriptions')

T = TypeVar('T')
R = TypeVar('R')


# ---------------------------------------------------------------------------
# Small helpers for SDK values: UNSET never leaves this module.
# ---------------------------------------------------------------------------

def _opt(value: T | None | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _iso(value: datetime | None | UnsetType) -> str | None:
    value = _opt(value)
    return value.isoformat() if value is not None else None


def _price(cents: int | None) -> str | None:
    # Maxio quotes prices in cents, i.e. two minor units.
    return str((Decimal(cents) / 100).quantize(Decimal('0.01'))) if cents is not None else None


def _reference(*parts: str) -> str:
    return '-'.join((settings.MAXIO_REFERENCE_PREFIX,) + parts)


def _send_window() -> timedelta:
    # Longer than one attempt can take: past this a "sending" claim is stale.
    return timedelta(seconds=settings.MAXIO_TIMEOUT * 3 + 30)


# ---------------------------------------------------------------------------
# The claim store (the Django database) and the safe write.
# ---------------------------------------------------------------------------

class OutcomeUnknown(BillingError):
    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'outcome_unknown',
            'The billing provider did not confirm the request; it may still have been applied. '
            'Retry with the same request to find out.',
            outcome_unknown=True)
        self.reference = reference


@dataclass(frozen=True)
class Answer:
    """What one provider response says, read the same way for every write."""
    provider_id: str
    outcome: str
    state: str = ''
    provider_time: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    echo_mismatch: str = ''


def try_claim(reference: str, kind: str, user: Any, plan_handle: str = '') -> bool:
    """Insert-or-fail: the UNIQUE constraint on reference lets exactly one caller through."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(
                reference=reference, kind=kind, user=user, plan_handle=plan_handle,
                outcome=ProviderWrite.SENDING, claimed_at=timezone.now())
    except IntegrityError:
        return False
    return True


def load_existing(reference: str) -> ProviderWrite:
    return ProviderWrite.objects.get(reference=reference)


def complete(reference: str, outcome: str, answer: Answer | None = None) -> ProviderWrite:
    record = load_existing(reference)
    record.outcome = outcome
    if answer is not None:
        record.provider_id = answer.provider_id
        record.provider_state = answer.state
        record.provider_time = answer.provider_time
        record.detail = answer.detail
    if outcome == ProviderWrite.FAILED and not record.provider_id:
        # Nothing exists upstream: release the claim so the same reference can be used again.
        record.delete()
        return record
    record.save()
    return record


def _rejection(exc: ApiError) -> BillingError:
    """A provider 4xx on a write that did not land."""
    if exc.status_code in (401, 403):
        return BillingError(502, 'billing_misconfigured', 'Billing provider refused our credentials.')
    if exc.status_code == 429:
        return BillingError(503, 'billing_rate_limited', 'Billing provider is rate limiting us.')
    if exc.status_code in (400, 422):
        return BillingError(422, 'billing_rejected', 'The billing provider rejected the request.',
                            details=_error_messages(exc))
    return BillingError(502, 'billing_unavailable', 'Billing provider rejected the request.')


def _error_messages(exc: ApiError) -> list[str]:
    body = exc.error
    if isinstance(body, ErrorListResponse1):
        return list(body.errors)
    if isinstance(body, CustomerErrorResponse1):
        errors = _opt(body.errors)
        if isinstance(errors, list):
            return list(errors)
        if isinstance(errors, CustomerError):
            message = _opt(errors.customer)
            return [message] if message else []
    return []


def says_reference_taken(exc: ApiError) -> bool:
    """Maxio answers a reused customer reference with 422 "Reference: must be unique - ... taken"."""
    return exc.status_code == 422 and any(
        'reference' in m.lower() and 'taken' in m.lower() for m in _error_messages(exc))


def safe_write(reference: str, *, kind: str, user: Any, plan_handle: str = '',
               send: Callable[[str], R], find: Callable[[str], R | None],
               read: Callable[[R], Answer], repeat_is_safe: bool) -> ProviderWrite:
    """
    The one path for every provider write that creates something.

    reference       derived from the operation, the same on every attempt and every repeat
    send(ref)       makes the provider call carrying ``ref`` as the write's reference field
    find(ref)       looks the write up by ``ref``; None when the provider has nothing (yet)
    read(result)    that response as an ``Answer``
    repeat_is_safe  the provider refuses a second create with the same reference
    """
    # 1. CLAIM FIRST.
    checking = False
    if not try_claim(reference, kind, user, plan_handle):
        existing = load_existing(reference)
        if existing.outcome == ProviderWrite.SENDING and existing.claimed_at > timezone.now() - _send_window():
            return existing  # in flight elsewhere: no provider call
        if existing.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return existing  # done / pending / failed-with-id / needs_review: answer from it
        checking = True  # stale sender or unresolved: look, never create blind
        existing.claimed_at = timezone.now()
        existing.save(update_fields=['claimed_at', 'updated_at'])

    resending = checking and repeat_is_safe

    # 2. CALL (a first attempt, or a check by same-reference resend).
    result: R | None = None
    if resending or not checking:
        try:
            result = send(reference)
        except NEVER_SENT as exc:
            complete(reference, ProviderWrite.UNKNOWN if resending else ProviderWrite.FAILED)
            if resending:
                raise OutcomeUnknown(reference) from exc
            raise BillingError(502, 'billing_unreachable', 'Billing provider is unreachable.') from exc
        except ApiError as exc:
            if says_reference_taken(exc):
                pass  # an earlier attempt landed: find it below
            elif exc.status_code < 500:
                refused = exc.status_code in (400, 422) or not resending
                complete(reference, ProviderWrite.FAILED if refused else ProviderWrite.UNKNOWN)
                if not refused:
                    raise OutcomeUnknown(reference) from exc
                raise _rejection(exc) from exc
            # a 5xx may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: may have landed

    # 3. CHECK, by the reference we sent.
    if result is None:
        try:
            result = find(reference)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            complete(reference, ProviderWrite.UNKNOWN)
            raise OutcomeUnknown(reference) from exc
        if result is None:
            complete(reference, ProviderWrite.UNKNOWN)  # not found YET: a timed-out write can still land
            raise OutcomeUnknown(reference)

    # 4. VERIFY before keeping it.
    answer = read(result)
    if answer.echo_mismatch:
        complete(reference, ProviderWrite.NEEDS_REVIEW, answer)
        logger.error('Maxio write %s needs review: %s', reference, answer.echo_mismatch)
        raise BillingError(502, 'needs_review', 'The billing provider applied something other than requested; '
                                                'it has been flagged for review.')

    # 5. COMPLETE from what the provider said.
    return complete(reference, answer.outcome, answer)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def plan_to_dict(product: Product) -> dict[str, Any]:
    cents = _opt(product.price_in_cents)
    interval_unit = _opt(product.interval_unit)
    return {
        'planHandle': _opt(product.handle),
        'planId': _opt(product.id),
        'name': _opt(product.name),
        'description': _opt(product.description) or '',
        'priceInCents': cents,
        'price': _price(cents),
        'interval': _opt(product.interval),
        'intervalUnit': str(interval_unit) if interval_unit is not None else None,
        'requiresPaymentMethod': bool(_opt(product.require_credit_card)),
    }


def list_plans() -> list[dict[str, Any]]:
    client = get_client()
    family = 'handle:%s' % settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    per_page = 50
    plans: list[dict[str, Any]] = []
    for page in range(1, 21):
        try:
            batch = client.product_families.list_products_for_product_family(family, page=page, per_page=per_page)
        except Exception as exc:
            raise_for_read(exc, 'list plans')
        for item in batch:
            product = item.product
            if _opt(product.archived_at) is not None or not _opt(product.handle):
                continue
            plans.append(plan_to_dict(product))
        if len(batch) < per_page:
            break
    return plans


def get_plan(plan_handle: str) -> dict[str, Any] | None:
    for plan in list_plans():
        if plan['planHandle'] == plan_handle:
            return plan
    return None


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def customer_reference(user: Any) -> str:
    return _reference('u%s' % user.pk, 'customer')


def _read_customer(response: CustomerResponse) -> Answer:
    customer = response.customer
    customer_id = _opt(customer.id)
    if customer_id is None:
        return Answer(provider_id='', outcome=ProviderWrite.UNKNOWN)
    return Answer(provider_id=str(customer_id), outcome=ProviderWrite.DONE,
                  provider_time=_opt(customer.created_at),
                  detail={'email': _opt(customer.email)})


def _find_customer(reference: str) -> CustomerResponse | None:
    try:
        return get_client().customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise


class InProgress(Exception):
    """Another request for the same thing is in flight."""


def ensure_customer(user: Any) -> int:
    """The Maxio customer id for ``user``, creating the customer exactly once."""
    reference = customer_reference(user)
    email = user.email or ''
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=user.first_name or email.split('@')[0] or 'Customer',
        last_name=user.last_name or 'Shopper',
        email=email,
        reference=reference,
    ))
    record = safe_write(
        reference, kind=ProviderWrite.KIND_CUSTOMER, user=user,
        send=lambda ref: get_client().customers.create_customer(body=body),
        find=_find_customer,
        read=_read_customer,
        repeat_is_safe=True,  # Maxio refuses a second customer with the same reference
    )
    if record.outcome == ProviderWrite.SENDING:
        raise InProgress()
    if record.outcome != ProviderWrite.DONE or not record.provider_id:
        raise OutcomeUnknown(reference)
    return int(record.provider_id)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def subscription_reference(user: Any, plan_handle: str, idempotency_key: str) -> str:
    key = hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:16]
    return _reference('u%s' % user.pk, 'sub', plan_handle, key)


def collection_method() -> CollectionMethod:
    try:
        return CollectionMethod(settings.MAXIO_PAYMENT_COLLECTION_METHOD.strip().lower())
    except ValueError:
        raise ImproperlyConfigured('MAXIO_PAYMENT_COLLECTION_METHOD must be one of %s' % ', '.join(
            m.value for m in CollectionMethod)) from None


def subscription_detail(subscription: Subscription) -> dict[str, Any]:
    product = _opt(subscription.product)
    cents = _opt(subscription.product_price_in_cents)
    if cents is None and product is not None:
        cents = _opt(product.price_in_cents)
    state = _opt(subscription.state)
    return {
        'subscriptionId': _opt(subscription.id),
        'reference': _opt(subscription.reference),
        'planHandle': _opt(product.handle) if product is not None else None,
        'planName': _opt(product.name) if product is not None else None,
        'state': str(state) if state is not None else None,
        'outcome': subscription_outcome(state),
        'priceInCents': cents,
        'price': _price(cents),
        'nextBillingAt': _iso(subscription.next_assessment_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'createdAt': _iso(subscription.created_at),
    }


def _answer_for_subscription(plan_handle: str) -> Callable[[SubscriptionResponse], Answer]:
    def read(response: SubscriptionResponse) -> Answer:
        subscription = _opt(response.subscription)
        if subscription is None or _opt(subscription.id) is None:
            return Answer(provider_id='', outcome=ProviderWrite.UNKNOWN)
        detail = subscription_detail(subscription)
        mismatch = ''
        if detail['planHandle'] != plan_handle:
            mismatch = 'asked for plan %r, provider has %r' % (plan_handle, detail['planHandle'])
        return Answer(provider_id=str(detail['subscriptionId']), outcome=detail['outcome'],
                      state=detail['state'] or '', provider_time=_opt(subscription.created_at),
                      detail=detail, echo_mismatch=mismatch)
    return read


def _find_subscription(reference: str) -> SubscriptionResponse | None:
    try:
        response = get_client().subscriptions.find_subscription(reference=reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    return response if _opt(response.subscription) is not None else None


@dataclass(frozen=True)
class SubscribeResult:
    record: ProviderWrite
    plan: dict[str, Any]


def subscribe(user: Any, plan_handle: str, idempotency_key: str) -> SubscribeResult:
    plan = get_plan(plan_handle)
    if plan is None:
        raise BillingError(400, 'unknown_plan', 'No such plan: %s' % plan_handle)
    payment_collection_method = collection_method()
    customer_id = ensure_customer(user)
    reference = subscription_reference(user, plan_handle, idempotency_key)
    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan_handle,
        customer_id=customer_id,
        reference=reference,
        payment_collection_method=payment_collection_method,
    ))
    record = safe_write(
        reference, kind=ProviderWrite.KIND_SUBSCRIPTION, user=user, plan_handle=plan_handle,
        send=lambda ref: get_client().subscriptions.create_subscription(body=body),
        find=_find_subscription,
        read=_answer_for_subscription(plan_handle),
        repeat_is_safe=False,  # uniqueness of a subscription reference is not documented: look up, never resend
    )
    return SubscribeResult(record=record, plan=plan)


def record_to_dict(record: ProviderWrite) -> dict[str, Any]:
    detail = dict(record.detail or {})
    outcome = record.outcome
    if outcome == ProviderWrite.SENDING:
        outcome = 'in_progress'
    detail.update({
        'subscriptionId': int(record.provider_id) if record.provider_id else None,
        'reference': record.reference,
        'planHandle': detail.get('planHandle') or record.plan_handle,
        'state': record.provider_state or None,
        'outcome': outcome,
    })
    for key in ('planName', 'priceInCents', 'price', 'nextBillingAt', 'currentPeriodEndsAt', 'createdAt'):
        detail.setdefault(key, None)
    return detail


def _settle(record: ProviderWrite) -> None:
    """Re-check an unsettled subscription claim by its reference; only the provider's answer settles it."""
    stale = record.claimed_at <= timezone.now() - _send_window()
    if record.outcome == ProviderWrite.SENDING and not stale:
        return
    try:
        found = _find_subscription(record.reference)
    except (ApiError, httpx.RequestError, ValueError):
        return  # still unknown
    if found is None:
        if record.outcome == ProviderWrite.SENDING:
            complete(record.reference, ProviderWrite.UNKNOWN)
        return
    answer = _answer_for_subscription(record.plan_handle)(found)
    outcome = ProviderWrite.NEEDS_REVIEW if answer.echo_mismatch else answer.outcome
    complete(record.reference, outcome, answer)


def my_subscriptions(user: Any) -> list[dict[str, Any]]:
    customer = ProviderWrite.objects.filter(
        user=user, kind=ProviderWrite.KIND_CUSTOMER, outcome=ProviderWrite.DONE).first()
    local = {r.reference: r for r in ProviderWrite.objects.filter(user=user, kind=ProviderWrite.KIND_SUBSCRIPTION)}
    remote: list[Subscription] = []
    if customer is not None and customer.provider_id:
        try:
            responses = get_client().customers.list_customer_subscriptions(int(customer.provider_id))
        except Exception as exc:
            raise_for_read(exc, 'list subscriptions')
        remote = [s for s in (_opt(r.subscription) for r in responses) if s is not None]

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for subscription in remote:
        detail = subscription_detail(subscription)
        reference = detail['reference']
        record = local.get(reference) if reference else None
        if record is not None:
            seen.add(record.reference)
            # The provider's word replaces what we stored.
            if record.outcome != ProviderWrite.NEEDS_REVIEW:
                outcome = detail['outcome']
                answer = Answer(provider_id=str(detail['subscriptionId']), outcome=outcome,
                                state=detail['state'] or '', provider_time=_opt(subscription.created_at),
                                detail=detail)
                complete(record.reference, outcome, answer)
            detail['outcome'] = ProviderWrite.NEEDS_REVIEW if record.outcome == ProviderWrite.NEEDS_REVIEW \
                else detail['outcome']
        entries.append(detail)

    for reference, record in local.items():
        if reference in seen:
            continue
        if record.outcome in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.PENDING):
            _settle(record)
            record.refresh_from_db()
        entries.append(record_to_dict(record))
    return entries

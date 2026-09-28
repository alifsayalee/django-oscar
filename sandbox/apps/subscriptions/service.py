"""
Subscription billing flows, with Maxio as the system of record.

Plans and subscriptions live in Maxio; locally we keep only the claim for
each write we make (``MaxioWrite``). The shopper is Oscar's own user model.
"""
import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest,
    CustomerErrorResponse1, CustomerResponse, ErrorListResponse1, Product, Subscription,
    SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod

from . import maxio
from .errors import InvalidRequest, error_messages, translate
from .models import MaxioWrite
from .outcomes import customer_outcome, state_value, status_from_provider
from .safe_write import Answer, WriteResult, reference_prefix, safe_write, settle

logger = logging.getLogger(__name__)

R = TypeVar('R')

PAGE_SIZE = 200  # the maximum Maxio returns per page
MAX_PAGES = 10

# ISO 4217 minor units for currencies that do not have two.
CURRENCY_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def _set(value: Any) -> Any:
    """An SDK member as a plain value: UNSET becomes None."""
    return None if isinstance(value, UnsetType) else value


def _text(value: Any) -> str | None:
    value = _set(value)
    if value is None:
        return None
    return value.value if hasattr(value, 'value') else str(value)


def _iso(value: Any) -> str | None:
    value = _set(value)
    return value.isoformat() if isinstance(value, datetime) else None


def format_amount(cents: int | None, currency: str | None) -> str | None:
    if cents is None:
        return None
    places = CURRENCY_EXPONENT.get(currency or '', 2)
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal(1).scaleb(-places)))


# --- plans -----------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    name: str | None
    description: str | None
    price_in_cents: int | None
    currency: str | None
    interval: int | None
    interval_unit: str | None
    trial_price_in_cents: int | None
    trial_interval: int | None
    trial_interval_unit: str | None
    initial_charge_in_cents: int | None
    requires_payment_method: bool

    def as_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'name': self.name,
            'description': self.description,
            'price': {
                'amountInCents': self.price_in_cents,
                'amount': format_amount(self.price_in_cents, self.currency),
                'currency': self.currency,
                'interval': self.interval,
                'intervalUnit': self.interval_unit,
            },
            'trial': ({
                'priceInCents': self.trial_price_in_cents,
                'interval': self.trial_interval,
                'intervalUnit': self.trial_interval_unit,
            } if self.trial_interval else None),
            'setupFeeInCents': self.initial_charge_in_cents,
            'requiresPaymentMethod': self.requires_payment_method,
        }


@dataclass(frozen=True)
class SiteInfo:
    currency: str | None
    relationship_invoicing: bool


_site_info: SiteInfo | None = None
_site_lock = threading.Lock()


def site_info() -> SiteInfo:
    """The Maxio site's currency and invoicing architecture; read once per process."""
    global _site_info
    if _site_info is None:
        client = maxio.get_client()
        try:
            site = maxio.read(lambda: client.sites.read_site()).site
        except (ApiError, httpx.RequestError, ValueError) as exc:
            raise translate(exc, operation='read_site') from exc
        with _site_lock:
            _site_info = SiteInfo(
                currency=_set(site.currency),
                # Unreported means the current (Relationship Invoicing) architecture.
                relationship_invoicing=_set(site.relationship_invoicing_enabled) is not False,
            )
    return _site_info


def invoice_collection_method() -> CollectionMethod:
    """
    How a subscription with no payment method on file is billed: by invoice.
    The value depends on the site's invoicing architecture.
    """
    if site_info().relationship_invoicing:
        return CollectionMethod.REMITTANCE
    return CollectionMethod.INVOICE


def _plan(product: Product, currency: str | None) -> Plan | None:
    handle = _set(product.handle)
    if not handle or _set(product.archived_at) is not None:
        return None
    return Plan(
        handle=handle,
        name=_set(product.name),
        description=_set(product.description) or None,
        price_in_cents=_set(product.price_in_cents),
        currency=currency,
        interval=_set(product.interval),
        interval_unit=_text(product.interval_unit),
        trial_price_in_cents=_set(product.trial_price_in_cents),
        trial_interval=_set(product.trial_interval),
        trial_interval_unit=_text(product.trial_interval_unit),
        initial_charge_in_cents=_set(product.initial_charge_in_cents),
        requires_payment_method=bool(_set(product.require_credit_card)),
    )


def product_family() -> str:
    family = getattr(settings, 'MAXIO_DEFAULT_PRODUCT_FAMILY', None)
    if not family:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY is not set')
    return str(family)


def list_plans() -> list[Plan]:
    client = maxio.get_client()
    family_id = f'handle:{product_family()}'
    currency = site_info().currency
    plans: list[Plan] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            products = maxio.read(lambda: client.product_families.list_products_for_product_family(
                family_id, page=page, per_page=PAGE_SIZE))
        except (ApiError, httpx.RequestError, ValueError) as exc:
            # An unknown family answers 404 with an empty body, which arrives as a
            # ValueError: either way the catalogue is unavailable, not the caller's fault.
            raise translate(exc, operation='list_products_for_product_family') from exc
        plans.extend(p for p in (_plan(r.product, currency) for r in products) if p)
        if len(products) < PAGE_SIZE:
            break
    return plans


def get_plan(handle: str) -> Plan:
    for plan in list_plans():
        if plan.handle == handle:
            return plan
    raise InvalidRequest(400, 'unknown_plan', f'No subscription plan with handle {handle!r}.')


# --- customers -------------------------------------------------------------

def customer_reference(user: Any) -> str:
    return f'{reference_prefix()}:u:{user.pk}'


def _reference_taken(e: ApiError) -> bool:
    """Maxio's 422 for a duplicate reference: an earlier attempt landed."""
    if not isinstance(e.error, (CustomerErrorResponse1, ErrorListResponse1)):
        return False
    return any(m.lower().startswith('reference') and 'taken' in m.lower()
               for m in error_messages(e.error))


def _not_found_is_none(lookup: Callable[[str], R]) -> Callable[[str], R | None]:
    """A lookup by reference where Maxio's 404 means "not found (yet)"."""
    def find(ref: str) -> R | None:
        try:
            return maxio.read(lambda: lookup(ref))
        except ApiError as e:
            if e.status_code == 404:
                return None
            raise
    return find


def _read_customer(response: CustomerResponse) -> Answer:
    customer = response.customer
    customer_id = _set(customer.id)
    return Answer(
        provider_id=str(customer_id) if customer_id is not None else None,
        status=customer.reference,  # customers carry no status; the echoed reference decides
        provider_time=_set(customer.created_at),
    )


def ensure_customer(user: Any) -> WriteResult[CustomerResponse]:
    """Make sure a Maxio customer exists for ``user``, creating it at most once."""
    email = (user.email or '').strip()
    if not email:
        raise InvalidRequest(400, 'email_required', 'Your account needs an email address to subscribe.')
    first_name = (user.first_name or '').strip() or email.split('@')[0]
    last_name = (user.last_name or '').strip() or 'Customer'
    ref = customer_reference(user)
    client = maxio.get_client()

    def send(reference: str) -> CustomerResponse:
        return client.customers.create_customer(
            body=CreateCustomerRequest(customer=CreateCustomer(
                first_name=first_name, last_name=last_name, email=email, reference=reference)),
            request_options={'timeout': maxio.WRITE_TIMEOUT})

    try:
        return safe_write(
            ref,
            kind=MaxioWrite.CUSTOMER,
            user=user,
            send=send,
            find=_not_found_is_none(client.customers.read_customer_by_reference),
            read=_read_customer,
            outcome_of=customer_outcome(ref),
            repeat_is_safe=True,  # Maxio allows only one customer per reference
            already_exists=_reference_taken,
        )
    except (ApiError, httpx.RequestError, ValueError) as exc:
        # Refused, or never sent: the claim is already released and nothing landed.
        raise translate(exc, operation='create_customer') from exc


# --- subscriptions ---------------------------------------------------------

def subscription_reference(user: Any, plan_handle: str, idempotency_key: str | None) -> str:
    key = f'key:{idempotency_key}' if idempotency_key else f'plan:{plan_handle}'
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return f'{reference_prefix()}:u:{user.pk}:s:{digest}'


def _subscription_reader(plan_handle: str, customer_id: int,
                         price_in_cents: int | None) -> Callable[[SubscriptionResponse], Answer]:
    """Read a subscription response, checking it is the plan, customer and price we asked for."""
    def read(response: SubscriptionResponse) -> Answer:
        subscription = _set(response.subscription)
        if subscription is None:
            return Answer(provider_id=None, status=None, provider_time=None)
        subscription_id = _set(subscription.id)
        product = _set(subscription.product)
        customer = _set(subscription.customer)
        as_asked = (
            (product is None or _set(product.handle) == plan_handle)
            and (customer is None or _set(customer.id) == customer_id)
            and (price_in_cents is None
                 or _set(subscription.product_price_in_cents) in (None, price_in_cents))
        )
        if not as_asked:
            logger.error('Maxio subscription %s differs from what was asked (plan %s, customer %s)',
                         subscription_id, plan_handle, customer_id)
        return Answer(
            provider_id=str(subscription_id) if subscription_id is not None else None,
            status=_set(subscription.state),
            provider_time=_set(subscription.created_at),
            provider_state=state_value(_set(subscription.state)) or '',
            as_asked=as_asked,
        )
    return read


@dataclass
class SubscribeResult:
    outcome: str
    record: MaxioWrite
    subscription: Subscription | None
    stage: str  # which write the outcome belongs to: "customer" or "subscription"


def _find_subscription(ref: str) -> SubscriptionResponse | None:
    client = maxio.get_client()
    return _not_found_is_none(lambda r: client.subscriptions.find_subscription(reference=r))(ref)


def subscribe(user: Any, plan_handle: str, idempotency_key: str | None = None) -> SubscribeResult:
    plan = get_plan(plan_handle)
    if plan.requires_payment_method:
        # This API does not capture cards, so such a plan cannot be signed up for here.
        raise InvalidRequest(422, 'payment_method_required',
                             f'Plan {plan.handle!r} requires a payment method, which this API does not collect.')
    collection_method = invoice_collection_method()

    customer = ensure_customer(user)
    if customer.record.outcome != MaxioWrite.DONE or not customer.record.provider_id:
        return SubscribeResult(customer.record.outcome, customer.record, None, 'customer')
    customer_id = int(customer.record.provider_id)

    ref = subscription_reference(user, plan.handle, idempotency_key)
    client = maxio.get_client()

    def send(reference: str) -> SubscriptionResponse:
        return client.subscriptions.create_subscription(body=CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan.handle, customer_id=customer_id, reference=reference,
                # No card is captured, so the subscription is billed by invoice.
                payment_collection_method=collection_method)),
            request_options={'timeout': maxio.WRITE_TIMEOUT})

    try:
        written = safe_write(
            ref,
            kind=MaxioWrite.SUBSCRIPTION,
            user=user,
            plan_handle=plan.handle,
            send=send,
            find=_find_subscription,
            read=_subscription_reader(plan.handle, customer_id, plan.price_in_cents),
            outcome_of=status_from_provider,
            repeat_is_safe=False,  # reference uniqueness is not documented: check by lookup only
            already_exists=_reference_taken,
        )
    except (ApiError, httpx.RequestError, ValueError) as exc:
        # Refused, or never sent: the claim is already released and nothing landed.
        raise translate(exc, operation='create_subscription') from exc
    subscription = _set(written.result.subscription) if written.result is not None else None
    if subscription is None and written.record.provider_id:
        # A repeat answered from the record: read the subscription back for display.
        try:
            found = _find_subscription(ref)
            subscription = _set(found.subscription) if found is not None else None
        except (ApiError, httpx.RequestError, ValueError) as exc:
            logger.warning('Could not read back subscription %s: %s', ref, type(exc).__name__)
    return SubscribeResult(written.record.outcome, written.record, subscription, 'subscription')


def subscription_json(subscription: Subscription | None, *, outcome: str,
                      record: MaxioWrite | None = None) -> dict[str, Any]:
    product = _set(subscription.product) if subscription is not None else None
    currency = _set(subscription.currency) if subscription is not None else None
    price = _set(subscription.product_price_in_cents) if subscription is not None else None
    subscription_id = _set(subscription.id) if subscription is not None else None
    if subscription_id is None and record is not None and record.provider_id:
        subscription_id = int(record.provider_id)
    state = state_value(_set(subscription.state)) if subscription is not None else None
    return {
        'subscriptionId': subscription_id,
        'status': outcome,
        'state': state or (record.provider_state if record is not None else None) or None,
        'reference': (_set(subscription.reference) if subscription is not None else None)
        or (record.reference if record is not None else None),
        'plan': {
            'planHandle': (_set(product.handle) if product is not None else None)
            or (record.plan_handle if record is not None else None),
            'name': _set(product.name) if product is not None else None,
        },
        'price': {
            'amountInCents': price,
            'amount': format_amount(price, currency),
            'currency': currency,
            'interval': _set(product.interval) if product is not None else None,
            'intervalUnit': _text(product.interval_unit) if product is not None else None,
        },
        'nextBillingAt': _iso(subscription.next_assessment_at) if subscription is not None else None,
        'currentPeriodEndsAt': (_iso(subscription.current_period_ends_at)
                                if subscription is not None else None),
        'createdAt': _iso(subscription.created_at) if subscription is not None else None,
    }


def my_subscriptions(user: Any) -> list[dict[str, Any]]:
    """
    The user's subscriptions as Maxio has them, plus any of our writes that
    have not settled yet. Unknown writes are looked up (never re-sent) first.
    """
    writes = list(MaxioWrite.objects.filter(user=user, kind=MaxioWrite.SUBSCRIPTION))
    customer_write = MaxioWrite.objects.filter(
        user=user, kind=MaxioWrite.CUSTOMER, outcome=MaxioWrite.DONE,
        provider_id__isnull=False).first()

    if customer_write is not None:
        customer_id = int(customer_write.provider_id or 0)
        for index, write in enumerate(writes):
            if write.outcome in (MaxioWrite.UNKNOWN, MaxioWrite.SENDING):
                # The plan's price may have changed since: check plan and customer only.
                reader = _subscription_reader(write.plan_handle, customer_id, None)
                writes[index] = settle(write, find=_find_subscription, read=reader,
                                       outcome_of=status_from_provider).record

    by_provider_id = {w.provider_id: w for w in writes if w.provider_id}
    entries: list[dict[str, Any]] = []

    if customer_write is not None:
        client = maxio.get_client()
        customer_id = int(customer_write.provider_id or 0)
        try:
            listed = maxio.read(lambda: client.customers.list_customer_subscriptions(customer_id))
        except (ApiError, httpx.RequestError, ValueError) as exc:
            raise translate(exc, operation='list_customer_subscriptions') from exc
        for item in listed:
            subscription = _set(item.subscription)
            if subscription is None:
                continue
            record = by_provider_id.pop(str(_set(subscription.id)), None)
            entries.append(subscription_json(
                subscription, outcome=status_from_provider(_set(subscription.state)), record=record))

    # Our writes Maxio did not list (not settled, or not visible yet).
    for write in writes:
        if write.provider_id in by_provider_id or not write.provider_id:
            if write.outcome == MaxioWrite.FAILED and not write.provider_id:
                continue  # refused before anything landed
            entries.append(subscription_json(None, outcome=write.outcome, record=write))

    entries.sort(key=lambda e: e['createdAt'] or '', reverse=True)
    return entries

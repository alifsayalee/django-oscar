"""
Subscription billing flows against Maxio: plans, customer, subscribe, read back.

Every SDK value is mapped into plain Python here, so nothing of the SDK's
(``UNSET`` in particular) reaches a response encoder.
"""
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeVar

from django.conf import settings
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models.enums import CollectionMethod
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest,
    CustomerResponse, Product, Subscription, SubscriptionResponse)

from .errors import InvalidRequest, ProviderError, provider_call
from .maxio import get_client
from .models import MaxioCustomer, Outcome, SubscriptionEnrollment
from .outcomes import status_from_provider
from .writes import Answer, Claim, WriteResult, idempotency_options, safe_write

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = Any

T = TypeVar('T')

PLANS_PAGE_SIZE = 200

_collection_method: CollectionMethod | None = None


def _v(value: T | UnsetType) -> T | None:
    """An SDK member as a plain value: UNSET becomes None."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: datetime | None | UnsetType) -> str | None:
    value = _v(value)
    return value.astimezone(dt_timezone.utc).isoformat() if value is not None else None


def _price(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


# ---------------------------------------------------------------- plans

@dataclass(frozen=True)
class Plan:
    handle: str
    product_id: int | None
    name: str | None
    description: str | None
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    requires_payment_method: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'productId': self.product_id,
            'name': self.name,
            'description': self.description,
            'priceInCents': self.price_in_cents,
            'price': _price(self.price_in_cents),
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
            'requiresPaymentMethod': self.requires_payment_method,
        }


def _plan(product: Product) -> Plan | None:
    handle = _v(product.handle)
    if not handle or _v(product.archived_at) is not None:
        return None
    unit = _v(product.interval_unit)
    return Plan(
        handle=handle,
        product_id=_v(product.id),
        name=_v(product.name),
        description=_v(product.description),
        price_in_cents=_v(product.price_in_cents),
        interval=_v(product.interval),
        interval_unit=str(unit) if unit is not None else None,
        requires_payment_method=_v(product.require_credit_card),
    )


def list_plans() -> list[Plan]:
    """The live plans of the configured product family (by handle; ids are not stable)."""
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise ProviderError(502, 'billing_misconfigured', 'No product family is configured.')
    client = get_client()
    plans: list[Plan] = []
    page = 1
    while True:
        try:
            with provider_call():
                batch = client.product_families.list_products_for_product_family(
                    f'handle:{family}', page=page, per_page=PLANS_PAGE_SIZE)
        except ProviderError as e:
            if isinstance(e.__cause__, ApiError) and e.__cause__.status_code == 404:
                # Our configuration, not the caller's request.
                raise ProviderError(502, 'billing_misconfigured',
                                    'The configured product family does not exist.') from e
            raise
        plans.extend(p for p in (_plan(r.product) for r in batch) if p is not None)
        if len(batch) < PLANS_PAGE_SIZE:
            return plans
        page += 1


def get_plan(handle: str) -> Plan:
    for plan in list_plans():
        if plan.handle == handle:
            return plan
    raise InvalidRequest(404, 'unknown_plan', f'No subscription plan with handle {handle!r}.')


def collection_method() -> CollectionMethod:
    """
    Invoice-based collection, so a plan that needs no payment method subscribes
    without a card on file. Its name depends on the site's invoicing
    architecture: ``remittance`` under Relationship Invoicing, ``invoice`` on
    the legacy Statements architecture. Read once per process.
    """
    global _collection_method
    if _collection_method is None:
        with provider_call():
            site = get_client().sites.read_site().site
        relationship = _v(site.relationship_invoicing_enabled)
        if relationship is None:
            raise ProviderError(502, 'billing_unreadable',
                                "Billing did not say which invoicing architecture the site uses.")
        _collection_method = (CollectionMethod.REMITTANCE if relationship
                              else CollectionMethod.INVOICE)
    return _collection_method


# ------------------------------------------------------------- customer

def customer_reference(user: User) -> str:
    """Unique to this install (prefix), this user row, and this database (date_joined)."""
    joined = getattr(user, 'date_joined', None)
    stamp = int(joined.timestamp()) if joined else 0
    return f'{settings.MAXIO_REFERENCE_PREFIX}:user:{user.pk}:{stamp}'


def _customer_body(user: User, reference: str) -> CreateCustomerRequest:
    email = (getattr(user, 'email', '') or '').strip()
    if not email:
        raise InvalidRequest(400, 'email_required',
                             'Your account needs an email address before subscribing.')
    first = (getattr(user, 'first_name', '') or '').strip() or email.split('@', 1)[0]
    last = (getattr(user, 'last_name', '') or '').strip() or 'Customer'
    return CreateCustomerRequest(customer=CreateCustomer(
        first_name=first, last_name=last, email=email, reference=reference))


def _read_customer(response: CustomerResponse) -> Answer:
    customer = response.customer
    # A customer carries no status: done once Maxio names it.
    return Answer(
        provider_id=_v(customer.id),
        outcome=Outcome.DONE,
        provider_time=_v(customer.created_at),
    )


def ensure_customer(user: User) -> WriteResult[MaxioCustomer]:
    """Idempotently make sure one Maxio customer exists for ``user``."""
    reference = customer_reference(user)
    claim = Claim(MaxioCustomer, reference, lookup={'user': user}, defaults={},
                  provider_id_field='maxio_customer_id')
    client = get_client()

    def find() -> CustomerResponse | None:
        try:
            return client.customers.read_customer_by_reference(reference)
        except ApiError as e:
            if e.status_code == 404:
                return None
            raise

    def send() -> CustomerResponse:
        body = _customer_body(user, reference)
        try:
            return client.customers.create_customer(
                body=body, request_options=idempotency_options(reference))
        except ApiError as e:
            # Maxio allows one customer per reference, so a 422 may mean an
            # earlier attempt landed: the lookup tells a landing from a refusal.
            if e.status_code == 422:
                found = find()
                if found is not None:
                    return found
            raise

    # Build the body before claiming so a caller-side problem claims nothing.
    _customer_body(user, reference)
    return safe_write(claim, send, find, _read_customer, repeat_is_safe=True)


# --------------------------------------------------------- subscriptions

def _subscription_fields(sub: Subscription) -> dict[str, Any]:
    product = _v(sub.product)
    state = _v(sub.state)
    return {
        'state': str(state) if state is not None else '',
        'plan_name': (_v(product.name) if product is not None else None) or '',
        'price_in_cents': _v(sub.product_price_in_cents),
        'currency': _v(sub.currency) or '',
        'next_billing_at': _v(sub.next_assessment_at),
    }


def _read_subscription(response: SubscriptionResponse) -> Answer:
    sub = _v(response.subscription)
    if sub is None:
        return Answer(provider_id=None, outcome=Outcome.UNKNOWN, provider_time=None)
    return Answer(
        provider_id=_v(sub.id),
        outcome=status_from_provider(_v(sub.state)),
        provider_time=_v(sub.created_at),
        amount_in_cents=_v(sub.product_price_in_cents),
        fields=_subscription_fields(sub),
    )


@dataclass(frozen=True)
class SubscribeResult:
    enrollment: SubscriptionEnrollment | None
    customer: MaxioCustomer
    in_flight: bool


def subscribe(user: User, plan_handle: str) -> SubscribeResult:
    """Enroll ``user`` in ``plan_handle``; a repeat of the same request never creates a second one."""
    plan = get_plan(plan_handle)
    if plan.price_in_cents is None:
        raise ProviderError(502, 'billing_unreadable', 'The plan has no readable price.')
    if plan.requires_payment_method:
        # Card capture is not part of this API.
        raise InvalidRequest(422, 'payment_method_required',
                             'This plan needs a payment method, which this API does not collect.')
    method = collection_method()

    customer = ensure_customer(user)
    if customer.record.outcome != Outcome.DONE or customer.record.maxio_customer_id is None:
        return SubscribeResult(None, customer.record, in_flight=True)
    customer_id = customer.record.maxio_customer_id

    # A new attempt only once the previous one is settled as failed at Maxio.
    latest = (SubscriptionEnrollment.objects
              .filter(user=user, plan_handle=plan.handle).order_by('-attempt').first())
    attempt = 1
    if latest is not None:
        settled_failed = (latest.outcome == Outcome.FAILED
                          and latest.maxio_subscription_id is not None)
        attempt = latest.attempt + 1 if settled_failed else latest.attempt
    reference = f'{customer.record.reference}:sub:{plan.handle}:{attempt}'

    claim = Claim(
        SubscriptionEnrollment, reference,
        lookup={'user': user, 'plan_handle': plan.handle, 'attempt': attempt},
        defaults={'expected_price_in_cents': plan.price_in_cents},
        provider_id_field='maxio_subscription_id')
    client = get_client()

    def send() -> SubscriptionResponse:
        body = CreateSubscriptionRequest(subscription=CreateSubscription(
            product_handle=plan.handle, customer_id=customer_id, reference=reference,
            payment_collection_method=method))
        return client.subscriptions.create_subscription(
            body=body, request_options=idempotency_options(reference))

    def find() -> SubscriptionResponse | None:
        try:
            return client.subscriptions.find_subscription(reference=reference)
        except ApiError as e:
            if e.status_code == 404:
                return None
            raise

    # Subscription-reference uniqueness is not documented, so a resend is not a
    # safe check: an unknown outcome is settled by lookup only.
    written = safe_write(claim, send, find, _read_subscription, repeat_is_safe=False,
                         expected_amount_in_cents=plan.price_in_cents)
    return SubscribeResult(written.record, customer.record, written.in_flight)


def subscription_as_dict(sub: Subscription) -> dict[str, Any]:
    product = _v(sub.product)
    price = _v(sub.product_price_in_cents)
    state = _v(sub.state)
    unit = _v(product.interval_unit) if product is not None else None
    return {
        'subscriptionId': _v(sub.id),
        'reference': _v(sub.reference),
        'planHandle': _v(product.handle) if product is not None else None,
        'planName': _v(product.name) if product is not None else None,
        'state': str(state) if state is not None else None,
        'outcome': status_from_provider(state),
        'priceInCents': price,
        'price': _price(price),
        'currency': _v(sub.currency),
        'interval': _v(product.interval) if product is not None else None,
        'intervalUnit': str(unit) if unit is not None else None,
        'nextBillingAt': _iso(sub.next_assessment_at),
        'currentPeriodEndsAt': _iso(sub.current_period_ends_at),
        'activatedAt': _iso(sub.activated_at),
        'canceledAt': _iso(sub.canceled_at),
        'createdAt': _iso(sub.created_at),
    }


def enrollment_as_dict(enrollment: SubscriptionEnrollment) -> dict[str, Any]:
    return {
        'subscriptionId': enrollment.maxio_subscription_id,
        'reference': enrollment.reference,
        'planHandle': enrollment.plan_handle,
        'planName': enrollment.plan_name or None,
        'state': enrollment.state or None,
        'outcome': enrollment.outcome,
        'priceInCents': enrollment.price_in_cents,
        'price': _price(enrollment.price_in_cents),
        'expectedPriceInCents': enrollment.expected_price_in_cents,
        'currency': enrollment.currency or None,
        'nextBillingAt': _iso(enrollment.next_billing_at),
    }


def _customer_id(user: User) -> int | None:
    record = MaxioCustomer.objects.filter(user=user, outcome=Outcome.DONE).first()
    return record.maxio_customer_id if record is not None else None


def my_subscriptions(user: User) -> dict[str, Any]:
    """The user's subscriptions read live from Maxio, reconciled into local records."""
    enrollments = list(SubscriptionEnrollment.objects.filter(user=user).order_by('id'))
    customer_id = _customer_id(user)
    subscriptions: list[Subscription] = []
    if customer_id is not None:
        with provider_call():
            responses = get_client().customers.list_customer_subscriptions(customer_id)
        subscriptions = [s for s in (_v(r.subscription) for r in responses) if s is not None]

    by_reference = {e.reference: e for e in enrollments}
    by_id = {e.maxio_subscription_id: e for e in enrollments if e.maxio_subscription_id}
    seen: set[int] = set()
    for sub in subscriptions:
        sub_id, sub_reference = _v(sub.id), _v(sub.reference)
        if sub_id is None:
            continue
        enrollment = by_id.get(sub_id) or by_reference.get(sub_reference or '')
        if enrollment is None:
            continue
        seen.add(enrollment.pk)
        answer = _read_subscription(SubscriptionResponse(subscription=sub))
        outcome = answer.outcome
        if answer.amount_in_cents != enrollment.expected_price_in_cents:
            outcome = Outcome.NEEDS_REVIEW   # it happened, but not at the price we showed
        enrollment.outcome = outcome
        enrollment.maxio_subscription_id = answer.provider_id
        enrollment.provider_time = answer.provider_time
        for name, value in answer.fields.items():
            setattr(enrollment, name, value)
        enrollment.save()

    return {
        'customerId': customer_id,
        'subscriptions': [subscription_as_dict(s) for s in subscriptions],
        # Requests whose outcome Maxio has not confirmed yet (repeat POST to settle).
        'unsettled': [enrollment_as_dict(e) for e in enrollments
                      if e.pk not in seen and e.outcome in (Outcome.SENDING, Outcome.UNKNOWN)],
    }


def get_subscription(user: User, subscription_id: int) -> dict[str, Any]:
    customer_id = _customer_id(user)
    not_found = InvalidRequest(404, 'not_found', 'No such subscription.')
    if customer_id is None:
        raise not_found
    try:
        with provider_call():
            response = get_client().subscriptions.read_subscription(subscription_id)
    except ProviderError as e:
        if e.status_code == 404:
            raise not_found from e
        raise
    sub = _v(response.subscription)
    owner = _v(sub.customer) if sub is not None else None
    if sub is None or owner is None or _v(owner.id) != customer_id:
        raise not_found   # never reveal another customer's subscription
    return subscription_as_dict(sub)

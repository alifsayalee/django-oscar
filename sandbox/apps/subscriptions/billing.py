"""
Subscription billing through Maxio Advanced Billing.

Every Maxio write that creates something goes through ``safe_write``: the
claim row is inserted and committed *before* the call, carries the reference
sent to Maxio, and a write whose outcome is unknown is settled only by looking
it up at Maxio under that same reference.
"""
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription,
    CreateSubscriptionRequest, Customer, CustomerError, CustomerErrorResponse1,
    CustomerResponse, ErrorListResponse1, Product, ProductResponse,
    Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import (
    CollectionMethod, SubscriptionState)

from .maxio_client import get_client, product_family, timeout_seconds
from .models import (
    BillingCustomer, BillingInstall, Outcome, ProviderWriteClaim,
    SubscriptionClaim)

logger = logging.getLogger(__name__)

# Failures raised before the request left: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

PLAN_HANDLE_RE = re.compile(r'^[A-Za-z0-9_\-]{1,128}$')

ClaimT = TypeVar('ClaimT', bound=ProviderWriteClaim)
T = TypeVar('T')


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BillingError(Exception):
    """A failure the API answers with ``status_code`` and a message we wrote."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class OutcomeUnknown(BillingError):
    """A write may have landed at Maxio and a lookup by its reference could not settle it."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'outcome_unknown',
            'Maxio did not confirm the request. It may still have been applied; '
            'repeat the same request to check its status.',
            outcome_unknown=True)
        self.reference = reference


def unset_to_none(value: T | None | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _provider_messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = unset_to_none(error.errors)
        if isinstance(errors, list):
            return [str(e) for e in errors]
        if isinstance(errors, CustomerError):
            message = unset_to_none(errors.customer)
            return [message] if message else []
    return []


def translate(exc: BaseException) -> BillingError:
    """The one mapping from an SDK / transport failure to what our API answers."""
    if isinstance(exc, BillingError):
        return exc
    if isinstance(exc, ImproperlyConfigured):
        logger.error('Maxio integration is not configured: %s', exc)
        return BillingError(502, 'billing_not_configured', 'Billing is not configured on this site.')
    if isinstance(exc, ApiError):
        status = exc.status_code
        if isinstance(exc.error, RawError):
            logger.warning('Maxio answered HTTP %s: %s', status, exc.error.text()[:500])
        else:
            logger.warning('Maxio answered HTTP %s: %s', status, _provider_messages(exc.error))
        if status in (401, 403):
            return BillingError(502, 'provider_auth', 'Billing provider refused our credentials.')
        if status == 429:
            return BillingError(503, 'provider_rate_limited', 'Billing provider is busy; try again shortly.')
        if 400 <= status < 500:
            details = _provider_messages(exc.error)
            return BillingError(status, 'provider_rejected',
                                'The billing provider rejected the request.', details=details)
        return BillingError(502, 'provider_error', 'Billing provider is unavailable.',
                            outcome_unknown=True)
    if isinstance(exc, NEVER_SENT):
        return BillingError(502, 'provider_unreachable', 'Billing provider could not be reached.')
    if isinstance(exc, httpx.RequestError):
        return BillingError(504, 'provider_timeout', 'Billing provider did not answer.',
                            outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic.ValidationError, or a non-JSON body on a 2xx
        logger.exception('Unreadable response from Maxio')
        return BillingError(502, 'provider_unreadable', 'Billing provider sent an unreadable answer.',
                            outcome_unknown=True)
    raise exc


def call(fn: Callable[[], T]) -> T:
    """Run a Maxio read, converting every failure into a ``BillingError``."""
    try:
        return fn()
    except (ApiError, httpx.RequestError, ValueError, ImproperlyConfigured) as exc:
        raise translate(exc) from exc


def lookup(fn: Callable[[], T]) -> T | None:
    """A Maxio read where 404 means "no such record"."""
    try:
        return fn()
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Provider status -> our outcome
# ---------------------------------------------------------------------------

def status_from_provider(state: object) -> str:
    """The ONE place a Maxio subscription state becomes an outcome of ours."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return Outcome.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING
              | SubscriptionState.AWAITING_SIGNUP):
            return Outcome.PENDING          # the provider has not finished
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.ON_HOLD | SubscriptionState.PAUSED | SubscriptionState.SUSPENDED):
            return Outcome.PENDING          # exists, but not in good standing: never done
        case SubscriptionState.FAILED_TO_CREATE:
            return Outcome.FAILED
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return Outcome.FAILED           # happened, then ended: no longer in effect
        case _:
            return Outcome.UNKNOWN          # absent, or a state newer than this SDK


# ---------------------------------------------------------------------------
# The safe write
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    """What one write's response says, read the same way for every write."""
    provider_id: int | None
    outcome: str
    provider_time: datetime | None
    as_asked: bool = True                      # the provider echoed what we asked for
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteResult:
    claim: Any
    created: bool                              # this request made the claim (and the provider call)
    in_flight: bool = False                    # another request holds a fresh claim


def send_window() -> timedelta:
    return timedelta(seconds=2 * timeout_seconds() + 30)


def _complete(claim: ClaimT, answer: Answer) -> ClaimT:
    outcome = answer.outcome if answer.as_asked else Outcome.NEEDS_REVIEW
    claim.outcome = outcome
    claim.provider_time = answer.provider_time
    for name, value in answer.fields.items():
        setattr(claim, name, value)
    claim.save()
    if outcome == Outcome.NEEDS_REVIEW:
        logger.error('Maxio write %s landed but not as asked; needs review', claim.reference)
    return claim


def _mark(claim: ClaimT, outcome: str) -> None:
    claim.outcome = outcome
    claim.save(update_fields=['outcome', 'updated_at'])


def safe_write(
    *,
    insert_claim: Callable[[], ClaimT],
    load_claim: Callable[[], ClaimT | None],
    release: Callable[[ClaimT], None],
    send: Callable[[str], T],
    find: Callable[[str], T | None],
    read: Callable[[T, str], Answer],
    rejection_may_be_duplicate: bool = False,
) -> WriteResult:
    """
    The one path for every Maxio write that creates something.

    insert_claim   inserts the claim row (outcome "sending") - the DB's unique
                   constraints reject a second one with IntegrityError
    load_claim     the claim that beat us, after our insert was rejected
    release        called for a first send that never left or was refused:
                   nothing exists at Maxio, so the claim is given up
    send(ref)      makes the Maxio call carrying ``ref`` as its reference
    find(ref)      the record carrying ``ref`` at Maxio, or None
    read(result, ref)
                   the response as an ``Answer``, checked against ``ref``
    rejection_may_be_duplicate
                   Maxio refuses a second create with the same reference, so a
                   4xx on the first send may mean an earlier attempt landed
    """
    # 1. CLAIM FIRST - committed before any provider call; the DB picks the winner.
    try:
        with transaction.atomic():
            claim = insert_claim()
        checking = False
        created = True
    except IntegrityError:
        existing = load_claim()
        if existing is None:        # the winner released its claim between our insert and our read
            return WriteResult(None, created=False, in_flight=True)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, created=False, in_flight=True)   # in flight: no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return WriteResult(existing, created=False)                   # answer from what is stored
        claim, checking, created = existing, True, False                  # stale or unresolved: LOOK only

    ref = claim.reference
    result: T | None = None

    # 2. CALL - only on a first attempt. A request that lost the claim never sends.
    if not checking:
        try:
            result = send(ref)
        except NEVER_SENT as exc:
            release(claim)                                 # never left: nothing happened
            raise translate(exc) from exc
        except ApiError as exc:
            if exc.status_code < 500:
                landed = None
                if rejection_may_be_duplicate:
                    try:
                        landed = lookup(lambda: find(ref))
                    except (ApiError, httpx.RequestError, ValueError):
                        landed = None
                if landed is None:
                    release(claim)                         # refused: nothing exists at Maxio
                    raise translate(exc) from exc
                result = landed                            # an earlier attempt under this reference landed
            # a 5xx may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass                                           # sent, no readable answer: may have landed

    # 3. CHECK by the reference we sent - the only thing that settles an unknown outcome.
    if result is None:
        try:
            result = lookup(lambda: find(ref))
        except (ApiError, httpx.RequestError, ValueError):
            _mark(claim, Outcome.UNKNOWN)
            logger.warning('Maxio write %s: outcome unknown (lookup failed)', ref)
            raise OutcomeUnknown(ref) from None
        if result is None:
            _mark(claim, Outcome.UNKNOWN)                  # not found YET - it can still land
            logger.warning('Maxio write %s: outcome unknown (not found)', ref)
            raise OutcomeUnknown(ref)

    # 4 + 5. VERIFY what came back, then COMPLETE from what the provider said.
    try:
        answer = read(result, ref)
    except ValueError:
        _mark(claim, Outcome.UNKNOWN)
        raise OutcomeUnknown(ref) from None
    return WriteResult(_complete(claim, answer), created=created)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def reference_prefix() -> str:
    configured: str = getattr(settings, 'MAXIO_REFERENCE_PREFIX', '')
    return configured or f'oscar-{BillingInstall.current_token()}'


def customer_reference(user: Any) -> str:
    return f'{reference_prefix()}-customer-u{user.pk}'


def subscription_reference(user: Any, plan_handle: str, attempt: int) -> str:
    return f'{reference_prefix()}-sub-u{user.pk}-{plan_handle}-{attempt}'


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteSettings:
    currency: str | None
    collection_method: CollectionMethod


_site_lock = threading.Lock()
_site_settings: SiteSettings | None = None


def site_settings() -> SiteSettings:
    """The Maxio site's currency and the collection method subscriptions use (read once per process)."""
    global _site_settings
    if _site_settings is not None:
        return _site_settings
    client = call(get_client)
    site = call(client.sites.read_site).site
    configured: str = getattr(settings, 'MAXIO_PAYMENT_COLLECTION_METHOD', '')
    if configured:
        try:
            method = CollectionMethod(configured.strip().lower())
        except ValueError:
            raise translate(ImproperlyConfigured(
                f'MAXIO_PAYMENT_COLLECTION_METHOD must be one of {[m.value for m in CollectionMethod]}')) from None
    elif unset_to_none(site.relationship_invoicing_enabled) is False:
        method = CollectionMethod.INVOICE           # legacy Statements Architecture
    else:
        method = CollectionMethod.REMITTANCE        # Relationship Invoicing
    with _site_lock:
        _site_settings = SiteSettings(currency=unset_to_none(site.currency), collection_method=method)
    return _site_settings


def reset_site_settings() -> None:
    global _site_settings
    with _site_lock:
        _site_settings = None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    handle: str
    name: str
    description: str
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    trial_price_in_cents: int | None
    initial_charge_in_cents: int | None
    requires_payment_method: bool | None
    product_id: int | None


def _plan(product: Product) -> Plan:
    handle = unset_to_none(product.handle)
    if not handle:
        raise BillingError(502, 'provider_unreadable', 'Billing provider sent a plan without a handle.')
    interval_unit = unset_to_none(product.interval_unit)
    return Plan(
        handle=handle,
        name=unset_to_none(product.name) or handle,
        description=unset_to_none(product.description) or '',
        price_in_cents=unset_to_none(product.price_in_cents),
        interval=unset_to_none(product.interval),
        interval_unit=str(interval_unit) if interval_unit is not None else None,
        trial_price_in_cents=unset_to_none(product.trial_price_in_cents),
        initial_charge_in_cents=unset_to_none(product.initial_charge_in_cents),
        requires_payment_method=unset_to_none(product.require_credit_card),
        product_id=unset_to_none(product.id),
    )


def _is_available(product: Product) -> bool:
    return unset_to_none(product.archived_at) is None


def list_plans() -> list[Plan]:
    family = product_family()
    client = call(get_client)
    responses: list[ProductResponse] = call(
        lambda: client.product_families.list_products_for_product_family(
            f'handle:{family}', per_page=200))
    return [_plan(r.product) for r in responses if _is_available(r.product)]


def get_plan(plan_handle: str) -> Plan:
    """The plan behind ``plan_handle``, if it is an available plan of the configured family."""
    if not PLAN_HANDLE_RE.match(plan_handle):
        raise BillingError(400, 'invalid_plan_handle', 'planHandle is not a valid plan handle.')
    family = product_family()
    client = call(get_client)
    response = call(lambda: lookup(lambda: client.products.read_product_by_handle(plan_handle)))
    if response is None:
        raise BillingError(404, 'unknown_plan', f'No plan with handle {plan_handle!r}.')
    product = response.product
    product_family_ = unset_to_none(product.product_family)
    family_handle = unset_to_none(product_family_.handle) if product_family_ is not None else None
    if family_handle != family or not _is_available(product):
        raise BillingError(404, 'unknown_plan', f'No plan with handle {plan_handle!r}.')
    return _plan(product)


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def _customer_body(user: Any, reference: str) -> CreateCustomerRequest:
    email = (user.email or '').strip()
    if not email:
        raise BillingError(400, 'email_required', 'Your account needs an email address to subscribe.')
    local_part = email.split('@', 1)[0]
    first_name = (user.first_name or '').strip() or local_part
    last_name = (user.last_name or '').strip() or 'Customer'
    return CreateCustomerRequest(customer=CreateCustomer(
        first_name=first_name, last_name=last_name, email=email, reference=reference))


def _read_customer(response: CustomerResponse, reference: str) -> Answer:
    customer: Customer = response.customer
    customer_id = unset_to_none(customer.id)
    if customer_id is None:
        raise ValueError('Maxio customer response carries no id')
    return Answer(
        provider_id=customer_id,
        outcome=Outcome.DONE,         # a customer has no status: a readable record is the outcome
        provider_time=unset_to_none(customer.created_at),
        as_asked=unset_to_none(customer.reference) == reference,
        fields={'maxio_customer_id': customer_id},
    )


def _customer_result(result: WriteResult) -> BillingCustomer:
    if result.in_flight or result.claim is None:
        raise BillingError(409, 'customer_in_progress',
                           'Your billing account is being set up; retry in a moment.')
    customer: BillingCustomer = result.claim
    if customer.outcome != Outcome.DONE:
        if customer.outcome == Outcome.UNKNOWN:
            raise OutcomeUnknown(customer.reference)
        raise BillingError(409, 'customer_needs_review',
                           'Your billing account needs attention; please contact support.')
    return customer


def _release_customer_claim(claim: BillingCustomer) -> None:
    claim.delete()                  # nothing exists at Maxio: the next request may claim again


def ensure_customer(user: Any) -> tuple[BillingCustomer, bool]:
    """The caller's Maxio customer, created once (idempotent). Returns (customer, created_now)."""
    reference = customer_reference(user)
    client = call(get_client)
    body = _customer_body(user, reference)

    result = safe_write(
        insert_claim=lambda: BillingCustomer.objects.create(
            user=user, reference=reference, claimed_at=timezone.now()),
        load_claim=lambda: BillingCustomer.objects.filter(user=user).first(),
        release=_release_customer_claim,
        send=lambda ref: client.customers.create_customer(body=body),
        find=client.customers.read_customer_by_reference,
        read=_read_customer,
        rejection_may_be_duplicate=True,   # Maxio customer references must be unique
    )
    return _customer_result(result), result.created


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def _subscription_fields(subscription: Subscription) -> dict[str, Any]:
    state = unset_to_none(subscription.state)
    product = unset_to_none(subscription.product)
    currency = unset_to_none(subscription.currency)
    next_billing = unset_to_none(subscription.next_assessment_at) or unset_to_none(
        subscription.current_period_ends_at)
    price = unset_to_none(subscription.product_price_in_cents)
    fields: dict[str, Any] = {
        'maxio_subscription_id': unset_to_none(subscription.id),
        'state': str(state) if state is not None else '',
        'next_billing_at': next_billing,
        'currency': currency or '',
    }
    if price is not None:
        fields['price_in_cents'] = price
    plan_name = unset_to_none(product.name) if product is not None else None
    if plan_name:
        fields['plan_name'] = plan_name
    return fields


def _read_subscription(response: SubscriptionResponse, reference: str, plan_handle: str) -> Answer:
    subscription = unset_to_none(response.subscription)
    if subscription is None or unset_to_none(subscription.id) is None:
        raise ValueError('Maxio subscription response carries no subscription id')
    product = unset_to_none(subscription.product)
    echoed_plan = unset_to_none(product.handle) if product is not None else None
    outcome = status_from_provider(unset_to_none(subscription.state))
    return Answer(
        provider_id=unset_to_none(subscription.id),
        outcome=outcome,
        provider_time=unset_to_none(subscription.updated_at) or unset_to_none(subscription.created_at),
        as_asked=(unset_to_none(subscription.reference) == reference and echoed_plan == plan_handle),
        fields=_subscription_fields(subscription) | {'slot_open': outcome != Outcome.FAILED},
    )


def _release_subscription_claim(claim: SubscriptionClaim) -> None:
    claim.outcome = Outcome.FAILED
    claim.slot_open = False
    claim.save(update_fields=['outcome', 'slot_open', 'updated_at'])


def subscribe(user: Any, plan_handle: str) -> tuple[WriteResult, Plan, BillingCustomer]:
    """Subscribe ``user`` to ``plan_handle``. A repeat is answered from the first request's claim."""
    plan = get_plan(plan_handle)
    site = site_settings()
    customer, _ = ensure_customer(user)
    customer_id = customer.maxio_customer_id
    if customer_id is None:          # a "done" customer always carries its Maxio id
        raise OutcomeUnknown(customer.reference)
    client = call(get_client)

    def insert_claim() -> SubscriptionClaim:
        attempt = SubscriptionClaim.objects.filter(user=user, plan_handle=plan.handle).count() + 1
        return SubscriptionClaim.objects.create(
            user=user, plan_handle=plan.handle, slot_open=True, claimed_at=timezone.now(),
            reference=subscription_reference(user, plan.handle, attempt),
            plan_name=plan.name, price_in_cents=plan.price_in_cents, currency=site.currency or '')

    def send(ref: str) -> SubscriptionResponse:
        return client.subscriptions.create_subscription(body=CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan.handle, customer_id=customer_id, reference=ref,
                payment_collection_method=site.collection_method)))

    def find(ref: str) -> SubscriptionResponse:
        return client.subscriptions.find_subscription(reference=ref)

    def load_claim() -> SubscriptionClaim | None:
        return SubscriptionClaim.objects.filter(user=user, plan_handle=plan.handle, slot_open=True).first()

    result = safe_write(
        insert_claim=insert_claim,
        load_claim=load_claim,
        release=_release_subscription_claim,
        send=send,
        find=find,
        read=lambda response, ref: _read_subscription(response, ref, plan.handle),
    )
    return result, plan, customer


def _is_unsettled(claim: ProviderWriteClaim) -> bool:
    return claim.outcome == Outcome.UNKNOWN or (
        claim.outcome == Outcome.SENDING and claim.claimed_at <= timezone.now() - send_window())


def _settle_customer(customer: BillingCustomer) -> None:
    """Look an unsettled customer claim up at Maxio by its reference (never a new create)."""
    client = get_client()
    try:
        response = lookup(lambda: client.customers.read_customer_by_reference(customer.reference))
    except (ApiError, httpx.RequestError, ValueError):
        logger.warning('Could not settle Maxio customer claim %s', customer.reference)
        return
    if response is None:
        if customer.outcome == Outcome.SENDING:
            _mark(customer, Outcome.UNKNOWN)
        return
    try:
        _complete(customer, _read_customer(response, customer.reference))
    except ValueError:
        _mark(customer, Outcome.UNKNOWN)


def _settle_subscription(claim: SubscriptionClaim) -> None:
    """Look an unsettled claim up at Maxio by its reference (never a new create)."""
    client = get_client()
    try:
        response = lookup(lambda: client.subscriptions.find_subscription(reference=claim.reference))
    except (ApiError, httpx.RequestError, ValueError):
        logger.warning('Could not settle Maxio subscription claim %s', claim.reference)
        return
    if response is None:
        if claim.outcome == Outcome.SENDING:
            _mark(claim, Outcome.UNKNOWN)
        return
    try:
        _complete(claim, _read_subscription(response, claim.reference, claim.plan_handle))
    except ValueError:
        _mark(claim, Outcome.UNKNOWN)


def refresh_claim(claim: SubscriptionClaim, subscription: Subscription) -> None:
    """Record a later read of a subscription on its claim (state changes at Maxio)."""
    outcome = status_from_provider(unset_to_none(subscription.state))
    if claim.outcome == Outcome.NEEDS_REVIEW:
        outcome = Outcome.NEEDS_REVIEW
    _complete(claim, Answer(
        provider_id=unset_to_none(subscription.id),
        outcome=outcome,
        provider_time=unset_to_none(subscription.updated_at) or unset_to_none(subscription.created_at),
        fields=_subscription_fields(subscription) | {
            'slot_open': claim.slot_open and outcome != Outcome.FAILED},
    ))


@dataclass
class MySubscriptions:
    customer: BillingCustomer | None
    subscriptions: list[Subscription]
    claims_by_subscription: dict[int, SubscriptionClaim]
    unsettled: list[SubscriptionClaim]


def my_subscriptions(user: Any) -> MySubscriptions:
    """The caller's subscriptions, read live from Maxio (the system of record)."""
    client = call(get_client)
    customer = BillingCustomer.objects.filter(user=user).first()
    if customer is not None and _is_unsettled(customer):
        _settle_customer(customer)
    claims = list(SubscriptionClaim.objects.filter(user=user))
    for claim in claims:
        if claim.maxio_subscription_id is None and _is_unsettled(claim):
            _settle_subscription(claim)

    if customer is None or customer.outcome != Outcome.DONE or customer.maxio_customer_id is None:
        unsettled = [c for c in claims if c.maxio_subscription_id is None and c.outcome != Outcome.FAILED]
        return MySubscriptions(customer, [], {}, unsettled)

    customer_id = customer.maxio_customer_id
    responses = call(lambda: client.customers.list_customer_subscriptions(customer_id))
    subscriptions = [s for s in (unset_to_none(r.subscription) for r in responses) if s is not None]

    by_id = {c.maxio_subscription_id: c for c in claims if c.maxio_subscription_id is not None}
    by_ref = {c.reference: c for c in claims}
    matched: dict[int, SubscriptionClaim] = {}
    for subscription in subscriptions:
        subscription_id = unset_to_none(subscription.id)
        if subscription_id is None:
            continue
        owned = by_id.get(subscription_id)
        if owned is None:
            owned = by_ref.get(unset_to_none(subscription.reference) or '')
        if owned is not None:
            refresh_claim(owned, subscription)
            matched[subscription_id] = owned

    unsettled = [c for c in claims
                 if c.maxio_subscription_id is None and c.outcome not in (Outcome.FAILED, Outcome.DONE)]
    return MySubscriptions(customer, subscriptions, matched, unsettled)


def format_cents(cents: int | None) -> str | None:
    """Maxio prices are in cents (hundredths of the site currency)."""
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))
